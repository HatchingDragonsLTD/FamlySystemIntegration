"""Slack messages for the onboarding job. NO NAMES, emails, phones, addresses or
dates of birth: a deal id, a link, codes, counts and presence flags only.

`SlackNotifier` is the real one; the worker takes any object with the same two
methods, so tests pass a fake and scan what it was handed.
"""

import logging

from integrations import slack

from . import approval, config, messages

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
    # The count equals the writes the worker will make: the bill payer create or attach
    # is one write; a present or flagged payer is none.
    bp_section = plan.get("billPayer")
    bill_payer_writes = 1 if isinstance(bp_section, dict) and bp_section.get("status") in (
        "would_create", "would_attach") else 0
    lines.append(
        f"Famly writes planned: {child_writes} child, {creating} contact(s)"
        + (f", {bill_payer_writes} bill payer" if isinstance(bp_section, dict) else "")
    )
    lines.append(
        f"Child: {child_state} · Contacts: {len(contacts)} ({creating} to create, "
        f"{present} present) · Bill payer identified: {'yes' if bill_payer else 'no'}"
    )
    bill_payer = plan.get("billPayer")
    if isinstance(bill_payer, dict):
        status = bill_payer.get("status")
        lines.append("Bill payer: " + {
            "would_create": "would be created in Famly",
            "would_attach": "an existing bill payer would be linked to the child",
            "present": "already linked to the child in Famly",
        }.get(status, "needs a person (see the warnings)")
            + (f" · different details: {', '.join(bill_payer['differs'])}" if bill_payer.get("differs") else ""))
    shown = messages.classify(plan.get("warnings") or [])
    sentences = shown[messages.NOT_DONE] + shown[messages.CHECK]
    lines.append("Warnings: " + (" ".join(sentences) if sentences else "none"))
    lines.append(f"Plan: `{plan_hash[:approval.HASH_PREFIX]}`")
    return "\n".join(lines)


def deal_ref(deal_id: str) -> str:
    link = config.deal_link(deal_id)
    return f"<{link}|{deal_id}>" if link else str(deal_id)


def notice_text(deal_id: str, headline: str, codes, extra_footer=()) -> str:
    """The staff notice: a headline, plain-English "Not done" and "To check" lists,
    and a small code footer. Configuration (admin) codes are NOT in it: see
    `admin_notice_text`."""
    return messages.staff_text(deal_ref(deal_id), headline, codes, extra_footer)


def admin_notice_text(deal_id: str, codes) -> str | None:
    """The one separate admin line for configuration problems, or None."""
    return messages.admin_text(deal_ref(deal_id), codes)


class SlackNotifier:
    """Posts through integrations.slack. Never raises."""

    def gate(self, text: str, job_id: str, plan_hash: str, nonce: str) -> str | None:
        """Post the Approve / Reject request; returns the message ref or None."""
        return slack.post_onboard_gate(text, approval.button_value(job_id, plan_hash, nonce))

    def notice(self, text: str) -> bool:
        return slack.post_notice(text)
