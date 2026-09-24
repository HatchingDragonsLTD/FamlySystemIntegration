"""What happens when someone clicks Approve or Reject in Slack.

THIS IS THE ONLY PATH THAT WRITES TO FAMLY FROM THE WEB LAYER. Every guard is
load-bearing; read before changing:

  1. COMMIT_ENABLED must be true. A master switch, default off.
  2. The preview must exist and still be within its TTL. An approval clicked
     days later would commit against stale pricing.
  3. The preview must not already be committed, rejected, or mid-commit. The
     claim is atomic, so a double-click cannot create two real plans.
  4. The child must be in COMMIT_ALLOWED_CHILD_IDS. `runner.commit` enforces
     this itself and is not weakened here -- the check below only produces a
     clearer message before we bother claiming.
  5. `runner.commit` is called with confirm=True, and does its own preview
     before writing.

Scope: CREATE only. Overwriting a plan and adding a second plan are separate
operations and are not built.

The committed body is the exact one that was previewed, read back from the
store -- never rebuilt, so what is written is what the approver saw.

Returns Slack message text; no Flask, no HTTP. Importable and testable
directly.
"""

import copy
import logging
import threading
import uuid

from core.config import ConfigError, load_config
from core.rest_client import RestHTTPError
from integrations import slack

from . import preview_store, runner

logger = logging.getLogger(__name__)

# Slack message text. Kept here so the router stays free of per-action wording.
MSG_REJECTED = "❌ Rejected — no plan created"
MSG_COMMIT_DISABLED = (
    "⚠️ Commit is disabled — nothing was written. "
    "(Set COMMIT_ENABLED=true to allow commits.)"
)
MSG_NOT_FOUND = "⚠️ Preview expired or not found — please re-run the preview"
MSG_ALREADY_COMMITTED = "ℹ️ Already committed — no second plan was created"
MSG_ALREADY_REJECTED = "⚠️ This preview was already rejected — nothing was written"
MSG_IN_PROGRESS = "⏳ A commit is already in progress for this preview"
MSG_SUPERSEDED = (
    "⚠️ This preview was replaced by an adjusted one "
    "— approve that message instead"
)


def _commit_refused(detail: str) -> str:
    return f"⚠️ Commit refused: {detail}"


def _commit_failed(detail: str) -> str:
    return f"❌ Commit failed: {detail}"


def handle_click(
    action_id: str,
    preview_id: str | None,
    user: str | None,
    trigger_id: str | None = None,
) -> str | None:
    """Act on a verified Slack button click and return the message to show.

    Args:
        action_id: `plan_approve` or `plan_reject` (already validated upstream).
        preview_id: the button's value -- the preview to act on.
        user: who clicked, for the audit log.

    Returns:
        Text to replace the Slack message with, or None when there is nothing
        to say (Adjust & Approve opens a modal and leaves the message alone).
        Never raises: a failure is reported to the approver rather than thrown
        at Slack, which would show them nothing.
    """
    if action_id == slack.ACTION_REJECT:
        return _reject(preview_id, user)
    if action_id == slack.ACTION_ADJUST:
        return handle_adjust_click(preview_id, user, trigger_id)
    return _approve(preview_id, user)


def _reject(preview_id: str | None, user: str | None) -> str:
    """Record a rejection. Never calls Famly."""
    if preview_id:
        preview_store.mark(preview_id, preview_store.STATUS_REJECTED)

    logger.info(
        "PLAN REJECTED preview_id=%s user=%s -- nothing written", preview_id, user
    )
    return MSG_REJECTED


def _approve(preview_id: str | None, user: str | None) -> str:
    """Commit the stored plan, subject to every guard."""
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Cannot commit: %s", exc)
        return _commit_failed(str(exc))

    # Guard 1: the master switch.
    if not config.commit_enabled:
        logger.warning(
            "APPROVAL BLOCKED preview_id=%s user=%s: COMMIT_ENABLED is false",
            preview_id,
            user,
        )
        return MSG_COMMIT_DISABLED

    # Guard 2/3: must exist, be fresh, and not already resolved.
    stored = preview_store.get(preview_id or "")
    if stored is None or stored.is_expired:
        logger.warning(
            "APPROVAL BLOCKED preview_id=%s user=%s: preview %s",
            preview_id,
            user,
            preview_store.describe(stored),
        )
        return MSG_NOT_FOUND

    if stored.is_committed:
        # Double click, or a retry after the message failed to update.
        logger.info(
            "APPROVAL IGNORED preview_id=%s user=%s: already committed",
            preview_id,
            user,
        )
        return MSG_ALREADY_COMMITTED

    if stored.status == preview_store.STATUS_REJECTED:
        return MSG_ALREADY_REJECTED

    if stored.status == preview_store.STATUS_SUPERSEDED:
        # An adjusted copy exists; that one is the approvable plan now.
        return MSG_SUPERSEDED

    if stored.status == preview_store.STATUS_COMMITTING:
        return MSG_IN_PROGRESS

    child_id = stored.child_id

    # Guard 4: clearer message than the runner's own refusal, which still runs.
    if not config.commit_allows(child_id):
        logger.warning(
            "APPROVAL BLOCKED preview_id=%s user=%s: child %s not in "
            "COMMIT_ALLOWED_CHILD_IDS",
            preview_id,
            user,
            child_id,
        )
        return _commit_refused(
            f"child `{child_id}` is not in the allowed list"
        )

    # Claim it: pending -> committing, atomically. Only one click can win, so a
    # double-click cannot produce two real plans.
    claimed = preview_store.claim_for_commit(preview_id or "")
    if claimed is None:
        current = preview_store.get(preview_id or "")
        if current is not None and current.is_committed:
            return MSG_ALREADY_COMMITTED
        return MSG_IN_PROGRESS

    # The allow-list the runner enforces. When the sentinel has opened it up,
    # pass this child specifically -- the runner's guard must still receive a
    # concrete set rather than being bypassed.
    if config.commit_allows_any_child:
        logger.warning(
            "COMMIT_ALLOWED_CHILD_IDS is open to every child (sentinel set)"
        )
        allowed_child_ids = {child_id}
    else:
        allowed_child_ids = set(config.commit_allowed_child_ids)

    logger.info(
        "COMMITTING preview_id=%s child_id=%s user=%s",
        preview_id,
        child_id,
        user,
    )

    try:
        result = runner.commit(
            claimed.plan_body,
            claimed.version,
            confirm=True,
            allowed_child_ids=allowed_child_ids,
        )
    except runner.PlanCommitRefused as exc:
        # A guard inside the runner stopped it. Nothing was written.
        preview_store.release_claim(preview_id or "")
        logger.warning("COMMIT REFUSED preview_id=%s: %s", preview_id, exc)
        return _commit_refused(str(exc))
    except RestHTTPError as exc:
        preview_store.release_claim(preview_id or "")
        # Same 4xx/5xx distinction the router draws: a 4xx is a bad plan and
        # will never succeed as-is; a 5xx is worth clicking again.
        if 400 <= exc.status_code < 500:
            detail = f"Famly rejected the plan (HTTP {exc.status_code}). {exc}"
        else:
            detail = (
                f"Famly upstream error (HTTP {exc.status_code}) -- worth "
                f"trying again. {exc}"
            )
        logger.error("COMMIT FAILED preview_id=%s: %s", preview_id, exc)
        return _commit_failed(detail)
    except Exception as exc:  # noqa: BLE001 - never throw at Slack
        preview_store.release_claim(preview_id or "")
        logger.exception("COMMIT FAILED preview_id=%s", preview_id)
        return _commit_failed(str(exc))

    preview_store.mark(preview_id or "", preview_store.STATUS_COMMITTED)

    plan_id = getattr(result.plan, "id", None)
    logger.info(
        "COMMITTED preview_id=%s child_id=%s plan_id=%s user=%s",
        preview_id,
        child_id,
        plan_id,
        user,
    )

    text = "✅ Plan committed to Famly"
    if plan_id:
        text += f" (plan `{plan_id}`)"

    warning_count = len(result.warnings or [])
    if warning_count:
        text += f"\n:warning: committed with {warning_count} warning(s)"

    return text


# --------------------------------------------------------------------------- #
# Adjust & Approve
#
# Adds a rounding adjustment to a previewed plan, re-prices it, and offers the
# result for commit under a NEW preview_id. Nothing here commits: the Confirm
# Commit button carries ACTION_APPROVE, so the write goes through _approve
# above with every guard intact.
# --------------------------------------------------------------------------- #
def _adjust_error(detail: str) -> str:
    return f"⚠️ Could not adjust: {detail}"


def _blocking_status(stored) -> str | None:
    """Why this preview cannot be acted on, or None when it can.

    The same checks the Approve path makes, so an adjustment cannot slip past
    an expiry, a rejection, or a commit that already happened.
    """
    if stored is None or stored.is_expired:
        return MSG_NOT_FOUND
    if stored.is_committed:
        return MSG_ALREADY_COMMITTED
    if stored.status == preview_store.STATUS_REJECTED:
        return MSG_ALREADY_REJECTED
    if stored.status == preview_store.STATUS_SUPERSEDED:
        return MSG_SUPERSEDED
    if stored.status == preview_store.STATUS_COMMITTING:
        return MSG_IN_PROGRESS
    return None


def handle_adjust_click(preview_id, user, trigger_id) -> str | None:
    """Open the adjustment modal. Changes nothing by itself.

    Returns None when the modal opened (the message is left as it is), or text
    to show when it could not.
    """
    stored = preview_store.get(preview_id or "")
    blocked = _blocking_status(stored)
    if blocked is not None:
        logger.warning(
            "ADJUST BLOCKED preview_id=%s user=%s: %s",
            preview_id,
            user,
            preview_store.describe(stored),
        )
        return blocked

    if not slack.open_modal(trigger_id, slack.adjustment_modal(preview_id)):
        return _adjust_error("the Slack dialog could not be opened, please retry")

    logger.info("ADJUST MODAL opened preview_id=%s user=%s", preview_id, user)
    return None


def _parse_adjustment(raw):
    """Parse and range-check the entered adjustment.

    Returns (value, error_message). The range is a HARD boundary, checked here
    on the only path that can produce an adjusted body -- there is no route
    that bypasses it.
    """
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return None, "Enter an adjustment, e.g. 0.50"

    # A typed currency symbol is a natural slip, not worth blocking on.
    text = text.lstrip("£").strip()

    try:
        value = float(text)
    except (TypeError, ValueError):
        return None, f"{raw!r} is not a number. Enter something like 0.50 or -0.25"

    if value < slack.MIN_ADJUSTMENT or value > slack.MAX_ADJUSTMENT:
        return None, (
            f"Adjustment must be between {slack.MIN_ADJUSTMENT:.2f} and "
            f"{slack.MAX_ADJUSTMENT:.2f} (got {value:.2f})"
        )

    return value, None


def _with_adjustment(plan_body: dict, pricing_group_id: str, value: float) -> dict:
    """A deep copy of the body with one totalAdjustments entry added.

    Copied rather than mutated: the stored original must stay exactly as it was
    previewed, in case the adjustment is abandoned.
    """
    adjusted = copy.deepcopy(plan_body)
    parts = adjusted.get("plan", {}).get("planParts")
    if not isinstance(parts, list) or not parts:
        return adjusted

    part = parts[0]
    existing = part.get("totalAdjustments")
    part["totalAdjustments"] = (
        list(existing) if isinstance(existing, list) else []
    ) + [{"pricingGroupId": pricing_group_id, "adjustment": value}]
    return adjusted


def _money(value) -> str:
    return f"{value:,.2f}" if isinstance(value, (int, float)) else "unknown"


def _base_estimate_changed(before, after) -> bool:
    """Whether Famly's base estimate differs from the pre-adjustment one.

    DIAGNOSTIC ONLY -- logged, never shown, and not a problem either way.
    `monthlyEstimate` and `totalAdjustments` are separate figures in Famly's
    model: the adjustment is stored and applied at INVOICING, and is never
    folded into the estimate. So an unchanged base estimate is the expected
    result of a successful adjustment, not a sign that anything was ignored.

    Kept because a base estimate that DOES move means something else about the
    plan changed between previews, which is worth being able to see in a log.
    """
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return False
    return abs(float(after) - float(before)) >= 0.005


def total_to_bill(estimate, value):
    """What the family is actually charged: Famly's base plus the adjustment.

    Famly computes this only at invoicing time -- there is no combined figure
    anywhere in the preview response -- so it is worked out here for the
    approver to confirm against.

    Returns None when the base estimate is unknown, since a total built on a
    missing number would be a guess presented as a fact.
    """
    if not isinstance(estimate, (int, float)) or isinstance(estimate, bool):
        return None
    return float(estimate) + float(value)


def _adjusted_summary(value: float, before, estimate, result, pricing_group_id) -> str:
    """The re-priced message: the base figure, the adjustment, and the real total.

    Famly keeps `monthlyEstimate` and `totalAdjustments` apart and only combines
    them when it invoices, so the preview will NOT show a changed estimate. The
    approver is confirming money, so all three numbers are spelled out and the
    total is calculated here rather than left for them to do in their head.
    """
    total = total_to_bill(estimate, value)

    lines = [
        "*Adjusted plan — awaiting final confirmation*",
        f"• Base monthly estimate (Famly, pre-adjustment — unchanged by "
        f"this adjustment): {_money(estimate)}",
        f"• Adjustment applied: £{value:+.2f}",
        f"• *Total to be billed: {_money(total)}*",
        "_(Famly bills base + adjustment at invoicing; this total is "
        "calculated here for confirmation.)_",
    ]

    warnings = getattr(result, "warnings", None) or []
    if warnings:
        lines.append(
            f"• :warning: {len(warnings)} warning(s) on the re-priced plan"
        )

    lines.append("")
    lines.append("_Confirm Commit writes this adjusted plan to Famly._")
    return "\n".join(lines)


def _spawn(work) -> None:
    """Run `work` off the request thread.

    A plain daemon thread, which is what gunicorn's sync workers can support
    without adding an async framework. Slack allows roughly three seconds to
    acknowledge a submission, and a Famly re-price can exceed that, so the ack
    must not wait for it.

    Caveat: a worker restarted mid-flight loses the in-progress re-price. The
    approver then sees no Confirm Commit message and can simply adjust again --
    nothing has been committed, and the original preview is only superseded
    once the re-price has succeeded.
    """
    threading.Thread(target=work, daemon=True).start()


def handle_adjust_submission(
    preview_id, raw_adjustment, user, spawn=None
) -> dict | None:
    """Validate an adjustment, then re-price it in the background.

    Returns a Slack `response_action: errors` body to show inline in the modal,
    or None to close it.

    ORDERING MATTERS. Everything that can produce an inline error runs
    synchronously first -- the range check, the store lookup, the pricing group
    -- because Slack will only display an error in the response to this
    request. Only the Famly re-price and the Slack post happen afterwards, on a
    background thread, so the acknowledgement is never held up by a network
    call.

    NEVER COMMITS. On success the adjusted body is stored under a NEW
    preview_id and offered with a Confirm Commit button, so the write still
    goes through the ordinary Approve path and every guard on it.

    Args:
        spawn: how to run the background work. Defaults to a daemon thread;
            tests pass their own to run it inline or to control timing.
    """
    value, error = _parse_adjustment(raw_adjustment)
    if error is not None:
        logger.warning(
            "ADJUST REJECTED preview_id=%s user=%s: %s", preview_id, user, error
        )
        return slack.modal_error(error)

    stored = preview_store.get(preview_id or "")
    blocked = _blocking_status(stored)
    if blocked is not None:
        return slack.modal_error(blocked)

    pricing_group_id = stored.pricing_group_id
    if not pricing_group_id:
        # A preview stored before the pricing group was recorded, or a plan
        # that never reported one. Guessing would misprice the adjustment.
        return slack.modal_error(
            "This preview has no recorded pricing group, so an adjustment "
            "cannot be priced. Please re-run the preview and try again."
        )

    adjusted_body = _with_adjustment(stored.plan_body, pricing_group_id, value)

    # Validation is done; everything past here is network work. Ack first.
    (spawn or _spawn)(
        lambda: _complete_adjustment(
            preview_id, stored, adjusted_body, pricing_group_id, value, user
        )
    )
    return None


def _complete_adjustment(
    preview_id, stored, adjusted_body, pricing_group_id, value, user
) -> None:
    """Re-price, store and post -- off the request thread.

    Never raises: nothing is waiting on this, so an escaping exception would
    vanish into the thread and leave the approver watching for a message that
    never comes. Every failure is reported into the channel instead.
    """
    try:
        result = runner.preview(adjusted_body, stored.version)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.exception("ADJUST RE-PREVIEW FAILED preview_id=%s", preview_id)
        slack.post_notice(
            f"❌ Could not re-price the plan for adjustment "
            f"£{value:+.2f} (preview `{preview_id}`): {exc}\n"
            f"_Nothing was changed. The original preview can still be approved "
            f"or adjusted again._"
        )
        return

    estimate = getattr(result.plan, "monthly_estimate", None) if result.plan else None
    new_preview_id = str(uuid.uuid4())

    try:
        preview_store.save(
            new_preview_id,
            adjusted_body,
            stored.child_id,
            stored.version,
            site_code=stored.site_code,
            institution_id=stored.institution_id,
            pricing_group_id=pricing_group_id,
            monthly_estimate=estimate,
        )

        # Only now is the original replaced. Doing it earlier would strand the
        # plan unapprovable if the re-price failed.
        preview_store.mark(preview_id, preview_store.STATUS_SUPERSEDED)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ADJUST STORE FAILED preview_id=%s", preview_id)
        slack.post_notice(
            f"❌ Re-priced the plan but could not store it "
            f"(preview `{preview_id}`): {exc}\n"
            f"_Nothing was changed. Please adjust again._"
        )
        return

    before = stored.monthly_estimate

    # Which source answered for the pricing group. Retained because it confirms
    # the adjustment reached Famly against the group the plan is priced under,
    # which is worth being able to check independently of any figure.
    source = "stored"
    if result.plan is not None and hasattr(result.plan, "pricing_group_source"):
        _, source = result.plan.pricing_group_source()

    logger.info(
        "ADJUSTED preview_id=%s -> %s adjustment=%.2f base_estimate=%s -> %s "
        "(base_changed=%s, expected False) total_to_bill=%s pricing_group=%s "
        "(source=%s) user=%s",
        preview_id,
        new_preview_id,
        value,
        before,
        estimate,
        _base_estimate_changed(before, estimate),
        total_to_bill(estimate, value),
        pricing_group_id,
        source,
        user,
    )

    if not slack.post_adjusted(
        _adjusted_summary(value, before, estimate, result, pricing_group_id),
        new_preview_id,
    ):
        # The plan is re-priced and stored, but the approver cannot see it.
        # Name the preview_id so it is still traceable.
        logger.error(
            "ADJUST POST FAILED preview_id=%s new_preview_id=%s",
            preview_id,
            new_preview_id,
        )
        slack.post_notice(
            f"⚠\ufe0f Re-priced the plan (adjustment £{value:+.2f}) but "
            f"could not post the confirmation message. The adjusted preview is "
            f"`{new_preview_id}`."
        )
