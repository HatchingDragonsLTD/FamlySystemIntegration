"""Plain-English wording for every code the onboarding pipeline can emit.

Staff read the Slack messages, so a code never appears as the message: each code
maps to ONE sentence (what is wrong and what to do), and the message lists the
sentences under "To check" (the work is done, look at this) and "Not done"
(something did not happen). A short code footer stays for debugging.

Three kinds of code:
  * CHECK     the work is done; a person should look.
  * NOT_DONE  something did not happen; a person has to do it or fix the deal.
  * ADMIN     a configuration or HubSpot-property problem, not a staff problem:
              logged and sent as ONE separate admin line, never in the staff list.
  * INFO      nothing to tell staff (it appears only in the footer).

`lookup(code)` is the single entry point. Codes with a parameter
(`missing:famly_sync_status`, `child_http_400`) match by prefix or pattern. A code
that has no translation falls back to a generic sentence, and the test in
tests/test_onboard_messages.py fails if any code the pipeline emits does.

No message carries personal data: ids, counts and fixed sentences only.
"""

import re

CHECK = "check"
NOT_DONE = "not_done"
ADMIN = "admin"
INFO = "info"

# code -> (kind, sentence)
_STATIC: dict[str, tuple[str, str]] = {
    # ---- holds: the plan stopped, nothing was created ---------------------------
    "deal_not_allowlisted": (NOT_DONE, "This deal is not enabled for automatic onboarding yet, so nothing was created."),
    "stage_not_eligible": (NOT_DONE, "The deal is not in the Operations pipeline at Deposit Pending, so nothing was created."),
    "deal_not_found": (NOT_DONE, "The deal could not be found in HubSpot."),
    "site_unmapped": (NOT_DONE, "The nursery on the deal (Location of interest) is not one we recognise. Fix it on the deal."),
    "site_id_missing": (NOT_DONE, "The nursery has no Famly ID set up. Ask an admin to add it."),
    "group_missing": (NOT_DONE, "No room could be worked out for this child's age and nursery. Ask an admin."),
    "group_ambiguous": (NOT_DONE, "More than one room fits this child's age and nursery, so none was chosen. Ask an admin."),
    "dob_missing": (NOT_DONE, "The child's date of birth is missing on the deal."),
    "start_date_missing": (NOT_DONE, "The start date is missing on the deal."),
    "date_invalid": (NOT_DONE, "A date on the deal could not be read. Check the date of birth, start date and end date."),
    "dates_inconsistent": (NOT_DONE, "The start date is before the child's date of birth. Check both dates on the deal."),
    "end_date_before_start": (NOT_DONE, "The end date is before the start date. Check both dates on the deal."),
    "end_date_missing": (NOT_DONE, "No end date could be worked out. Add an end date or the date of birth on the deal."),
    "child_name_incomplete": (NOT_DONE, "The child's first or last name is missing on the deal."),
    "child_possible_duplicate": (NOT_DONE, "A child with the same date of birth and a matching name already exists at this nursery. Check Famly before running again."),
    "child_ambiguous": (NOT_DONE, "More than one child in Famly matches this deal. Check Famly."),
    "famly_childid_not_found": (NOT_DONE, "The Famly child ID saved on the deal is not a child in Famly. Check the ID on the deal."),
    "contact_no_email": (NOT_DONE, "A parent on the deal has no email address, so they were not added."),
    "contact_name_incomplete": (NOT_DONE, "A parent's first or last name is missing in HubSpot, so they were not added."),
    "contact_ambiguous_on_child": (NOT_DONE, "A parent's email matches more than one contact on the child in Famly. Check the child's contacts."),
    "ambiguous_on_child": (NOT_DONE, "A parent's email matches more than one contact on the child in Famly. Check the child's contacts."),
    "contact_unmatched_on_existing_child": (NOT_DONE, "A parent from the deal is not on the child in Famly, and was not added because the child already existed. Add them in Famly if needed."),
    "bill_payer_unclear": (NOT_DONE, "The deal does not have exactly one bill payer. Fix the Bill Payer label on the contacts."),
    # ---- the job stopped or a step failed ---------------------------------------
    "plan_read_failed": (NOT_DONE, "The deal could not be read from HubSpot or Famly. Try again later."),
    "plan_unstable": (NOT_DONE, "The deal's details kept changing between checks, so approval was not asked for a third time. Check the deal, then run it again."),
    "create_outcome_unknown": (NOT_DONE, "It is not clear whether Famly created this. Check Famly for the deal ID before running again."),
    "duplicate_create_blocked": (NOT_DONE, "A second attempt to create the child was blocked. Check Famly for the deal ID before running again."),
    "readback_child_lookup_failed": (NOT_DONE, "The child was created but could not be found again by the deal ID. Check Famly."),
    "readback_failed": (NOT_DONE, "A parent was sent to Famly but could not be found on the child afterwards. Check the child's contacts."),
    "contact_create_unconfirmed": (NOT_DONE, "An earlier attempt to add this parent was never confirmed and the parent is not showing on the child, so they were not added again. Check the child's contacts in Famly, add the parent by hand if missing, then run the job again."),
    "urn_site_code_unknown": (NOT_DONE, "This site has no URN code yet, so no child URN could be made and nothing was created. Ask an admin to add the site."),
    "child_possible_returning": (NOT_DONE, "A child with the same name and date of birth may already exist in Famly or on another deal (their URNs are in the dry-run plan), so no new URN was made. If it is the same child, put their existing URN on this deal and run it again; if it is a different child, run onboard-enqueue with --new-urn."),
    "urn_hubspot_write_failed": (NOT_DONE, "The new child URN could not be saved on the HubSpot deal, so nothing was created in Famly. The same URN is kept for this deal. Run the job again."),
    "urn_sequence_exhausted": (NOT_DONE, "The site has used all its child URN numbers. Ask an admin."),
    "urn_allocated": (INFO, ""),
    "urn_readback_mismatch": (CHECK, "The new child URN could not be confirmed on the Famly child. Check the child's URN in Famly."),
    "relation_not_added": (CHECK, "Famly refused to set a parent's relation to Parent on a contact already on the child. Set it in Famly by hand."),
    "relation_unconfirmed": (CHECK, "A parent's relation was sent to Famly but could not be confirmed afterwards. Check the parent's relation in Famly."),
    "relation_unreadable": (CHECK, "The relation of a parent already on the child could not be read, so it was not changed. Check it in Famly."),
    "relation_added": (INFO, ""),
    "contact_error": (NOT_DONE, "A parent could not be added because of an unexpected error. Add them in Famly by hand."),
    "worker_error": (NOT_DONE, "An unexpected error stopped the job. Check Famly for the deal ID before running again."),
    "interrupted": (NOT_DONE, "The job was interrupted part-way. Check Famly for the deal ID before running again."),
    "approval_rejected": (NOT_DONE, "The approval was rejected, so nothing was created."),
    "approval_request_not_posted": (NOT_DONE, "The approval request could not be posted to Slack, so nothing was created."),
    "hubspot_writeback_failed": (NOT_DONE, "The child was created in Famly, but the Famly child ID or the default end date could not be saved on the HubSpot deal. Add it to the deal by hand."),
    "child_graphql_error": (NOT_DONE, "Famly refused to create the child, so nothing was created. Ask an admin."),
    "contact_graphql_error": (NOT_DONE, "Famly refused to add a parent. Add them in Famly by hand."),
    # ---- the bill payer stage (ONBOARD_BILL_PAYER=on) ----------------------------
    "bill_payer_created": (INFO, ""),
    "bill_payer_attached": (INFO, ""),
    "bill_payer_present": (INFO, ""),
    "bill_payer_ambiguous": (NOT_DONE, "More than one live bill payer in Famly has this parent's email, so none was created or linked. Check Famly."),
    "bill_payer_no_contact": (NOT_DONE, "The deal does not have exactly one contact with the Bill Payer label, so no bill payer was set up."),
    "bill_payer_no_email": (NOT_DONE, "The Bill Payer contact has no email address, so no bill payer was set up."),
    "bill_payer_child_linked_elsewhere": (NOT_DONE, "The child is already linked to a different bill payer in Famly, so no bill payer was added, to avoid billing twice. Check Famly."),
    "bill_payer_plan_changed": (NOT_DONE, "The bill payer situation in Famly changed after approval, so nothing was written for the bill payer. Run the job again."),
    "bill_payer_outcome_unknown": (NOT_DONE, "It is not clear whether Famly set up the bill payer. Check the child's bill payer in Famly before running again."),
    "bill_payer_readback_mismatch": (NOT_DONE, "The bill payer was sent to Famly but did not read back as expected. Check the child's bill payer in Famly."),
    "bill_payer_create_unconfirmed": (NOT_DONE, "An earlier attempt to set up the bill payer was never confirmed and none is showing for this child, so nothing was written again. Check the child's bill payer in Famly, set it up by hand if missing, then run the job again."),
    "bill_payer_write_rejected": (NOT_DONE, "Famly rejected the bill payer and none was created. Set it up in Famly by hand."),
    "bill_payer_scan_failed": (NOT_DONE, "Famly's bill payers could not be read, so none was created or linked. Run the job again later."),
    "bill_payer_differs": (CHECK, "The bill payer already in Famly has different details from HubSpot (the fields are in the codes below). Nothing was changed. Check the bill payer in Famly."),
    "contact_outcomes": (INFO, ""),
    "child_only_stage": (INFO, ""),
    "created": (INFO, ""),
    "child_existed": (INFO, ""),
    "present": (INFO, ""),
    "end_date_already_set": (INFO, ""),
    # ---- warnings: the work went ahead, look at it ------------------------------
    "no_quote": (CHECK, "No quote is attached to the deal."),
    "deposit_amount_missing": (CHECK, "No deposit amount is set on the deal."),
    "sales_deal_ambiguous": (CHECK, "More than one Sales deal is linked to this deal, so the quote could not be found."),
    "child_urn_missing": (CHECK, "The deal has no child URN, so the child could only be looked up by deal ID."),
    "famly_childid_differs_from_lookup": (CHECK, "The Famly child ID saved on the deal differs from the child found for this deal. Check which is right."),
    "duplicate_email_on_deal": (CHECK, "Two contacts on the deal share an email address."),
    "phone_missing": (CHECK, "A parent has no phone number on the deal. Add it in Famly by hand."),
    "phone_unnormalised": (CHECK, "The parent's phone number was not added because it could not be put in a standard format. Add it in Famly by hand."),
    "address_incomplete": (CHECK, "A parent's address was not added because the street, postcode or city is missing."),
    "duplicate_check_skipped": (CHECK, "The check for an existing child with the same details could not be done, because the date of birth or nursery is missing."),
    "child_name_unavailable_for_duplicate_check": (CHECK, "The child's name was not available, so the check for an existing child used the date of birth only."),
    "end_date_in_past": (CHECK, "The end date is in the past, so Famly will show the child as no longer attending."),
    # ---- flags from comparing Famly's own summary with what was sent ------------
    "summary_start_mismatch": (CHECK, "Famly shows a different start date from HubSpot. Check the child's start date in Famly."),
    "summary_end_mismatch": (CHECK, "Famly shows a different end date from HubSpot. Check the child's end date in Famly."),
    "summary_group_mismatch": (CHECK, "Famly shows a different room from the one chosen. Check the child's room in Famly."),
    "test_flag_mismatch": (CHECK, "The child's test flag in Famly is not what was intended. Check it in Famly."),
    "summary_unrecognised": (CHECK, "Famly's summary of the child could not be read, so the room and dates were not double-checked. Check the child in Famly."),
    "summary_unreadable": (CHECK, "Famly's summary of the child could not be loaded, so the room and dates were not double-checked. Check the child in Famly."),
    # ---- configuration: the admin line, never the staff list ----------------------
    "preflight_failed": (ADMIN, "The HubSpot deal properties could not be listed, so the HubSpot updates were attempted without checking them."),
    "end_date_check_failed": (ADMIN, "The deal's end date could not be checked before saving the default, so it was not saved."),
}

# prefix -> (kind, sentence). The text after the prefix is a property or value name.
_PREFIX: tuple[tuple[str, str, str], ...] = (
    ("missing:", ADMIN, "A HubSpot deal property the job writes to does not exist."),
    ("skipped:", ADMIN, "A HubSpot update was skipped because its deal property does not exist."),
    ("value_not_offered:", ADMIN, "A HubSpot dropdown does not offer a value the job tried to write."),
    ("write_failed:", ADMIN, "A HubSpot update failed."),
)

_PATTERNS: tuple[tuple["re.Pattern", str, str], ...] = (
    (re.compile(r"^child_http_\d+$"), NOT_DONE,
     "Famly refused to create the child, so nothing was created. Ask an admin."),
    (re.compile(r"^contact_http_\d+$"), NOT_DONE,
     "Famly refused to add a parent. Add them in Famly by hand."),
)

UNKNOWN_SENTENCE = "Something unexpected was reported. Ask an admin to check the job."


def lookup(code: str) -> tuple[str, str] | None:
    """(kind, sentence) for a code, or None if it has no translation."""
    code = str(code)
    if code in _STATIC:
        return _STATIC[code]
    for prefix, kind, sentence in _PREFIX:
        if code.startswith(prefix):
            return kind, sentence
    for pattern, kind, sentence in _PATTERNS:
        if pattern.match(code):
            return kind, sentence
    return None


def has_prefix_rule(prefix: str) -> bool:
    return any(prefix == p for p, _, _ in _PREFIX)


def _dedupe(items) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def classify(codes) -> dict[str, list[str]]:
    """Sentences by kind, in a fixed order, each sentence once. A code with no
    translation becomes the generic sentence (and counts as CHECK)."""
    out: dict[str, list[str]] = {CHECK: [], NOT_DONE: [], ADMIN: [], INFO: []}
    for code in sorted({str(c) for c in codes if c}):
        found = lookup(code)
        kind, sentence = found if found else (CHECK, UNKNOWN_SENTENCE)
        if kind != INFO and sentence not in out[kind]:
            out[kind].append(sentence)
    return out


def codes_of_kind(codes, *kinds) -> list[str]:
    """The codes (not sentences) whose kind is one of `kinds`."""
    wanted = []
    for code in sorted({str(c) for c in codes if c}):
        found = lookup(code)
        if (found[0] if found else CHECK) in kinds:
            wanted.append(code)
    return wanted


def staff_text(deal_ref: str, headline: str, codes, extra_footer=()) -> str:
    """The staff message: a headline line, then "To check" and "Not done" lists of
    plain sentences, then one small code footer (staff-facing codes only)."""
    sentences = classify(codes)
    lines = [f"{headline} (deal {deal_ref})"]
    if sentences[NOT_DONE]:
        lines.append("*Not done:*")
        lines += [f"• {s}" for s in sentences[NOT_DONE]]
    if sentences[CHECK]:
        lines.append("*To check:*")
        lines += [f"• {s}" for s in sentences[CHECK]]
    footer = codes_of_kind(codes, CHECK, NOT_DONE, INFO) + [str(x) for x in extra_footer if x]
    if footer:
        lines.append("_Codes: " + ", ".join(_dedupe(footer)) + "_")
    return "\n".join(lines)


def admin_text(deal_ref: str, codes) -> str | None:
    """The one separate admin line for configuration problems, or None."""
    admin_codes = codes_of_kind(codes, ADMIN)
    if not admin_codes:
        return None
    return (f"Onboarding admin (deal {deal_ref}): HubSpot setup needs attention. "
            f"{' '.join(classify(admin_codes)[ADMIN])} _Codes: {', '.join(admin_codes)}_")


# Outcome sentences for a CHILD-ONLY stage (derive.CHILD_ONLY_STAGES), with the stage label.
CHILD_ONLY_CREATED = "Child created in Famly. No contacts or bill payer, because the deal is in {stage}."
CHILD_ONLY_EXISTED = "Child was already in Famly. No contacts or bill payer, because the deal is in {stage}."


def child_only_summary(created: bool, stage: str | None) -> str:
    return (CHILD_ONLY_CREATED if created else CHILD_ONLY_EXISTED).format(stage=stage or "a child-only stage")


def relation_summary(count: int) -> str:
    return f"{count} existing contact(s) set to Parent."


# --------------------------------------------------------------------------- #
# The Gate A / dry-run STATUS BLOCK: what is already done and what would be written.
# State code -> sentence. `plan_status.block` picks the codes from the plan; the wording lives
# only here. Placeholders: {id} a HubSpot contact id, {role} "" or " (bill payer)", {stage}, {why},
# {fields} (the NAMES of bill payer fields that differ, never values).
# --------------------------------------------------------------------------- #
_STATE: dict[str, str] = {
    "child_would_create": "Child: will be created in Famly.",
    "child_would_create_test": "Child: will be created in Famly, as a test child.",
    "child_found_reference": "Child: already in Famly (found by the deal reference).",
    "child_found_urn": "Child: already in Famly (found by the child URN).",
    "child_found_famly_childid": "Child: already in Famly (found by the child ID recorded on the deal).",
    "child_created_by_this_job": "Child: already in Famly (created by this job).",
    "child_created_earlier": "Child: already in Famly (created by an earlier onboarding job).",
    "child_ambiguous": "Child: more than one child matches, so a person needs to check Famly.",
    "child_unresolved": "Child: the child ID saved on the deal is not a child in Famly, so a person needs to check.",
    "contact_would_create": "Contact {id}{role}: will be created.",
    "contact_present_reference": "Contact {id}{role}: already present (matched by its HubSpot reference).",
    "contact_present_email": "Contact {id}{role}: already present (matched by email).",
    "contact_relation_will_add": "Contact {id}{role}: already present; the relation will be set to Parent.",
    "contact_relation_unreadable": "Contact {id}{role}: already present; its relation could not be read, so it will not be changed.",
    "contact_flagged": "Contact {id}{role}: needs a person. {why}",
    "contact_not_checked": "Contact {id}{role}: not checked, because the child could not be resolved.",
    "bill_payer_would_create": "Bill payer: will be created.",
    "bill_payer_would_attach": "Bill payer: an existing bill payer will be linked to the child.",
    "bill_payer_present": "Bill payer: already linked to the child.",
    "bill_payer_flagged": "Bill payer: needs a person. {why}",
    "bill_payer_differs": "Bill payer: the existing bill payer's details differ from HubSpot in: {fields}.",
    "scope_child_only": "Held back by the deal's stage ({stage}): only the child is onboarded, so no contacts or bill payer are written.",
    "nothing_to_write": "Nothing to write: everything is already in Famly.",
    "no_writes_planned": "No Famly writes are planned; a person needs to look at the lines above.",
}


def state(code: str, **fields) -> str:
    """The sentence for a status-block state code (a KeyError for an unknown code is a bug)."""
    return _STATE[code].format(**fields)


def state_codes() -> frozenset:
    return frozenset(_STATE)
