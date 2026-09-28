"""Web handler for the additional_plan action.

Adds a SECOND plan alongside a child's EXISTING one -- Famly operation #3:
`oldPlanId=<existing plan id>` + `replaceOldPlan=false`. This is a genuinely
new write capability (the existing plan is kept, not touched), so it goes
through the exact same guarded pipeline as an ordinary create:

    this module (resolve the existing plan) -> plan_write.web.run_preview_and_post
    -> preview_store (records old_plan_id/replace_old_plan)
    -> Slack Approve/Adjust/Reject, same UI as any other preview
    -> approval.py's Approve click -> runner.commit(..., old_plan_id=...)

Nothing here calls `runner.commit` or posts a Famly write directly -- the only
write path is the existing Slack-approval one, unchanged, with `old_plan_id`
carried through it (see preview_store.py and approval.py).

Request body: the SAME shape plan_preview accepts (childId plus the flat plan
fields, whatever shape the flattener already handles -- funded or not). This
action resolves the existing plan's id itself; the caller does not supply one.

Decision tree:
    zero plans, still zero after one retry -> Slack notice, no preview,
        error response (409).
    exactly one plan                        -> preview with old_plan_id set to
        that plan's id, stored, posted to Slack exactly like an ordinary
        preview.
    two or more plans                       -> Slack notice naming every plan
        id, no preview, error response (409). Human resolution required; this
        action does not guess which plan a new one should sit alongside.

The router knows only this module's ACTION_NAME and `handle`.
"""

import logging
import os
import time

from actions.plan_write import web as plan_write_web
from actions.plan_write.hubspot_intake import unwrap_payload
from actions.read_child_plans import runner as read_child_plans
from integrations import slack

logger = logging.getLogger(__name__)

ACTION_NAME = "additional_plan"

# Same env var plan_preview/plan_status read, so every action checks the same
# plans API version by default.
PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))

# How long to wait before re-checking a child that shows zero plans. This
# covers a race between this webhook and whatever just created the child's
# first plan (e.g. HubSpot firing additional_plan immediately after the
# intake flow that creates plan #1, before Famly's read model has caught up).
#
# THIS BLOCKS THE REQUEST THREAD for up to this many seconds, synchronously
# (a plain time.sleep). A webhook sender with a timeout shorter than roughly
# RETRY_DELAY_SECONDS plus the two Famly reads either side of it could treat
# this request as failed and retry it -- if HubSpot's own webhook timeout is
# anywhere near ~30-35s, this needs revisiting (an immediate ack plus a
# background job, rather than a blocking sleep) before relying on it in
# production.
RETRY_DELAY_SECONDS = 30


def handle(payload: dict, *, sleeper=time.sleep) -> tuple[dict, int]:
    """Resolve the child's existing plan, then preview a second one beside it.

    Args:
        payload: the request body. May be nested under `properties`/`data`,
            same as the plan_write payload.
        sleeper: how to wait before the zero-plans retry. Defaults to
            `time.sleep`; tests pass a no-op so the suite does not actually
            block for `RETRY_DELAY_SECONDS`.

    Returns:
        (data, status). On the happy path (exactly one existing plan), this is
        exactly `plan_write_web.run_preview_and_post`'s own return shape --
        `plan`/`previewed`/`preview_id`/`slack_posted`/`warnings`/`errors` --
        since this action's preview is posted through that same function.
        Zero plans (even after the retry) or two-or-more plans are reported as
        409s with `errors` set; no preview is attempted and no Famly write of
        any kind happens on either of those paths.

    Raises:
        RestHTTPError: propagated deliberately from the existing-plan read, so
            the router's existing 4xx/5xx classification applies -- a Famly
            read failure must never be treated as "zero plans".
    """
    unwrapped = unwrap_payload(payload)
    child_id = unwrapped.get("childId")

    data = {
        "plan": None,
        "previewed": False,
        "preview_id": None,
        "slack_posted": False,
        "warnings": [],
        "errors": [],
    }

    if not child_id:
        data["errors"] = ["missing 'childId'"]
        return data, 422

    result = read_child_plans.run(child_id, version=PLAN_VERSION)

    if not result.has_plans:
        # One retry only -- see RETRY_DELAY_SECONDS for the tradeoff this makes.
        sleeper(RETRY_DELAY_SECONDS)
        result = read_child_plans.run(child_id, version=PLAN_VERSION)

    plan_count = len(result.plans)

    if plan_count == 0:
        message = (
            f"⚠️ additional_plan for child {child_id}: no existing plan found "
            f"after retry — expected one to exist"
        )
        logger.warning(
            "ADDITIONAL_PLAN BLOCKED child_id=%s: no plan found after retry",
            child_id,
        )
        slack.post_notice(message)
        data["errors"] = [message]
        return data, 409

    if plan_count > 1:
        plan_ids = [p.id for p in result.plans]
        message = (
            f"⚠️ additional_plan for child {child_id}: found {plan_count} "
            f"existing plans ({', '.join(pid or 'unknown' for pid in plan_ids)}) "
            f"— cannot tell which one a new plan should sit alongside. This "
            f"needs human resolution."
        )
        logger.warning(
            "ADDITIONAL_PLAN BLOCKED child_id=%s: %d existing plans %s",
            child_id,
            plan_count,
            plan_ids,
        )
        slack.post_notice(message)
        data["errors"] = [message]
        data["existingPlanIds"] = plan_ids
        return data, 409

    # Exactly one plan: preview a second one alongside it, through the SAME
    # preview -> store -> Slack pipeline plan_preview uses. old_plan_id is the
    # only thing that differs from an ordinary create.
    existing_plan = result.plans[0]

    return plan_write_web.run_preview_and_post(
        payload, old_plan_id=existing_plan.id, replace_old_plan=False
    )
