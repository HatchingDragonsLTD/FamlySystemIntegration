"""Gate A: the Slack Approve / Reject click for an onboarding job.

The same pattern as the plan approval gate: a signed Slack interaction (verified
upstream in server.py) arrives with the button's value, and the decision is made
HERE with an ATOMIC state change, so a double click, or a click on a message that
has been superseded, does nothing the second time.

The button value is `<job_id>:<plan hash prefix>:<nonce>`. Approve only counts if
that prefix is still the hash of the plan the job is waiting on AND the nonce is
the one of the CURRENT approval request: the message the approver clicked is the
plan they saw, and an older message for the same plan (or a Slack redelivery of an
earlier click) is stale once the job has asked again. It does not commit anything itself; it
returns the job to `queued` with `approved_hash` set, and the WORKER re-runs the
plan and writes only if the hash is unchanged.

Messages carry no names.
"""

import logging

from integrations import slack

from . import config, store

logger = logging.getLogger(__name__)

HASH_PREFIX = 16

MSG_APPROVED = "✅ Approved. The worker will re-check the plan and then create the child and contacts."
MSG_REJECTED = "🚫 Rejected. Nothing was written."
MSG_HANDLED = "This approval request has already been handled."
MSG_STALE = "⚠️ This approval request is out of date (the plan changed). Use the newer message."
MSG_UNKNOWN = "⚠️ Unknown onboarding job."
MSG_RETRY_QUEUED = ("🔁 Retry queued. The worker will read the deal again and post a new approval "
                    "request if one is needed.")
MSG_RETRY_HANDLED = "This Retry has already been used. Nothing new was started."
MSG_RETRY_STALE = "⚠️ This Retry button is out of date. Use the newer message."
MSG_RETRY_BUSY = "A job for this deal is already queued or running. Nothing new was started."
MSG_RETRY_DONE = "This deal has already been onboarded. Nothing was queued."
MSG_RETRY_OFF = "Onboarding is switched off. Nothing was queued."
MSG_RETRY_NOT_ALLOWED = "This deal is not allowed for live onboarding. Nothing was queued."


_RESULT_CODES = {}


def button_value(job_id: str, plan_hash: str, nonce: str | None) -> str:
    return f"{job_id}:{plan_hash[:HASH_PREFIX]}:{nonce or ''}"


def _parse(value: str | None) -> tuple[str | None, str | None, str | None]:
    if not value or ":" not in value:
        return None, None, None
    job_id, _, rest = value.partition(":")
    prefix, _, nonce = rest.partition(":")
    return job_id or None, prefix or None, nonce or None


def handle_click(action_id: str, value: str | None, user: str | None) -> str:
    """Act on a verified Gate A click. Never raises; returns the text to show.
    One INFO line per click, whatever the outcome (ids and codes only)."""
    text = _decide(action_id, value, user)
    logger.info("ONBOARD CLICK action=%s job=%s result=%s", action_id,
                _parse(value)[0], _RESULT_CODES.get(text, "unknown"))
    return text


def _decide(action_id: str, value: str | None, user: str | None) -> str:
    job_id, prefix, nonce = _parse(value)
    job = store.get(job_id) if job_id else None
    if job is None:
        return MSG_UNKNOWN

    if action_id == slack.ACTION_ONBOARD_RETRY:
        return _retry(job, nonce, user)

    if action_id == slack.ACTION_ONBOARD_REJECT:
        if (job.status == store.AWAITING_APPROVAL
                and (not job.approval_nonce or nonce != job.approval_nonce)):
            return MSG_STALE
        if store.transition(job.id, store.AWAITING_APPROVAL, store.HELD,
                            last_error_code="approval_rejected", approval_nonce=None):
            logger.info("ONBOARD REJECTED job=%s deal=%s user=%s -- nothing written",
                        job.id, job.deal_id, user)
            return MSG_REJECTED
        return MSG_HANDLED

    if action_id == slack.ACTION_ONBOARD_APPROVE:
        if job.status != store.AWAITING_APPROVAL:
            return MSG_HANDLED
        if not job.plan_hash or not prefix or not job.plan_hash.startswith(prefix):
            return MSG_STALE
        if not job.approval_nonce or nonce != job.approval_nonce:
            return MSG_STALE
        if store.transition(job.id, store.AWAITING_APPROVAL, store.QUEUED,
                            approved_hash=job.plan_hash, approval_nonce=None):
            logger.info("ONBOARD APPROVED job=%s deal=%s user=%s", job.id, job.deal_id, user)
            return MSG_APPROVED
        return MSG_HANDLED

    return MSG_UNKNOWN


def _retry(job, nonce, user) -> str:
    """The Retry button on a hold notice: the same enqueue as `onboard-enqueue` (same mode
    and allowlist checks, a fresh job), nothing skipped: the new job plans again and posts
    its own Gate A. Only the CURRENT notice's nonce counts, and it is spent atomically, so
    a second press does nothing."""
    if job.status != store.HELD:
        return MSG_RETRY_HANDLED
    if not job.approval_nonce:
        return MSG_RETRY_HANDLED                    # already spent by an earlier press
    if nonce != job.approval_nonce:
        return MSG_RETRY_STALE
    mode = config.mode()
    if mode == config.MODE_OFF:
        logger.info("ONBOARD RETRY job=%s deal=%s result=mode_off", job.id, job.deal_id)
        return MSG_RETRY_OFF
    if mode == config.MODE_LIVE and not config.allowed_for_job(job, mode):
        logger.info("ONBOARD RETRY job=%s deal=%s result=not_allowlisted", job.id, job.deal_id)
        return MSG_RETRY_NOT_ALLOWED
    if not store.consume_hold_nonce(job.id, nonce):
        return MSG_RETRY_HANDLED
    # The held job's own source: a webhook job keeps its Deposit Pending check.
    result = store.enqueue(job.deal_id, mode, source=job.source, file_hash=job.file_hash)
    logger.info("ONBOARD RETRY job=%s deal=%s result=%s new_job=%s user=%s",
                job.id, job.deal_id, result.outcome, result.job.id, user)
    return {store.CREATED: MSG_RETRY_QUEUED, store.ALREADY_QUEUED: MSG_RETRY_BUSY,
            store.ALREADY_DONE: MSG_RETRY_DONE}[result.outcome]


_RESULT_CODES.update({
    MSG_RETRY_QUEUED: "retry_queued", MSG_RETRY_HANDLED: "retry_already_used", MSG_RETRY_STALE: "retry_stale",
    MSG_RETRY_BUSY: "retry_job_active", MSG_RETRY_DONE: "retry_already_done",
    MSG_RETRY_OFF: "retry_mode_off", MSG_RETRY_NOT_ALLOWED: "retry_not_allowlisted",
    MSG_APPROVED: "approved", MSG_REJECTED: "rejected", MSG_HANDLED: "already_handled",
    MSG_STALE: "stale", MSG_UNKNOWN: "unknown",
})
