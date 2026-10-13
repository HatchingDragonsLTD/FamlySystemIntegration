"""delete_plans -- TEMPORARY, DESTRUCTIVE. Delete ALL of one child's plans.

THIS IS A ONE-OFF CLEAN-UP UTILITY. Delete the whole `actions/delete_plans/`
package, its entry in `web_registry.py`, its tests and the `delete_log` table
once the clean-up is finished. Like `urn_lookup`, it exists for a bounded job and
should not outlive it.

It deletes plans in Famly, which cannot be undone, so it is built to do nothing
unless every guard says yes:

  * `DELETE_PLANS_MODE`: `off` (the default), `dry_run` or `live`. Anything
    else counts as `off`.
  * the child must be on the ALLOWLIST FILE (`DELETE_PLANS_ALLOWLIST_FILE`,
    default `var/delete_plans_allowlist.txt`, one child id per line). It is
    re-read on every request, a missing or empty file allows nothing, and it is
    gitignored.
  * the request must say `"confirm": "DELETE"` exactly.
  * `DELETE_PLANS_MAX_PER_DAY` caps how many CHILDREN are touched per UTC day
    (default 40), checked before every delete.
  * a PAUSE flag, set after 3 consecutive failed children, checked before every
    delete.

PER CHILD, in `live` mode: the child's plans are read live; they are deleted
LATEST-STARTING FIRST; each plan must carry `CanDeletePlan` (otherwise it is
skipped and reported); its full read-back JSON is written to
`var/plan-backups/<child>/<plan>.json` BEFORE it is deleted (no backup, no
delete); the plans are read back afterwards to prove it is gone, and the
neighbouring plans' end dates are reported. A delete is NEVER retried automatically.

`dry_run` reads and logs -- the plans that would be deleted appear in the digest --
and never calls `_delete_plan`.

`_delete_plan` sends ONE request, `DELETE v2/plans/<id>` (no params, no body),
exactly as captured. Famly answers HTTP 200 with `[]`, but `[]` is NOT proof of
deletion: the read-back after each delete decides.

"UNEXPECTED" -- what stops a child -- is exactly one of: the plan is still present
in the read-back, an HTTP error, or the child's plan count not dropping by
exactly one. (A guard -- no backup, the pause flag, the daily cap -- also stops a
child before a delete, as described above.) A change to a neighbouring plan's end
date is reported (the digest and the log) but NEVER stops the run: every plan on
the child is being deleted anyway.

Every outcome is a row in the `delete_log` table of the preview store's SQLite
file. The backups are personal data and live under the gitignored `var/`.

SLACK. There is ONE end-of-run DIGEST (see digest.py), not a message per child.
By default it comes only from the `delete-digest` command; with
DELETE_DIGEST_ON_COMPLETE=true a webhook request also posts it once every
allowlist child has reported (with bare ids).
Only three things post immediately: an unexpected result that stops a child, a
tripped pause (the breaker) and a daily-cap stop. Everything else -- deleted,
nothing to delete, partly deleted, refused, a dry run -- waits for the digest.
Names appear only in a digest posted from the CLI (`delete-digest`), fetched
then; they are never fetched during a webhook request and never stored.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from actions.plan_write import input_schema, preview_store
from actions.read_child_plans import runner as read_child_plans
from core.rest_client import RestClient
from integrations import slack

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ALLOWLIST_FILE = PROJECT_ROOT / "var" / "delete_plans_allowlist.txt"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "var" / "plan-backups"

MODE_OFF, MODE_DRY_RUN, MODE_LIVE = "off", "dry_run", "live"
DEFAULT_MAX_PER_DAY = 40
BREAKER_THRESHOLD = 3
CONFIRM_WORD = "DELETE"
CAN_DELETE = "CanDeletePlan"

PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))

# The pause flag and the failure count live in the shared `queue_state` table,
# each under its OWN key. The plan queue's pause is the bare key "paused" and its
# failure count "consecutive_failures" (actions/queue_timeline/jobs.py): these
# are different keys, so tripping one never pauses the other.
PAUSED_KEY = "delete_plans:paused"
FAILURES_KEY = "delete_plans:consecutive_failures"

# EVERY value `data.outcome` can take in a response, and what it means. The
# README documents this table, and a test keeps the two (and the code) in step.
OUTCOMES = {
    "malformed": "the request was malformed (HTTP 422): a bad childId or a confirm that is not exactly DELETE. Nothing was done.",
    "off": "DELETE_PLANS_MODE is off (or unset, or unknown). Nothing was done, logged or posted.",
    "not_allowlisted": "the child is not on the allowlist file. Nothing was read or done; a Slack notice was posted.",
    "nothing_to_delete": "the child has no plans: ok, and a harmless no-op (so a repeat request lands here).",
    "dry_run": "dry_run mode: the plans were listed in Slack and logged. Nothing was deleted.",
    "deleted": "live mode: every plan was deleted, each backed up first and confirmed gone by read-back.",
    "partial": "live mode: every plan that could be deleted was, but at least one was skipped for lacking CanDeletePlan and remains.",
    "failed": "live mode: the child was stopped (a backup, delete or read-back failure, an unexpected read-back, the pause flag, the daily cap). See `message`.",
    "error": "an unexpected error (for example Famly could not be read). Reported, never raised.",
}


# --------------------------------------------------------------------------- #
# Settings, read fresh on every request
# --------------------------------------------------------------------------- #
def mode() -> str:
    """DELETE_PLANS_MODE: off | dry_run | live. Unset or unknown is off."""
    raw = os.environ.get("DELETE_PLANS_MODE", "").strip().lower()
    if raw in (MODE_DRY_RUN, MODE_LIVE):
        return raw
    if raw not in ("", MODE_OFF):
        logger.error("DELETE_PLANS_MODE=%r is not off, dry_run or live; treating it as off", raw)
    return MODE_OFF


def allowlist_path() -> Path:
    override = os.environ.get("DELETE_PLANS_ALLOWLIST_FILE", "").strip()
    return Path(override) if override else DEFAULT_ALLOWLIST_FILE


def backup_dir() -> Path:
    override = os.environ.get("DELETE_PLANS_BACKUP_DIR", "").strip()
    return Path(override) if override else DEFAULT_BACKUP_DIR


def allowlist() -> set[str]:
    """The child ids allowed, re-read now. A missing or empty file allows NOTHING."""
    try:
        text = allowlist_path().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {
        line.strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


_TRUE_VALUES = ("1", "true", "yes", "on")


def digest_on_complete() -> bool:
    """DELETE_DIGEST_ON_COMPLETE: may a webhook request post the digest? Default no.

    Read fresh on every request. Anything but a clear yes is no, so a typo can
    only ever mean fewer messages. When false, digests come ONLY from
    `delete-digest`; the immediate messages (an unexpected stop, a tripped pause,
    a daily-cap stop) are not affected.
    """
    return os.environ.get("DELETE_DIGEST_ON_COMPLETE", "").strip().lower() in _TRUE_VALUES


def max_per_day() -> int:
    raw = os.environ.get("DELETE_PLANS_MAX_PER_DAY", "").strip()
    if not raw:
        return DEFAULT_MAX_PER_DAY
    try:
        return int(raw)
    except ValueError:
        logger.error("DELETE_PLANS_MAX_PER_DAY=%r is not a number; using %d", raw, DEFAULT_MAX_PER_DAY)
        return DEFAULT_MAX_PER_DAY


def _now() -> datetime:
    """The clock, in one place so tests can move it."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# The audit log and the small shared state
# --------------------------------------------------------------------------- #
def log_row(child_id: str, plan_id: str | None, run_mode: str, outcome: str, detail: str | None = None) -> None:
    with preview_store.open_store() as connection:
        connection.execute(
            "INSERT INTO delete_log (child_id, plan_id, at, mode, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (child_id, plan_id, _now().isoformat(), run_mode, outcome, detail),
        )
    logger.info("DELETE_PLANS %s child=%s plan=%s: %s %s", run_mode, child_id, plan_id, outcome, detail or "")


def log_rows(child_id: str | None = None) -> list[dict]:
    with preview_store.open_store() as connection:
        if child_id:
            rows = connection.execute("SELECT * FROM delete_log WHERE child_id = ? ORDER BY id", (child_id,))
        else:
            rows = connection.execute("SELECT * FROM delete_log ORDER BY id")
        return [dict(r) for r in rows.fetchall()]


def _state_get(key: str) -> str | None:
    with preview_store.open_store() as connection:
        row = connection.execute("SELECT value FROM queue_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _state_set(key: str, value: str | None) -> None:
    with preview_store.open_store() as connection:
        if value is None:
            connection.execute("DELETE FROM queue_state WHERE key = ?", (key,))
        else:
            connection.execute(
                "INSERT INTO queue_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def is_paused() -> bool:
    return _state_get(PAUSED_KEY) == "1"


def set_paused(paused: bool) -> None:
    """Set or clear the pause flag. Clearing also resets the failure count."""
    _state_set(PAUSED_KEY, "1" if paused else None)
    if not paused:
        _state_set(FAILURES_KEY, None)


def consecutive_failures() -> int:
    return int(_state_get(FAILURES_KEY) or 0)


def last_log_rows(count: int = 5) -> list[dict]:
    """The most recent `count` delete_log rows, newest first."""
    with preview_store.open_store() as connection:
        rows = connection.execute(
            "SELECT * FROM delete_log ORDER BY id DESC LIMIT ?", (count,)
        ).fetchall()
    return [dict(r) for r in rows]


def state() -> dict:
    """The current state of delete_plans: mode, pause flag, today's count, recent log."""
    today = children_today()
    return {
        "mode": mode(),
        "paused": is_paused(),
        "consecutiveFailures": consecutive_failures(),
        "childrenDoneToday": len(today),
        "maxPerDay": max_per_day(),
        "lastLogRows": last_log_rows(5),
    }


def resume() -> dict:
    """Clear the delete_plans pause flag (and its failure count); report the state.

    Touches only delete_plans' own keys: the plan queue's pause is not affected.
    """
    was_paused = is_paused()
    set_paused(False)
    return {"wasPaused": was_paused, **state()}


def children_today(now: datetime | None = None) -> set[str]:
    """The children a LIVE delete has started on today (UTC)."""
    day = (now or _now()).astimezone(timezone.utc).date().isoformat()
    with preview_store.open_store() as connection:
        rows = connection.execute(
            "SELECT DISTINCT child_id FROM delete_log "
            "WHERE mode = ? AND outcome = 'started' AND substr(at, 1, 10) = ?",
            (MODE_LIVE, day),
        ).fetchall()
    return {r["child_id"] for r in rows}


def _record_child_result(failed: bool, child_id: str) -> None:
    """Count a live child toward the breaker: 3 failed in a row pause everything."""
    if not failed:
        _state_set(FAILURES_KEY, None)
        return
    with preview_store.open_store() as connection:
        connection.execute(
            "INSERT INTO queue_state (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
            (FAILURES_KEY,),
        )
    if consecutive_failures() >= BREAKER_THRESHOLD and not is_paused():
        _state_set(PAUSED_KEY, "1")
        slack.post_notice(
            f":rotating_light: *delete_plans paused* — {BREAKER_THRESHOLD} children in a row "
            f"failed (the last: `{child_id}`). Nothing more will be deleted until the pause "
            f"flag is cleared."
        )


# --------------------------------------------------------------------------- #
# The delete itself -- NOT WRITTEN YET
# --------------------------------------------------------------------------- #
_PLAN_ID = re.compile(r"[A-Za-z0-9_-]+")


def _delete_plan(plan_id: str) -> None:
    """Delete one plan in Famly: `DELETE v2/plans/<plan_id>`, no params, no body.

    That is the captured request. Famly answers HTTP 200 with `[]`; the answer is
    NOT inspected -- `[]` proves nothing, and the caller's read-back of the child's
    plans is what decides whether the plan is gone. A non-2xx response raises
    RestHTTPError (from the client) and is not caught here. Exactly one request is
    made: nothing is retried.

    Refuses (ValueError, before any request) an id that is not a plain token, so
    nothing but a plan id can ever be spliced into the path.
    """
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise ValueError(f"not a plan id: {plan_id!r}")
    RestClient().request("DELETE", f"v2/plans/{plan_id}")


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
def _day(value) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return date.min


def latest_first(plans: list) -> list:
    """Plans ordered latest-starting first (undated ones last)."""
    return sorted(plans, key=lambda p: _day(p.from_), reverse=True)


def can_delete(plan) -> bool:
    return CAN_DELETE in (plan.behaviors or [])


def plan_row(plan) -> dict:
    return {
        "planId": plan.id,
        "from": plan.from_,
        "to": plan.to,
        "monthlyEstimate": plan.current_monthly_estimate() if hasattr(plan, "current_monthly_estimate") else plan.monthly_estimate,
        "canDelete": can_delete(plan),
        "result": None,
    }


def read_plans(child_id: str) -> list:
    return read_child_plans.run(child_id, version=PLAN_VERSION).plans


def write_backup(child_id: str, plan) -> Path:
    """Write the plan's full read-back JSON to var/plan-backups/<child>/<plan>.json.

    Raises when it cannot -- and then the plan is NOT deleted. The file is
    written whole (to a temporary name, then renamed) so a half-written backup
    never stands in for a real one.
    """
    folder = backup_dir() / child_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{plan.id}.json"
    temporary = folder / f"{plan.id}.json.part"
    temporary.write_text(json.dumps(plan.raw, indent=2, default=str), encoding="utf-8")
    temporary.replace(target)
    if not target.is_file() or target.stat().st_size == 0:
        raise OSError(f"backup {target} is missing or empty after writing")
    return target


# --------------------------------------------------------------------------- #
# One child
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    child_id: str
    mode: str
    ok: bool = False
    outcome: str = ""
    message: str = ""
    plans: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def data(self) -> dict:
        return {
            "ok": self.ok,
            "childId": self.child_id,
            "mode": self.mode,
            "outcome": self.outcome,
            "message": self.message,
            "plans": self.plans,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def _refuse(result: Result, outcome: str, message: str, *, notify: bool, log: bool = True) -> Result:
    result.outcome, result.message = outcome, message
    if outcome != "ok":
        result.errors = [message]
    if log:
        log_row(result.child_id, None, result.mode, outcome, message)
    if notify:
        slack.post_notice(f":no_entry: delete_plans — child `{result.child_id}`: {message}")
    return result


def run(child_id: str) -> Result:
    """Handle one validated request for one child. Never raises.

    The caller has already checked the request is well formed. Only when
    DELETE_DIGEST_ON_COMPLETE is true is the digest posted after each child, if
    every allowlist child now has an unreported row (see digest.py) -- from the
    webhook, so with ids only: no name is fetched. By default a webhook request
    never posts a digest.
    """
    result = _process(child_id)
    if result.mode != MODE_OFF and digest_on_complete():
        try:
            from . import digest

            digest.post_if_complete()
        except Exception:  # noqa: BLE001 - the digest must never fail the request
            logger.exception("DELETE_PLANS digest check failed")
    return result


def _process(child_id: str) -> Result:
    run_mode = mode()
    result = Result(child_id=child_id, mode=run_mode)

    if run_mode == MODE_OFF:
        result.outcome = "off"
        result.message = "delete_plans is off (DELETE_PLANS_MODE); nothing was done"
        return result  # no log row, no Slack, no Famly: it is switched off

    try:
        return _run(result)
    except Exception as exc:  # noqa: BLE001 - a destructive call must never raise at HubSpot
        logger.exception("DELETE_PLANS crashed child=%s", child_id)
        if run_mode == MODE_LIVE:
            _record_child_result(True, child_id)
        return _refuse(
            result,
            "error",
            f"unexpected error: {type(exc).__name__}: {exc}",
            notify=True,
        )


def _run(result: Result) -> Result:
    child_id = result.child_id

    if child_id.lower() not in allowlist():
        # Refused, and reported in the digest -- not a message of its own.
        return _refuse(result, "not_allowlisted", "this child is not on the allowlist; nothing was done", notify=False)

    plans = read_plans(child_id)
    if not plans:
        result.ok = True
        result.outcome = "nothing_to_delete"
        result.message = "nothing to delete: the child has no plans"
        log_row(child_id, None, result.mode, result.outcome)
        return result

    if result.mode == MODE_DRY_RUN:
        return _dry_run(result, plans)
    return _live(result, plans)


def _dry_run(result: Result, plans: list) -> Result:
    ordered = latest_first(plans)
    result.plans = [plan_row(p) for p in ordered]

    result.ok = True
    result.outcome = "dry_run"
    result.message = f"dry run: {len(plans)} plan(s) listed; nothing was deleted"
    # The plans that WOULD be deleted ride in the log row as JSON (no names, only
    # ids, dates and figures): the digest lists them from there.
    log_row(
        result.child_id,
        None,
        MODE_DRY_RUN,
        "dry_run",
        json.dumps(
            [
                {k: row[k] for k in ("planId", "from", "to", "monthlyEstimate", "canDelete")}
                for row in result.plans
            ],
            default=str,
        ),
    )
    return result


def _neighbours(before: list, after: list, gone_id: str) -> tuple[list[str], list[str]]:
    """What happened to the end dates of the plans either side of a deleted one.

    Returns (lines, changes): a line for each neighbour, and a short note for each
    whose end date MOVED. Reported, never a reason to stop.
    """
    lines: list[str] = []
    changes: list[str] = []
    ordered = sorted(before, key=lambda p: _day(p.from_))
    index = next((i for i, p in enumerate(ordered) if p.id == gone_id), None)
    if index is None:
        return lines, changes
    after_by_id = {p.id: p for p in after}
    for label, offset in (("previous", -1), ("next", 1)):
        j = index + offset
        if 0 <= j < len(ordered) and ordered[j].id in after_by_id:
            was, now = ordered[j].to, after_by_id[ordered[j].id].to
            change = "unchanged" if was == now else "changed"
            lines.append(
                f"  {label} plan `{ordered[j].id}`: end date {was or 'open-ended'} -> "
                f"{now or 'open-ended'} ({change})"
            )
            if was != now:
                changes.append(f"`{ordered[j].id}` {was or 'open-ended'} -> {now or 'open-ended'}")
    return lines, changes


def _live(result: Result, plans: list) -> Result:
    child_id = result.child_id
    summary: list[str] = []
    end_changes: list[str] = []  # neighbour end dates that moved, for the digest
    deleted = skipped = 0
    failure: str | None = None
    counts_as_failed = True  # a pause or the daily cap is not a failed child
    stopped_by = "stopped"  # the terminal log row: stopped (unexpected) or refused (pause, cap)
    current = list(plans)

    for plan in latest_first(plans):
        row = plan_row(plan)
        result.plans.append(row)

        if not can_delete(plan):
            row["result"] = "skipped"
            result.warnings.append(f"plan {plan.id} skipped: no {CAN_DELETE}")
            summary.append(f"• `{plan.id}` skipped — no {CAN_DELETE}")
            log_row(child_id, plan.id, MODE_LIVE, "skipped", f"no {CAN_DELETE}")
            skipped += 1
            continue

        # The pause flag and the daily cap are checked before EVERY delete.
        if is_paused():
            row["result"] = "paused"
            failure = "the delete_plans pause flag is set"
            counts_as_failed = False
            stopped_by = "refused"
            log_row(child_id, plan.id, MODE_LIVE, "paused", failure)
            break

        today = children_today()
        if child_id not in today:
            if len(today) >= max_per_day():
                row["result"] = "cap"
                failure = f"the daily limit of {max_per_day()} children has been reached"
                counts_as_failed = False
                log_row(child_id, plan.id, MODE_LIVE, "cap_reached", failure)
                # A daily-cap stop is one of the three things that post at once.
                slack.post_notice(
                    f":hourglass: *delete_plans daily limit reached* — {max_per_day()} children "
                    f"today (UTC). Child `{child_id}` was not processed; the rest wait for tomorrow."
                )
                stopped_by = "refused"
                break
            log_row(child_id, None, MODE_LIVE, "started", f"{len(plans)} plan(s)")

        try:
            target = write_backup(child_id, plan)
        except Exception as exc:  # noqa: BLE001 - no backup, no delete
            row["result"] = "backup_failed"
            failure = f"the backup of plan {plan.id} failed ({type(exc).__name__}: {exc}); it was NOT deleted"
            log_row(child_id, plan.id, MODE_LIVE, "backup_failed", failure)
            break

        try:
            _delete_plan(plan.id)  # exactly one attempt, never retried
        except Exception as exc:  # noqa: BLE001 - the first unexpected result stops the child
            row["result"] = "delete_failed"
            failure = f"deleting plan {plan.id} failed ({type(exc).__name__}: {exc})"
            log_row(child_id, plan.id, MODE_LIVE, "delete_failed", failure)
            break

        try:
            after = read_plans(child_id)
        except Exception as exc:  # noqa: BLE001
            row["result"] = "readback_failed"
            failure = f"plan {plan.id} was deleted but the read-back failed ({type(exc).__name__}); stopping"
            log_row(child_id, plan.id, MODE_LIVE, "readback_failed", failure)
            break

        # The two read-back results that are unexpected: the plan is still there,
        # or the plan count did not drop by exactly one.
        still_there = plan.id in {p.id for p in after}
        if still_there or len(after) != len(current) - 1:
            row["result"] = "unexpected"
            failure = (
                f"after deleting plan {plan.id} it is still there; stopping"
                if still_there
                else f"after deleting plan {plan.id} the child has {len(after)} plan(s), not "
                f"{len(current) - 1} (the count should drop by exactly one); stopping"
            )
            log_row(child_id, plan.id, MODE_LIVE, "unexpected", failure)
            break

        # A neighbour's end date moving is reported and logged -- never a stop.
        lines, changed = _neighbours(current, after, plan.id)
        row["result"] = "deleted"
        summary.append(f"• `{plan.id}` deleted (backup {target.name})")
        summary.extend(lines)
        end_changes.extend(changed)
        log_row(child_id, plan.id, MODE_LIVE, "deleted", "; ".join(l.strip() for l in lines) or None)
        deleted += 1
        current = after

    if failure:
        result.outcome = "failed"
        result.message = failure
        result.errors = [failure]
        _terminal_row(child_id, stopped_by, deleted, skipped, failure, end_changes)
        if counts_as_failed:
            _record_child_result(True, child_id)
            # An unexpected result that stopped a child posts AT ONCE.
            slack.post_notice(
                "\n".join(
                    [f":x: *delete_plans (live)* — child `{child_id}`: stopped. {failure}", *summary]
                )
            )
    else:
        result.ok = skipped == 0
        result.outcome = "deleted" if skipped == 0 else "partial"
        result.message = f"{deleted} plan(s) deleted" + (f", {skipped} skipped" if skipped else "")
        _terminal_row(
            child_id, "done" if skipped == 0 else "partial", deleted, skipped, result.message, end_changes
        )
        _record_child_result(False, child_id)
        # Routine: reported in the digest, not on its own.

    return result


def _terminal_row(
    child_id: str, outcome: str, deleted: int, skipped: int, message: str, end_changes: list[str] | None = None
) -> None:
    """The one row per live child request that says how it ended, for the digest.

    outcome: done | partial | stopped (an unexpected result) | refused (the pause
    flag or the daily cap). `detail` is JSON: {deleted, skipped, message, end_changes}.
    """
    log_row(
        child_id,
        None,
        MODE_LIVE,
        outcome,
        json.dumps(
            {"deleted": deleted, "skipped": skipped, "message": message, "end_changes": end_changes or []}
        ),
    )
