"""The onboarding worker (phase 2): creates the child and the parent contacts in
Famly for ONE queued deal at a time, behind every gate.

MECHANISM REUSED from queue_timeline: a SQLite jobs table, a single worker
process that takes the OLDEST queued job, and a CLI command with `--once` (run at
most one job and exit) or a poll loop (what a systemd unit would run on the
server), plus start-up recovery of a job found mid-write. See
`actions/queue_timeline/queue_worker.py` and `worker.py`.

MODES (config.py): off (the worker does nothing), dry (the full plan and Slack
message, NO writes anywhere, the job ends `dry_done`), live (writes, but only for
ONBOARD_ALLOWED_DEAL_IDS: any other deal ends `held`, `deal_not_allowlisted`,
before any read or write).

THE JOB, in live mode:

 1. Run the LIVE plan (actions/onboard_dry_run; nothing from a cache). Any hold:
    the job ends `held` with the reason codes, a Slack notice, no writes.
 2. GATE A. If anything would be created and the plan's hash is not the one a
    human approved, post the Slack approval request and wait (`awaiting_approval`).
    Approve returns the job to `queued`; the worker then re-runs the plan and
    writes ONLY if the hash is unchanged, otherwise it asks again.
 3. Fail closed on HubSpot: the four sync properties must exist (hubspot_sync.py)
    BEFORE any Famly write.
 4. CHILD (`creating_child`): look again, live, immediately before the write
    (deal id, then urn). A child that now exists is used, not created; two or
    more is `needs_review` (`child_ambiguous`). Otherwise ONE create, never
    retried; an unknown outcome is settled by a live lookup, never a second
    create. Read back: the lookup returns exactly that child, and the internal
    summary has the expected group, dates, test flag and a room move that starts
    on the start date (`room_move_mismatch` -> `needs_review`, NO further writes).
 5. CONTACTS (`creating_contacts`): one create per contact, empty roles, after a
    live existing-match check, each read back from `contacts.list`. A failed or
    held contact does not roll the child back; the job ends `needs_review`.
 6. HubSpot write-back (famly_childid and the sync status/note/gaps).

NOTHING IS REPLAYED. A job found in `creating_*` at start-up (a worker died after
it may have written) becomes `needs_review` (`interrupted`); re-enqueueing starts
a fresh job whose live plan sees whatever landed and does only what is missing. A
job sends at most one child create (a persisted flag), so a re-run can never
create a second child or a duplicate contact.

PERSONAL DATA is read at run time from HubSpot, held in memory for the one call
that needs it, and never stored, logged or put in Slack. A log line here carries
codes, ids and exception TYPE names only.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from actions.onboard import (
    config,
    famly_writes,
    hubspot_sync,
    inputs,
    notifier as notifier_mod,
    store,
    summary_check,
)
from actions.onboard_dry_run import derive
from actions.onboard_dry_run.famly_reads import FamlyReads, normalise_email
from actions.onboard_dry_run.runner import DryRun, plan_hash
from core.hubspot import HubSpotError

logger = logging.getLogger(__name__)

POLL_SECONDS = 5

# Contact outcome codes.
CREATED, PRESENT = "created", "present"
OK_OUTCOMES = (CREATED, PRESENT)


class StopJob(Exception):
    """End the job in `status` with `code`. Nothing further is written."""

    def __init__(self, status: str, code: str, info: dict | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.info = info or {}


@dataclass
class Deps:
    hubspot: Any                 # HubSpotDealWriter: the reads, plus the one writer
    public: Any                  # public-API GraphQL client (reads and the two creates)
    rest: Any                    # internal REST client (the child summary)
    notifier: Any                # .gate(text, job_id, plan_hash) -> ref | None; .notice(text)
    famly_factory: Callable[[], FamlyReads] | None = None

    def famly(self) -> FamlyReads:
        """A FRESH set of live reads. Never reuse one across the plan and a
        write: it memoises its roster scan."""
        return self.famly_factory() if self.famly_factory else FamlyReads(self.public)


def default_deps() -> Deps:
    from actions.pull_roster.runner import _public_client
    from core.hubspot import HubSpotDealWriter
    from core.rest_client import RestClient

    return Deps(
        hubspot=HubSpotDealWriter(),
        public=_public_client(),
        rest=RestClient(),
        notifier=notifier_mod.SlackNotifier(),
    )


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def recover() -> int:
    """Start-up recovery: a job caught mid-write is never replayed."""
    return store.recover_in_flight()


def run_once(deps: Deps | None = None) -> dict:
    """Run at most one queued job. Returns codes and ids only."""
    run_mode = config.mode()
    if run_mode == config.MODE_OFF:
        return {"ran": False, "mode": run_mode}
    job = store.claim_next()
    if job is None:
        return {"ran": False, "mode": run_mode}

    deps = deps or default_deps()
    info: dict = {}
    try:
        info = _process(job, deps, run_mode)
    except StopJob as stop:
        info = stop.info
        _end(job, deps, stop.status, stop.code)
    except Exception as exc:  # noqa: BLE001 - a job must never die silently
        # Type only: the message could carry a response body.
        logger.error("onboard: job %s hit %s", job.id, type(exc).__name__)
        after = store.get(job.id)
        mid_write = after is not None and after.status in (
            store.CREATING_CHILD, store.CREATING_CONTACTS)
        _end(job, deps, store.NEEDS_REVIEW if mid_write else store.FAILED, "worker_error")

    final = store.get(job.id)
    return {
        "ran": True, "mode": run_mode, "jobId": job.id, "status": final.status,
        "code": final.last_error_code, "outcomes": final.outcomes, **info,
    }


def run_forever(poll: float = POLL_SECONDS, deps: Deps | None = None) -> None:
    recover()
    while True:
        if not run_once(deps)["ran"]:
            time.sleep(poll)


# --------------------------------------------------------------------------- #
# One job
# --------------------------------------------------------------------------- #
def _process(job: store.Job, deps: Deps, run_mode: str) -> dict:
    deal_id = job.deal_id
    store.update(job.id, mode=run_mode)

    # Live mode writes only for allowlisted deals. Checked FIRST, before any read.
    if run_mode == config.MODE_LIVE and not config.is_allowed(deal_id):
        _held(job, deps, ["deal_not_allowlisted"], write_back=False)
        return {}

    # A job from the webhook must first be confirmed as an Operations deal at
    # Deposit Pending. Its FIRST live read; a CLI job has no such restriction.
    # No write of any kind, HubSpot included, when it is not.
    if job.source == "webhook":
        reason = _webhook_hold_reason(deps, deal_id)
        if reason:
            _held(job, deps, [reason], write_back=False)
            return {}

    context: dict = {}
    # The child id in the job record, if THIS job created a child: the plan then
    # knows the child is ours and not one that already existed.
    own_child_id = job.child_id if job.child_create_sent else None
    plan = DryRun(deps.hubspot, deps.famly()).plan_deal(
        deal_id, context, created_child_id=own_child_id
    )
    if plan["status"] == "error":
        raise StopJob(store.FAILED, "plan_read_failed")

    digest = plan_hash(plan)
    reasons = sorted({h["reason"] for h in plan["holds"]})
    if reasons:
        store.update(job.id, plan_hash=digest)
        _held(job, deps, reasons, write_back=run_mode == config.MODE_LIVE)
        return {}

    if run_mode == config.MODE_DRY:
        deps.notifier.notice(notifier_mod.gate_text(plan, digest, run_mode))
        store.set_status(job.id, store.DRY_DONE, plan_hash=digest)
        return {}

    # GATE A applies to ANY live plan that would perform a Famly write: a child
    # create, or only contact creates on a child that exists.
    if famly_writes_planned(plan) and job.approved_hash != digest:
        _ask_for_approval(job, deps, plan, digest, run_mode, plan_changed=job.approved_hash is not None)
        return {}

    return _write_phase(job, deps, plan, context, digest)


def famly_writes_planned(plan: dict) -> bool:
    """Whether the plan would write to Famly at all: a child create and/or any
    contact create. Such a plan needs a Slack approval (Gate A) in live mode."""
    return plan["child"]["status"] == "child_would_create" or any(
        c["status"] == "would_create" for c in plan["contacts"]
    )


STAGE_NOT_ELIGIBLE = "stage_not_eligible"
ELIGIBLE_PIPELINE = "operations"
ELIGIBLE_STAGE = "deposit pending"


def _webhook_hold_reason(deps, deal_id: str) -> str | None:
    """None if the deal is in the Operations pipeline at Deposit Pending, else the
    hold reason (`stage_not_eligible`, or `deal_not_found`).

    Raises:
        StopJob: HubSpot could not be read (`plan_read_failed`).
    """
    try:
        deal = deps.hubspot.get_deal(deal_id)
        pipeline, stage = deps.hubspot.stage_names().get(str(deal.get("dealstage")), ("", ""))
    except HubSpotError as exc:
        if exc.status_code == 404:
            return "deal_not_found"
        raise StopJob(store.FAILED, "plan_read_failed") from None
    if (
        derive.norm_stage(pipeline) == ELIGIBLE_PIPELINE
        and derive.norm_stage(stage) == ELIGIBLE_STAGE
    ):
        return None
    return STAGE_NOT_ELIGIBLE


def _ask_for_approval(job, deps, plan, digest, run_mode, plan_changed: bool) -> None:
    store.set_status(job.id, store.AWAITING_APPROVAL, plan_hash=digest, approved_hash=None)
    text = notifier_mod.gate_text(plan, digest, run_mode, plan_changed=plan_changed)
    ref = deps.notifier.gate(text, job.id, digest)
    if ref is None:
        # Nobody can approve a message that never arrived: do not wait for it.
        store.set_status(job.id, store.HELD, last_error_code="approval_request_not_posted")
        return
    store.update(job.id, approval_ref=ref)


def _write_phase(job, deps, plan, context, digest) -> dict:
    deal_id = job.deal_id
    # Re-checked at the point of writing, in case the environment changed.
    if config.mode() != config.MODE_LIVE or not config.is_allowed(deal_id):
        raise StopJob(store.HELD, "deal_not_allowlisted")

    # Fail closed on HubSpot BEFORE any Famly write.
    try:
        check = hubspot_sync.check_properties(deps.hubspot)
    except HubSpotError:
        raise StopJob(store.FAILED, "hubspot_property_check_failed") from None
    logger.info("onboard: famly_ deal properties found: %s", check.found_famly)
    info = {"hubspotSyncPropertiesFound": check.found_famly}
    if not check.ok:
        codes = [f"missing:{n}" for n in check.missing] + [f"option_missing:{v}" for v in check.bad_options]
        deps.notifier.notice(notifier_mod.notice_text(
            deal_id, "cannot write back to HubSpot, so nothing was created in Famly", codes))
        raise StopJob(store.FAILED, "hubspot_property_missing", info)

    store.set_status(job.id, store.CREATING_CHILD, plan_hash=digest)
    deal = context["deal"]
    child_id, created = _ensure_child(job, deps, plan, deal)
    store.update(job.id, child_id=child_id)
    # Ours if this job created it now, or created it earlier (the job record).
    own_child = created or (job.child_create_sent and job.child_id == child_id)

    _write_back(deps, deal_id, hubspot_sync.STATUS_CHILD_READY, "child_ready",
                plan["warnings"], child_id)

    store.set_status(job.id, store.CREATING_CONTACTS)
    outcomes, contact_warnings, all_ok = _create_contacts(
        job, deps, plan, context, child_id, own_child
    )
    store.update(job.id, outcomes=outcomes)

    codes = sorted({o.split(":", 1)[1] for o in outcomes if o.split(":", 1)[1] not in OK_OUTCOMES})
    gaps = list(plan["warnings"]) + contact_warnings
    if all_ok:
        _write_back(deps, deal_id, hubspot_sync.STATUS_CONTACTS_LINKED, "phase2_done", gaps, child_id)
        store.set_status(job.id, store.DONE, last_error_code=None)
        deps.notifier.notice(notifier_mod.notice_text(
            deal_id, "child and contacts are in Famly", ["created" if created else "child_existed"]))
    else:
        note = "phase2_needs_review:" + ",".join(codes)
        _write_back(deps, deal_id, hubspot_sync.STATUS_NEEDS_ATTENTION, note, gaps, child_id)
        store.set_status(job.id, store.NEEDS_REVIEW, last_error_code="contact_outcomes")
        deps.notifier.notice(notifier_mod.notice_text(
            deal_id, "the child exists but some contacts need a person", codes))
    return info


# --------------------------------------------------------------------------- #
# Child
# --------------------------------------------------------------------------- #
def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _lookup_child(deps, deal: dict, deal_id: str, urn_fallback: bool) -> list[str]:
    famly = deps.famly()
    ids = famly.children_by_deal_id(deal_id)
    if not ids and urn_fallback and _clean(deal.get("child_urn")):
        ids = famly.children_by_urn(_clean(deal.get("child_urn")))
    return ids


def _ensure_child(job, deps, plan, deal) -> tuple[str, bool]:
    """(child id, whether this job created it)."""
    deal_id = job.deal_id

    # The live lookup immediately before the write. A child that now exists wins.
    ids = _lookup_child(deps, deal, deal_id, urn_fallback=True)
    if len(ids) > 1:
        raise StopJob(store.NEEDS_REVIEW, "child_ambiguous")
    if ids:
        return ids[0], False

    is_test = config.is_test_deal(deal_id)
    try:
        payload = inputs.build_child_input(deal, deal_id, plan, is_test)
    except inputs.InputIncomplete as exc:
        raise StopJob(store.FAILED, exc.code) from None

    # Persisted BEFORE the request: this job never sends a second create.
    if not store.mark_child_create_sent(job.id):
        raise StopJob(store.NEEDS_REVIEW, "duplicate_create_blocked")

    try:
        child_id = famly_writes.create_child(deps.public, payload)
    except famly_writes.CreateRejected as exc:
        raise StopJob(store.FAILED, exc.code) from None
    except famly_writes.OutcomeUnknown:
        # Did it land? Look; never send again.
        landed = _lookup_child(deps, deal, deal_id, urn_fallback=False)
        if len(landed) > 1:
            raise StopJob(store.NEEDS_REVIEW, "child_ambiguous") from None
        if not landed:
            raise StopJob(store.FAILED, "child_create_unknown_not_landed") from None
        child_id = landed[0]

    _verify_child(deps, deal, deal_id, plan, child_id, is_test)
    return child_id, True


def _verify_child(deps, deal, deal_id, plan, child_id, is_test) -> None:
    """Read the new child back. Any mismatch is `needs_review` with NO further
    writes (in particular `room_move_mismatch`)."""
    found = deps.famly().children_by_deal_id(deal_id)
    if found != [child_id]:
        raise StopJob(store.NEEDS_REVIEW, "readback_child_lookup_failed")

    try:
        summary = deps.rest.get(f"/v2/children/{child_id}/summary")
    except Exception:  # noqa: BLE001 - REST error text can carry a body
        raise StopJob(store.NEEDS_REVIEW, "summary_unreadable") from None

    start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
    end = derive.parse_hubspot_date(deal.get("end_date"))
    code = summary_check.check(
        summary,
        expected_group_id=plan["groupId"],
        expected_start=start.strftime("%Y-%m-%d"),
        expected_end=end.strftime("%Y-%m-%d") if end else None,
        expected_test=is_test,
    )
    if code:
        deps.notifier.notice(notifier_mod.notice_text(
            deal_id, "the child was created but its read-back did not match; no further writes", [code]))
        raise StopJob(store.NEEDS_REVIEW, code)


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
def _create_contacts(
    job, deps, plan, context, child_id, own_child: bool
) -> tuple[list[str], list[str], bool]:
    """(outcome codes, warning codes, whether every contact is created/present)."""
    famly = deps.famly()
    by_id = {c.hubspot_id: c for c in context["contacts"]}
    on_child = famly.child_contacts(child_id)

    outcomes: list[str] = []
    warnings: list[str] = []
    all_ok = True
    stopped = False

    def record(hubspot_id: str, code: str) -> None:
        nonlocal all_ok
        outcomes.append(f"{hubspot_id}:{code}")
        if code not in OK_OUTCOMES:
            all_ok = False

    def matches(rows, target):
        return list(dict.fromkeys(cid for cid, mail in rows if mail == target))

    for entry in plan["contacts"]:
        hubspot_id = entry["hubspotContactId"]
        contact = by_id[hubspot_id]
        if stopped:
            record(hubspot_id, "not_attempted")
            continue

        target = normalise_email(contact.email)
        here = matches(on_child, target)          # the existing-match check, live
        if len(here) == 1:
            record(hubspot_id, PRESENT)
            continue
        if len(here) > 1:
            record(hubspot_id, "ambiguous_on_child")
            continue

        if not own_child:
            # A child that already existed: an unmatched contact is never added to
            # it (the plan holds on this; this is the last line, for a child that
            # appeared between the plan and the write).
            record(hubspot_id, "contact_unmatched_on_existing_child")
            continue

        try:
            payload, contact_warnings = inputs.build_contact_input(contact, child_id)
        except inputs.InputIncomplete as exc:
            record(hubspot_id, exc.code)           # held; the others carry on
            continue
        warnings.extend(contact_warnings)

        try:
            famly_writes.create_contact(deps.public, payload)
        except famly_writes.CreateRejected as exc:
            record(hubspot_id, exc.code)
            stopped = True
            continue
        except famly_writes.OutcomeUnknown:
            landed = matches(famly.child_contacts(child_id), target)
            if len(landed) != 1:
                record(hubspot_id, "create_unknown_not_landed")
                stopped = True
                continue

        # Read-back: exactly once on the child, with the expected email.
        on_child = famly.child_contacts(child_id)
        if len(matches(on_child, target)) != 1:
            record(hubspot_id, "readback_failed")
            stopped = True
            deps.notifier.notice(notifier_mod.notice_text(
                job.deal_id, "a created contact did not read back; no more contacts will be created",
                ["readback_failed"]))
            continue
        record(hubspot_id, CREATED)

    return outcomes, warnings, all_ok


# --------------------------------------------------------------------------- #
# Ending a job
# --------------------------------------------------------------------------- #
def _write_back(deps, deal_id, status, note, gaps, child_id=None) -> None:
    """HubSpot write-back. A failure here, after Famly writes, is `needs_review`
    (`hubspot_writeback_failed`) and never causes another Famly write."""
    try:
        hubspot_sync.write_back(deps.hubspot, deal_id, status=status, note=note,
                                gaps=gaps, child_id=child_id)
    except (HubSpotError, ValueError):
        raise StopJob(store.NEEDS_REVIEW, "hubspot_writeback_failed") from None


def _held(job, deps, reasons: list[str], write_back: bool) -> None:
    store.set_status(job.id, store.HELD, last_error_code=",".join(reasons), outcomes=reasons)
    deps.notifier.notice(notifier_mod.notice_text(job.deal_id, "held, nothing was written", reasons))
    if write_back:
        try:   # best effort: a held deal is visible in HubSpot too
            hubspot_sync.write_back(deps.hubspot, job.deal_id,
                                    status=hubspot_sync.STATUS_NEEDS_ATTENTION,
                                    note="held:" + ",".join(reasons), gaps=[])
        except (HubSpotError, ValueError):
            logger.warning("onboard: held write-back failed for job %s", job.id)


def _end(job, deps, status: str, code: str) -> None:
    store.set_status(job.id, status, last_error_code=code)
    try:
        deps.notifier.notice(notifier_mod.notice_text(job.deal_id, f"{status}", [code]))
    except Exception:  # noqa: BLE001 - a notice must never mask the outcome
        logger.warning("onboard: notice failed for job %s", job.id)
