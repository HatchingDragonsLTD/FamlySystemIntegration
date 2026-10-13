"""Keeping create_timeline chains from getting stuck.

A chain waits on a human (the Slack approval) and on a worker thread (the step
after a commit), and either can go missing: a preview expires unclicked, or the
worker restarts between "commit recorded" and "next plan posted". Nothing here
commits anything. Every function either closes a chain's RECORD, tells Slack, or
posts the next plan through the normal preview path -- and none of it deletes or
edits anything in Famly.

    expire_stale_chains   a chain whose pending preview expired unapproved ->
                          `expired`, Slack told, the child freed. Run at the
                          start of every create_timeline and by chain-sweep.
    notify_stuck_chains   a chain whose last commit was recorded more than
                          STUCK_AFTER_SECONDS ago with no next plan posted ->
                          one Slack notice. Run by chain-sweep.
    sweep                 both, for cron.
    status / cancel / resume   the operator commands (chain-status, chain-cancel,
                          chain-resume).

Importable and testable without Flask or argparse; the `chain_*.py` modules in
this package are the thin CLI wiring.
"""

import logging
from datetime import datetime, timezone

from actions.plan_write import preview_store
from actions.read_child_plans import runner as read_child_plans
from integrations import slack

from . import chains, engine

logger = logging.getLogger(__name__)

# How long a recorded commit may sit with nothing posted after it before
# chain-sweep says so.
STUCK_AFTER_SECONDS = 10 * 60


def _parse_time(text) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def expire_stale_chains() -> list[str]:
    """Expire every active chain whose pending preview expired unapproved.

    The chain is marked `expired` (which frees the child), and Slack is told
    which plans exist and which do not. A chain with no preview at all for its
    next plan is NOT expired here -- that is the stuck case, below.

    Never raises: it runs ahead of every create_timeline, which must not fail
    because of some other child's chain. Returns the chain ids it expired.
    """
    expired: list[str] = []
    for chain in chains.list_active():
        try:
            if chain.next_index >= len(chain.segments):
                continue
            stored = preview_store.latest_for_chain(chain.chain_id, chain.next_index)
            if stored is None or not stored.is_expired:
                continue
            where = engine.heading_text(chain.segments, chain.next_index)
            if engine.stop_chain(
                chain.chain_id,
                f"{where} expired without being approved or rejected.",
                status=chains.STATUS_EXPIRED,
                title="Timeline expired",
            ):
                expired.append(chain.chain_id)
        except Exception:  # noqa: BLE001 - one bad chain must not block the rest
            logger.exception("Could not expire chain_id=%s", chain.chain_id)
    return expired


# --------------------------------------------------------------------------- #
# Stuck between "commit recorded" and "next plan posted"
# --------------------------------------------------------------------------- #
def notify_stuck_chains(now: datetime | None = None) -> list[str]:
    """Post ONE notice for each chain stuck after a recorded commit.

    Stuck means: at least one plan committed, the last commit was recorded more
    than STUCK_AFTER_SECONDS ago, and nothing is waiting on an approver (no
    preview exists for the next plan, or every plan is committed yet the chain
    never completed). The notice is posted once per stuck point, however often
    this runs. Returns the chain ids notified.
    """
    now = now or datetime.now(timezone.utc)
    notified: list[str] = []

    for chain in chains.list_active():
        try:
            if chain.next_index == 0:
                continue
            last = chain.next_index - 1
            moment = _parse_time(
                chain.segments[last].get("committed_at") or chain.created_at
            )
            if moment is None or (now - moment).total_seconds() < STUCK_AFTER_SECONDS:
                continue
            if (
                chain.next_index < len(chain.segments)
                and preview_store.latest_for_chain(chain.chain_id, chain.next_index)
                is not None
            ):
                continue  # a preview exists: expiry's business, not this one's
            if not chains.note_stuck_notified(chain.chain_id, last):
                continue

            what = (
                f"{engine.heading_text(chain.segments, chain.next_index)} was never posted"
                if chain.next_index < len(chain.segments)
                else "every plan is committed but the chain was never completed"
            )
            slack.post_notice(
                f":warning: *Timeline stuck* — child `{chain.child_id}`, chain "
                f"`{chain.chain_id}`. "
                f"{engine._short_name(chain, last)} was committed "
                f"(`{chain.segments[last].get('plan_id')}`) over "
                f"{STUCK_AFTER_SECONDS // 60} minutes ago, but {what}. Run "
                f"`chain-resume {chain.child_id}` to carry on, or "
                f"`chain-cancel {chain.child_id}` to close the chain."
            )
            notified.append(chain.chain_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not check chain_id=%s for being stuck", chain.chain_id)
    return notified


def sweep(now: datetime | None = None) -> dict:
    """chain-sweep: expire stale chains, then flag stuck ones."""
    expired = expire_stale_chains()
    stuck = notify_stuck_chains(now)
    return {"expired": expired, "stuck_notified": stuck}


# --------------------------------------------------------------------------- #
# Operator commands
# --------------------------------------------------------------------------- #
def _pending_preview(chain: chains.Chain):
    if chain.next_index >= len(chain.segments):
        return None
    return preview_store.latest_for_chain(chain.chain_id, chain.next_index)


def status(child_id: str | None = None) -> dict:
    """chain-status: the active chains, optionally for one child."""
    rows = []
    for chain in chains.list_active():
        if child_id and chain.child_id != child_id:
            continue
        pending = _pending_preview(chain)
        rows.append(
            {
                "chainId": chain.chain_id,
                "childId": chain.child_id,
                "created": chain.created_at,
                "committed": chain.next_index,
                "total": len(chain.segments),
                "waitingOn": (
                    engine.heading_text(chain.segments, chain.next_index)
                    if chain.next_index < len(chain.segments)
                    else None
                ),
                "pendingPreviewId": pending.preview_id if pending else None,
                "pendingPreviewStatus": pending.status if pending else None,
                "planIds": [s.get("plan_id") for s in chain.segments if s.get("plan_id")],
            }
        )
    return {"chains": rows, "count": len(rows)}


def cancel(child_id: str) -> dict:
    """chain-cancel: close the child's active chain record.

    Reads the child's plans from Famly and posts them, so the channel sees what
    exists. DELETES NOTHING: no plan is touched. The chain's own pending preview
    is marked rejected locally, so it cannot later be approved into a plan the
    chain no longer knows about.
    """
    chain = chains.find_active(child_id)
    if chain is None:
        return {"cancelled": False, "reason": f"child {child_id} has no active chain"}

    try:
        plans = read_child_plans.run(child_id, version=engine.PLAN_VERSION).plans
        famly_lines = ["", "*Plans in Famly for this child right now:*"] + (
            [
                f"• `{p.id}`: from {p.from_ or 'unknown'} to {p.to or 'open-ended'}"
                for p in plans
            ]
            or ["• none"]
        )
    except Exception as exc:  # noqa: BLE001 - cancelling must not depend on Famly
        famly_lines = ["", f"_Could not read the child's plans from Famly: {type(exc).__name__}_"]

    pending = _pending_preview(chain)
    if pending is not None and pending.status in (
        preview_store.STATUS_PENDING,
        preview_store.STATUS_EXPIRED,
    ):
        preview_store.mark(pending.preview_id, preview_store.STATUS_REJECTED)

    engine.stop_chain(
        chain.chain_id,
        "cancelled by an operator (chain-cancel). Nothing in Famly was changed or deleted.",
        extra_lines=famly_lines,
        status=chains.STATUS_CANCELLED,
        title="Timeline cancelled",
    )
    return {"cancelled": True, "chainId": chain.chain_id, "committed": chain.next_index}


def resume(child_id: str) -> dict:
    """chain-resume: post the next plan of a chain that has stopped moving.

    Refuses -- changing nothing -- when the child has no active chain, no plan
    has been committed yet, the next plan's preview is already waiting on an
    approver, the last completed plan is not in Famly, or it is not the child's
    latest-starting plan. Otherwise the next segment is posted through the
    normal path, naming the last completed plan as its `oldPlanId`.
    """
    chain = chains.find_active(child_id)
    if chain is None:
        return {"resumed": False, "reason": f"child {child_id} has no active chain"}

    if chain.next_index == 0:
        return {
            "resumed": False,
            "reason": "no plan has been committed yet, so there is nothing to resume "
            "from; chain-cancel it and start again",
        }

    pending = _pending_preview(chain)
    if pending is not None and pending.status in (
        preview_store.STATUS_PENDING,
        preview_store.STATUS_COMMITTING,
    ):
        return {
            "resumed": False,
            "reason": f"the next plan's preview `{pending.preview_id}` is already pending approval",
        }

    last_id = chain.segments[chain.next_index - 1].get("plan_id")
    plans = read_child_plans.run(child_id, version=engine.PLAN_VERSION).plans
    last = engine._find(plans, last_id)
    if last is None:
        return {
            "resumed": False,
            "reason": f"the last completed plan `{last_id}` is not in the child's plans in Famly",
        }

    latest = max(plans, key=lambda p: engine._day(p.from_) or engine.date.min)
    if latest.id != last_id:
        return {
            "resumed": False,
            "reason": f"the last completed plan `{last_id}` is not the child's "
            f"latest-starting plan (`{latest.id}` starts {latest.from_})",
        }

    if chain.next_index >= len(chain.segments):
        chains.finish(chain.chain_id, chains.STATUS_COMPLETE)
        slack.post_notice(
            f":white_check_mark: All {len(chain.segments)} plans created for child "
            f"`{chain.child_id}` (chain `{chain.chain_id}`)."
        )
        return {"resumed": True, "completed": True, "chainId": chain.chain_id}

    data, http_status = engine.present_segment(chain, chain.next_index, old_plan_id=last_id)
    return {
        "resumed": http_status == 200,
        "chainId": chain.chain_id,
        "index": chain.next_index,
        "previewId": data.get("preview_id"),
        "status": http_status,
    }
