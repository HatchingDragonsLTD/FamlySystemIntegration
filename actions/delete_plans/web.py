"""Web handler for the delete_plans action -- TEMPORARY, DESTRUCTIVE.

THIS IS A ONE-OFF CLEAN-UP UTILITY: see `runner.py`'s module docstring for what
it does, every guard on it, and what to delete when the clean-up is finished.
Follows `urn_lookup`'s structure.

Request body:

    {"action": "delete_plans", "childId": "<uuid>", "confirm": "DELETE"}

Deletes ALL of that one child's plans (in `live` mode only, and only once
`_delete_plan` has been written -- until then only `dry_run` works).

RESPONSE CODES. The outcome is reported in the response body, and the HTTP
status is 200 for EVERY outcome, failures included, so HubSpot never retries a
destructive call. 422 is only for a malformed request (a missing or bad
`childId`, or `confirm` that is not exactly "DELETE"), which deletes nothing.
A repeat request for a child with no plans is a harmless no-op.

`data` carries `ok` (did the request achieve what it asked), `outcome`, `mode`,
`childId`, `message` and `plans`, plus `warnings` and `errors`, which the router
lifts into the envelope. NOTE the envelope's own `ok` mirrors HTTP 200 (see
server.py), so read `data.ok` for the real answer.

The router knows only this module's ACTION_NAME and `handle`.
"""

import logging

from actions.plan_write import input_schema

from . import runner

logger = logging.getLogger(__name__)

ACTION_NAME = "delete_plans"


def handle(payload: dict) -> tuple[dict, int]:
    """Validate the request, then hand one child to the runner. 200 unless malformed."""
    data = {
        "ok": False,
        "childId": None,
        "mode": runner.mode(),
        "outcome": "malformed",
        "message": "",
        "plans": [],
        "warnings": [],
        "errors": [],
    }

    raw = payload if isinstance(payload, dict) else {}
    child_id = raw.get("childId")
    errors = []

    if not isinstance(child_id, str) or not input_schema.is_uuid(child_id.strip()):
        errors.append("childId: required, and must be a UUID")
    if raw.get("confirm") != runner.CONFIRM_WORD:
        errors.append(f"confirm: must be exactly {runner.CONFIRM_WORD!r}")

    if errors:
        data["errors"] = errors
        data["message"] = "; ".join(errors)
        return data, 422

    child_id = child_id.strip()
    logger.warning("DELETE_PLANS request child_id=%s mode=%s", child_id, data["mode"])
    return runner.run(child_id).data(), 200
