"""Web handler for the create_timeline action.

Builds a NEW starter's plans as a chain -- one plan per stretch of the
placement (see `actions/plan_write/timeline.py`) -- with one Slack approval per
plan. This handler only STARTS the chain: it checks the child, records the
chain and previews and posts the FIRST plan. Each later plan is previewed by the
approval of the one before it (`approval.py` -> `engine.continue_after_commit`).

Nothing here writes to Famly. Every plan is committed by the ordinary Slack
Approve click, behind every guard that click already has.

Request body: the flat HubSpot payload plan_preview accepts, plus

    start_date, leave_date (optional)
    has_funding,      funding_start,  funding_end,  funding_entitlement ("30"/"15")
    has_advanced_fee, advanced_start, advanced_end, advanced_discount

Decision tree (each refusal posts a Slack notice and creates nothing):

    bad or incomplete input        -> 422, no Famly call at all
    the child has an active chain  -> 409
    the child already has a plan   -> 409. Reconciling existing plans is out
                                      of scope: this action only builds a
                                      starter's plans from nothing.
    otherwise                      -> the chain is recorded and plan 1 of N is
                                      previewed and posted.

The router knows only this module's ACTION_NAME and `handle`.
"""

import logging
import os
import uuid

from actions.plan_write import timeline
from actions.plan_write.hubspot_intake import deal_id_of, unwrap_payload
from actions.read_child_plans import runner as read_child_plans
from integrations import slack

from . import chains, engine, maintenance

logger = logging.getLogger(__name__)

ACTION_NAME = "create_timeline"

# Same env var every plan action reads.
PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))


def _blank() -> dict:
    return {
        "chain_id": None,
        "segments": [],
        "previewed": False,
        "preview_id": None,
        "slack_posted": False,
        "warnings": [],
        "errors": [],
    }


def _refuse(data: dict, message: str, status: int) -> tuple[dict, int]:
    """Tell Slack and the caller why nothing was created."""
    slack.post_notice(message)
    data["errors"] = [message]
    return data, status


def _duplicate_chain_message(child_id: str, chain: chains.Chain) -> str:
    waiting = (
        engine.heading_text(chain.segments, chain.next_index)
        if chain.next_index < len(chain.segments)
        else "its last plan"
    )
    return (
        f"⚠️ create_timeline for child {child_id}: chain `{chain.chain_id}` is "
        f"still active (waiting on {waiting}). Nothing was created. Approve or "
        f"reject its pending plan first."
    )


def handle(payload: dict) -> tuple[dict, int]:
    """Start a chain: refuse if it cannot be started, else post plan 1.

    Returns:
        (data, status). 200 when plan 1 was previewed and posted; 422 for bad
        input (nothing contacted); 409 when the child already has a plan or an
        active chain; 502 when plan 1 could not be posted to Slack (the chain is
        stopped).

    Raises:
        RestHTTPError: propagated deliberately -- from the existing-plans read
            (a failed read must never be treated as "no plans") and from plan
            1's preview, in which case the chain has already been stopped.
    """
    raw = unwrap_payload(payload)
    data = _blank()

    if not isinstance(raw, dict):
        data["errors"] = ["body must be a JSON object"]
        return data, 422

    parsed = engine.parse_request(raw, deal_id=deal_id_of(payload))
    if parsed.request is None:
        data["errors"] = parsed.errors
        return data, 422
    request = parsed.request
    child_id = request.child_id

    # Segments: pure, no Famly. A malformed or impossible period is a hard
    # error -- guessing it would price the wrong plans.
    try:
        segments, timeline_warnings = timeline.build_segments(
            request.start,
            request.leave,
            funding=request.funding,
            advanced=request.advanced,
        )
    except timeline.TimelineError as exc:
        data["errors"] = [str(exc)]
        return data, 422
    data["warnings"] = list(timeline_warnings)

    # Every plan's input is checked now, before anything is created, so a chain
    # cannot get plans in and then fail on a later plan's input.
    problems = engine.validate_segments(request.payload, segments)
    if problems:
        data["errors"] = problems
        return data, 422

    # Free any child whose chain is waiting on a preview that has expired, this
    # one included, before deciding whether a chain is in the way.
    maintenance.expire_stale_chains()

    active = chains.find_active(child_id)
    if active is not None:
        logger.warning(
            "CREATE_TIMELINE BLOCKED child_id=%s: chain %s is active",
            child_id,
            active.chain_id,
        )
        return _refuse(data, _duplicate_chain_message(child_id, active), 409)

    existing = read_child_plans.run(child_id, version=PLAN_VERSION).plans
    if existing:
        ids = ", ".join(p.id or "unknown" for p in existing)
        logger.warning(
            "CREATE_TIMELINE BLOCKED child_id=%s: %d existing plan(s) %s",
            child_id,
            len(existing),
            ids,
        )
        return _refuse(
            data,
            f"⚠️ create_timeline for child {child_id}: the child already has "
            f"{len(existing)} plan(s) ({ids}). Reconciling existing plans is not "
            f"something create_timeline does, so nothing was created.",
            409,
        )

    chain_id = str(uuid.uuid4())
    if not chains.create(chain_id, child_id, request.payload, segments):
        # Lost a race with another request for the same child.
        active = chains.find_active(child_id)
        message = (
            _duplicate_chain_message(child_id, active)
            if active is not None
            else f"⚠️ create_timeline for child {child_id}: a chain is already active."
        )
        return _refuse(data, message, 409)

    data["chain_id"] = chain_id
    data["segments"] = [dict(s) for s in segments]

    if timeline_warnings:
        slack.post_notice(
            f"ℹ️ create_timeline for child {child_id}: "
            + " ".join(timeline_warnings)
        )

    chain = chains.get(chain_id)
    result, status = engine.present_segment(chain, 0, old_plan_id=None)

    # Plan 1's own response (plan summary, preview_id, slack_posted, warnings),
    # under the chain's.
    result_warnings = result.pop("warnings", [])
    errors = result.pop("errors", [])
    data.update(result)
    data["warnings"] = list(timeline_warnings) + list(result_warnings)
    data["errors"] = errors
    return data, status
