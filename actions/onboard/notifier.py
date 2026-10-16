"""Slack messages for the onboarding job. NO NAMES, emails, phones, addresses or
dates of birth: a deal id, a link, codes, counts and presence flags only.

`SlackNotifier` is the real one; the worker takes any object with the same two
methods, so tests pass a fake and scan what it was handed.
"""

import logging

from integrations import slack

from . import approval, config

logger = logging.getLogger(__name__)


def gate_text(
    plan: dict,
    plan_hash: str,
    mode: str,
    plan_changed: bool = False,
    this_job_child_id: str | None = None,
) -> str:
    """The Gate A message body for a plan. `this_job_child_id` is the child this
    very job created (if any), to tell `created by this job` from `created by
    pipeline` (a child an EARLIER job created)."""
    deal_id = plan["dealId"]
    link = config.deal_link(deal_id)
    contacts = plan.get("contacts") or []
    child = plan.get("child") or {}
    creating = sum(1 for c in contacts if c.get("status") == "would_create")
    present = sum(1 for c in contacts if c.get("status") == "present")
    bill_payer = any(c.get("isBillPayer") for c in contacts)
    dates = plan.get("dates") or {}

    lines = []
    if mode == config.MODE_DRY:
        lines.append("*[DRY RUN] Onboarding: would create the child and contacts in Famly*")
    else:
        lines.append("*Onboarding approval: create the child and contacts in Famly*")
    if plan_changed:
        lines.append("⚠️ The plan changed since it was last approved; approval is needed again.")
    lines.append(f"Deal: <{link}|{deal_id}>" if link else f"Deal: {deal_id} (set HUBSPOT_PORTAL_ID for a link)")
    if dates.get("lastDayDefaulted") and child.get("status") == "child_would_create":
        end_text = "defaulted (31 August after the 4th birthday)"
    elif dates.get("lastDayDefaulted"):
        end_text = "none on the deal"          # an existing child: no end date is sent
    else:
        end_text = "present" if dates.get("lastDayPresent") else "none"
    lines.append(
        f"Site: {plan.get('siteCode')} · Room: {plan.get('groupTitle')} · "
        f"Start date: {'present' if dates.get('firstDayPresent') else 'missing'} · "
        f"End date: {end_text}"
    )
    if child.get("status") == "child_would_create":
        child_state = "would be created"
    elif child.get("origin") == "created_by_pipeline":
        mine = this_job_child_id and child.get("famlyChildId") == this_job_child_id
        child_state = "created by this job" if mine else "created by pipeline"
    else:
        child_state = "already in Famly"
    child_writes = 1 if child.get("status") == "child_would_create" else 0
    lines.append(
        f"Famly writes planned: {child_writes} child, {creating} contact(s)"
    )
    lines.append(
        f"Child: {child_state} · Contacts: {len(contacts)} ({creating} to create, "
        f"{present} present) · Bill payer identified: {'yes' if bill_payer else 'no'}"
    )
    warnings = sorted(set(plan.get("warnings") or []))
    lines.append("Warnings: " + (", ".join(warnings) if warnings else "none"))
    lines.append(f"Plan: `{plan_hash[:approval.HASH_PREFIX]}`")
    return "\n".join(lines)


def notice_text(deal_id: str, headline: str, codes) -> str:
    """A plain notice: deal id, a headline, and codes."""
    link = config.deal_link(deal_id)
    deal = f"<{link}|{deal_id}>" if link else str(deal_id)
    codes = [str(c) for c in codes if c]
    return f"Onboarding deal {deal}: {headline}" + (f" [{', '.join(codes)}]" if codes else "")


class SlackNotifier:
    """Posts through integrations.slack. Never raises."""

    def gate(self, text: str, job_id: str, plan_hash: str) -> str | None:
        """Post the Approve / Reject request; returns the message ref or None."""
        return slack.post_onboard_gate(text, approval.button_value(job_id, plan_hash))

    def notice(self, text: str) -> bool:
        return slack.post_notice(text)
