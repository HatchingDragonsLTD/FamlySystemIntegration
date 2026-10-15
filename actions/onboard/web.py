"""Web handler for the `onboard_enqueue` action: the HTTP trigger for onboarding.

Dispatched by the existing intake route (server.py `POST /intake`, header
`X-Intake-Secret`) exactly like every other action; the router authenticates,
reads `action`, and wraps what this returns in the standard envelope. Nothing
about auth or routing is duplicated here.

Request body (what the HubSpot webhook action sends):

    {"action": "onboard_enqueue", "dealId": <the deal record id>}

`dealId` may be a JSON number or a string; it must be DIGITS ONLY (ASCII 0-9),
or the request is a 400. Nothing else in the body is read.

THIS DOES NO WORK. It calls the same `store.enqueue` the CLI uses and returns the
job id at once: no Famly call, no HubSpot call. The worker does everything later.

STATUS CODES. HubSpot retries on a 5xx, so a 5xx must mean a real fault and never
a deliberate refusal:

    200  queued | already_queued | already_done           (data.jobId set)
    200  refused_mode_off          ONBOARD_MODE is off         (no job)
    200  refused_not_allowlisted   live mode, deal not in ONBOARD_ALLOWED_DEAL_IDS
                                    (no job; a Slack notice with the deal id only)
    400  dealId missing or not digits only
    500  a genuine internal error (the database failed, ...)

A job enqueued here has source "webhook": the worker's first read confirms the
deal is in the Operations pipeline at Deposit Pending, else it HOLDs
`stage_not_eligible` with no writes (a CLI job has no such restriction).

NO PERSONAL DATA is read, logged or returned: a deal id, an outcome code, a job
id. The secret and the request headers are never logged here (and the router does
not log them either). An unexpected exception is logged by TYPE only and its text
is not returned.
"""

import logging
import re

from . import config, notifier, store

logger = logging.getLogger(__name__)

ACTION_NAME = "onboard_enqueue"
SOURCE = "webhook"

OUTCOME_QUEUED = "queued"
OUTCOME_ALREADY_QUEUED = "already_queued"
OUTCOME_ALREADY_DONE = "already_done"
OUTCOME_MODE_OFF = "refused_mode_off"
OUTCOME_NOT_ALLOWLISTED = "refused_not_allowlisted"

_DIGITS = re.compile(r"[0-9]{1,20}")


def parse_deal_id(value) -> str | None:
    """The deal id as a digits-only string, or None.

    A JSON number (an int, not a float, not a bool) or a string of ASCII digits.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value) if value >= 0 else ""
    elif isinstance(value, str):
        text = value
    else:
        return None
    return text if _DIGITS.fullmatch(text) else None


def _notify(text: str) -> None:
    """Post a Slack notice. Never raises (post_notice never does); a seam so
    tests can capture it."""
    notifier.SlackNotifier().notice(text)


def _data(outcome: str, job: store.Job | None = None) -> dict:
    return {
        "outcome": outcome,
        "jobId": job.id if job else None,
        "status": job.status if job else None,
        "warnings": [],
        "errors": [],
    }


def handle(payload: dict) -> tuple[dict, int]:
    deal_id = parse_deal_id(payload.get("dealId") if isinstance(payload, dict) else None)
    if deal_id is None:
        # The value is not echoed: it could be anything.
        data = _data("bad_request")
        data["errors"] = ["dealId must be a deal id: digits only"]
        logger.info("onboard webhook: outcome=bad_request")
        return data, 400

    try:
        mode = config.mode()
        if mode == config.MODE_OFF:
            logger.info("onboard webhook: outcome=%s deal=%s", OUTCOME_MODE_OFF, deal_id)
            return _data(OUTCOME_MODE_OFF), 200

        if mode == config.MODE_LIVE and not config.is_allowed(deal_id):
            logger.info("onboard webhook: outcome=%s deal=%s", OUTCOME_NOT_ALLOWLISTED, deal_id)
            _notify(notifier.notice_text(
                deal_id, "webhook refused, the deal is not allowlisted for live onboarding",
                ["deal_not_allowlisted"]))
            return _data(OUTCOME_NOT_ALLOWLISTED), 200

        result = store.enqueue(deal_id, mode, source=SOURCE)
    except Exception as exc:  # noqa: BLE001 - a genuine fault: HubSpot should retry
        # The type only: the message could carry anything.
        logger.error("onboard webhook: internal error %s", type(exc).__name__)
        data = _data("internal_error")
        data["errors"] = ["internal error"]
        return data, 500

    outcome = {
        store.CREATED: OUTCOME_QUEUED,
        store.ALREADY_QUEUED: OUTCOME_ALREADY_QUEUED,
        store.ALREADY_DONE: OUTCOME_ALREADY_DONE,
    }[result.outcome]
    logger.info("onboard webhook: outcome=%s deal=%s job=%s", outcome, deal_id, result.job.id)
    return _data(outcome, result.job), 200
