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

import logging
from dataclasses import dataclass, field
from typing import Any

from integrations import slack

from . import hubspot_flatten, input_schema, runner, warnings as plan_warnings

logger = logging.getLogger(__name__)

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


DEAL_ID_KEYS = ("dealId", "deal_id", "hs_object_id")


DEAL_ID_CONFLICT = "plan_flag_deal_id_conflict"


def _id_in(body: Any) -> str | None:
    """The first of DEAL_ID_KEYS in `body` that holds digits only, else None."""
    if not isinstance(body, dict):
        return None
    for key in DEAL_ID_KEYS:
        value = str(body.get(key) if body.get(key) is not None else "").strip()
        if value.isdigit():
            return value
    return None


def deal_id_of(payload: Any, notify=None) -> str | None:
    """The HubSpot deal record id the webhook carried, or None. Digits only (an id, never personal
    data); anything else is ignored, never echoed.

    Read from the TOP LEVEL of the body and from the unwrapped inner dict (`properties` or `data`): a
    webhook may put the deal id beside the wrapper or inside it. When both hold DIFFERENT ids the id is
    treated as missing: a code is logged (`plan_flag_deal_id_conflict`), one Slack line is sent and nothing
    is written for this deal."""
    top = _id_in(payload)
    inner = _id_in(unwrap_payload(payload))
    if top and inner and top != inner:
        logger.warning("PLAN FLAG %s ids=%s,%s", DEAL_ID_CONFLICT, top, inner)
        try:
            (notify or slack.post_notice)(
                f"famly_plan_uploaded will not be set for this plan (code {DEAL_ID_CONFLICT}): the webhook "
                f"body holds two different deal ids ({top} and {inner}). Run "
                "hubspot-set-plan-uploaded --from-famly after approving to repair the flag.")
        except Exception as exc:  # noqa: BLE001 - a Slack problem never breaks the preview
            logger.warning("PLAN FLAG conflict notice failed (%s)", type(exc).__name__)
        return None
    return top or inner


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
    old_plan_id: str | None = None,
    replace_old_plan: bool = False,
) -> IntakeResult:
    """Parse, validate, and (when valid) preview a plan from a webhook payload.

    Validation happens BEFORE any network call: an invalid payload returns its
    errors and Famly is never contacted.

    Args:
        payload: the normalized JSON, optionally nested under properties/data.
        version: the plan version the request targets.
        dry_run: must stay True. Preview only -- never writes.
        client: optional RestClient, mainly for tests.
        old_plan_id: passed straight through to `runner.preview` -- set this to
            preview as a SECOND plan alongside an existing one (Famly's
            oldPlanId), used by `actions.additional_plan`. None (the default)
            leaves the ordinary create-plan preview completely unchanged.
        replace_old_plan: passed straight through to `runner.preview`, only
            meaningful alongside `old_plan_id`.

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
    result = runner.preview(
        plan_body,
        version,
        client=client,
        old_plan_id=old_plan_id,
        replace_old_plan=replace_old_plan,
    )

    # Advisory input problems (e.g. a malformed discount slot, already excluded
    # from the body) ride the SAME warnings channel as Famly's own, so the
    # approver sees one list: the computed plan plus everything flagged about
    # it. They never block the preview -- exactly how a Famly warning behaves,
    # where an HTTP 200 can still carry a complaint.
    if result is not None:
        extra = [
            plan_warnings.local_warning(problem) for problem in plan_input.problems
        ]

        # Unresolved site context is advisory too: it changes nothing about the
        # plan, but the approver should know which site this is (or that we
        # could not tell).
        site_warning = (plan_input.metadata or {}).get("warning")
        if site_warning:
            extra.append(
                plan_warnings.local_warning(
                    site_warning, error=plan_warnings.ERROR_SITE_UNRESOLVED
                )
            )

        if extra:
            result.warnings = list(result.warnings) + extra

    return IntakeResult(
        ok=True,
        errors=[],
        plan_input=plan_input,
        plan_body=plan_body,
        result=result,
    )
