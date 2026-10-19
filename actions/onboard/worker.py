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
 3. HubSpot never blocks Famly: the deal's properties are listed as a PRE-FLIGHT
    (hubspot_sync.py); a missing or value-rejecting property is warned about (log
    and Slack) and its write skipped, never a reason to hold the Famly work.
 4. CHILD (`creating_child`): look again, live, immediately before the write
    (deal id, then urn). A child that now exists is used, not created; two or
    more is `needs_review` (`child_ambiguous`). Otherwise ONE create (lastDay is
    always sent: the deal's end date, or the first 31 August on or after the 4th
    birthday), never retried; an unknown outcome is settled by a live lookup,
    never a second create. The only STOPPING read-back is exactly one child by the
    deal-id lookup. The internal summary (group, start, end, test flag) is compared
    afterwards and any difference is a FLAG in `famly_sync_gaps` and Slack, never a
    stop.
 5. CONTACTS (`creating_contacts`): every contact is tried independently, bill
    payer first, one create per call, empty roles, each read back from
    `contacts.list`. The live existing-contact lookup is skipped only for the first
    contact of a child this job has just cleanly created. A failed or held contact
    does not stop the others or roll the child back; the job ends `needs_review`
    with per-contact outcome codes.
 6. HubSpot write-back, one property per request: `famly_childid`, a DEFAULTED
    `end_date` (only if still empty, after the Famly write), then status/note/gaps.
    The job ends `needs_review` (`hubspot_writeback_failed`) only if the child id or
    a defaulted end date could not be written; it never blocks or retries Famly.

NOTHING IS REPLAYED. A job found in `creating_*` at start-up (a worker died after
it may have written) becomes `needs_review` (`interrupted`); re-enqueueing starts
a fresh job whose live plan sees whatever landed and does only what is missing. A
job sends at most one child create (a persisted flag), so a re-run can never
create a second child or a duplicate contact.

PERSONAL DATA is read at run time from HubSpot, held in memory for the one call
that needs it, and never stored, logged or put in Slack. A log line here carries
codes, ids and exception TYPE names only.
"""

import hashlib
import json
import logging
import secrets
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
from core import hubspot
from core.hubspot import HubSpotError

logger = logging.getLogger(__name__)

POLL_SECONDS = 5

# Contact outcome codes.
CREATED, PRESENT = "created", "present"
OK_OUTCOMES = (CREATED, PRESENT)

# A create whose outcome is unknown and whose follow-up lookup finds nothing.
UNKNOWN_OUTCOME = "create_outcome_unknown"
UNKNOWN_OUTCOME_ADVICE = "check Famly for the deal id before re-running"


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
    notifier: Any                # .gate(text, job_id, plan_hash, nonce) -> ref | None; .notice(text)
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
    # The job HISTORY for the deal (this job and earlier ones): which children a
    # create this pipeline sent produced. A child found by lookup that is not one
    # of them already existed, and is handled more strictly.
    plan = DryRun(deps.hubspot, deps.famly()).plan_deal(
        deal_id, context, created_child_ids=store.created_child_ids_for_deal(deal_id)
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
        deps.notifier.notice(notifier_mod.gate_text(
            plan, digest, run_mode, this_job_child_id=_this_jobs_child(job)))
        store.set_status(job.id, store.DRY_DONE, plan_hash=digest)
        return {}

    # GATE A applies to ANY live plan that would perform a Famly write: a child
    # create, or only contact creates on a child that exists.
    if famly_writes_planned(plan) and job.approved_hash != digest:
        _ask_for_approval(job, deps, plan, digest, run_mode, plan_changed=job.approved_hash is not None)
        return {}

    return _write_phase(job, deps, plan, context, digest)


def _this_jobs_child(job) -> str | None:
    """The child THIS job created, if it has (for the Slack wording only)."""
    return job.child_id if job.child_create_sent else None


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


def plan_section_hashes(plan: dict) -> dict:
    """A short hash per top-level plan section (hashes only, no personal data), logged
    on every ask so a plan that changes between runs names the section that did."""
    return {
        key: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:8]
        for key, value in sorted(plan.items())
    }


def _ask_for_approval(job, deps, plan, digest, run_mode, plan_changed: bool) -> None:
    # A fresh nonce per ask: only THIS message's buttons can approve it.
    nonce = secrets.token_hex(8)
    store.set_status(job.id, store.AWAITING_APPROVAL, plan_hash=digest, approved_hash=None,
                     approval_nonce=nonce)
    logger.info("onboard: approval asked job=%s plan_changed=%s digest=%s sections=%s",
                job.id, plan_changed, digest, plan_section_hashes(plan))
    text = notifier_mod.gate_text(
        plan, digest, run_mode, plan_changed=plan_changed,
        this_job_child_id=_this_jobs_child(job))
    ref = deps.notifier.gate(text, job.id, digest, nonce)
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

    # PRE-FLIGHT. HubSpot never blocks Famly: a missing property, or a value a
    # dropdown does not offer, is reported (log and Slack) and that property's write
    # is skipped. Nothing here can stop the Famly work below.
    pre = hubspot_sync.preflight(deps.hubspot)
    logger.info("onboard: famly_ deal properties found: %s", pre.found_famly)
    info = {"hubspotSyncPropertiesFound": pre.found_famly}
    if pre.warnings:
        logger.warning("onboard: HubSpot property pre-flight: %s", pre.warnings)
        deps.notifier.notice(notifier_mod.notice_text(
            deal_id, "HubSpot property check: these writes will be skipped, Famly work goes on",
            pre.warnings))
    syncer = hubspot_sync.Syncer(deps.hubspot, deal_id, pre)

    store.set_status(job.id, store.CREATING_CHILD, plan_hash=digest)
    deal = context["deal"]
    child = _ensure_child(job, deps, plan, deal)
    store.update(job.id, child_id=child.child_id)
    # The same history-based rule as the plan, applied at the last moment: ours if
    # this job just created it, or if it was found by the deal-id reference and a
    # create this pipeline sent (an earlier job, or this one) produced that id.
    own_child = child.created or (
        child.via == VIA_DEAL and child.child_id in store.created_child_ids_for_deal(deal_id)
    )

    # The internal summary is compared and FLAGGED, never a reason to stop.
    flags: list[str] = []
    if child.created:
        flags = _summary_flags(deps, deal, plan, child.child_id, config.is_test_deal(deal_id))

    _write_child_back(deps, syncer, deal, deal_id, child)

    store.set_status(job.id, store.CREATING_CONTACTS)
    outcomes, contact_warnings, all_ok = _create_contacts(job, deps, plan, context, child, own_child)
    store.update(job.id, outcomes=outcomes)

    bad_codes = sorted({o.split(":", 1)[1] for o in outcomes if o.split(":", 1)[1] not in OK_OUTCOMES})
    gaps = list(plan["warnings"]) + contact_warnings + flags
    if all_ok:
        syncer.write_status(hubspot_sync.STATUS_CONTACTS_LINKED, "phase2_done", gaps)
    else:
        syncer.write_status(hubspot_sync.STATUS_NEEDS_ATTENTION,
                            "phase2_needs_review:" + ",".join(bad_codes), gaps)

    # The job ends needs_review if any contact failed, or if the child id or a
    # defaulted end date could not be written to the deal. The Famly work is done
    # either way and nothing is retried.
    failures = []
    if not all_ok:
        failures.append("contact_outcomes")
    if syncer.critical_failed:
        failures.append("hubspot_writeback_failed")
    detail = bad_codes + syncer.critical_failed + flags + syncer.reports
    if failures:
        store.set_status(job.id, store.NEEDS_REVIEW, last_error_code=",".join(failures))
        headline = "the child is in Famly but the job needs a person"
        if UNKNOWN_OUTCOME in bad_codes:
            headline += f"; {UNKNOWN_OUTCOME_ADVICE}"
        deps.notifier.notice(notifier_mod.notice_text(deal_id, headline, failures + detail))
    else:
        store.set_status(job.id, store.DONE, last_error_code=None)
        deps.notifier.notice(notifier_mod.notice_text(
            deal_id, "child and contacts are in Famly",
            ["created" if child.created else "child_existed"] + detail))
    return info


# --------------------------------------------------------------------------- #
# Child
# --------------------------------------------------------------------------- #
def _clean(value) -> str:
    return " ".join(str(value or "").split())


VIA_DEAL = "external_system_reference"
VIA_URN = "urn"


@dataclass
class ChildResult:
    child_id: str
    created: bool         # THIS job created it
    clean_create: bool    # the create request returned the id (not settled by a lookup
                          # after an unknown outcome)
    via: str              # how a found child was found


def _lookup_child(deps, deal: dict, deal_id: str, urn_fallback: bool) -> tuple[list[str], str]:
    """(child ids, how they were found: the deal-id reference or the URN)."""
    famly = deps.famly()
    ids = famly.children_by_deal_id(deal_id)
    if ids:
        return ids, VIA_DEAL
    if urn_fallback and _clean(deal.get("child_urn")):
        return famly.children_by_urn(_clean(deal.get("child_urn"))), VIA_URN
    return [], VIA_DEAL


def _ensure_child(job, deps, plan, deal) -> ChildResult:
    deal_id = job.deal_id

    # The live lookup immediately before the write. A child that now exists wins.
    ids, via = _lookup_child(deps, deal, deal_id, urn_fallback=True)
    if len(ids) > 1:
        raise StopJob(store.NEEDS_REVIEW, "child_ambiguous")
    if ids:
        return ChildResult(ids[0], created=False, clean_create=False, via=via)

    is_test = config.is_test_deal(deal_id)
    try:
        payload = inputs.build_child_input(deal, deal_id, plan, is_test)
    except inputs.InputIncomplete as exc:
        raise StopJob(store.FAILED, exc.code) from None

    # Persisted BEFORE the request: this job never sends a second create.
    if not store.mark_child_create_sent(job.id):
        raise StopJob(store.NEEDS_REVIEW, "duplicate_create_blocked")

    clean = True
    try:
        child_id = famly_writes.create_child(deps.public, payload)
    except famly_writes.CreateRejected as exc:
        raise StopJob(store.FAILED, exc.code) from None
    except famly_writes.OutcomeUnknown:
        # Did it land? Look; never send again.
        clean = False
        landed, _via = _lookup_child(deps, deal, deal_id, urn_fallback=False)
        if len(landed) > 1:
            raise StopJob(store.NEEDS_REVIEW, "child_ambiguous") from None
        if not landed:
            # The request may have landed and the lookup is blind to it: a person looks.
            raise StopJob(store.NEEDS_REVIEW, UNKNOWN_OUTCOME) from None
        child_id = landed[0]

    # Recorded AT ONCE, before the read-back: this is the job history that tells a
    # later job the child is the pipeline's.
    store.update(job.id, child_id=child_id)

    # THE ONLY STOPPING READ-BACK: exactly one child by the deal-id reference, and it
    # is this one. (The summary comparison below is a flag, never a stop.)
    if deps.famly().children_by_deal_id(deal_id) != [child_id]:
        raise StopJob(store.NEEDS_REVIEW, "readback_child_lookup_failed")
    return ChildResult(child_id, created=True, clean_create=clean, via=VIA_DEAL)


def _summary_flags(deps, deal, plan, child_id, is_test) -> list[str]:
    """Compare the internal summary with what was sent. Flags, never a stop."""
    try:
        summary = deps.rest.get(f"/v2/children/{child_id}/summary")
    except Exception:  # noqa: BLE001 - REST error text can carry a body
        return [summary_check.UNREADABLE]
    try:
        start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
        end, _defaulted = derive.resolve_end_date(deal)
        return summary_check.flags(
            summary,
            expected_group_id=plan["groupId"],
            expected_start=start.strftime("%Y-%m-%d"),
            expected_end=end.strftime("%Y-%m-%d") if end else None,
            expected_test=is_test,
        )
    except Exception:  # noqa: BLE001 - a flag check must never stop a job
        return [summary_check.UNRECOGNISED]


def _write_child_back(deps, syncer, deal, deal_id, child: ChildResult) -> None:
    """After the Famly child write: the child id, and a DEFAULTED end date.

    Both are CRITICAL (the Syncer records a failure to write either). The end date
    is written only if this job created the child with a defaulted lastDay AND the
    deal's `end_date` is STILL empty when checked now: it is never overwritten.
    """
    syncer.write(hubspot.FAMLY_CHILDID_PROPERTY, child.child_id)

    try:
        end, defaulted = derive.resolve_end_date(deal)
    except ValueError:
        end, defaulted = None, False
    if child.created and defaulted and end is not None:
        try:
            live = deps.hubspot.get_deal(deal_id)
        except HubSpotError:
            # Cannot confirm it is still empty, so it is NOT written.
            syncer._fail(hubspot.END_DATE_PROPERTY, "end_date_check_failed")
        else:
            if str(live.get("end_date") or "").strip():
                syncer.reports.append("end_date_already_set")
            else:
                syncer.write(hubspot.END_DATE_PROPERTY, end.strftime("%Y-%m-%d"))

    syncer.write(hubspot.SYNC_STATUS_PROPERTY, hubspot_sync.STATUS_CHILD_READY)


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
def _create_contacts(
    job, deps, plan, context, child: ChildResult, own_child: bool
) -> tuple[list[str], list[str], bool]:
    """(outcome codes, warning codes, whether every contact is created/present).

    EVERY contact is tried, independently, BILL PAYER FIRST: one failure is
    recorded and the next contact goes ahead. Nothing here stops or holds the
    others.

    The live existing-contact lookup is skipped ONLY while this same job has just
    cleanly created the child and has not yet sent a contact create (a brand new
    child has no contacts). In every other case (a re-run, an existing child, a
    child whose create outcome was unknown) every contact is looked up first.
    """
    famly = deps.famly()
    child_id = child.child_id
    by_id = {c.hubspot_id: c for c in context["contacts"]}
    entries = sorted(plan["contacts"], key=lambda e: not e["isBillPayer"])   # bill payer first

    outcomes: list[str] = []
    warnings: list[str] = []
    all_ok = True
    creates_sent = 0

    def matches(rows, target):
        return list(dict.fromkeys(cid for cid, mail in rows if mail == target))

    for entry in entries:
        hubspot_id = entry["hubspotContactId"]
        code = "contact_error"
        try:
            contact = by_id[hubspot_id]
            target = normalise_email(contact.email)

            if child.clean_create and creates_sent == 0:
                here = []                                   # a new child: nothing to find
            else:
                here = matches(famly.child_contacts(child_id), target)   # the live check

            if len(here) == 1:
                code = PRESENT
            elif len(here) > 1:
                code = "ambiguous_on_child"
            elif not own_child:
                # A child that already existed: an unmatched contact is never added
                # to it (the plan holds on this; this is the last line).
                code = "contact_unmatched_on_existing_child"
            else:
                code, contact_warnings, sent = _create_one(
                    deps, famly, contact, child_id, target, matches)
                warnings.extend(contact_warnings)
                creates_sent += sent
        except Exception as exc:  # noqa: BLE001 - one contact must never stop the rest
            logger.error("onboard: contact %s hit %s", hubspot_id, type(exc).__name__)
            code = "contact_error"

        outcomes.append(f"{hubspot_id}:{code}")
        if code not in OK_OUTCOMES:
            all_ok = False

    return outcomes, warnings, all_ok


def _create_one(deps, famly, contact, child_id, target, matches) -> tuple[str, list[str], int]:
    """Create one contact. Returns (outcome code, warning codes, create requests sent)."""
    try:
        payload, contact_warnings = inputs.build_contact_input(contact, child_id)
    except inputs.InputIncomplete as exc:
        return exc.code, [], 0                              # held; nothing was sent

    try:
        famly_writes.create_contact(deps.public, payload)
    except famly_writes.CreateRejected as exc:
        return exc.code, contact_warnings, 1
    except famly_writes.OutcomeUnknown:
        if len(matches(famly.child_contacts(child_id), target)) != 1:
            return UNKNOWN_OUTCOME, contact_warnings, 1

    # Read-back: exactly once on the child, with the expected email.
    if len(matches(famly.child_contacts(child_id), target)) != 1:
        return "readback_failed", contact_warnings, 1
    return CREATED, contact_warnings, 1


# --------------------------------------------------------------------------- #
# Ending a job
# --------------------------------------------------------------------------- #
def _held(job, deps, reasons: list[str], write_back: bool) -> None:
    store.set_status(job.id, store.HELD, last_error_code=",".join(reasons), outcomes=reasons)
    deps.notifier.notice(notifier_mod.notice_text(job.deal_id, "held, nothing was written", reasons))
    if write_back:
        # Best effort, one property per request: a held deal is visible in HubSpot too,
        # and a HubSpot problem is only ever reported, never an error here.
        hubspot_sync.Syncer(deps.hubspot, job.deal_id).write_status(
            hubspot_sync.STATUS_NEEDS_ATTENTION, "held:" + ",".join(reasons), [])


def _end(job, deps, status: str, code: str) -> None:
    store.set_status(job.id, status, last_error_code=code)
    try:
        headline = f"{status}, {UNKNOWN_OUTCOME_ADVICE}" if code == UNKNOWN_OUTCOME else f"{status}"
        deps.notifier.notice(notifier_mod.notice_text(job.deal_id, headline, [code]))
    except Exception:  # noqa: BLE001 - a notice must never mask the outcome
        logger.warning("onboard: notice failed for job %s", job.id)
