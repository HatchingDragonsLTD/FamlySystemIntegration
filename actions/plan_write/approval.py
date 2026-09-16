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

import logging

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


def _commit_refused(detail: str) -> str:
    return f"⚠️ Commit refused: {detail}"


def _commit_failed(detail: str) -> str:
    return f"❌ Commit failed: {detail}"


def handle_click(action_id: str, preview_id: str | None, user: str | None) -> str:
    """Act on a verified Slack button click and return the message to show.

    Args:
        action_id: `plan_approve` or `plan_reject` (already validated upstream).
        preview_id: the button's value -- the preview to act on.
        user: who clicked, for the audit log.

    Returns:
        Text to replace the Slack message with. Never raises: a failure is
        reported to the approver rather than thrown at Slack, which would show
        them nothing.
    """
    if action_id == slack.ACTION_REJECT:
        return _reject(preview_id, user)
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
