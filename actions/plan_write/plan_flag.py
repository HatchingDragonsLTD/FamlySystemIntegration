"""The server owns the deal's plan status: after a plan is COMMITTED to Famly (an Approve click that passed
every guard in approval.py, `runner.commit` returned) and a live read of the child's plans shows it there,
the deal gets `famly_plan_status = plan_created` with `famly_plan_note` cleared (`status_sync`), which also
overwrites an earlier `needs_attention`. A plan added alongside another (`old_plan_id`) writes it too.

While the old property exists, `PLAN_FLAG_LEGACY_WRITE` (on by default; only the value `off` switches it off)
ALSO sets `famly_plan_uploaded = true` through `write_plan_uploaded`, for a first plan only (an additional plan
leaves the old flag alone, as before). Delete the old property, then set the switch to off.

  * the old flag's live value is read first and nothing is written when it is already true; the status is read
    first too and nothing is written when it already says plan_created with no note;
  * a preview with no deal id (the webhook did not send one) sets nothing (`no_deal_id`) and says so in Slack;
  * the live read is tried up to five times (waits of 1, 2, 4 and 8 seconds, in the background thread); a
    plan still not there writes nothing (`commit_not_confirmed`) and says so in Slack;
  * the old flag is only ever set to "true" here, never "false"; a plan that cannot be confirmed writes
    `needs_attention` with the note `commit_unknown` (a later success overwrites it);
  * a rejected, refused, failed or in-progress commit never reaches this module;
  * a HubSpot problem NEVER undoes or retries the Famly commit: it is a code in the log and one Slack
    line (codes and ids only). The reconcile mode of `hubspot-set-plan-uploaded --from-famly` repairs it.

Never raises. Returns a short code.
"""

import logging
import os
import time

from actions.read_child_plans import runner as read_child_plans
from core.hubspot import HubSpotDealWriter, HubSpotError
from integrations import slack

from . import status_codes, status_sync

logger = logging.getLogger(__name__)

SET, UNCHANGED = "plan_flag_set", "plan_flag_unchanged"
SKIPPED_ADDITIONAL, NO_DEAL_ID, NOT_CONFIRMED = (
    "plan_flag_skipped_additional_plan", "plan_flag_no_deal_id", "plan_flag_commit_not_confirmed")
HUBSPOT_FAILED = "plan_flag_hubspot_failed"

# The live read of the child's plans can lag the commit (Famly's read model), so it is tried again after
# each of these waits, in the background thread only (reads only; never the approval response).
READ_WAITS = (1, 2, 4, 8)
_sleep = time.sleep


LEGACY_ENV = "PLAN_FLAG_LEGACY_WRITE"


def legacy_write_enabled(environ=None) -> bool:
    """PLAN_FLAG_LEGACY_WRITE: on (the default) also writes famly_plan_uploaded=true; only `off` disables it."""
    value = (environ if environ is not None else os.environ).get(LEGACY_ENV, "")
    return value.strip().lower() != "off"


def _confirmed(claimed, plan_id) -> bool:
    """A live read of the child's plans shows the committed plan (by id when known, else any plan)."""
    plans = read_child_plans.run(claimed.child_id, version=claimed.version).plans
    ids = {str(getattr(p, "id", "")) for p in plans}
    return str(plan_id) in ids if plan_id else bool(plans)


def _confirmed_with_retries(claimed, plan_id) -> bool:
    """`_confirmed`, tried again after each wait in READ_WAITS (1, 2, 4, 8 seconds: at most five reads).
    A read that raises is logged at WARNING with the error type and counts as "not yet"; it never passes."""
    for attempt in range(len(READ_WAITS) + 1):
        try:
            if _confirmed(claimed, plan_id):
                return True
        except Exception as exc:  # noqa: BLE001 - an unmade check is "not confirmed", never a pass
            logger.warning("PLAN FLAG plan read failed preview_id=%s attempt=%d error=%s",
                           getattr(claimed, "preview_id", None), attempt + 1, type(exc).__name__)
        if attempt < len(READ_WAITS):
            _sleep(READ_WAITS[attempt])
    return False


def _tell(code: str, claimed, deal_id, notify) -> None:
    """One Slack line for a flag that was not set: the code and an id (the deal's, else the preview's)."""
    which = f"deal {deal_id}" if deal_id else f"preview {getattr(claimed, 'preview_id', None)}"
    try:
        (notify or slack.post_notice)(
            f"famly_plan_uploaded was not set for {which} (code {code}). The plan is in Famly; run "
            "hubspot-set-plan-uploaded --from-famly to repair the flag.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("PLAN FLAG notice failed (%s)", type(exc).__name__)


def after_commit(claimed, plan_id, writer=None, notify=None) -> str:
    """Set the deal's flag after a confirmed commit. See the module text."""
    deal_id = getattr(claimed, "deal_id", None)
    try:
        if not deal_id:
            _tell(NO_DEAL_ID, claimed, deal_id, notify)
            return _done(NO_DEAL_ID, claimed, deal_id)
        writer = writer or HubSpotDealWriter()
        if not _confirmed_with_retries(claimed, plan_id):
            _tell(NOT_CONFIRMED, claimed, deal_id, notify)
            status_sync.attention_now(deal_id, status_codes.COMMIT_UNKNOWN, writer, notify)
            return _done(NOT_CONFIRMED, claimed, deal_id)
        wrote, _codes = status_sync.PlanStatusSyncer(writer, notify).mark_plan_created(deal_id)
        if legacy_write_enabled() and not getattr(claimed, "old_plan_id", None):
            if (writer.read_plan_uploaded(deal_id) or "").strip().lower() != "true":
                writer.write_plan_uploaded(deal_id, "true")        # one PATCH, never retried here
                wrote = True
        return _done(SET if wrote else UNCHANGED, claimed, deal_id)
    except Exception as exc:  # noqa: BLE001 - nothing here may reach the Famly result
        code = HUBSPOT_FAILED
        logger.error("PLAN FLAG %s preview_id=%s deal=%s error=%s", code,
                     getattr(claimed, "preview_id", None), deal_id, type(exc).__name__)
        try:
            (notify or slack.post_notice)(f"famly_plan_uploaded was not set for deal {deal_id} (code {code}). "
                   "The plan is in Famly; run hubspot-set-plan-uploaded --from-famly to repair the flag.")
        except Exception:  # noqa: BLE001
            pass
        return code


def report_failed_commit(claimed, detail) -> None:
    """A direct (non-chain) commit failed: write needs_attention with a note CODE (never the error text).
    Called, off the request thread, by approval's `_report_plan_attention`. Never raises."""
    try:
        status_sync.attention_now(getattr(claimed, "deal_id", None), status_codes.commit_failed(detail))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PLAN STATUS commit-failure hook error=%s", type(exc).__name__)


def _done(code: str, claimed, deal_id) -> str:
    logger.info("PLAN FLAG %s preview_id=%s child_id=%s deal=%s", code, getattr(claimed, "preview_id", None),
                getattr(claimed, "child_id", None), deal_id)
    return code
