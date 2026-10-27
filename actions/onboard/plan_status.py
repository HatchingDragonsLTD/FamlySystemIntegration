"""The per-stage STATUS BLOCK for a plan: one line each for the child, every contact, the bill
payer and anything held back by the deal's stage, then what will be written (or that nothing
will). Used by the Gate A message and the dry-run output.

Built only from fields of the plan that are in its hash (child status, via, origin and test flag,
each contact's status, matchedBy and relation, the bill payer status), so the hash changes
whenever a line would. Ids, counts and codes only; every sentence comes from `messages.state`."""

from actions.onboard import messages

_CHILD_VIA = {"external_system_reference": "child_found_reference", "urn": "child_found_urn",
              "famly_childid": "child_found_famly_childid"}
_CONTACT_FLAG_CODES = {"no_email": "contact_no_email", "ambiguous_on_child": "contact_ambiguous_on_child"}


def planned_writes(plan: dict) -> dict:
    """The Famly writes the worker will make for this plan: child, contact creates, contact
    relation updates, bill payer (create or attach is one write; present or flagged is none)."""
    bill_payer = plan.get("billPayer")
    return {
        "child": 1 if (plan.get("child") or {}).get("status") == "child_would_create" else 0,
        "contact": sum(1 for c in plan.get("contacts") or [] if c.get("status") == "would_create"),
        "relation": sum(1 for c in plan.get("contacts") or [] if c.get("relation") == "would_add"),
        "bill_payer": 1 if isinstance(bill_payer, dict)
        and bill_payer.get("status") in ("would_create", "would_attach") else 0,
        # The bill payer's contact as an invoice recipient: one write, whatever the payer decision.
        "recipient": 1 if isinstance(bill_payer, dict) and bill_payer.get("recipient") == "will_add" else 0,
    }


def _child_line(plan: dict, this_job_child_id: str | None) -> str:
    child = plan.get("child") or {}
    status = child.get("status")
    if status == "child_would_create":
        return messages.state("child_would_create_test" if child.get("testChild") else "child_would_create")
    if status == "child_ambiguous":
        return messages.state("child_ambiguous")
    if status == "child_unresolved":
        return messages.state("child_unresolved")
    if child.get("origin") == "created_by_pipeline":
        mine = this_job_child_id and child.get("famlyChildId") == this_job_child_id
        return messages.state("child_created_by_this_job" if mine else "child_created_earlier")
    return messages.state(_CHILD_VIA.get(child.get("via"), "child_found_reference"))


def _contact_line(entry: dict, position: int = 1, total: int = 1, child_found: bool = False) -> str:
    fields = {"id": entry.get("hubspotContactId"), "role": " (bill payer)" if entry.get("isBillPayer") else ""}
    status = entry.get("status")
    if status == "would_create" and child_found:
        return messages.state("contact_missing_on_existing_child", n=position, total=total)
    if status == "would_create":
        return messages.state("contact_would_create", **fields)
    if status == "present":
        if entry.get("relation") == "would_add":
            return messages.state("contact_relation_will_add", **fields)
        if entry.get("relation") == "unreadable":
            return messages.state("contact_relation_unreadable", **fields)
        by = entry.get("matchedBy")
        return messages.state(
            "contact_present_reference" if by == "reference"
            else "contact_present_additional_email" if by == "additional_email" else "contact_present_email",
            **fields)
    if status == "child_unresolved":
        return messages.state("contact_not_checked", **fields)
    found = messages.lookup(_CONTACT_FLAG_CODES.get(status, status or ""))
    return messages.state("contact_flagged", why=found[1] if found else messages.UNKNOWN_SENTENCE, **fields)


def _bill_payer_lines(section: dict) -> list[str]:
    status = section.get("status")
    if status in ("would_create", "would_attach"):
        lines = [messages.state("bill_payer_would_create" if status == "would_create" else "bill_payer_would_attach")]
    elif status == "present":
        lines = [messages.state("bill_payer_present")]
    else:
        found = messages.lookup(section.get("flag") or "")
        lines = [messages.state("bill_payer_flagged", why=found[1] if found else messages.UNKNOWN_SENTENCE)]
    if section.get("differs"):
        lines.append(messages.state("bill_payer_differs", fields=", ".join(section["differs"])))
    recipient = {"will_add": "bill_payer_recipient_will_add", "already_linked": "bill_payer_recipient_linked",
                 "contact_not_found": "bill_payer_recipient_contact_not_found"}.get(section.get("recipient"))
    if recipient:
        lines.append(messages.state(recipient))
    return lines


def _all_settled(plan: dict) -> bool:
    """True when nothing in the plan needs a person: no hold, and every part is present or
    would be written."""
    if plan.get("holds") or (plan.get("child") or {}).get("status") not in ("child_found", "child_would_create"):
        return False
    if plan.get("scope") == "child_only":
        return True
    if any(c.get("status") not in ("present", "would_create") or c.get("relation") == "unreadable"
           for c in plan.get("contacts") or []):
        return False
    section = plan.get("billPayer")
    if isinstance(section, dict) and section.get("recipient") == "contact_not_found":
        return False
    return not isinstance(section, dict) or section.get("status") in (
        "would_create", "would_attach", "present", "skipped_stage")


def block(plan: dict, this_job_child_id: str | None = None) -> list[str]:
    """The status lines for a plan, in a fixed order, ending with the writes sentence when no
    write is planned."""
    lines = [_child_line(plan, this_job_child_id)]
    if plan.get("scope") == "child_only":
        stage = (plan.get("stage") or {}).get("name") or "a child-only stage"
        lines.append(messages.state("scope_child_only", stage=stage))
    else:
        contacts = plan.get("contacts") or []
        child_found = (plan.get("child") or {}).get("status") == "child_found"
        lines += [_contact_line(c, n, len(contacts), child_found) for n, c in enumerate(contacts, start=1)]
        other = plan.get("childOtherContacts")
        if other and other.get("count"):
            lines.append(messages.state("child_other_contacts", k=other["count"]))
            if other.get("candidateIds"):
                lines.append(messages.state("child_other_contacts_candidates",
                                            ids=", ".join(other["candidateIds"])))
        section = plan.get("billPayer")
        if isinstance(section, dict) and section.get("status") != "skipped_stage":
            lines += _bill_payer_lines(section)
    if sum(planned_writes(plan).values()) == 0:
        lines.append(messages.state("nothing_to_write" if _all_settled(plan) else "no_writes_planned"))
    return lines
