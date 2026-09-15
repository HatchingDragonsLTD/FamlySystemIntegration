"""Web handler for the plan_preview action.

All plan-preview-specific web logic lives here rather than in `server.py`: the
intake call, the plan summary, and the warning serialisation. The router knows
only this module's ACTION_NAME and `handle`.

Preview only. `handle_intake` is dry-run by construction and raises if asked to
write, so there is no commit path through the web layer.

Importable and unit-testable without Flask or a running server: `handle` is a
plain function taking a dict and returning a dict plus an HTTP status.
"""

import dataclasses
import os
from typing import Any

from . import hubspot_intake

ACTION_NAME = "plan_preview"

# The plan `version` passed through to preview. Read here rather than in the
# router so per-action config stays with the action.
PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))


def _plan_summary(plan: Any) -> dict | None:
    """Flatten the computed plan to the operationally useful fields.

    Mirrors `read_child_plans.runner.summarise`, so a previewed plan reads the
    same way as an existing one.
    """
    if plan is None:
        return None

    return {
        "planId": plan.id,
        "childId": plan.child_id,
        "from": plan.from_,
        "to": plan.to,
        "billingScheme": plan.billing_scheme,
        "monthlyEstimate": plan.monthly_estimate,
        "planPartIds": plan.plan_part_ids,
        "sessionCount": plan.session_booking_count,
    }


def _serialise_warnings(warnings: list) -> list:
    """Warnings as plain JSON. Dataclasses become dicts; anything else stringifies."""
    return [
        dataclasses.asdict(w) if dataclasses.is_dataclass(w) else str(w)
        for w in warnings
    ]


def handle(payload: dict) -> tuple[dict, int]:
    """Run a dry-run plan preview for a webhook payload.

    Args:
        payload: the request body. Plan fields may sit at the top level or be
            nested under `properties`/`data`; the `action` field the router
            dispatched on is ignored here.

    Returns:
        (data, status). `data` carries the action's payload -- `plan`,
        `plan_full`, `previewed` -- plus `warnings` and `errors`, which the
        router lifts out into the envelope. Status is 200 on a successful
        preview and 422 when validation rejected the payload (a client problem:
        the same body will never succeed on retry).

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

    data = {
        "plan": _plan_summary(preview.plan) if preview else None,
        # Famly's full computed response, untouched.
        "plan_full": preview.raw if preview else None,
        "previewed": preview is not None,
        "warnings": _serialise_warnings(result.warnings),
        "errors": list(result.errors),
    }

    # Validation failures are a client problem (bad payload) -> 422.
    return data, (200 if result.ok else 422)
