"""The server owns `famly_plan_uploaded`: after a plan is COMMITTED to Famly (an Approve click that
passed every guard in approval.py, `runner.commit` returned) and a live read of the child's plans shows
it there, the deal's flag is set to true through the one HubSpot writer (`write_plan_uploaded`).

  * only a first plan sets it: a plan added alongside another (`old_plan_id`) leaves the flag alone;
  * the live value is read first and nothing is written when it is already true;
  * a preview with no deal id (the webhook did not send one) sets nothing (`no_deal_id`);
  * a rejected, refused, failed or in-progress commit never reaches this module;
  * a HubSpot problem NEVER undoes or retries the Famly commit: it is a code in the log and one Slack
    line (codes and ids only). The reconcile mode of `hubspot-set-plan-uploaded --from-famly` repairs it.

Never raises. Returns a short code.
"""

import logging

from actions.read_child_plans import runner as read_child_plans
from core.hubspot import HubSpotDealWriter, HubSpotError
from integrations import slack

logger = logging.getLogger(__name__)

SET, UNCHANGED = "plan_flag_set", "plan_flag_unchanged"
SKIPPED_ADDITIONAL, NO_DEAL_ID, NOT_CONFIRMED = (
    "plan_flag_skipped_additional_plan", "plan_flag_no_deal_id", "plan_flag_commit_not_confirmed")
HUBSPOT_FAILED = "plan_flag_hubspot_failed"


def _confirmed(claimed, plan_id) -> bool:
    """A live read of the child's plans shows the committed plan (by id when known, else any plan)."""
    plans = read_child_plans.run(claimed.child_id, version=claimed.version).plans
    ids = {str(getattr(p, "id", "")) for p in plans}
    return str(plan_id) in ids if plan_id else bool(plans)


def after_commit(claimed, plan_id, writer=None, notify=None) -> str:
    """Set the deal's flag after a confirmed commit. See the module text."""
    deal_id = getattr(claimed, "deal_id", None)
    try:
        if getattr(claimed, "old_plan_id", None):
            return _done(SKIPPED_ADDITIONAL, claimed, deal_id)
        if not deal_id:
            return _done(NO_DEAL_ID, claimed, deal_id)
        try:
            confirmed = _confirmed(claimed, plan_id)
        except Exception:  # noqa: BLE001 - an unmade check is "not confirmed", never a pass
            confirmed = False
        if not confirmed:
            return _done(NOT_CONFIRMED, claimed, deal_id)
        writer = writer or HubSpotDealWriter()
        if (writer.read_plan_uploaded(deal_id) or "").strip().lower() == "true":
            return _done(UNCHANGED, claimed, deal_id)
        writer.write_plan_uploaded(deal_id, "true")        # one PATCH, never retried here
        return _done(SET, claimed, deal_id)
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


def _done(code: str, claimed, deal_id) -> str:
    logger.info("PLAN FLAG %s preview_id=%s child_id=%s deal=%s", code, getattr(claimed, "preview_id", None),
                getattr(claimed, "child_id", None), deal_id)
    return code
