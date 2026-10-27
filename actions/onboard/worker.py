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
    messages,
    notifier as notifier_mod,
    plan_status,
    phone as phone_mod,
    store,
    summary_check,
)
from actions.onboard import bill_payer_writes
from actions.onboard_dry_run import bill_payers as bill_payer_scan
from actions.onboard_dry_run import derive
from actions.onboard_dry_run import urn as urn_fmt
from actions.onboard_dry_run.famly_reads import FamlyReads, normalise_email
from actions.onboard_dry_run.runner import DryRun, hashed_plan, plan_hash
from core import hubspot
from core.hubspot import HubSpotError

logger = logging.getLogger(__name__)

POLL_SECONDS = 5

# Contact outcome codes.
CREATED, PRESENT = "created", "present"
OK_OUTCOMES = (CREATED, PRESENT)

# A create whose outcome is unknown and whose follow-up lookup finds nothing.
UNKNOWN_OUTCOME = "create_outcome_unknown"

# Famly's search (listByExternalSystemReference) lags a create. A read-back or settle
# lookup that finds nothing (or not the expected child) is read again after each of
# these waits (seconds): 5 attempts in all, about 15 s worst case. Reads only.
LOOKUP_RETRY_WAITS = (1, 2, 4, 8)

# A contact create whose outcome was never confirmed: a later job must not create the
# parent again while the child's contact list does not show it (the list can lag).
CONTACT_UNCONFIRMED = "contact_create_unconfirmed"
UNRESOLVED_CONTACT_OUTCOMES = ("readback_failed", UNKNOWN_OUTCOME, CONTACT_UNCONFIRMED)
UNRESOLVED_PAYER_OUTCOMES = (bill_payer_scan.READBACK_MISMATCH, bill_payer_scan.OUTCOME_UNKNOWN,
                             bill_payer_scan.CREATE_UNCONFIRMED)

# The plan changed AGAIN after one re-ask: stop instead of asking a third time.
PLAN_UNSTABLE = "plan_unstable"


class StopJob(Exception):
    """End the job in `status` with `code`. Nothing further is written."""

    def __init__(self, status: str, code: str, info: dict | None = None,
                 detail: list[str] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.info = info or {}
        self.detail = detail or []      # extra codes for the Slack notice


@dataclass
class Deps:
    hubspot: Any                 # HubSpotDealWriter: the reads, plus the one writer
    public: Any                  # public-API GraphQL client (reads and the two creates)
    rest: Any                    # internal REST client (the child summary)
    notifier: Any                # .gate(text, job_id, plan_hash, nonce) -> ref | None; .notice(text)
    famly_factory: Callable[[], FamlyReads] | None = None
    sleep: Callable[[float], None] = time.sleep       # the bill payer stage's 10 s settle wait

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
        _end(job, deps, stop.status, stop.code, stop.detail)
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
    if run_mode == config.MODE_LIVE and not config.allowed_for_job(job, run_mode):
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
        deal_id, context, created_child_ids=store.created_child_ids_for_deal(deal_id),
        # A live job RESERVES the deal's URN here (plan time), so the number approved in
        # Slack is the number written; a dry job only shows the next number, reserving nothing.
        urn={"allocate": run_mode == config.MODE_LIVE, "skip_returning": job.new_urn, "job_id": job.id},
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
    if job.approved_hash is not None:
        # The re-check after an approval: the worker re-ran the plan and compares.
        logger.info("onboard: approval re-check job=%s unchanged=%s digest=%s approved=%s",
                    job.id, job.approved_hash == digest, digest, job.approved_hash)
    if famly_writes_planned(plan) and job.approved_hash != digest:
        if job.approved_hash is not None and job.reask_count >= 1:
            # Approved, re-checked, DIFFERENT, asked once already, and different again.
            differing = _differing_sections(job.plan_sections, plan_section_hashes(plan))
            logger.warning("onboard: plan unstable job=%s sections=%s", job.id, differing)
            raise StopJob(store.NEEDS_REVIEW, PLAN_UNSTABLE, detail=differing)
        _ask_for_approval(job, deps, plan, digest, run_mode, plan_changed=job.approved_hash is not None)
        return {}

    return _write_phase(job, deps, plan, context, digest)


def _this_jobs_child(job) -> str | None:
    """The child THIS job created, if it has (for the Slack wording only)."""
    return job.child_id if job.child_create_sent else None


def famly_writes_planned(plan: dict) -> bool:
    """Whether the plan would write to Famly at all: a child create and/or any
    contact create. Such a plan needs a Slack approval (Gate A) in live mode."""
    return sum(plan_status.planned_writes(plan).values()) > 0


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
    on every ask so a plan that changes between runs names the section that did.
    Over the HASHED part of the plan, so it agrees with the digest."""
    plan = hashed_plan(plan)
    return {
        key: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:8]
        for key, value in sorted(plan.items())
    }


def _differing_sections(before: dict | None, after: dict) -> list[str]:
    """`section:old>new` for each plan section whose short hash differs (names and
    short hashes only)."""
    before = before or {}
    return [
        f"{name}:{before.get(name, '-')}>{after.get(name, '-')}"
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    ]


def _ask_for_approval(job, deps, plan, digest, run_mode, plan_changed: bool) -> None:
    # A fresh nonce per ask: only THIS message's buttons can approve it.
    nonce = secrets.token_hex(8)
    # Compare-and-set: only the run that still holds the claim may ask. A run that
    # lagged (the job was approved, re-claimed or finished meanwhile) does nothing.
    if not store.begin_approval_wait(
            job.id, job.claimed_at, digest, nonce,
            sections=plan_section_hashes(plan), reask=plan_changed):
        logger.warning("onboard: approval ask skipped job=%s: the job moved on or was re-claimed",
                       job.id)
        return
    logger.info("onboard: approval asked job=%s plan_changed=%s digest=%s sections=%s",
                job.id, plan_changed, digest, plan_section_hashes(plan))
    text = notifier_mod.gate_text(
        plan, digest, run_mode, plan_changed=plan_changed,
        this_job_child_id=_this_jobs_child(job))
    ref = deps.notifier.gate(text, job.id, digest, nonce)
    if ref is None:
        # Nobody can approve a message that never arrived: do not wait for it.
        store.transition(job.id, store.AWAITING_APPROVAL, store.HELD,
                         last_error_code="approval_request_not_posted", approval_nonce=None)
        return
    store.set_approval_ref(job.id, nonce, ref)


def _write_phase(job, deps, plan, context, digest) -> dict:
    deal_id = job.deal_id
    # Re-checked at the point of writing, in case the environment changed.
    if config.mode() != config.MODE_LIVE or not config.allowed_for_job(job, config.mode()):
        raise StopJob(store.HELD, "deal_not_allowlisted")

    # PRE-FLIGHT. HubSpot never blocks Famly: a missing property, or a value a
    # dropdown does not offer, is reported (log and Slack) and that property's write
    # is skipped. Nothing here can stop the Famly work below.
    pre = hubspot_sync.preflight(deps.hubspot)
    logger.info("onboard: famly_ deal properties found: %s", pre.found_famly)
    info = {"hubspotSyncPropertiesFound": pre.found_famly}
    if pre.warnings:
        logger.warning("onboard: HubSpot property pre-flight: %s", pre.warnings)
        _admin_notice(deps, deal_id, pre.warnings)
    syncer = hubspot_sync.Syncer(deps.hubspot, deal_id, pre)

    deal = context["deal"]
    # The allocated URN goes onto the deal in HubSpot BEFORE any Famly write; if that
    # fails nothing is sent to Famly and a retry reuses the same URN.
    urn_written = _write_allocated_urn(job, deps, plan, deal)

    store.set_status(job.id, store.CREATING_CHILD, plan_hash=digest)
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
        if urn_written:
            flags += _urn_readback_flags(deps, urn_written, child.child_id)

    _write_child_back(deps, syncer, deal, deal_id, child)

    if plan.get("scope") == derive.SCOPE_CHILD_ONLY:
        return _finish_child_only(job, deps, plan, syncer, child, flags, info, urn_written)

    store.set_status(job.id, store.CREATING_CONTACTS)
    outcomes, contact_warnings, all_ok = _create_contacts(job, deps, plan, context, child, own_child)
    store.update(job.id, outcomes=outcomes)
    # RELATION = Parent on a present contact that has none; flags only, never a stop.
    relation_entries, relation_flags, relations_added = _add_relations(job, deps, plan, context, child)
    outcomes_stored = outcomes + relation_entries
    if relation_entries:
        store.update(job.id, outcomes=outcomes_stored)

    # THE BILL PAYER STAGE (ONBOARD_BILL_PAYER=on): after the child and contacts, never
    # blocking either, never rolling either back. A flag ends the job needs_review; a
    # difference in an existing payer's details (bill_payer_differs) is a "To check" line only.
    bill_payer = None
    if config.bill_payer_enabled() and plan.get("billPayer"):
        bill_payer = _bill_payer_stage(job, deps, plan, context, child)
        store.update(job.id, outcomes=outcomes_stored + [f"billpayer:{bill_payer.code}"])
    bill_payer_failed = bill_payer is not None and bill_payer.code not in bill_payer_scan.SUCCESS_CODES
    bill_payer_codes = [] if bill_payer is None else [bill_payer.code, *bill_payer.extra]
    bill_payer_footer = [f"differs:{','.join(bill_payer.differs)}"] if bill_payer and bill_payer.differs else []

    bad_codes = sorted({o.split(":", 1)[1] for o in outcomes if o.split(":", 1)[1] not in OK_OUTCOMES})
    if bill_payer_failed:
        bad_codes.append(bill_payer.code)
    gaps = list(plan["warnings"]) + contact_warnings + flags + relation_flags
    if bill_payer is not None and bill_payer.differs:
        gaps.append(bill_payer_scan.DIFFERS)
    contact_codes = [o.split(":", 1)[1] for o in outcomes]
    summary = _summary_line(child.created, contact_codes, bill_payer, relations_added)
    if all_ok and not bill_payer_failed:
        # `bill_payer_linked` only if HubSpot's dropdown already offers it.
        status = hubspot_sync.STATUS_CONTACTS_LINKED
        if (bill_payer is not None and bill_payer.code in bill_payer_scan.SUCCESS_CODES
                and syncer.offered(hubspot.SYNC_STATUS_PROPERTY, hubspot_sync.STATUS_BILL_PAYER_LINKED)):
            status = hubspot_sync.STATUS_BILL_PAYER_LINKED
        syncer.write_status(status, hubspot_sync.note_done(summary), gaps)
    else:
        syncer.write_status(hubspot_sync.STATUS_NEEDS_ATTENTION,
                            hubspot_sync.note_needs_attention(summary, bad_codes), gaps)

    # The job ends needs_review if any contact failed, or if the child id or a
    # defaulted end date could not be written to the deal. The Famly work is done
    # either way and nothing is retried.
    failures = []
    if not all_ok:
        failures.append("contact_outcomes")
    if bill_payer_failed:
        failures.append(bill_payer.code)
    if syncer.critical_failed:
        failures.append("hubspot_writeback_failed")
    codes = (bad_codes + [f for f in failures if f != "contact_outcomes"] + flags + relation_flags
             + (["relation_added"] if relations_added else []) + syncer.reports + list(plan["warnings"]) + contact_warnings + bill_payer_codes
             + (["urn_allocated"] if urn_written else [])
             # Informational footer code: a parent was added to a child that already existed.
             + (["contact_unmatched_on_existing_child"] if not own_child and CREATED in contact_codes else [])
             + ["created" if child.created else "child_existed"])
    if failures:
        store.set_status(job.id, store.NEEDS_REVIEW, last_error_code=",".join(failures))
        deps.notifier.notice(notifier_mod.notice_text(
            deal_id, f"⚠️ {summary} The job needs a person.", codes, bill_payer_footer))
    else:
        store.set_status(job.id, store.DONE, last_error_code=None)
        deps.notifier.notice(notifier_mod.notice_text(deal_id, f"✅ {summary}", codes, bill_payer_footer))
    _admin_notice(deps, deal_id, codes)
    return info


def _write_allocated_urn(job, deps, plan, deal: dict) -> str | None:
    """If the plan carries a URN allocated for this deal and the deal has none, write it to the
    HubSpot deal and CONFIRM by re-reading the deal. Returns the URN written, or None when there
    was nothing to write. Any failure ends the job needs_review (`urn_hubspot_write_failed`)
    before a single Famly request; the allocation stays with the deal, so a retry reuses it."""
    section = plan.get("urn") or {}
    if section.get("status") != urn_fmt.ALLOCATED:
        return None
    urn = section["urn"]
    try:
        live = deps.hubspot.get_deal(job.deal_id)
        current = str(live.get("child_urn") or "").strip()
        if current and current.upper() != urn:
            raise HubSpotError("the deal has a different child_urn now")
        if current.upper() != urn:
            deps.hubspot.write_deal_properties(job.deal_id, {hubspot.CHILD_URN_PROPERTY: urn})
            confirmed = str(deps.hubspot.get_deal(job.deal_id).get("child_urn") or "").strip()
            if confirmed.upper() != urn:
                raise HubSpotError("the child_urn did not read back")
    except (HubSpotError, ValueError) as exc:
        logger.error("onboard: urn write to HubSpot failed job=%s (%s)", job.id, type(exc).__name__)
        raise StopJob(store.NEEDS_REVIEW, "urn_hubspot_write_failed") from None
    store.mark_urn_written(job.deal_id)
    deal["child_urn"] = urn                       # the child is created with it as externalId
    return urn


def _urn_readback_flags(deps, urn: str, child_id: str) -> list[str]:
    """A flag, never a stop: the new child should be the one child carrying the URN."""
    try:
        if deps.famly().children_by_urn(urn) == [child_id]:
            return []
    except Exception:  # noqa: BLE001 - a flag check must never stop a job
        pass
    return ["urn_readback_mismatch"]


def _finish_child_only(job, deps, plan, syncer, child: "ChildResult", flags, info,
                       urn_written: str | None = None) -> dict:
    """A deal in a child-only stage ends here: the child is in Famly, and no contact is
    read or created and no bill payer is scanned or written. The status is `child_linked`
    (never contacts_linked / bill_payer_linked)."""
    stage = (plan.get("stage") or {}).get("name")
    summary = messages.child_only_summary(child.created, stage)
    gaps = list(plan["warnings"]) + flags
    syncer.write_status(hubspot_sync.STATUS_CHILD_LINKED, hubspot_sync.note_done(summary), gaps)
    codes = (flags + syncer.reports + list(plan["warnings"])
             + (["hubspot_writeback_failed"] if syncer.critical_failed else [])
             + (["urn_allocated"] if urn_written else [])
             + ["child_only_stage", "created" if child.created else "child_existed"])
    if syncer.critical_failed:
        store.set_status(job.id, store.NEEDS_REVIEW, last_error_code="hubspot_writeback_failed")
        deps.notifier.notice(notifier_mod.notice_text(
            job.deal_id, f"⚠️ {summary} The job needs a person.", codes))
    else:
        store.set_status(job.id, store.DONE, last_error_code=None)
        deps.notifier.notice(notifier_mod.notice_text(job.deal_id, f"✅ {summary}", codes))
    _admin_notice(deps, job.deal_id, codes)
    return info


def _bill_payer_stage(job, deps, plan, context, child: "ChildResult"):
    """Run the bill payer stage. It never raises: an unexpected error after a request may
    have gone out is an UNKNOWN outcome (type only is logged)."""
    try:
        site_id = plan["siteId"]
        result = bill_payer_writes.run_stage(
            public=deps.public, scan=lambda: deps.famly().bill_payers(site_id),
            contacts=context["contacts"], approved=plan["billPayer"], site_id=site_id,
            child_id=child.child_id, sleep=deps.sleep,
            unresolved=store.last_outcome_for(job.deal_id, child.child_id, "billpayer", job.id)
            in UNRESOLVED_PAYER_OUTCOMES,
            lookup=lambda fn: _lookup_with_settle(job, deps, fn, label="bill_payer_readback"))
    except Exception as exc:  # noqa: BLE001 - one stage must never stop the job's end
        logger.error("onboard: bill payer stage hit %s", type(exc).__name__)
        result = bill_payer_writes.StageResult(bill_payer_scan.OUTCOME_UNKNOWN)
    if "phone_unnormalised" in result.extra:
        payer = next((c for c in context["contacts"] if c.is_bill_payer), None)
        if payer is not None:
            _log_phone_reason(job, "bill_payer", payer.props)
    if result.bill_payer_id:
        store.update(job.id, bill_payer_id=result.bill_payer_id)      # the id only
    logger.info("onboard: bill payer stage job=%s code=%s sent=%s id=%s", job.id, result.code,
                result.sent, result.bill_payer_id)
    return result


def _summary_line(child_created: bool, contact_codes: list[str], bill_payer=None,
                  relations_added: int = 0) -> str:
    """One plain sentence: what was done. Counts only."""
    created = contact_codes.count(CREATED)
    present = contact_codes.count(PRESENT)
    missed = len(contact_codes) - created - present
    line = ("Child created in Famly. " if child_created else "Child was already in Famly. ")
    line += f"{created} contact(s) created, {present} already present"
    line += f", {missed} not added." if missed else "."
    if relations_added:
        line += " " + messages.relation_summary(relations_added)
    if bill_payer is not None:
        line += {bill_payer_scan.CREATED: " Bill payer created.",
                 bill_payer_scan.ATTACHED: " Child linked to the existing bill payer.",
                 bill_payer_scan.PRESENT: " Child already linked to the bill payer."}.get(
            bill_payer.code, "")
    return line


def _admin_notice(deps, deal_id: str, codes) -> None:
    """The ONE separate admin line for configuration problems (never the staff
    list), also logged."""
    admin = notifier_mod.admin_notice_text(deal_id, codes)
    if admin is None:
        return
    logger.warning("onboard: configuration problem for deal %s: %s", deal_id,
                   messages.codes_of_kind(codes, messages.ADMIN))
    deps.notifier.notice(admin)


# --------------------------------------------------------------------------- #
# Child
# --------------------------------------------------------------------------- #
def _clean(value) -> str:
    return " ".join(str(value or "").split())


VIA_DEAL = "external_system_reference"
VIA_URN = "urn"
VIA_RECORDED = "famly_childid"


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


def _lookup_with_settle(job, deps, fn: Callable[[], list],
                        expect: str | None = None, label: str = "lookup") -> tuple[list, bool]:
    """Read `fn()` (a lookup; never a mutation) until it shows what is expected.

    Returns (ids from the last successful read, readable). Retries only on an empty
    result, a result without `expect` (or, with no `expect`, no ids at all) and a read
    that raised; 2+ ids is not lag and returns at once. `readable` is False when the
    LAST attempt raised: an error is "could not read", never "not found"."""
    attempts = len(LOOKUP_RETRY_WAITS) + 1
    ids: list = []
    readable = False
    for attempt in range(1, attempts + 1):
        try:
            ids = list(fn())
            readable = True
        except Exception as exc:  # noqa: BLE001 - HTTP/GraphQL errors; the text may carry a body
            logger.warning("onboard: lookup failed job=%s attempt=%d error=%s",
                           job.id, attempt, type(exc).__name__)
            readable = False
        if readable:
            if len(ids) > 1:
                return ids, True
            if (expect in ids) if expect else bool(ids):
                if attempt > 1:
                    # How long the lag lasts: ids and counts only.
                    logger.info("onboard: lookup settled after retries job=%s deal=%s lookup=%s "
                                "attempts=%d count=%d", job.id, job.deal_id, label, attempt, len(ids))
                return ids, True
        if attempt < attempts:
            deps.sleep(LOOKUP_RETRY_WAITS[attempt - 1])
    logger.warning("onboard: lookup did not settle job=%s deal=%s lookup=%s attempts=%d "
                   "readable=%s count=%d ids=%s",
                   job.id, job.deal_id, label, attempts, readable, len(ids) if readable else 0,
                   ",".join(str(i) for i in ids) if readable else "")
    return (ids if readable else []), readable


def _ensure_child(job, deps, plan, deal) -> ChildResult:
    deal_id = job.deal_id

    # The live lookup immediately before the write. A child that now exists wins.
    ids, via = _lookup_child(deps, deal, deal_id, urn_fallback=True)
    if len(ids) > 1:
        raise StopJob(store.NEEDS_REVIEW, "child_ambiguous")
    if ids:
        return ChildResult(ids[0], created=False, clean_create=False, via=via)

    # The plan also treats the deal's own recorded `famly_childid` as the child when no reference
    # or URN finds one (an existing child by that route). Without this the worker would go on to
    # CREATE it, and fail on a missing name that only a create needs.
    recorded = _clean(deal.get("famly_childid"))
    if recorded and deps.famly().child_exists(recorded):
        return ChildResult(recorded, created=False, clean_create=False, via=VIA_RECORDED)

    is_test = config.is_test_deal(deal_id)
    try:
        payload = inputs.build_child_input(deal, deal_id, plan, is_test)
    except inputs.InputIncomplete as exc:
        raise StopJob(store.FAILED, exc.code) from None

    # Persisted BEFORE the request: this job never sends a second create.
    if not store.mark_child_create_sent(job.id):
        raise StopJob(store.NEEDS_REVIEW, "duplicate_create_blocked")

    clean = True
    rejected_code = None
    try:
        child_id = famly_writes.create_child(deps.public, payload)
    except (famly_writes.CreateRejected, famly_writes.OutcomeUnknown) as exc:
        # An error status or `errors` does not mean it did not run. Did it land? Look
        # (by the deal-id reference); never send again. Only if the lookup finds
        # nothing is a rejection reported.
        clean = False
        rejected_code = exc.code if isinstance(exc, famly_writes.CreateRejected) else None
        landed, readable = _lookup_with_settle(
            job, deps, lambda: _lookup_child(deps, deal, deal_id, urn_fallback=False)[0],
            label="settle")
        if len(landed) > 1:
            raise StopJob(store.NEEDS_REVIEW, "child_ambiguous") from None
        if not landed:
            if rejected_code and readable:
                raise StopJob(store.FAILED, rejected_code) from None
            # The request may have landed and the lookup is blind to it: a person looks.
            raise StopJob(store.NEEDS_REVIEW, UNKNOWN_OUTCOME) from None
        child_id = landed[0]

    # Recorded AT ONCE, before the read-back: this is the job history that tells a
    # later job the child is the pipeline's.
    store.update(job.id, child_id=child_id)

    # THE ONLY STOPPING READ-BACK: exactly one child by the deal-id reference, and it
    # is this one. (The summary comparison below is a flag, never a stop.)
    # Famly's search lags a create, so an empty answer is read again (bounded).
    seen, readable = _lookup_with_settle(
        job, deps, lambda: deps.famly().children_by_deal_id(deal_id), expect=child_id,
        label="readback")
    # The child id is known and recorded, so an unreadable lookup is also "created but
    # could not be found again" (not an unknown create outcome).
    if not readable or seen != [child_id]:
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

    present_contacts: dict[str, str] = context.setdefault("present_contacts", {})
    # The APPROVED plan is about one child. If the live child is not that child (the plan would have
    # created one but it exists now, or a different child was found), nothing is added to it.
    planned_child = plan["child"]
    child_changed = (
        (planned_child["status"] == "child_would_create" and not child.created)
        or (planned_child["status"] == "child_found"
            and planned_child.get("famlyChildId") != child.child_id))

    for slot, entry in enumerate(entries):
        hubspot_id = entry["hubspotContactId"]
        code = "contact_error"
        try:
            contact = by_id[hubspot_id]
            target = normalise_email(contact.email)

            if child_changed:
                outcomes.append(f"{hubspot_id}:plan_changed")
                all_ok = False
                continue
            if child.clean_create and creates_sent == 0:
                here = []                                   # a new child: nothing to find
            else:
                # The live check: the HubSpot reference on this child first, then email.
                here = famly.locate_contact(child_id, hubspot_id, target).ids

            planned = entry.get("status")             # what the APPROVED plan said about this contact
            if len(here) == 1 and planned == "would_create":
                code = "plan_changed"                 # a matching contact appeared since approval
            elif len(here) == 1:
                code = PRESENT
                present_contacts[hubspot_id] = here[0]
            elif len(here) > 1:
                code = "ambiguous_on_child"
            elif planned == "present":
                code = "plan_changed"                 # the contact the plan found is gone
            elif store.last_outcome_for(job.deal_id, child_id, hubspot_id, job.id)                     in UNRESOLVED_CONTACT_OUTCOMES:
                # An earlier job sent this parent's create and never confirmed it, and the
                # live list does not show it: do not create it a second time.
                code = CONTACT_UNCONFIRMED
            else:
                code, contact_warnings, sent = _create_one(
                    job, deps, famly, contact, child_id, target, slot)
                warnings.extend(contact_warnings)
                creates_sent += sent
        except Exception as exc:  # noqa: BLE001 - one contact must never stop the rest
            logger.error("onboard: contact %s hit %s", hubspot_id, type(exc).__name__)
            code = "contact_error"

        outcomes.append(f"{hubspot_id}:{code}")
        if code not in OK_OUTCOMES:
            all_ok = False

    return outcomes, warnings, all_ok


def _add_relations(job, deps, plan, context, child: ChildResult) -> tuple[list[str], list[str], int]:
    """RELATION = Parent on each contact that is PRESENT on the child and has no RELATION record.

    Only for a contact the approved plan marked `would_add` AND that a fresh read still shows
    without a RELATION record: an existing value of any kind is never overwritten. One update
    per contact through the single-field `records` argument (it merges), sent once, never
    retried. A request that did not answer cleanly is settled by reading the contact's records
    (contacts.list on the child). Any failure is a FLAG for a person, never a stop.

    Returns (stored outcome entries `relation:<hubspot id>:<code>`, flag codes, relations added)."""
    present = context.get("present_contacts") or {}
    planned = {e["hubspotContactId"] for e in plan["contacts"] if e.get("relation") == "would_add"}
    wanted = {hid: fid for hid, fid in present.items() if hid in planned}
    if not wanted:
        return [], [], 0
    famly = deps.famly()
    try:
        before = famly.child_contact_relations(child.child_id)
    except Exception as exc:  # noqa: BLE001 - a flag check must never stop a job
        logger.warning("onboard: relation read failed job=%s error=%s", job.id, type(exc).__name__)
        return [], ["relation_unreadable"], 0

    entries: list[str] = []
    flags: list[str] = []
    added = 0
    for hid, fid in wanted.items():
        has = before.get(fid)
        if has is None:
            entries.append(f"relation:{hid}:relation_unreadable")
            flags.append("relation_unreadable")
            continue
        if has:
            continue                                   # someone set it since the plan: leave it
        rejected = False
        try:
            famly_writes.add_contact_relation(deps.public, fid, "Parent")
        except famly_writes.CreateRejected:
            rejected = True
        except Exception as exc:  # noqa: BLE001 - an unknown outcome is settled by a read below
            logger.warning("onboard: relation update outcome unknown job=%s error=%s",
                           job.id, type(exc).__name__)
        seen, readable = _lookup_with_settle(
            job, deps,
            lambda fid=fid: [fid] if famly.child_contact_relations(child.child_id).get(fid) else [],
            expect=fid, label="relation_readback")
        if fid in seen:
            code = "relation_added"
            added += 1
        elif rejected and readable:
            code = "relation_not_added"
        else:
            code = "relation_unconfirmed"
        entries.append(f"relation:{hid}:{code}")
        if code != "relation_added":
            flags.append(code)
    return entries, flags, added


def _log_phone_reason(job, slot, props) -> None:
    """Why a phone was not normalised: the fixed reason code, the parent SLOT (an index,
    or `bill_payer`) and the HubSpot property name. Never the number or any part of it."""
    _source, _type, _number, reason, prop = phone_mod.pick_phone_detail(props or {})
    logger.warning("onboard: phone not normalised job=%s deal=%s slot=%s property=%s reason=%s",
                   job.id, job.deal_id, slot, prop, reason)


def _create_one(job, deps, famly, contact, child_id, target, slot) -> tuple[str, list[str], int]:
    """Create one contact. Returns (outcome code, warning codes, create requests sent)."""
    try:
        payload, contact_warnings = inputs.build_contact_input(contact, child_id)
    except inputs.InputIncomplete as exc:
        return exc.code, [], 0                              # held; nothing was sent
    if "phone_unnormalised" in contact_warnings:
        _log_phone_reason(job, slot, contact.props)

    try:
        famly_writes.create_contact(deps.public, payload)
    except (famly_writes.CreateRejected, famly_writes.OutcomeUnknown) as exc:
        # An error status or `errors` does not mean it did not run: look with the
        # existing present-check. Found exactly once = it was created; nothing found =
        # only now is a rejection reported. Never sent again.
        # Reads only (the reference lookup, then the child's contacts by email), with the
        # search-lag retries; an unreadable lookup is never "found 0".
        rows, readable = _lookup_with_settle(
            job, deps, lambda: famly.locate_contact(child_id, contact.hubspot_id, target).ids,
            label="contact_settle")
        found = len(rows)
        if found != 1:
            if found == 0 and readable and isinstance(exc, famly_writes.CreateRejected):
                return exc.code, contact_warnings, 1
            return UNKNOWN_OUTCOME, contact_warnings, 1

    # Read-back: exactly once on the child, with the expected email.
    rows, _readable = _lookup_with_settle(
        job, deps, lambda: famly.locate_contact(child_id, contact.hubspot_id, target).ids,
        label="contact_readback")
    if len(rows) != 1:
        return "readback_failed", contact_warnings, 1
    return CREATED, contact_warnings, 1


# --------------------------------------------------------------------------- #
# Ending a job
# --------------------------------------------------------------------------- #
# A hold the person cannot fix in HubSpot (configuration): no Retry button.
NO_RETRY_HOLDS = ("deal_not_allowlisted",)


def _held(job, deps, reasons: list[str], write_back: bool) -> None:
    retry = hasattr(deps.notifier, "hold_notice") and not set(reasons) & set(NO_RETRY_HOLDS)
    nonce = secrets.token_hex(8) if retry else None      # the current notice's Retry nonce
    store.set_status(job.id, store.HELD, last_error_code=",".join(reasons), outcomes=reasons,
                     approval_nonce=nonce)
    headline = "Onboarding did not start. Nothing was created in Famly."
    if retry:
        headline += " Fix the deal in HubSpot first, then press Retry."
    text = notifier_mod.notice_text(job.deal_id, headline, reasons)
    if retry:
        deps.notifier.hold_notice(text, job.id, nonce)
    else:
        deps.notifier.notice(text)
    if write_back:
        # Best effort, one property per request: a held deal is visible in HubSpot too,
        # and a HubSpot problem is only ever reported, never an error here.
        hubspot_sync.Syncer(deps.hubspot, job.deal_id).write_status(
            hubspot_sync.STATUS_NEEDS_ATTENTION, hubspot_sync.note_not_started(reasons), [])


def _end(job, deps, status: str, code: str, detail: list[str] | None = None) -> None:
    store.set_status(job.id, status, last_error_code=code)
    try:
        headline = ("Onboarding needs a person." if status == store.NEEDS_REVIEW
                    else "Onboarding stopped.")
        # `detail` (the plan sections that differed, as short hashes) is for the
        # footer only: it is not a code with a sentence.
        deps.notifier.notice(notifier_mod.notice_text(
            job.deal_id, headline, [code], extra_footer=detail or []))
    except Exception:  # noqa: BLE001 - a notice must never mask the outcome
        logger.warning("onboard: notice failed for job %s", job.id)
