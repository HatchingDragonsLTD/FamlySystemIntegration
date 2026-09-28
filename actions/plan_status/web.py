"""Web handler for the plan_status action.

Checks whether a child already has a plan and reports a DECISION so a HubSpot
workflow can branch accordingly, rather than blindly calling plan_preview on a
child who already has a plan. Read-only: this module never writes to Famly.

Request body:

    {"action": "plan_status", "childId": "...", "intended_action": "..."}

`action` is the field the router dispatches on ("plan_status", to reach this
handler at all) and is unrelated to `INTENDED_ACTION_FIELD` below, which is
HubSpot's separate signal for what it is ABOUT to do with the result -- e.g.
"change_plan" or "plan_preview". Everything else in the payload is passed
through harmlessly; only `childId` and the intended-action field matter here.

Decisions:
    new_plan            -- the child has no plan. Safe to plan_preview.
    change_plan         -- the child has a plan AND the caller said so via
                            `intended_action`. Reported only: no Famly write
                            happens here (see the TODO below).
    error_plan_exists   -- the child has a plan but the caller intended
                            "plan_preview" (or said nothing). This means the
                            calling workflow is about to preview a second plan
                            for a child who already has one, so it is flagged
                            in Slack as well as in the response.

The router knows only this module's ACTION_NAME and `handle`.
"""

import logging
import os

from actions.plan_write.hubspot_intake import unwrap_payload
from actions.read_child_plans import runner as read_child_plans
from integrations import slack

logger = logging.getLogger(__name__)

ACTION_NAME = "plan_status"

# Same env var plan_preview reads, so both actions check against the same
# plans API version by default.
PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))

DECISION_NEW_PLAN = "new_plan"
DECISION_CHANGE_PLAN = "change_plan"
DECISION_ERROR_PLAN_EXISTS = "error_plan_exists"

# HubSpot's field for what it intends to do with a plan_status result. Kept
# distinct from the payload's own `action` field (see module docstring).
INTENDED_ACTION_FIELD = "intended_action"
INTENDED_ACTION_CHANGE_PLAN = "change_plan"


def handle(payload: dict) -> tuple[dict, int]:
    """Report whether a child has a plan, and what the caller should do next.

    Args:
        payload: the request body. May be nested under `properties`/`data`,
            same as the plan_write payload -- `unwrap_payload` handles both.

    Returns:
        (data, status). HTTP is 200 for any completed check, including a
        "error_plan_exists" decision: this endpoint reports facts, not
        errors. `data` carries `decision`, `existing_plan_id` and
        `existing_version` (each `None` when not applicable), plus `warnings`
        and `errors`, which the router lifts into the envelope. A missing
        `childId` is the one case that cannot be checked at all, so it is the
        one non-200 response, at 422 (a client problem, not a Famly one).

    Raises:
        RestHTTPError: propagated deliberately when the Famly read itself
            fails, so the router's existing 4xx/5xx classification applies --
            this must never be treated as "the child has no plan".
    """
    unwrapped = unwrap_payload(payload)
    child_id = unwrapped.get("childId")
    intended_action = unwrapped.get(INTENDED_ACTION_FIELD)

    data = {
        "decision": None,
        "existing_plan_id": None,
        "existing_version": None,
        "warnings": [],
        "errors": [],
    }

    if not child_id:
        data["errors"] = ["missing 'childId'"]
        return data, 422

    result = read_child_plans.run(child_id, version=PLAN_VERSION)

    if not result.has_plans:
        data["decision"] = DECISION_NEW_PLAN
        return data, 200

    plan = result.current_plan
    existing_plan_id = plan.id if plan else None
    existing_version = plan.version if plan else None

    if intended_action == INTENDED_ACTION_CHANGE_PLAN:
        data["decision"] = DECISION_CHANGE_PLAN
        data["existing_plan_id"] = existing_plan_id
        data["existing_version"] = existing_version
        # TODO: change_plan execution -- build separately, test-child first,
        # per the original commit build's discipline. This decision is
        # reported only; nothing above or below this point calls
        # runner.commit, previews a mutation, or posts anything to Famly.
        return data, 200

    # The caller intended "plan_preview" (or said nothing) for a child who
    # already has a plan. That is a real signal something is misconfigured in
    # the calling workflow, so it goes to Slack, not just back in the response.
    data["decision"] = DECISION_ERROR_PLAN_EXISTS
    data["existing_plan_id"] = existing_plan_id

    slack.post_notice(
        f"⚠️ Plan preview requested for child {child_id}, but they already "
        f"have plan {existing_plan_id}. This needs a change_plan action, not "
        f"a new preview."
    )

    return data, 200
