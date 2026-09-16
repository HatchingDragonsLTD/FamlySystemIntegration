"""Web handler for the plan_preview action.

All plan-preview-specific web logic lives here rather than in `server.py`: the
intake call, the plan summary, the warning serialisation, and posting the
result to Slack for approval. The router knows only this module's ACTION_NAME
and `handle`.

Preview only. `handle_intake` is dry-run by construction and raises if asked to
write, so there is no commit path through the web layer. The Slack buttons this
posts are inert -- clicking them is logged, not acted on (see
`server.py:/slack/interactivity`).

Famly's full computed plan is NOT returned to the caller. It is logged
server-side at INFO, keyed by `preview_id`, so it stays recoverable without
shipping a large nested body back to HubSpot.

Importable and unit-testable without Flask or a running server: `handle` is a
plain function taking a dict and returning a dict plus an HTTP status.
"""

import dataclasses
import json
import logging
import os
import uuid
from typing import Any

from actions.read_child_plans import runner as read_child_plans
from integrations import slack

from . import hubspot_intake

logger = logging.getLogger(__name__)

ACTION_NAME = "plan_preview"

# The plan `version` passed through to preview. Read here rather than in the
# router so per-action config stays with the action.
PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))


def _plan_summary(plan: Any) -> dict | None:
    """Flatten the computed plan to the operationally useful fields.

    Mirrors `read_child_plans.runner.summarise`, so a previewed plan reads the
    same way as an existing one, plus the public funding figures the Slack
    summary reports on.
    """
    if plan is None:
        return None

    funding = getattr(plan, "public_funding", None)

    return {
        "planId": plan.id,
        "childId": plan.child_id,
        "from": plan.from_,
        "to": plan.to,
        "billingScheme": plan.billing_scheme,
        "monthlyEstimate": plan.monthly_estimate,
        "planPartIds": plan.plan_part_ids,
        "sessionCount": plan.session_booking_count,
        "publicFundingAmount": getattr(funding, "amount", None),
        "publicFundingHours": getattr(funding, "hours", None),
        "publicFundingMinutes": getattr(funding, "minutes", None),
    }


def _serialise_warnings(warnings: list) -> list:
    """Warnings as plain JSON. Dataclasses become dicts; anything else stringifies."""
    return [
        dataclasses.asdict(w) if dataclasses.is_dataclass(w) else str(w)
        for w in warnings
    ]


def _log_plan_full(preview_id: str, plan_full: Any) -> None:
    """Record the full computed plan so it is recoverable by preview_id.

    This is the only place the full plan is kept: there is no store yet, and a
    commit path will need something to look up. Logged rather than returned,
    because the caller (HubSpot) has no use for the nested body.
    """
    try:
        rendered = json.dumps(plan_full, default=str)
    except (TypeError, ValueError):
        rendered = repr(plan_full)

    logger.info("PLAN PREVIEW preview_id=%s plan_full=%s", preview_id, rendered)


def _catalogue_titles(plan: Any) -> tuple[dict, dict]:
    """Fetch the child's session/product catalogue for naming bookings.

    The preview response identifies sessions and products by UUID only, so the
    names come from `read_child_plans.run`, which returns the catalogue
    alongside the child's plans. ONE call serves both maps.

    Never raises: a catalogue that cannot be fetched means the Slack summary
    falls back to showing UUIDs. A cosmetic lookup must not cost the approver
    the message, nor break a preview that has already succeeded.
    """
    child_id = getattr(plan, "child_id", None)
    if not child_id:
        logger.warning("No childId on the previewed plan; cannot resolve names")
        return {}, {}

    try:
        catalogue = read_child_plans.run(child_id, version=PLAN_VERSION)
    except Exception as exc:  # noqa: BLE001 - naming must never break a preview
        logger.warning(
            "Could not fetch the reference catalogue for child %s: %s; "
            "the Slack summary will show raw IDs",
            child_id,
            exc,
        )
        return {}, {}

    return catalogue.session_titles, catalogue.product_titles


def handle(payload: dict) -> tuple[dict, int]:
    """Run a dry-run plan preview for a webhook payload.

    On success: logs the full plan against a generated `preview_id` and posts a
    readable summary to Slack with Approve / Reject buttons. A Slack failure is
    reported as `slack_posted: false` and never changes the HTTP status -- the
    preview itself succeeded.

    Args:
        payload: the request body. Plan fields may sit at the top level or be
            nested under `properties`/`data`; the `action` field the router
            dispatched on is ignored here.

    Returns:
        (data, status). `data` carries `plan`, `previewed`, `preview_id` and
        `slack_posted`, plus `warnings` and `errors`, which the router lifts out
        into the envelope. Status is 200 on a successful preview and 422 when
        validation rejected the payload (a client problem: the same body will
        never succeed on retry).

    Raises:
        RestHTTPError: propagated deliberately. The router classifies it, so
            every action reports upstream failures the same way.
    """
    result = hubspot_intake.handle_intake(
        payload,
        version=PLAN_VERSION,
        dry_run=True,  # never commit from the webhook path
    )

    preview = result.result
    warnings = _serialise_warnings(result.warnings)
    plan_summary = _plan_summary(preview.plan) if preview else None

    data = {
        "plan": plan_summary,
        "previewed": preview is not None,
        "preview_id": None,
        "slack_posted": False,
        "warnings": warnings,
        "errors": list(result.errors),
    }

    if not result.ok or preview is None:
        # Validation failures are a client problem (bad payload) -> 422. Nothing
        # is posted to Slack: there is no plan to approve.
        return data, 422

    preview_id = str(uuid.uuid4())
    data["preview_id"] = preview_id

    # The full plan is logged, not returned.
    _log_plan_full(preview_id, preview.raw)

    # Names come from the child's live catalogue, which the preview response
    # does not carry. One fetch, reused for both maps.
    session_titles, product_titles = _catalogue_titles(preview.plan)

    data["slack_posted"] = slack.post_preview(
        slack.build_summary(
            preview.plan,
            warnings,
            session_titles=session_titles,
            product_titles=product_titles,
        ),
        preview_id,
    )

    return data, 200
