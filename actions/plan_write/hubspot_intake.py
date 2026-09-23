"""Intake for the normalized payload a HubSpot custom-code action sends.

The payload is the SAME normalized shape the CSV loader produces -- UUIDs
already resolved upstream. Nothing here maps labels to UUIDs, and nothing here
commits.

There is no HTTP server in this module and none belongs here. A Flask/FastAPI
shell can call `handle_intake` from `integrations/` once the read-only path is
proven; this stays a plain function so it is testable and importable without a
web framework.

No argparse, no commit path.
"""

from dataclasses import dataclass, field
from typing import Any

from . import hubspot_flatten, input_schema, runner, warnings as plan_warnings

# Wrappers a webhook may nest the payload under. Checked in order.
PAYLOAD_WRAPPER_KEYS = ("properties", "data")


@dataclass
class IntakeResult:
    """What an intake attempt produced.

    `ok` False with `errors` populated means nothing was sent to Famly.
    """

    ok: bool = False
    errors: list[str] = field(default_factory=list)
    plan_input: input_schema.PlanInput | None = None
    # The exact {"plan": {...}} body sent to Famly. Kept so an approval can
    # commit precisely what was previewed, rather than rebuilding it.
    plan_body: dict | None = None
    result: runner.ParsedPlanResult | None = None

    @property
    def warnings(self) -> list:
        """Plan warnings from the preview, if one ran."""
        return self.result.warnings if self.result else []


def unwrap_payload(payload: Any) -> Any:
    """Return the plan payload, unwrapping a `properties`/`data` nesting.

    A webhook may deliver the normalized object directly or nested one level
    down. Both are accepted; a wrapper is only unwrapped when it actually
    contains a dict.
    """
    if not isinstance(payload, dict):
        return payload

    for key in PAYLOAD_WRAPPER_KEYS:
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner

    return payload


def parse_hubspot_payload(payload: Any) -> input_schema.PlanInput:
    """Parse a webhook payload into a PlanInput.

    The HubSpot native webhook sends a flat field set (one sessionId per day,
    scalar billing/funding fields), so we reshape it into the nested plan
    structure before parsing. Defensive: never raises. An unusable payload
    yields an empty PlanInput, which `input_schema.validate` then reports on.
    """
    unwrapped = unwrap_payload(payload)
    nested = hubspot_flatten.flatten_to_nested(unwrapped)
    return input_schema.from_dict(nested)


def handle_intake(
    payload: Any,
    *,
    version: int,
    dry_run: bool = True,
    client=None,
) -> IntakeResult:
    """Parse, validate, and (when valid) preview a plan from a webhook payload.

    Validation happens BEFORE any network call: an invalid payload returns its
    errors and Famly is never contacted.

    Args:
        payload: the normalized JSON, optionally nested under properties/data.
        version: the plan version the request targets.
        dry_run: must stay True. Preview only -- never writes.
        client: optional RestClient, mainly for tests.

    Returns:
        IntakeResult. When `ok` is False, `errors` says why and nothing was sent.

    Raises:
        NotImplementedError: if `dry_run` is False. Committing from an automated
            intake path is deliberately not wired up: a commit must go through
            `runner.commit`, which requires an explicit confirm and a test-child
            allow-list that a webhook cannot satisfy on its own.
    """
    if not dry_run:
        raise NotImplementedError(
            "handle_intake is preview-only. Committing from an automated intake "
            "path is not wired up: use runner.commit() explicitly, with "
            "confirm=True and an allowed_child_ids set."
        )

    plan_input = parse_hubspot_payload(payload)

    errors = input_schema.validate(plan_input)
    if errors:
        return IntakeResult(ok=False, errors=errors, plan_input=plan_input)

    plan_body = input_schema.to_plan_body(plan_input)
    result = runner.preview(plan_body, version, client=client)

    # Advisory input problems (e.g. a malformed discount slot, already excluded
    # from the body) ride the SAME warnings channel as Famly's own, so the
    # approver sees one list: the computed plan plus everything flagged about
    # it. They never block the preview -- exactly how a Famly warning behaves,
    # where an HTTP 200 can still carry a complaint.
    if plan_input.problems and result is not None:
        result.warnings = list(result.warnings) + [
            plan_warnings.local_warning(problem) for problem in plan_input.problems
        ]

    return IntakeResult(
        ok=True,
        errors=[],
        plan_input=plan_input,
        plan_body=plan_body,
        result=result,
    )
