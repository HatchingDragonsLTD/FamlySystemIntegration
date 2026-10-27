"""The bill payer WRITE stage (ONBOARD_BILL_PAYER=on, live mode, after Gate A, after the
child and contacts steps). The decision logic is in actions/onboard_dry_run/bill_payers.py.

What this stage may send, and NOTHING else:
  * `billPayers.create`: one payer with the child attached inline at multiplier 1.0.
  * `billPayers.addChildren`: the one new child, at multiplier 1.0, onto the matched payer.
  * `billPayers.addInvoiceRecipients` (`link_recipient`): the deal's bill payer contact as an invoice
    recipient of the payer (see below).
There is no delete, archive, update or deleteChildren anywhere in it, and
`assert_stage_mutation` refuses any document that has one. Existing shares are never edited.

SEND ONCE, NEVER RETRY. An HTTP error or errors in the body does NOT mean the mutation did
not run (billPayers.create returned 403 on an unreadable field after it had executed), and
the "Transient" display value means nothing. So the only CLEAN success is an error-free
response that names the created/attached row. Anything else (any error, a non-2xx status,
a timeout, missing data, a partial response with `data` AND errors) is an UNKNOWN outcome,
settled by a LIVE SCAN: immediately, then once more after 10 seconds. Exactly one live payer
with the canonical email listing the child at 1.0 is a success; otherwise the outcome is
`bill_payer_write_rejected` (when the response said 4xx/errors) or
`bill_payer_outcome_unknown`. A clean success is read back with one scan.

The mutation documents are built from the live schema (argument names and types are not in
the repo); the create selects ONLY `billPayerId`; the addChildren selection comes from the
return type's own fields with `socialSecurityNumber`, `accountNumber` and `sortCode` never
selected. externalId is not read or written (existing payers use it for something else) and
no address is sent.

PRIVACY: the email, name and phone are read from HubSpot into memory for the call and never
stored, logged, put in Slack or in the jobs table. Only the bill payer id is stored.
"""

import json
import logging
import re
import tempfile
import time
import os
from dataclasses import dataclass, field
from pathlib import Path

import requests

from actions.onboard import inputs
from actions.onboard import phone as phone_mod
from actions.onboard_dry_run import bill_payers as scan_mod
from actions.probe_bill_payers import runner as probe_runner
from actions.probe_bill_payers import write_probe
from core.client import GraphQLError, GraphQLHTTPError

logger = logging.getLogger(__name__)

CREATE_OP = "OnboardBillPayerCreate"
ADD_CHILDREN_OP = "OnboardBillPayerAddChildren"
MULTIPLIER = 1.0
SETTLE_WAIT_SECONDS = 10

HERE = Path(__file__).parent
ADD_RECIPIENT_QUERY = HERE / "add_invoice_recipient.graphql"
ADD_RECIPIENT_OP = "OnboardAddInvoiceRecipient"
# Famly refuses a repeat add with HTTP 400, errorCode 1675864914, "The invoice recipient has already
# been added to the bill payer", and lists the pair in extensions.payload.existingEntries.
ALREADY_ADDED_ERROR_CODE = "1675864914"
ALREADY_ADDED_MESSAGE = "already been added"

RECIPIENT_LINKED = "bill_payer_recipient_linked"        # the add was accepted (or settled by a read)
RECIPIENT_PRESENT = "bill_payer_recipient_present"      # already linked (a read, or the "already added" 400)
RECIPIENT_UNCONFIRMED = "bill_payer_recipient_unconfirmed"
CONTACT_NOT_FOUND = "bill_payer_contact_not_found"


class NotConfigured(Exception):
    """The live schema does not offer what the stage needs. Nothing was sent."""


def assert_stage_mutation(text: str, kind: str) -> None:
    """`kind` is create or addChildren: exactly one mutation on `billPayers`, with no
    site-wide argument and nothing that deletes, archives or updates."""
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).lower()
    expected = CREATE_OP if kind == "create" else ADD_CHILDREN_OP
    token = "create(" if kind == "create" else "addchildren("
    other = "addchildren(" if kind == "create" else "create("
    if code.count("mutation") != 1 or f"mutation {expected.lower()}(" not in code:
        raise AssertionError(f"not a single {expected} mutation")
    if "billpayers {" not in code or token not in code or other in code:
        raise AssertionError(f"the document is not billPayers.{kind}")
    for forbidden in ("delete", "archive", "update(", "siteids", "socialsecuritynumber",
                      "accountnumber", "sortcode", "externalid"):
        if forbidden in code:
            raise AssertionError(f"the document contains {forbidden!r}")


# --------------------------------------------------------------------------- #
# The mutations, built from the live schema
# --------------------------------------------------------------------------- #
@dataclass
class Plan:
    write: write_probe.WritePlan
    phone: dict | None


def _phone_info(body: dict) -> dict | None:
    """How BillPayerInput takes a phone number (None if it has no phone field)."""
    types, _roots = probe_runner._types(body)
    by_name = {t.get("name"): t for t in types}
    fields = {f["name"]: f for f in (by_name.get("BillPayerInput") or {}).get("inputFields") or []}
    name = next((n for n in fields if "phone" in n.lower()), None)
    if name is None:
        return None
    ref = fields[name]["type"]
    named = by_name.get(probe_runner.named_type(ref) or "") or {}
    inner = {f["name"] for f in named.get("inputFields") or []} if named.get("kind") == "INPUT_OBJECT" else set()
    return {"name": name, "is_list": "[" in probe_runner.type_str(ref),
            "object": "value" in inner, "type_field": "phoneType" in inner}


def phone_value(info: dict, number: str):
    """The phone in the shape the schema asks for (a string, or {value[, phoneType]})."""
    item = number
    if info["object"]:
        item = {"value": number}
        if info["type_field"]:
            item["phoneType"] = "MOBILE"
    return [item] if info["is_list"] else item


def load_plan(client) -> Plan:
    """The write plan from the live schema. Raises NotConfigured if it cannot be built."""
    try:
        body = probe_runner.fetch_schema(client)
    except (GraphQLError, GraphQLHTTPError, requests.RequestException, TimeoutError, ConnectionError):
        raise NotConfigured("the schema could not be read") from None
    plan = write_probe.plan_from_schema(body)
    problems = list(plan.blockers)
    if plan.add_children is None:
        problems.append(plan.add_children_unusable or "billPayers.addChildren is unusable")
    if problems:
        raise NotConfigured("; ".join(problems))
    return Plan(write=plan, phone=_phone_info(body))


def create_document(plan: Plan) -> str:
    w = plan.write
    return (f"mutation {CREATE_OP}($input: {w.create_arg_type}) {{ billPayers "
            f"{{ create({w.create_arg}: $input) {{ billPayerId }} }} }}\n")


def add_children_document(plan: Plan) -> str:
    op = plan.write.add_children
    return (f"mutation {ADD_CHILDREN_OP}($input: {op.arg_type}) {{ billPayers "
            f"{{ addChildren({op.arg}: $input){op.select} }} }}\n")


def create_variables(plan: Plan, site_id: str, contact, child_id: str) -> tuple[dict, list[str]]:
    """The create variables and any warnings. Name and email come from the HubSpot
    contact; the phone only if the normaliser accepts it; no address, no externalId."""
    w = plan.write
    props = contact.props or {}
    name = inputs.full_name(props.get("firstname"), props.get("lastname"))
    if name is None:
        raise inputs.InputIncomplete("contact_name_incomplete")
    payload = {w.site_field: site_id, w.name_field: name,
               w.email_field: inputs.canonical_email(contact.email)}
    warnings: list[str] = []
    source, _type, number = phone_mod.pick_phone(props)
    if number and plan.phone is not None:
        payload[plan.phone["name"]] = phone_value(plan.phone, number)
    elif source != "none" and not number:
        warnings.append("phone_unnormalised")
    child = {w.child_id_field: child_id}
    if w.share_field:
        child[w.share_field] = write_probe.build_share(w, MULTIPLIER)
    payload[w.children_field] = [child]
    variables = {"input": [payload] if "[" in w.create_arg_type else payload}
    return variables, warnings


def add_children_variables(plan: Plan, child_id: str, bill_payer_id: str) -> dict:
    op = plan.write.add_children
    entry = write_probe.make_add_entry(op, child_id, bill_payer_id,
                                       write_probe.build_add_share(op, MULTIPLIER))
    return {"input": [entry] if op.is_list else entry}


# --------------------------------------------------------------------------- #
# One send, never retried
# --------------------------------------------------------------------------- #
@dataclass
class Sent:
    clean: bool                  # an error-free response that names the row: the ONLY clean success
    rejected_hint: bool = False  # the response said no (a 4xx, or errors); nothing proves it did not run
    bill_payer_id: str | None = None


def _body_dict(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def send_once(client, text: str, variables: dict, operation: str, kind: str,
              child_id: str, bill_payer_id: str | None = None) -> Sent:
    """ONE request. Never raises for a failed response: it returns what it saw."""
    assert_stage_mutation(text, kind)
    handle = tempfile.NamedTemporaryFile("w", suffix=".graphql", delete=False, encoding="utf-8")
    try:
        handle.write(text)
        handle.close()
        try:
            body = client.execute(query_path=Path(handle.name), variables=variables,
                                  operation_name=operation)
        except GraphQLHTTPError as exc:
            return Sent(False, rejected_hint=exc.status_code < 500)
        except GraphQLError:
            return Sent(False, rejected_hint=True)
        except (requests.RequestException, TimeoutError, ConnectionError):
            return Sent(False)
    finally:
        os.unlink(handle.name)

    data = ((body or {}).get("data") or {}).get("billPayers") or {}
    if kind == "create":
        rows = data.get("create")
        rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
        ids = [str(r["billPayerId"]) for r in rows if isinstance(r, dict) and r.get("billPayerId")]
        return Sent(len(ids) == 1, bill_payer_id=ids[0] if len(ids) == 1 else None)
    rows = data.get("addChildren")
    rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
    named = [r for r in rows if isinstance(r, dict) and str(r.get("childId")) == child_id
             and str(r.get("billPayerId")) == str(bill_payer_id)]
    return Sent(bool(named), bill_payer_id=bill_payer_id if named else None)


# --------------------------------------------------------------------------- #
# The stage
# --------------------------------------------------------------------------- #
@dataclass
class StageResult:
    code: str                                  # one of the scan_mod codes (or contact_name_incomplete)
    bill_payer_id: str | None = None
    extra: list[str] = field(default_factory=list)      # more codes (bill_payer_differs, phone_unnormalised)
    differs: list[str] = field(default_factory=list)    # field NAMES only
    sent: bool = False                          # a mutation was sent


def _verified(payers, email_key: str, child_id: str, expect_id: str | None) -> str | None:
    """The payer id if EXACTLY ONE live payer has the canonical email and lists the child
    at 1.0 (and is `expect_id`, when given); else None."""
    if payers is None:
        return None
    matches = [p for p in payers if p.email == email_key]
    if len(matches) != 1:
        return None
    payer = matches[0]
    if expect_id and payer.id != expect_id:
        return None
    return payer.id if scan_mod.lists_child_at(payer, child_id, MULTIPLIER) else None


def _single_read(fn):
    """The default read-back: one read (the worker passes the bounded-retry lookup)."""
    return fn(), True


def run_stage(*, public, scan, contacts, approved: dict, site_id: str, child_id: str,
              sleep=time.sleep, lookup=_single_read, unresolved: bool = False) -> StageResult:
    """The bill payer stage for one deal. `scan()` returns the site's live payers (a fresh
    read each call) or None if it failed; `approved` is the plan's `billPayer` section."""
    approved_decision = scan_mod.Decision(
        approved.get("status"), approved.get("flag"), approved.get("billPayerId"),
        approved.get("matches") or 0)
    if approved_decision.status == scan_mod.FLAGGED:
        return StageResult(approved_decision.flag or scan_mod.PLAN_CHANGED)       # no write

    # Re-scan immediately before writing and recompute, with the child id known.
    payers = scan()
    decision = scan_mod.decide(contacts, payers, child_id)
    if decision.flag == scan_mod.SCAN_FAILED:
        return StageResult(scan_mod.SCAN_FAILED)
    linkage_only = (decision.bill_payer_id == approved_decision.bill_payer_id
                    and decision.matches == approved_decision.matches
                    and (decision.status == scan_mod.STATUS_PRESENT
                         or decision.flag == scan_mod.CHILD_LINKED_ELSEWHERE))
    if decision.base() != approved_decision.base() and not linkage_only:
        logger.warning("onboard: bill payer decision changed since approval; nothing written")
        return StageResult(scan_mod.PLAN_CHANGED)
    if decision.flag:
        return StageResult(decision.flag, decision.bill_payer_id)
    extra = ["bill_payer_differs"] if decision.differs else []
    if decision.status == scan_mod.STATUS_PRESENT:
        return StageResult(scan_mod.PRESENT, decision.bill_payer_id, extra, decision.differs)

    if unresolved:
        # An earlier job sent a payer write and never confirmed it, and the fresh scan
        # does not show it (a "present" decision returned above): never write again.
        logger.warning("onboard: bill payer write withheld, an earlier outcome is unconfirmed")
        return StageResult(scan_mod.CREATE_UNCONFIRMED)

    bill_payer_contact = next(c for c in contacts if c.is_bill_payer)
    email_key = scan_mod.normalise_email(bill_payer_contact.email)
    try:
        plan = load_plan(public)
    except NotConfigured as exc:
        logger.error("onboard: bill payer write not configured: %s", exc)
        return StageResult(scan_mod.WRITE_REJECTED)

    creating = decision.status == scan_mod.WOULD_CREATE
    warnings: list[str] = []
    if creating:
        try:
            variables, warnings = create_variables(plan, site_id, bill_payer_contact, child_id)
        except inputs.InputIncomplete:
            return StageResult(scan_mod.NAME_INCOMPLETE)
        text, operation, kind, expect_id = create_document(plan), CREATE_OP, "create", None
    else:
        variables = add_children_variables(plan, child_id, decision.bill_payer_id)
        text, operation, kind, expect_id = (add_children_document(plan), ADD_CHILDREN_OP,
                                            "addChildren", decision.bill_payer_id)
    success_code = scan_mod.CREATED if creating else scan_mod.ATTACHED

    sent = send_once(public, text, variables, operation, kind, child_id, decision.bill_payer_id)
    extra += warnings

    if sent.clean:
        # Read back and verify, reading again (reads only) while the scan lags.
        def read_back() -> list:
            payers = scan()
            if payers is None:
                raise RuntimeError("scan_failed")         # could not read, never "not found"
            same = [p.id for p in payers if p.email == email_key]
            if len(same) > 1:
                return same                               # not lag: stop at once
            found_id = _verified(payers, email_key, child_id, expect_id)
            return [found_id] if found_id else []
        try:
            ids, _readable = lookup(read_back)
        except Exception:  # noqa: BLE001 - an unreadable read-back is a mismatch report
            ids = []
        found = ids[0] if len(ids) == 1 else None
        if found:
            return StageResult(success_code, found, extra, decision.differs, sent=True)
        return StageResult(scan_mod.READBACK_MISMATCH, sent.bill_payer_id, extra, sent=True)

    # Anything else is an UNKNOWN outcome: settle by scanning now, then once more after 10 s.
    found = _verified(scan(), email_key, child_id, expect_id)
    if not found:
        sleep(SETTLE_WAIT_SECONDS)
        found = _verified(scan(), email_key, child_id, expect_id)
    if found:
        return StageResult(success_code, found, extra, decision.differs, sent=True)
    code = scan_mod.WRITE_REJECTED if sent.rejected_hint else scan_mod.OUTCOME_UNKNOWN
    return StageResult(code, None, extra, sent=True)


# --------------------------------------------------------------------------- #
# The invoice recipient link
# --------------------------------------------------------------------------- #
def assert_recipient_mutation(text: str) -> None:
    """Exactly one mutation, calling only `addInvoiceRecipients`: nothing that creates, updates,
    deletes or archives."""
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).lower()
    calls = re.findall(r"\b(\w+)\s*\(\s*\w+\s*:\s*\$", code)
    if code.count("mutation") != 1 or calls != ["addinvoicerecipients"]:
        raise AssertionError("not a single addInvoiceRecipients mutation")
    for forbidden in ("delete", "archive", "update", "create", "socialsecuritynumber"):
        if forbidden in code:
            raise AssertionError(f"the recipient document contains {forbidden!r}")


def _find(node, key: str):
    """Every value stored under `key`, anywhere in a nested response."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find(v, key)
    elif isinstance(node, list):
        for item in node:
            yield from _find(item, key)


def _names_pair(entry, contact_id: str, payer_id: str) -> bool:
    return (isinstance(entry, dict) and str(entry.get("billPayerId")) == str(payer_id)
            and str(contact_id) in {str(entry.get("relationId")), str(entry.get("contactId"))})


def already_added(body, contact_id: str, payer_id: str) -> bool:
    """The "already added" refusal, CONFIRMED: Famly's error code or message, and the pair itself in
    `existingEntries`. Without the pair it is not confirmed (the caller settles by a read)."""
    body = _body_dict(body)
    if body is None:
        return False
    coded = any(str(v) == ALREADY_ADDED_ERROR_CODE for v in _find(body, "errorCode"))
    worded = any(isinstance(m, str) and ALREADY_ADDED_MESSAGE in m for m in _find(body, "message"))
    if not (coded or worded):
        return False
    return any(_names_pair(e, contact_id, payer_id)
               for entries in _find(body, "existingEntries") for e in (entries or []))


@dataclass
class LinkResult:
    code: str
    sent: bool = False


def link_recipient(*, public, contact_id: str, payer_id: str, read, lookup=_single_read) -> LinkResult:
    """Make `contact_id` an invoice recipient of `payer_id`. `read()` returns the contact ids that are
    recipients of the payer (a fresh read; it raises when it cannot read).

    A fresh read first: already a recipient = nothing is sent. Then ONE `addInvoiceRecipients`, never
    retried. An accepted add (the response names the pair) and the confirmed "already added" 400 are
    successes. Anything else (another error, a timeout, a partial response) is UNKNOWN and is settled by
    reading the payer's recipients with the caller's bounded waits: visible = linked; still not visible =
    `bill_payer_recipient_unconfirmed`, a flag for a person. Never resent."""
    text = ADD_RECIPIENT_QUERY.read_text(encoding="utf-8")
    assert_recipient_mutation(text)
    try:
        if contact_id in read():
            return LinkResult(RECIPIENT_PRESENT)
    except Exception:  # noqa: BLE001 - an unreadable first look: the add itself is safe to send
        pass

    variables = {"invoiceRecipients": [{"relationId": contact_id, "billPayerId": payer_id}]}
    outcome = None                                  # "linked" | "present" | None (unknown)
    try:
        body = public.execute(query_path=ADD_RECIPIENT_QUERY, variables=variables,
                              operation_name=ADD_RECIPIENT_OP)
        rows = (((body or {}).get("data") or {}).get("billPayers") or {}).get("addInvoiceRecipients")
        rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
        if any(_names_pair(r, contact_id, payer_id) for r in rows):
            outcome = "linked"
    except GraphQLHTTPError as exc:
        if already_added(exc.body, contact_id, payer_id):
            outcome = "present"
    except GraphQLError as exc:
        if already_added(exc.body, contact_id, payer_id):
            outcome = "present"
    except (requests.RequestException, TimeoutError, ConnectionError):
        pass
    if outcome == "linked":
        return LinkResult(RECIPIENT_LINKED, sent=True)
    if outcome == "present":
        return LinkResult(RECIPIENT_PRESENT, sent=True)

    try:
        ids, _readable = lookup(lambda: [contact_id] if contact_id in read() else [])
    except Exception:  # noqa: BLE001 - an unreadable settle is "not confirmed"
        ids = []
    return LinkResult(RECIPIENT_LINKED if contact_id in ids else RECIPIENT_UNCONFIRMED, sent=True)
