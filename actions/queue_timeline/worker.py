"""The queue worker: builds queued timelines unattended, behind every guard.

`python main.py queue-worker` polls the `jobs` table (see jobs.py) and runs ONE
job at a time. It reuses the create_timeline engine for everything about a
timeline -- the segments, the per-segment payloads, the validation, the join
check -- and the ordinary approval path for every commit.

THE COMMIT PATH IS THE EXISTING ONE. The worker never calls `runner.commit`
itself. It stores each segment's preview exactly as plan_preview does and then
hands it to `approval._approve`, the function behind the Slack Approve button,
so COMMIT_ENABLED, the child allow-list, the atomic claim and the live check of
the child's plans all apply to the queue exactly as they do to a human.

MODES (QUEUE_MODE):
    off       the worker does nothing.
    dry_run   (the default) commits NOTHING. Each segment is previewed on its
              own -- a later plan cannot be chained to one that does not exist
              yet -- rounded and clean-checked, and the result goes in a digest.
    auto      commits clean plans, in order, each verified by read-back.

A PLAN IS CLEAN only if it validated, raised no warning (the flattener's or
Famly's; the informational funding-limit note aside), rounding succeeded or was
not requested, and every period's estimate is above 0 and below
QUEUE_MAX_MONTHLY. A plan that is not clean is NOT committed: the job is HELD
and the normal Slack approval message is posted for that plan, with the
reasons. A human approving it returns the job to the queue (approval.py ->
jobs.release_after_approval) and the worker carries on.

SAFETY RAILS: QUEUE_ALLOWED_INSTITUTIONS (anything else is held), QUEUE_MAX_
COMMITS_PER_DAY (checked before every commit; the rest waits for the next UTC
day), a circuit breaker (3 consecutive failed jobs pause the queue), and a
manual pause, checked before EVERY commit rather than once per job.

FAILURES. A commit is NEVER retried. After any commit that did not report
success the worker reads the child's plans: a plan that is there was written,
and counts as a success. Reads and previews retry up to 3 times with backoff
(longer on HTTP 429). On startup a `running` job is resumed by READ-BACK, never
by replaying.
"""

import logging
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal

import requests

from actions.create_timeline import engine
from actions.plan_write import approval, auto_round, funded_hours, preview_store, rounding
from actions.plan_write import web as plan_write_web
from actions.read_child_plans import runner as read_child_plans
from core.rest_client import RestHTTPError
from integrations import child_lookup, slack

from . import jobs

logger = logging.getLogger(__name__)

PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))

MODE_OFF, MODE_DRY_RUN, MODE_AUTO = "off", "dry_run", "auto"

DEFAULT_MAX_MONTHLY = 5000
DEFAULT_MAX_COMMITS_PER_DAY = 20
BREAKER_THRESHOLD = 3
DIGEST_INTERVAL_SECONDS = 30 * 60
POLL_SECONDS = 5

# Retries for READS and PREVIEWS (never commits).
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2
RATE_LIMIT_BACKOFF_SECONDS = 30

# Famly warnings that are information, not a problem, and so do not make a plan
# unclean. Matched on the lowercased title.
INFORMATIONAL_TITLES = funded_hours.INFORMATIONAL_TITLE_FRAGMENTS

_sleep = time.sleep


def _now() -> datetime:
    """The clock, in one place so tests can move it."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Configuration, read fresh each time
# --------------------------------------------------------------------------- #
def mode() -> str:
    """QUEUE_MODE: off | dry_run | auto. Unset is dry_run; anything else is off."""
    raw = os.environ.get("QUEUE_MODE", "").strip().lower()
    if raw == "":
        return MODE_DRY_RUN
    if raw in (MODE_OFF, MODE_DRY_RUN, MODE_AUTO):
        return raw
    logger.error("QUEUE_MODE=%r is not off, dry_run or auto; treating it as off", raw)
    return MODE_OFF


def _env_number(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except (ValueError, ArithmeticError):
        logger.error("%s=%r is not a number; using %s", name, raw, default)
        return default


def max_monthly() -> Decimal:
    return _env_number("QUEUE_MAX_MONTHLY", Decimal(DEFAULT_MAX_MONTHLY), Decimal)


def max_commits_per_day() -> int:
    return _env_number("QUEUE_MAX_COMMITS_PER_DAY", DEFAULT_MAX_COMMITS_PER_DAY, int)


def allowed_institutions() -> set[str]:
    """QUEUE_ALLOWED_INSTITUTIONS, comma separated. Empty means NONE: nothing
    is committed automatically until institutions are listed."""
    return {
        item.strip().upper()
        for item in os.environ.get("QUEUE_ALLOWED_INSTITUTIONS", "").split(",")
        if item.strip()
    }


# --------------------------------------------------------------------------- #
# Retrying reads and previews
# --------------------------------------------------------------------------- #
def retrying(call, what: str):
    """Call `call()`, retrying transient failures up to RETRY_ATTEMPTS times.

    Backs off between tries, much longer on HTTP 429. A 4xx other than 429 is a
    real answer and is raised at once. NEVER used for a commit.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return call()
        except RestHTTPError as exc:
            if 400 <= exc.status_code < 500 and exc.status_code != 429:
                raise
            delay = (
                RATE_LIMIT_BACKOFF_SECONDS * attempt
                if exc.status_code == 429
                else RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            )
            error = f"HTTP {exc.status_code}"
        except requests.RequestException as exc:
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            error = type(exc).__name__

        if attempt == RETRY_ATTEMPTS:
            raise
        logger.warning("%s failed (%s); retry %d in %ss", what, error, attempt, delay)
        _sleep(delay)


def read_plans(child_id: str) -> list:
    return retrying(
        lambda: read_child_plans.run(child_id, version=PLAN_VERSION).plans, "plans read"
    )


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def _child_line(child_id: str) -> str:
    """The child as the other messages show them: name (when found) and id."""
    return slack.child_line(child_id, child_lookup.lookup_child_name(child_id))


def _where(job: jobs.Job, index: int) -> str:
    return engine.heading_text(job.segments, index)


def _exists_lines(job: jobs.Job) -> list[str]:
    committed = [(i, s) for i, s in enumerate(job.segments) if s.get("plan_id")]
    if not committed:
        return ["Plans that exist in Famly from this job: none."]
    lines = ["Plans that exist in Famly from this job:"]
    lines += [f"• {_where(job, i)} — plan `{s['plan_id']}`" for i, s in committed]
    last_index = committed[-1][0]
    if last_index < job.last_index:
        lines.append(
            f":warning: The last committed plan (`{committed[-1][1]['plan_id']}`) is "
            f"*open-ended*: it needs either an end date or its successor plan."
        )
    return lines


def post_failed(job: jobs.Job, reason: str) -> None:
    slack.post_notice(
        "\n".join(
            [
                f":x: *Queue job failed* — {_child_line(job.child_id)}",
                f"Job `{job.job_id}`: {reason}",
                *_exists_lines(job),
                "_Nothing will be retried automatically._",
            ]
        )
    )


# --------------------------------------------------------------------------- #
# Job outcomes
# --------------------------------------------------------------------------- #
def fail(job_id: str, reason: str) -> None:
    """Fail a job, tell Slack at once, and trip the breaker on the third in a row."""
    if not jobs.set_status(job_id, jobs.STATUS_FAILED, reason):
        return
    job = jobs.get(job_id)
    logger.error("QUEUE JOB FAILED job_id=%s: %s", job_id, reason)
    post_failed(job, reason)

    if jobs.state_increment("consecutive_failures") >= BREAKER_THRESHOLD:
        jobs.set_paused(True)
        slack.post_notice(
            f":rotating_light: *Queue paused* — {BREAKER_THRESHOLD} jobs in a row have "
            f"failed. Nothing more will be committed until someone looks and runs "
            f"`queue-resume`."
        )


def finish(job_id: str, detail: str | None = None, *, status: str | None = None) -> None:
    """End a job. `status` defaults to `done`; a dry run passes `dry_done`.

    The difference matters: `done` holds the job's idempotency key, `dry_done`
    does not, so the same request can be sent again and run for real.
    """
    if jobs.set_status(job_id, status or jobs.STATUS_DONE, detail):
        jobs.state_set("consecutive_failures", "0")


def hold(job: jobs.Job, index: int, prepared, reasons: list[str]) -> None:
    """Hold the job on this plan: post the normal approval message, with the why."""
    why = "; ".join(reasons)
    heading = f"*{_where(job, index)}*\n:pause_button: Held for approval — {why}"
    plan_write_web.post_prepared(prepared, heading=heading)
    jobs.set_status(job.job_id, jobs.STATUS_HELD, why, preview_id=prepared.preview_id)
    logger.warning("QUEUE JOB HELD job_id=%s index=%d: %s", job.job_id, index, why)


# --------------------------------------------------------------------------- #
# Is a plan clean?
# --------------------------------------------------------------------------- #
def _informational(warning) -> bool:
    inner = getattr(warning, "warning", warning)
    title = (getattr(inner, "title", None) or "").lower()
    return any(fragment in title for fragment in INFORMATIONAL_TITLES)


def clean_reasons(prepared, requested_rounding: bool) -> list[str]:
    """Everything that stops this previewed plan from being committed unattended.

    Empty means clean.
    """
    if prepared.status != 200:
        return [f"it did not validate: {'; '.join(prepared.data.get('errors') or ['unknown'])}"]

    reasons = []
    for warning in prepared.result.warnings:
        if _informational(warning):
            continue
        inner = getattr(warning, "warning", warning)
        reasons.append(f"warning: {getattr(inner, 'title', None) or 'untitled'}")

    outcome = prepared.outcome
    if requested_rounding and outcome is not None and outcome.reason:
        reasons.append(f"rounding was not applied: {outcome.reason}")

    states = getattr(prepared.preview.plan, "plan_states", None) or []
    if not states:
        reasons.append("the preview has no pricing periods")
    ceiling = max_monthly()
    for state in states:
        estimate = rounding.to_decimal(state.monthly_estimate)
        start = str(state.from_ or "unknown")[:10]
        if estimate is None or estimate <= 0:
            reasons.append(f"the estimate for the period from {start} is not above 0")
        elif estimate >= ceiling:
            reasons.append(
                f"the estimate for the period from {start} ({estimate}) is not below "
                f"QUEUE_MAX_MONTHLY ({ceiling})"
            )
    return reasons


def _prepare(job: jobs.Job, index: int, old_plan_id, *, store_job: bool):
    """Preview segment `index` through the ordinary path. Retried; never posted."""
    payload = engine.segment_payload(
        job.payload, job.segments[index], is_last=index == job.last_index
    )
    # A request without a site_code gets one from its institution -- the codes
    # are the same -- rather than being held for the "no site" warning. An
    # institution that is not a known site still raises that warning.
    if not str(payload.get("site_code") or "").strip() and job.institution:
        payload["site_code"] = job.institution
    prepared = retrying(
        lambda: plan_write_web.prepare_preview(
            payload,
            old_plan_id=old_plan_id,
            replace_old_plan=False,
            job_id=job.job_id if store_job else None,
            job_index=index if store_job else None,
        ),
        "preview",
    )
    return prepared, auto_round.enabled(payload)


# --------------------------------------------------------------------------- #
# What Famly says the child has
# --------------------------------------------------------------------------- #
def check_state(job: jobs.Job, plans: list) -> str | None:
    """Why the child's plans are not what this job expects, or None when they are.

    The job's committed plans, and nothing else, must be the child's plans: with
    none committed the child must have NO plans; otherwise the latest-starting
    plan must be the last committed one, every committed plan must be there, and
    each must meet the next. Rounding that did not save as intended is warned
    about, not failed.
    """
    done = job.segments[: job.next_index]
    if not done:
        if plans:
            ids = ", ".join(p.id or "unknown" for p in plans)
            return f"the child already has plan(s) ({ids}), so nothing was created"
        return None

    ids = [s.get("plan_id") for s in done]
    by_id = {p.id: p for p in plans}
    missing = [i for i in ids if i not in by_id]
    if missing:
        return f"committed plan(s) {', '.join(map(str, missing))} are not in the child's plans"
    extra = [p.id for p in plans if p.id not in ids]
    if extra:
        return f"the child has plan(s) this job did not create ({', '.join(map(str, extra))})"

    latest = max(plans, key=lambda p: engine._day(p.from_) or date.min)
    if latest.id != ids[-1]:
        return f"the last committed plan is not the child's latest-starting plan (`{latest.id}` is)"

    for earlier, later in zip(ids, ids[1:]):
        problem = engine.join_problem(by_id[earlier], by_id[later])
        if problem:
            return f"plan `{later}`: {problem}"

    last = by_id[ids[-1]]

    # The final plan's end date: the saved `to` must be the date that was sent
    # (leave + 1 day). A warning, not a failure -- the plan is saved.
    if len(done) == len(job.segments):
        end_problem = engine.final_end_problem(done[-1], last)
        if end_problem:
            slack.post_notice(
                f":warning: Queue job `{job.job_id}`: plan `{last.id}` — {end_problem}. "
                f"The job is complete; please check the plan's end date in Famly."
            )

    off = rounding.check_saved(last)
    if off:
        slack.post_notice(
            f":warning: Queue job `{job.job_id}`: plan `{last.id}` was auto-rounded but "
            f"does not add up as saved — {'; '.join(off)}. The job is continuing."
        )
    return None


def _find_written(job: jobs.Job, index: int, known_ids: set) -> object | None:
    """The plan this job just created for segment `index`, looked for by read-back.

    Matched on the segment's start date among plans the job did not already
    know. Looks again after a delay (Famly's read model can lag) before giving up.
    """
    start = engine._day(job.segments[index]["from"])
    for attempt in range(engine.READBACK_RETRIES + 1):
        if attempt:
            _sleep(engine.READBACK_DELAY_SECONDS)
        for plan in read_plans(job.child_id):
            if plan.id not in known_ids and engine._day(plan.from_) == start:
                return plan
    return None


# --------------------------------------------------------------------------- #
# Running a job
# --------------------------------------------------------------------------- #
def _cap_reached(now: datetime | None = None) -> bool:
    return jobs.commits_today(now or _now()) >= max_commits_per_day()


def _post_cap_notice(now: datetime | None = None) -> None:
    day = jobs.utc_day(now or _now())
    if jobs.state_get("cap_notice_day") == day:
        return
    jobs.state_set("cap_notice_day", day)
    slack.post_notice(
        f":hourglass: *Queue daily limit reached* — {max_commits_per_day()} commits "
        f"today (UTC). The rest of the batch waits for tomorrow."
    )


def run_job(job_id: str) -> None:
    """Run one claimed job. Never raises: whatever happens is the job's outcome."""
    try:
        _run(job_id)
    except Exception as exc:  # noqa: BLE001 - an escaping error is a failed job
        logger.exception("QUEUE JOB CRASHED job_id=%s", job_id)
        fail(job_id, f"unexpected error: {type(exc).__name__}: {exc}")


def _run(job_id: str) -> None:
    job = jobs.get(job_id)
    current_mode = mode()
    if job is None or current_mode == MODE_OFF:
        jobs.set_status(job_id, jobs.STATUS_QUEUED, "queue is off")
        return

    plans = read_plans(job.child_id)
    problem = check_state(job, plans)
    if problem:
        fail(job_id, problem)
        return

    if current_mode == MODE_DRY_RUN:
        _run_dry(job)
        return

    while True:
        job = jobs.get(job_id)
        if job.status != jobs.STATUS_RUNNING:
            return  # cancelled (or otherwise moved) underneath us
        if job.next_index >= len(job.segments):
            finish(job_id)
            return
        if not _do_segment(job):
            return


def _run_dry(job: jobs.Job) -> None:
    """Preview and check every segment on its own. Commits nothing."""
    for index in range(len(job.segments)):
        prepared, requested = _prepare(job, index, None, store_job=False)
        reasons = clean_reasons(prepared, requested)
        if job.institution.upper() not in allowed_institutions():
            reasons.append(f"institution {job.institution or 'unknown'} is not allowed to auto-commit")
        estimates = []
        if prepared.status == 200:
            estimates = [
                str(rounding.to_decimal(s.monthly_estimate))
                for s in prepared.preview.plan.plan_states
            ]
            # Nothing was posted for this preview, so nothing may ever approve it.
            preview_store.mark(prepared.preview_id, preview_store.STATUS_SUPERSEDED)
        jobs.record_segment(
            job.job_id,
            index,
            dry_run={
                "clean": not reasons,
                "reasons": reasons,
                "estimates": estimates,
                "rounded": [
                    f"{p.estimate:.2f} -> {p.target:.2f}"
                    for p in (prepared.rounded_periods if prepared.status == 200 else [])
                ],
            },
        )
    # dry_done, not done: nothing was committed, so the request must stay
    # repeatable -- the same payload can come back and be run for real.
    finish(
        job.job_id,
        "dry run: nothing was committed",
        status=jobs.STATUS_DRY_DONE,
    )


def _do_segment(job: jobs.Job) -> bool:
    """Preview, check and (when clean) commit the job's next plan.

    Returns True when the job should carry on with the following plan.
    """
    index = job.next_index
    old_plan_id = job.segments[index - 1]["plan_id"] if index else None

    prepared, requested = _prepare(job, index, old_plan_id, store_job=True)
    reasons = clean_reasons(prepared, requested)
    if prepared.status != 200:
        fail(job.job_id, f"{_where(job, index)} {reasons[0]}")
        return False
    if job.institution.upper() not in allowed_institutions():
        reasons.append(
            f"institution {job.institution or 'unknown'} is not in QUEUE_ALLOWED_INSTITUTIONS"
        )
    if reasons:
        hold(job, index, prepared, reasons)
        return False

    # Every check that can stop a commit runs HERE, immediately before it --
    # not once at the start of the job.
    if jobs.is_paused():
        _stand_down(job, prepared, "the queue is paused")
        return False
    if _cap_reached():
        _stand_down(job, prepared, "the daily commit limit was reached")
        _post_cap_notice()
        return False

    known_ids = {s["plan_id"] for s in job.segments[:index]}
    jobs.state_increment(f"commits:{jobs.utc_day(_now())}")  # counted as an attempt
    text = approval._approve(prepared.preview_id, "queue-worker")

    written = _find_written(job, index, known_ids)
    if written is None:
        fail(
            job.job_id,
            f"{_where(job, index)} was not created ({text}); the child's plans were "
            f"read back and it is not there",
        )
        return False

    stored = preview_store.get(prepared.preview_id)
    if stored is None or not stored.is_committed:
        # Written, though the commit reported a failure: a written plan is a
        # success. Make sure the preview can never be approved into a second one.
        logger.warning("Commit reported %r but plan %s exists; counting it written", text, written.id)
        preview_store.mark(prepared.preview_id, preview_store.STATUS_COMMITTED)

    jobs.mark_committed(job.job_id, index, written.id)
    job = jobs.get(job.job_id)

    plans = read_plans(job.child_id)
    problem = check_state(job, plans)
    if problem:
        fail(job.job_id, f"after {_where(job, index)}: {problem}")
        return False
    return True


def _stand_down(job: jobs.Job, prepared, why: str) -> None:
    """Leave the job queued, un-previewed: it will start this plan afresh later."""
    preview_store.mark(prepared.preview_id, preview_store.STATUS_SUPERSEDED)
    jobs.set_status(job.job_id, jobs.STATUS_QUEUED, why)
    logger.warning("QUEUE JOB STANDS DOWN job_id=%s: %s", job.job_id, why)


# --------------------------------------------------------------------------- #
# Startup, the loop, the digest
# --------------------------------------------------------------------------- #
def recover_running() -> list[str]:
    """Resume every job left `running` by a restart -- by READ-BACK, never by replay.

    The child's plans are read and matched to the job's segments by start date:
    each segment whose plan exists is recorded as committed, in order. Nothing is
    written to Famly. A job whose child's plans do not match what the job could
    have made is failed, not guessed at; the rest go back on the queue.
    """
    recovered = []
    for job in jobs.list_jobs(jobs.STATUS_RUNNING):
        try:
            plans = read_plans(job.child_id)
            matched = 0
            for index, segment in enumerate(job.segments):
                start = engine._day(segment["from"])
                plan = next((p for p in plans if engine._day(p.from_) == start), None)
                if plan is None:
                    break
                if index >= job.next_index:
                    jobs.mark_committed(job.job_id, index, plan.id)
                matched = index + 1
            fresh = jobs.get(job.job_id)
            if len(plans) != matched or matched < job.next_index:
                fail(
                    job.job_id,
                    f"after a restart the child's plans ({len(plans)}) do not match the "
                    f"job's ({matched} found)",
                )
            else:
                jobs.set_status(
                    job.job_id,
                    jobs.STATUS_QUEUED,
                    f"resumed after a restart ({fresh.next_index} plan(s) already in Famly)",
                    only_from=(jobs.STATUS_RUNNING,),
                )
                recovered.append(job.job_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not recover job_id=%s", job.job_id)
            fail(job.job_id, f"could not be resumed after a restart: {type(exc).__name__}: {exc}")
    return recovered


def expire_held() -> list[str]:
    """Cancel held jobs whose approval message has expired."""
    cancelled = []
    for job in jobs.list_jobs(jobs.STATUS_HELD):
        stored = preview_store.get(job.preview_id or "")
        if stored is not None and not stored.is_expired:
            continue
        if stored is not None or not job.preview_id:
            if jobs.set_status(
                job.job_id,
                jobs.STATUS_CANCELLED,
                "the approval for the held plan expired",
                only_from=(jobs.STATUS_HELD,),
            ):
                slack.post_notice(
                    f":hourglass: Queue job `{job.job_id}` ({_child_line(job.child_id)}) was "
                    f"cancelled: its held plan expired without an answer.\n"
                    + "\n".join(_exists_lines(job))
                )
                cancelled.append(job.job_id)
    return cancelled


def _digest_lines(job: jobs.Job) -> list[str]:
    label = " (dry run)" if job.status == jobs.STATUS_DRY_DONE else ""
    lines = [f"• {_child_line(job.child_id)} — job `{job.job_id}`{label}"]
    for index, segment in enumerate(job.segments):
        dry = segment.get("dry_run")
        if dry is not None:
            verdict = "clean" if dry["clean"] else f"would be HELD: {'; '.join(dry['reasons'])}"
            figures = ", ".join(dry["rounded"] or dry["estimates"]) or "no estimate"
            lines.append(f"    {_where(job, index)}: {verdict} ({figures})")
        elif segment.get("plan_id"):
            lines.append(f"    {_where(job, index)}: created, plan `{segment['plan_id']}`")
    return lines


def post_digest(now: datetime | None = None, *, drained: bool = False) -> bool:
    """One Slack digest of finished jobs: when the queue drains, or every 30 minutes."""
    pending = jobs.undigested_done()
    if not pending:
        return False

    now = now or _now()
    last = jobs.state_get("last_digest_at")
    if not drained:
        if last is None:
            jobs.state_set("last_digest_at", now.isoformat())
            return False
        if (now - datetime.fromisoformat(last)).total_seconds() < DIGEST_INTERVAL_SECONDS:
            return False

    # Report what the jobs themselves were, not just the mode now: a digest can
    # carry dry runs and real ones together after the mode was switched over.
    dry_count = sum(1 for j in pending if j.status == jobs.STATUS_DRY_DONE)
    heading = (
        f":white_check_mark: *Queue digest* ({mode()}) — {len(pending)} job(s) finished"
        + (
            f" ({dry_count} DRY RUN: nothing was committed)"
            if dry_count
            else ""
        )
    )
    lines = [heading]
    for job in pending:
        lines += _digest_lines(job)
    if slack.post_notice("\n".join(lines)):
        jobs.mark_digested([j.job_id for j in pending])
        jobs.state_set("last_digest_at", now.isoformat())
        return True
    return False


def run_once(now: datetime | None = None) -> bool:
    """One poll: returns True when a job was run."""
    now = now or _now()
    current_mode = mode()
    if current_mode == MODE_OFF:
        return False

    expire_held()

    if jobs.is_paused():
        return False
    if current_mode == MODE_AUTO and _cap_reached(now):
        if jobs.counts()[jobs.STATUS_QUEUED]:
            _post_cap_notice(now)
        return False

    job = jobs.claim_next()
    if job is None:
        post_digest(now, drained=True)
        return False

    run_job(job.job_id)
    counts = jobs.counts()
    drained = not counts[jobs.STATUS_QUEUED] and not counts[jobs.STATUS_RUNNING]
    post_digest(now, drained=drained)
    return True


def run_forever(poll_seconds: float = POLL_SECONDS) -> None:
    """The worker loop: recover, then poll until stopped."""
    recover_running()
    logger.info("Queue worker started (mode=%s)", mode())
    try:
        while True:
            try:
                worked = run_once()
            except Exception:  # noqa: BLE001 - the loop itself must survive
                logger.exception("Queue poll failed")
                worked = False
            if not worked:
                _sleep(poll_seconds)
    except KeyboardInterrupt:
        logger.info("Queue worker stopped")


# --------------------------------------------------------------------------- #
# Operator commands
# --------------------------------------------------------------------------- #
def status() -> dict:
    return {
        "mode": mode(),
        "paused": jobs.is_paused(),
        "consecutiveFailures": jobs.consecutive_failures(),
        "commitsToday": jobs.commits_today(),
        "maxCommitsPerDay": max_commits_per_day(),
        "allowedInstitutions": sorted(allowed_institutions()),
        "maxMonthly": str(max_monthly()),
        "jobs": jobs.counts(),
    }


def list_jobs(status_filter: str | None = None) -> dict:
    if status_filter and status_filter not in jobs.ALL_STATUSES:
        return {"error": f"status must be one of {', '.join(jobs.ALL_STATUSES)}", "jobs": []}
    rows = [
        {
            "jobId": j.job_id,
            "childId": j.child_id,
            "status": j.status,
            "committed": j.next_index,
            "total": len(j.segments),
            "detail": j.detail,
            "previewId": j.preview_id,
            "created": j.created_at,
        }
        for j in jobs.list_jobs(status_filter)
    ]
    return {"jobs": rows, "count": len(rows)}


def cancel(job_id: str) -> dict:
    """Cancel a job. Touches NOTHING in Famly: plans already created stay."""
    job = jobs.get(job_id)
    if job is None:
        return {"cancelled": False, "reason": f"no job {job_id}"}
    if not jobs.set_status(job_id, jobs.STATUS_CANCELLED, "cancelled by an operator (queue-cancel)"):
        # A finished job (including a dry run) never moves again. Say so plainly
        # rather than just naming the status.
        finished = " and cannot be cancelled" if job.status in jobs.FINISHED else ""
        return {"cancelled": False, "reason": f"job is already {job.status}{finished}"}
    if job.preview_id:
        stored = preview_store.get(job.preview_id)
        if stored is not None and stored.status in (
            preview_store.STATUS_PENDING,
            preview_store.STATUS_EXPIRED,
        ):
            preview_store.mark(job.preview_id, preview_store.STATUS_REJECTED)
    return {
        "cancelled": True,
        "jobId": job_id,
        "committed": job.next_index,
        "note": "plans already created in Famly were left untouched",
    }


def pause() -> dict:
    jobs.set_paused(True)
    return {"paused": True}


def resume() -> dict:
    """Clear the pause flag and the consecutive-failure count."""
    jobs.set_paused(False)
    jobs.state_set("consecutive_failures", "0")
    return {"paused": False}
