"""Web handler for the plan_preview action.

All plan-preview-specific web logic lives here rather than in `server.py`: the
intake call, the plan summary, the warning serialisation, and posting the
result to Slack for approval. The router knows only this module's ACTION_NAME
and `handle`.

`run_preview_and_post` is also called directly by `actions.additional_plan`,
with `old_plan_id` set -- see its docstring. `handle` itself is unchanged:
plan_preview never sets `old_plan_id`, so its behaviour is exactly as before
that parameter existed.

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

from actions.read_child_plans.runner import (
    current_monthly_estimate,
    current_pricing_summary,
)
from integrations import catalogue, child_lookup, slack

from . import auto_round, funded_hours, hubspot_intake, preview_store, status_codes, status_sync
from . import warnings as plan_warnings

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

    The pricing figures (`monthlyEstimate`, `weeklyTotal`, `publicFunding*`)
    are the pricing period covering TODAY, not the plan-level values, which
    only mirror the first period. The key names are unchanged so nothing
    reading this response breaks; `pricingFrom` says which period they are,
    and `periods` lists them all. See `current_pricing_summary`.
    """
    if plan is None:
        return None

    return {
        "planId": plan.id,
        "childId": plan.child_id,
        "from": plan.from_,
        "to": plan.to,
        "billingScheme": plan.billing_scheme,
        "planPartIds": plan.plan_part_ids,
        "sessionCount": plan.session_booking_count,
        **current_pricing_summary(plan),
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


def _site_payload(result: Any) -> dict | None:
    """Site context for the response. Informational; never sent to Famly."""
    metadata = (result.plan_input.metadata or {}) if result.plan_input else {}
    if not metadata:
        return None
    return {
        "siteCode": metadata.get("site_code"),
        "label": metadata.get("label"),
        "institutionId": metadata.get("institution_id"),
    }


def _catalogue_titles() -> tuple[dict, dict]:
    """Names for the session and product UUIDs the plan books.

    Read from the local catalogue file: Famly's plans response carries the ids
    only, with no titles, so there is nothing to look them up against over the
    API. See `integrations/catalogue.py` -- it never raises, and an id it does
    not cover falls back to the UUID.
    """
    return catalogue.load_titles()


def handle(payload: dict) -> tuple[dict, int]:
    """The plan_preview action: a dry-run preview with no old plan involved."""
    return run_preview_and_post(payload)


@dataclasses.dataclass
class Prepared:
    """A previewed (and, when valid, stored) plan that has NOT been posted yet.

    `prepare_preview` stops here so a caller that must look at the plan before
    deciding whether a human sees it -- the queue worker -- can. `post_prepared`
    does the rest; `run_preview_and_post` is the two back to back.
    """

    data: dict
    status: int
    result: Any = None  # the IntakeResult
    preview: Any = None  # its ParsedPlanResult
    warnings: list = dataclasses.field(default_factory=list)  # serialised
    rounded_periods: list = dataclasses.field(default_factory=list)
    outcome: Any = None  # the auto_round.Outcome, when auto-rounding was on
    funding_clamp: Any = None  # the funded_hours.Outcome, when hours were clamped
    site: dict = dataclasses.field(default_factory=dict)
    preview_id: str | None = None


def run_preview_and_post(
    payload: dict,
    *,
    old_plan_id: str | None = None,
    replace_old_plan: bool = False,
    chain_id: str | None = None,
    chain_index: int | None = None,
    heading: str | None = None,
) -> tuple[dict, int]:
    """Preview a plan and post it to Slack: `prepare_preview` then `post_prepared`.

    The arguments are those of `prepare_preview`, plus `heading` for the post.
    Behaviour is exactly what it was before the two steps were separated.
    """
    prepared = prepare_preview(
        payload,
        old_plan_id=old_plan_id,
        replace_old_plan=replace_old_plan,
        chain_id=chain_id,
        chain_index=chain_index,
    )
    if prepared.status != 200:
        return prepared.data, prepared.status

    post_prepared(prepared, heading=heading)
    return prepared.data, 200


def prepare_preview(
    payload: dict,
    *,
    old_plan_id: str | None = None,
    replace_old_plan: bool = False,
    chain_id: str | None = None,
    chain_index: int | None = None,
    job_id: str | None = None,
    job_index: int | None = None,
) -> Prepared:
    """Run a dry-run plan preview for a webhook payload and store it. No Slack.

    Shared by `handle` (plan_preview, no old plan) and the additional_plan
    action, which calls this directly with `old_plan_id` set to the child's
    existing plan id. Everything below -- validation, the preview call, the
    stored record, the Slack summary and its Approve/Adjust/Reject buttons --
    is IDENTICAL either way; the only difference is that `old_plan_id` (and
    `replace_old_plan`) ride along into the preview call and the stored
    record, so a later Approve click commits with the same params (see
    `runner.preview`/`commit` and `preview_store`).

    On success: logs the full plan against a generated `preview_id` and posts a
    readable summary to Slack with Approve / Reject buttons. A Slack failure is
    reported as `slack_posted: false` and never changes the HTTP status -- the
    preview itself succeeded.

    Args:
        payload: the request body. Plan fields may sit at the top level or be
            nested under `properties`/`data`; the `action` field the router
            dispatched on is ignored here.
        old_plan_id: when set, this previews (and later commits) as a SECOND
            plan alongside the child's existing one (Famly's oldPlanId +
            replaceOldPlan), rather than a plain create. None (the default)
            is the ordinary plan_preview path, completely unchanged.
        replace_old_plan: only meaningful alongside `old_plan_id`. False for
            additional_plan -- the existing plan is kept, not replaced.
        chain_id/chain_index: set only by create_timeline. Stored with the
            preview so the approval that commits it can advance the chain.
            None for every other caller, which behaves exactly as before.
        job_id/job_index: set only by the queue worker, likewise: so a human
            approving a HELD plan can release its job.

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
    try:
        result = hubspot_intake.handle_intake(
            payload,
            version=PLAN_VERSION,
            dry_run=True,  # never commit from the webhook path
            old_plan_id=old_plan_id,
            replace_old_plan=replace_old_plan,
        )
    except Exception as exc:
        # Famly refused the preview request itself: tell the deal (a code, off this thread), then propagate
        # exactly as before so the router still classifies it.
        status = getattr(exc, "status_code", None)
        if status is not None:
            status_sync.rejection_async(hubspot_intake.deal_id_of(payload),
                                        status_codes.plan_rejected_by_famly(status))
        raise

    preview = result.result
    warnings = _serialise_warnings(result.warnings)
    plan_summary = _plan_summary(preview.plan) if preview else None

    data = {
        "plan": plan_summary,
        "site": _site_payload(result),
        "previewed": preview is not None,
        "preview_id": None,
        "slack_posted": False,
        "warnings": warnings,
        "errors": list(result.errors),
    }

    if not result.ok or preview is None:
        # Validation failures are a client problem (bad payload) -> 422. Nothing
        # is posted to Slack: there is no plan to approve. The deal is told (a code, never the error text).
        status_sync.rejection_async(hubspot_intake.deal_id_of(payload), status_codes.plan_rejected(result.errors))
        return Prepared(data=data, status=422, result=result, preview=preview, warnings=warnings)

    store_body = result.plan_body or {}

    # Funded hours are clamped to the hours Famly can actually fund, BEFORE
    # rounding: the rounding then verifies and adjusts the clamped plan. When
    # the clamp cannot be verified it is discarded and the original body and
    # Famly's own warning stand. See funded_hours.py.
    funding_clamp = None
    if funded_hours.funded_settings(store_body) is not None:
        try:
            clamp = funded_hours.apply(
                store_body,
                preview,
                PLAN_VERSION,
                old_plan_id=old_plan_id,
                replace_old_plan=replace_old_plan,
            )
        except Exception:  # noqa: BLE001 - a clamp problem must never break a preview
            logger.exception("Funded-hours clamp failed; previewing the plan unclamped")
            clamp = funded_hours.Outcome(body=store_body, preview=preview)
        if clamp.applied:
            funding_clamp = clamp
            store_body = clamp.body
            # The re-priced plan is the plan now: its warnings and figures are
            # what the approver (and the queue's clean-check) see.
            preview = result.result = clamp.preview
            warnings[:] = _serialise_warnings(result.warnings)
            data["plan"] = _plan_summary(preview.plan)
        elif clamp.reason:
            warnings.extend(
                _serialise_warnings(
                    [plan_warnings.local_warning(f"Funded hours not clamped: {clamp.reason}")]
                )
            )

    # Automatic rounding of the monthly totals: the body that is stored, and so
    # committed, carries the adjustments. When it cannot be applied safely the
    # plan goes ahead exactly as previewed. See auto_round.py.
    rounded_periods: list = []
    outcome = None
    if auto_round.enabled(hubspot_intake.unwrap_payload(payload)):
        outcome = auto_round.apply(
            store_body,
            preview,
            PLAN_VERSION,
            old_plan_id=old_plan_id,
            replace_old_plan=replace_old_plan,
        )
        store_body = outcome.body
        rounded_periods = outcome.periods
        if outcome.reason:
            # Recorded where the approver reads: the plan's own warnings list.
            warnings.extend(
                _serialise_warnings(
                    [plan_warnings.local_warning(f"Not auto-rounded: {outcome.reason}")]
                )
            )

    preview_id = str(uuid.uuid4())
    data["preview_id"] = preview_id

    # The full plan is logged, not returned.
    _log_plan_full(preview_id, preview.raw)

    # Store the EXACT body that was previewed, so an Approve click commits
    # precisely what the approver saw rather than a rebuild of it. A store
    # failure must not break a preview that already succeeded -- it only means
    # the approval cannot be honoured, which the handler reports.
    site = (result.plan_input.metadata or {}) if result.plan_input else {}

    try:
        preview_store.save(
            preview_id,
            store_body,
            getattr(preview.plan, "child_id", None),
            PLAN_VERSION,
            # Metadata only: recorded with the preview, read by nothing in the
            # commit path.
            site_code=site.get("site_code"),
            institution_id=site.get("institution_id"),
            # Recorded so an adjustment can name the same pricing group the
            # plan was priced against. Both this and the estimate below are
            # the pricing period covering TODAY -- not the plan's first period,
            # which is often already over and priced under a different group.
            pricing_group_id=getattr(preview.plan, "active_pricing_group_id", None),
            # Kept so an adjustment can tell whether the re-price moved, and
            # shown as the base in the adjustment modal.
            monthly_estimate=current_monthly_estimate(preview.plan),
            # So a later Approve (or Adjust & Approve) commits with the SAME
            # oldPlanId/replaceOldPlan this preview used. None/False for an
            # ordinary plan_preview -- unchanged from before this parameter
            # existed.
            old_plan_id=old_plan_id,
            replace_old_plan=replace_old_plan,
            chain_id=chain_id,
            chain_index=chain_index,
            job_id=job_id,
            job_index=job_index,
            # The deal's record id when the webhook sent one: lets a successful commit set
            # famly_plan_uploaded. An id only.
            deal_id=hubspot_intake.deal_id_of(payload),
        )
    except Exception as exc:  # noqa: BLE001 - storing must not break the preview
        logger.warning(
            "Could not store preview_id=%s (%s); an approval will report it as "
            "not found",
            preview_id,
            exc,
        )

    return Prepared(
        data=data,
        status=200,
        result=result,
        preview=preview,
        warnings=warnings,
        rounded_periods=rounded_periods,
        outcome=outcome,
        funding_clamp=funding_clamp,
        site=site,
        preview_id=preview_id,
    )


def post_prepared(
    prepared: Prepared, *, heading: str | None = None, child_name: Any = ...
) -> bool:
    """Post a prepared preview to Slack with its Approve/Adjust/Reject buttons.

    Sets `slack_posted` on the prepared data and returns it. `heading` replaces
    the message's first line (create_timeline's "Plan 2 of 4 (...)"); None keeps
    the usual one. `child_name` is for a caller that has already looked the name
    up; left out, it is looked up here.
    """
    data, preview, warnings = prepared.data, prepared.preview, prepared.warnings

    # Names come from the local catalogue file; the preview response has ids
    # only. No Famly call, so a preview costs exactly one request as before.
    session_titles, product_titles = _catalogue_titles()

    # The child's name, looked up live for THIS message only: never stored
    # (not in the preview store above, not in the response or any log) and a
    # failure or timeout just shows the bare id. See integrations.child_lookup.
    if child_name is ...:
        child_name = child_lookup.lookup_child_name(getattr(preview.plan, "child_id", None))

    data["slack_posted"] = slack.post_preview(
        slack.build_summary(
            preview.plan,
            warnings,
            session_titles=session_titles,
            product_titles=product_titles,
            site=prepared.site,
            child_name=child_name,
            heading=heading,
            rounding=prepared.rounded_periods,
            funding_clamp=prepared.funding_clamp,
        ),
        prepared.preview_id,
    )
    return data["slack_posted"]
