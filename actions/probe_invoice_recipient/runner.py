"""ONE-OFF PROBE: how does an invoice recipient link a contact to a bill payer, is a repeated add
deduplicated, and can a HUBSPOT reference be set on an existing contact that has none?

DELETE THIS PACKAGE (`actions/probe_invoice_recipient/`) and its line in main.py once the answer
is recorded in docs/status.md.

THIS IS AN EXPLICIT, NARROW EXCEPTION to "never call a write against Famly outside tests"
(CLAUDE.md), the same as probe-contact-reference: it writes ONLY to a child verified as a TEST
child, creates at most 1 contact and 1 bill payer, and sends at most MAX_WRITES write requests,
each ONCE and never retried. It never runs from a web action or a queue. Guards, all before the
first write: `--confirm`; the child id is a UUID; GET /v2/children/{id}/summary says isTestChild
true; every document is checked (read documents hold no mutation; each write document is exactly
one mutation of the expected kind, never a delete).

OUTPUT: booleans, counts and ids only. The test contact and payer are named AAProbe with example.com
emails; none of those values is printed. Nothing is deleted by the probe: the created ids are listed
at the end for deletion by hand (the public schema has no contact delete; a payer with a share
cannot be deleted without deleteChildren first).

THE STEPS (5 writes in all)
  1. create a contact on the test child, with no reference (or, with --contact-id, reuse an
     AAProbe test contact after a read shows it is on that child: no write, step 1 is skipped);
  2. create a bill payer on the test child at --share 0.0 or 1.0 (default 1.0, the pipeline's own
     share; on a TEST child nothing bills and invoicing is off). The document and the
     variables are built from the LIVE SCHEMA by the same builders the pipeline uses
     (`bill_payer_writes.load_plan`, `create_document`, `write_probe.build_share`); only fixed test
     values go in (name AAProbe IR, an example.com email, the test child, the chosen share). No HubSpot
     contact, phone or address. If it fails, one read counts the child's bill payers;
  3. addInvoiceRecipients with the contact as relationId; read the payer's recipients back;
  4. the same addInvoiceRecipients again; read back (deduplicated or duplicated? an error is
     reported as its class and code only);
  5. the full `contacts` update with exactly one HUBSPOT reference on the contact (it has none).
     If docs/reports_2026-10-27.md has a "Captured contact update" section with a ```json block,
     that block is the ContactUpdate input: the string "$ID" becomes the contact id, "$FOREIGN_ID"
     the fake HubSpot id, and "$ECHO" the value just read back for that same key.
"""

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from actions.onboard import bill_payer_writes as bpw
from actions.probe_bill_payers import write_probe
from actions.probe_bill_payers.runner import assert_read_only
from actions.probe_contact_reference.runner import (
    UUID_RE, ProbeRefused, assert_probe_mutation, verify_test_child)
from actions.pull_roster.runner import ROSTER_TIME_RANGE
from core.client import GraphQLError, GraphQLHTTPError
from integrations import catalogue

HERE = Path(__file__).parent
REPORTS_DOC = HERE.parent.parent / "docs" / "reports_2026-10-27.md"

SITE_CHILDREN_DOC = HERE / "site_children.graphql"
READ_PAYERS_DOC = HERE / "read_payers.graphql"
READ_CONTACTS_DOC = HERE / "read_contacts.graphql"
CREATE_CONTACT_DOC = HERE / "create_contact.graphql"
ADD_DOC = HERE / "add_recipients.graphql"
UPDATE_CONTACT_DOC = HERE / "update_contact.graphql"

READ_DOCS = (SITE_CHILDREN_DOC, READ_PAYERS_DOC, READ_CONTACTS_DOC)
# document -> (top-level field, the one call it makes)
WRITE_DOCS = {
    CREATE_CONTACT_DOC: ("contacts", "create"),
    ADD_DOC: ("billPayers", "addInvoiceRecipients"),
    UPDATE_CONTACT_DOC: ("contacts", "update"),
}
OPS = {SITE_CHILDREN_DOC: "ProbeIRSiteChildren", READ_PAYERS_DOC: "ProbeIRPayers",
       READ_CONTACTS_DOC: "ProbeRefRead", CREATE_CONTACT_DOC: "ProbeIRCreateContact",
       ADD_DOC: "ProbeIRAddRecipients",
       UPDATE_CONTACT_DOC: "ProbeIRUpdateContact"}

MAX_WRITES = 5
READ_WAITS = (0, 2, 4, 8)           # a read that does not show the row yet is read again (reads only)
SYSTEM = "HUBSPOT"
SHARES = (0.0, 1.0)
DEFAULT_SHARE = 1.0
ERROR_FILE = "probe_invoice_recipient_error.json"
CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def assert_one_mutation(path: Path, top: str, call: str) -> None:
    """A write document is exactly one `<top> { <call>(...) }` mutation: no delete, no archive, no
    second call."""
    text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#")).lower()
    if len(re.findall(r"\bmutation\b\s*\w*\s*[({]", text)) != 1:
        raise AssertionError(f"{path.name} is not exactly one mutation")
    if "delete" in text or "archive" in text:
        raise AssertionError(f"{path.name} contains a delete")
    calls = re.findall(r"\b(\w+)\s*\(\s*\w+\s*:\s*\$", text)
    if calls != [call.lower()]:
        raise AssertionError(f"{path.name} calls {calls}, expected only {call}")
    if not re.search(rf"mutation\s*\w*\s*\([^)]*\)\s*\{{\s*{top.lower()}\s*\{{", text):
        raise AssertionError(f"{path.name} does not start at {top}")


def check_documents() -> None:
    for path in READ_DOCS:
        assert_read_only(path)
    for path, (top, call) in WRITE_DOCS.items():
        assert_one_mutation(path, top, call)
    assert_probe_mutation(CREATE_CONTACT_DOC, "create")
    assert_probe_mutation(UPDATE_CONTACT_DOC, "update")


VALIDATION_RE = re.compile(
    r"variable|field|argument|unknown type|cannot query|expected|required|not defined|validation|"
    r"of type|input|enum|must|cannot|invalid|positive|greater|less|minimum|maximum|multiplier|share", re.I)


def _string_values(value) -> list[str]:
    found = []
    if isinstance(value, dict):
        for item in value.values():
            found += _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found += _string_values(item)
    elif isinstance(value, str) and len(value) >= 3:
        found.append(value)
    return found


def sanitise_message(messages: list, variables) -> str | None:
    """A GraphQL VALIDATION message that is safe to print, or None. Kept only when it looks like a
    validation error (it names a field, type or argument), truncated to 200 characters, and only if it
    holds no `@`, no run of more than 6 digits and no string value from the variables."""
    text = "; ".join(m for m in messages if isinstance(m, str)).strip()
    if not text or not VALIDATION_RE.search(text):
        return None
    text = text[:200]
    if "@" in text or re.search(r"\d{7,}", text):
        return None
    lowered = text.casefold()
    if any(value.casefold() in lowered for value in _string_values(variables)):
        return None
    return text


def _error_messages(exc: Exception) -> list:
    body = getattr(exc, "body", None)
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except ValueError:
            return [body]
        body = parsed
    errors = list(body.get("errors") or []) if isinstance(body, dict) else []
    errors += [e for e in getattr(exc, "errors", None) or [] if e not in errors]
    return [e.get("message") for e in errors if isinstance(e, dict)]


def error_info(exc: Exception, variables=None) -> str:
    """The error CLASS and CODE: the exception type, the HTTP status and any `extensions.code` values
    (matched against a strict pattern). For an HTTP 400 also a sanitised validation message (see
    `sanitise_message`), so the wrong field or type can be seen without any personal data."""
    body = getattr(exc, "body", None)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = None
    errors = list(body.get("errors") or []) if isinstance(body, dict) else []
    errors += [e for e in getattr(exc, "errors", None) or [] if e not in errors]
    codes = set()
    for error in errors:
        code = ((error or {}).get("extensions") or {}).get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and CODE_RE.match(code):
            codes.add(code)
    status = getattr(exc, "status_code", None)
    info = f"{type(exc).__name__} status={status} codes={sorted(codes) or 'none'}"
    if status == 400:
        message = sanitise_message(_error_messages(exc), variables)
        if message:
            info += f" message={message!r}"
    return info


def captured_update_template(text: str | None = None) -> dict | None:
    """The ContactUpdate input Drew pasted under "Captured contact update" (a ```json block), or
    None when the section or the block is missing or empty."""
    if text is None:
        try:
            text = REPORTS_DOC.read_text(encoding="utf-8")
        except OSError:
            return None
    match = re.search(r"^#+\s*Captured contact update\s*$(.*?)(?=^#+\s|\Z)", text, re.S | re.M | re.I)
    if not match:
        return None
    block = re.search(r"```json\s*(.*?)```", match.group(1), re.S)
    if not block or not block.group(1).strip():
        return None
    try:
        value = json.loads(block.group(1))
    except ValueError:
        return None
    return value if isinstance(value, dict) and value else None


def fill_template(template: dict, contact_id: str, foreign_id: str, echo: dict) -> dict:
    out = {}
    for key, value in template.items():
        if value == "$ID":
            out[key] = contact_id
        elif value == "$FOREIGN_ID":
            out[key] = foreign_id
        elif value == "$ECHO":
            out[key] = echo[key]
        elif isinstance(value, list):
            out[key] = [{k: (foreign_id if v == "$FOREIGN_ID" else v) for k, v in item.items()}
                        if isinstance(item, dict) else item for item in value]
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    rows: dict = field(default_factory=dict)
    contact_id: str | None = None
    payer_id: str | None = None
    writes: int = 0
    stopped: str | None = None


def run_probe(child_id: str, confirm: bool, rest, gql, out: Callable[[str], None] = print,
              sleep: Callable[[float], None] = time.sleep, template: dict | None = None,
              contact_id: str | None = None, share: float = DEFAULT_SHARE,
              error_file: Path | str | None = None) -> Result:
    """Run the probe. Raises ProbeRefused (nothing written) if a guard fails."""
    if not confirm:
        raise ProbeRefused("refusing without --confirm; nothing was written")
    if not UUID_RE.match(str(child_id).strip().lower()):
        raise ProbeRefused("--child-id is not a UUID; nothing was written")
    child_id = child_id.strip()
    check_documents()
    verify_test_child(rest, child_id)
    if share not in SHARES:
        raise ProbeRefused("--share must be 0.0 or 1.0; nothing was written")
    error_path = Path(error_file or ERROR_FILE)
    error_entries: list = []
    if contact_id is not None and not UUID_RE.match(str(contact_id).strip().lower()):
        raise ProbeRefused("--contact-id is not a UUID; nothing was written")
    template = template if template is not None else captured_update_template()
    out(f"CAP: 1 contact, 1 bill payer, {MAX_WRITES} write requests; each write once, never retried; "
        f"bill payer share {share}")
    out(f"step 5 uses: {'the captured request in docs' if template else 'the schema shape'}")

    result = Result()
    stamp = str(int(time.time()))
    foreign_id = f"9{stamp}"                       # a fake numeric HubSpot id, never printed

    def pages(doc: Path, variables: dict, path: tuple):
        cursor = None
        for _ in range(100):
            body = gql.execute(query_path=doc, variables={**variables, "next": cursor},
                               operation_name=OPS[doc])
            node = body.get("data") or {}
            for key in path:
                node = (node or {}).get(key) or {}
            yield from node.get("result") or []
            cursor = node.get("next")
            if not cursor:
                return

    def site_of_child() -> str | None:
        for entry in catalogue.load_sites().values():
            site_id = entry.get("institutionId") if isinstance(entry, dict) else None
            if not site_id:
                continue
            for row in pages(SITE_CHILDREN_DOC, {"siteIds": [site_id], "timeRange": dict(ROSTER_TIME_RANGE)},
                             ("children", "listBySiteIds")):
                if str(row.get("id")) == child_id:
                    return site_id
        return None

    site_id = site_of_child()
    if site_id is None:
        raise ProbeRefused("the child was not found at any site; nothing was written")

    def send(doc: Path, variables: dict, operation: str | None = None, checked: bool = False):
        """ONE write request, never retried. Returns (response body or None, error text or None).
        `checked` is for a document the caller has already passed through its own mutation check."""
        if result.writes >= MAX_WRITES:
            result.stopped = "write cap reached"
            return None, "cap"
        assert checked or doc in WRITE_DOCS
        result.writes += 1
        try:
            return gql.execute(query_path=doc, variables=variables,
                               operation_name=operation or OPS[doc]), None
        except GraphQLHTTPError as exc:
            record_http_error(exc, doc, variables, operation or OPS[doc])
            return None, error_info(exc, variables)
        except GraphQLError as exc:
            return None, error_info(exc, variables)

    def record_http_error(exc, doc: Path, variables: dict, operation: str) -> None:
        """A non-2xx response: write the RAW body and the exact request (document text and variables:
        fixed test values only) to a local file, overwritten each run and never committed. No headers,
        no token. Prints only the path, the body length and the body's top-level JSON keys."""
        body = exc.body if isinstance(exc.body, str) else json.dumps(exc.body)
        try:
            parsed = json.loads(body)
            keys = sorted(parsed) if isinstance(parsed, dict) else f"json {type(parsed).__name__}"
        except ValueError:
            keys = "not json"
        error_entries.append({
            "operation": operation, "status": getattr(exc, "status_code", None),
            "request": {"document": doc.read_text(encoding="utf-8"), "variables": variables},
            "body": body})
        error_path.write_text(json.dumps({"errors": error_entries}, indent=2), encoding="utf-8")
        out(f"HTTP {getattr(exc, 'status_code', None)} on {operation}: full response and request written to "
            f"{error_path.resolve()} (body length {len(body)}, top-level keys {keys})")

    def count_child_payers() -> int | None:
        """ONE read of the site's live bill payers: how many list this child (None if unreadable)."""
        try:
            return sum(
                1 for row in pages(READ_PAYERS_DOC, {"siteIds": [site_id], "includeDeleted": False},
                                   ("billPayers", "listBySiteIds"))
                if child_id in [str(c.get("childId")) for c in row.get("children") or []])
        except (GraphQLError, GraphQLHTTPError):
            return None

    def read_recipients() -> list | None:
        """The payer's (relationId, contactId) pairs, or None if the payer is not listed (reads only;
        read again with the waits above, because a listing can lag)."""
        for wait in READ_WAITS:
            if wait:
                sleep(wait)
            for row in pages(READ_PAYERS_DOC, {"siteIds": [site_id], "includeDeleted": False},
                             ("billPayers", "listBySiteIds")):
                if str(row.get("billPayerId")) == result.payer_id:
                    return [(str(r.get("relationId")), str(r.get("contactId")))
                            for r in row.get("invoiceRecipients") or []]
        return None

    def read_contact() -> dict | None:
        cursor = None
        for _ in range(50):
            body = gql.execute(query_path=READ_CONTACTS_DOC, variables={"childIds": [child_id], "next": cursor},
                               operation_name=OPS[READ_CONTACTS_DOC])
            listing = (((body.get("data") or {}).get("contacts") or {}).get("list")) or {}
            for node in listing.get("result") or []:
                if isinstance(node, dict) and str(node.get("id")) == result.contact_id:
                    return node
            cursor = listing.get("next")
            if not cursor:
                return None
        return None

    rows = result.rows

    # The bill payer create document and variables come from the LIVE schema, by the pipeline's own
    # builders. Built (and the schema read) BEFORE any write: if they cannot be built, nothing is sent.
    try:
        payer_plan = bpw.load_plan(gql)
    except bpw.NotConfigured:
        raise ProbeRefused("the bill payer create could not be built from the live schema; "
                           "nothing was written") from None
    payer_text = bpw.create_document(payer_plan)
    bpw.assert_stage_mutation(payer_text, "create")

    if contact_id is not None:
        result.contact_id = contact_id.strip()
        node = read_contact()
        if node is None:
            raise ProbeRefused("the contact is not on that child; nothing was written")
        if not str((node.get("name") or {}).get("fullName") or "").startswith("AAProbe"):
            raise ProbeRefused("the contact is not an AAProbe test contact; nothing was written")
        rows["1_contact_reused_on_this_child"] = True

    # ---- 1. a contact with no reference ------------------------------------ #
    if contact_id is None:
        body, error = send(CREATE_CONTACT_DOC, {"contacts": [{
            "name": "AAProbe IR", "email": f"probe-ir-{stamp}@example.com",
            "phoneNumbers": [{"value": "+447700900123", "phoneType": "MOBILE"}],
            "records": [{"key": "TITLE", "value": "AAProbe"}, {"key": "OCCUPATION", "value": "AAProbe"}],
            "childIds": [child_id], "roles": [], "emergencyContact": False}]})
        created = ((((body or {}).get("data") or {}).get("contacts") or {}).get("create")) or []
        result.contact_id = str(created[0]["id"]) if created and created[0].get("id") else None
        rows["1_contact_created"] = result.contact_id is not None
        if error:
            rows["1_error"] = error
        if result.contact_id is None:
            result.stopped = "no contact id came back; nothing else was sent (a contact may exist: check Famly)"
            return _finish(result, out)

    # ---- 2. a bill payer at the chosen share -------------------------------- #
    w = payer_plan.write
    payload = {w.site_field: site_id, w.name_field: "AAProbe IR"}
    if w.email_field:
        payload[w.email_field] = f"probe-ir-payer-{stamp}@example.com"
    entry = {w.child_id_field: child_id}
    if w.share_field:
        entry[w.share_field] = write_probe.build_share(w, share)
    payload[w.children_field] = [entry]
    payer_variables = {"input": [payload] if "[" in w.create_arg_type else payload}
    handle = tempfile.NamedTemporaryFile("w", suffix=".graphql", delete=False, encoding="utf-8")
    try:
        handle.write(payer_text)
        handle.close()
        body, error = send(Path(handle.name), payer_variables, operation=bpw.CREATE_OP, checked=True)
    finally:
        os.unlink(handle.name)
    made = ((((body or {}).get("data") or {}).get("billPayers") or {}).get("create")) or []
    result.payer_id = str(made[0]["billPayerId"]) if made and made[0].get("billPayerId") else None
    rows["2_bill_payer_created"] = result.payer_id is not None
    if error:
        rows["2_error"] = error
    if result.payer_id is None:
        rows["2_child_bill_payer_count_after"] = count_child_payers()
        result.stopped = "no bill payer id came back; nothing else was sent (a payer may exist: check Famly)"
        return _finish(result, out)

    # ---- 3. add the recipient, read back ----------------------------------- #
    recipient = {"invoiceRecipients": [{"relationId": result.contact_id, "billPayerId": result.payer_id}]}
    before = read_recipients()
    rows["3_recipients_before"] = None if before is None else len(before)
    body, error = send(ADD_DOC, recipient)
    rows["3_add_accepted"] = body is not None and error is None
    if error:
        rows["3_error"] = error
    after = read_recipients()
    rows["3_payer_listed_after"] = after is not None
    rows["3_contact_present"] = bool(after and any(result.contact_id in pair for pair in after))
    rows["3_recipient_count"] = None if after is None else len(after)
    rows["3_relationId_equals_contactId"] = bool(after and all(r == c for r, c in after))

    # ---- 4. the same add again ---------------------------------------------- #
    body, error = send(ADD_DOC, recipient)
    rows["4_second_add_accepted"] = body is not None and error is None
    if error:
        rows["4_error"] = error
    again = read_recipients()
    rows["4_recipient_count_after"] = None if again is None else len(again)
    if after is not None and again is not None:
        rows["4_deduplicated"] = len(again) == len(after)
        rows["4_duplicated"] = len(again) > len(after)

    # ---- 5. one HUBSPOT reference on the contact (it has none) --------------- #
    node = read_contact()
    if node is None:
        result.stopped = "the contact could not be re-read: not echoing an unknown contact"
        return _finish(result, out)
    echo = {
        "id": result.contact_id,
        "name": str((node.get("name") or {}).get("fullName") or ""),
        "email": node.get("email"),
        "phoneNumbers": [{"value": p.get("value"), "phoneType": p.get("phoneType")}
                         for p in node.get("phoneNumbers") or []],
        "records": [{"key": r.get("key"), "value": r.get("value")} for r in node.get("records") or []],
        "roles": [],
        "emergencyContact": bool(node.get("emergencyContact")),
    }
    rows["5_contact_had_no_reference"] = not node.get("externalSystemsReferences")
    rows["5_contact_has_no_roles"] = not node.get("roles")
    if node.get("externalSystemsReferences") or node.get("roles"):
        result.stopped = "the contact already has a reference or a role: not sending the update"
        return _finish(result, out)
    if template:
        update = fill_template(template, result.contact_id, foreign_id, echo)
    else:
        update = {**echo, "externalSystemsReferences": [{"foreignId": foreign_id, "system": SYSTEM}]}
    body, error = send(UPDATE_CONTACT_DOC, {"contacts": [update]})
    rows["5_update_accepted"] = body is not None and error is None
    if error:
        rows["5_error"] = error
    final = read_contact() or {}
    refs = final.get("externalSystemsReferences") or []
    rows["5_reference_count_after"] = len(refs)
    rows["5_reference_present"] = any(str(r.get("foreignId")) == foreign_id for r in refs)
    rows["5_system_returned_hubspot"] = any(r.get("system") == SYSTEM for r in refs)
    rows["5_name_unchanged"] = str((final.get("name") or {}).get("fullName") or "") == echo["name"]
    rows["5_email_unchanged"] = final.get("email") == echo["email"]
    rows["5_phones_unchanged"] = [{"value": p.get("value"), "phoneType": p.get("phoneType")}
                                  for p in final.get("phoneNumbers") or []] == echo["phoneNumbers"]
    rows["5_records_unchanged"] = sorted((r.get("key"), r.get("value")) for r in final.get("records") or []) \
        == sorted((r["key"], r["value"]) for r in echo["records"])
    return _finish(result, out)


def _finish(result: Result, out) -> Result:
    out("")
    out("RESULT (booleans and counts; no values):")
    for key, value in result.rows.items():
        out(f"  {key}: {value}")
    if result.stopped:
        out(f"STOPPED: {result.stopped}")
    out(f"write requests sent: {result.writes}")
    out("CREATED (delete these in Famly by hand: the contact has no public delete; delete the payer's "
        "share and then the payer):")
    out(f"  contact id: {result.contact_id}")
    out(f"  bill payer id: {result.payer_id}")
    return result
