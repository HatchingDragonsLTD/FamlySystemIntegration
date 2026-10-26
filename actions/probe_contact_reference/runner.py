"""ONE-OFF PROBE: can a HUBSPOT contact-id reference be stored on a Famly contact and found
again, and what happens to the existing records and references on update?

DELETE THIS PACKAGE (`actions/probe_contact_reference/`) and its line in main.py once the answer
is recorded in docs/status.md.

THIS IS AN EXPLICIT, NARROW EXCEPTION to "never call a write against Famly outside tests"
(CLAUDE.md), the same as probe-contact-create: it writes ONLY to a child verified as a TEST child,
creates at most MAX_CONTACTS contacts and sends at most MAX_WRITES write requests, each ONCE and
never retried. It never runs from a web action or a queue. Guards, all before the first write:

  1. `--confirm`;
  2. the child id is a UUID;
  3. GET /v2/children/{id}/summary says isTestChild true (else "not a test child", nothing sent).

Every document is checked: the read documents must contain no mutation, and each of the three
write documents must be exactly one `contacts { create | update }` mutation (never a delete).

OUTPUT: booleans, counts, seconds and ids only. Test contacts are named AAProbe, use example.com
emails and fake numeric HubSpot ids; none of those values is printed. There is no contact delete
in the public schema: the created contact ids are listed at the end for deletion by hand.

THE STEPS
  1. create contact A (TITLE + OCCUPATION records, a HUBSPOT reference); read it back;
  2. poll `contacts.listByExternalSystemReference` every 2 s for up to 60 s;
  3. `contacts.update(records: ...)` adding RELATION; read back (replace or merge?);
  4. full `contacts` update adding a SECOND reference (everything else echoed from the read-back);
     read back and compare name, phones, email and records in memory;
  5. create contact B with the SAME HUBSPOT id; does the lookup return both?
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from actions.probe_bill_payers.runner import assert_read_only
from core.client import GraphQLError, GraphQLHTTPError
from core.rest_client import RestHTTPError

HERE = Path(__file__).parent
CREATE_DOC = HERE / "create_contact.graphql"
UPDATE_RECORDS_DOC = HERE / "update_records.graphql"
UPDATE_CONTACT_DOC = HERE / "update_contact.graphql"
READ_DOC = HERE / "read_contacts.graphql"
LOOKUP_DOC = HERE / "lookup.graphql"

CREATE_OP, UPDATE_RECORDS_OP, UPDATE_CONTACT_OP = "ProbeRefCreate", "ProbeRefUpdateRecords", "ProbeRefUpdateContact"
READ_OP, LOOKUP_OP = "ProbeRefRead", "ProbeRefLookup"

READ_DOCS = (READ_DOC, LOOKUP_DOC)
WRITE_DOCS = {CREATE_DOC: "create", UPDATE_RECORDS_DOC: "update", UPDATE_CONTACT_DOC: "update"}

MAX_CONTACTS = 2
MAX_WRITES = 8
POLL_SECONDS = 2
POLL_LIMIT_SECONDS = 60
SYSTEM = "HUBSPOT"
KEYS = ("TITLE", "OCCUPATION", "RELATION", "EXTRA_INFO", "PLACE_OF_WORK")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REFUSED_NOT_TEST_CHILD = "not a test child"


class ProbeRefused(Exception):
    """A guard failed. No write was made."""


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def assert_probe_mutation(path: Path, root: str) -> None:
    """A write document must be exactly one `contacts { <root> ... }` mutation: no delete, no
    second mutation, no other root field."""
    text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#")).lower()
    if len(re.findall(r"\bmutation\b\s*\w*\s*[({]", text)) != 1:
        raise AssertionError(f"{path.name} is not exactly one mutation")
    if "delete" in text or "archive" in text:
        raise AssertionError(f"{path.name} contains a delete")
    fields = re.findall(r"\b(create|update|delete|archive)\s*\(", text)
    if fields != [root]:
        raise AssertionError(f"{path.name} calls {fields}, expected only {root}")
    if not re.search(r"mutation\s*\w*\s*\([^)]*\)\s*\{\s*contacts\s*\{", text):
        raise AssertionError(f"{path.name} does not start at contacts")


def check_documents() -> None:
    for path in READ_DOCS:
        assert_read_only(path)
    for path, root in WRITE_DOCS.items():
        assert_probe_mutation(path, root)


def _find_all(node: Any, key: str) -> list:
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found.extend(_find_all(v, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_all(item, key))
    return found


def is_test_child(summary: Any) -> bool:
    values = _find_all(summary, "isTestChild")
    return bool(values) and all(v is True for v in values)


def verify_test_child(rest, child_id: str) -> None:
    try:
        summary = rest.get(f"/v2/children/{child_id}/summary")
    except RestHTTPError as exc:
        raise ProbeRefused(f"could not verify the child (HTTP {exc.status_code}); no write made") from None
    if not is_test_child(summary):
        raise ProbeRefused(REFUSED_NOT_TEST_CHILD)


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    """What a contact looked like, in memory only (never printed)."""
    id: str
    name: str = ""
    email: str = ""
    phones: tuple = ()
    records: dict = field(default_factory=dict)
    refs: list = field(default_factory=list)          # [(foreignId, system as returned)]
    emergency: bool = False
    roles: int = 0


@dataclass
class Result:
    rows: dict = field(default_factory=dict)          # label -> boolean / count / seconds / text code
    created_ids: list = field(default_factory=list)
    writes: int = 0
    stopped: str | None = None


def _snapshot(node: dict) -> Snapshot:
    return Snapshot(
        id=str(node.get("id")),
        name=str((node.get("name") or {}).get("fullName") or ""),
        email=str(node.get("email") or ""),
        phones=tuple((str(p.get("value")), str(p.get("phoneType"))) for p in node.get("phoneNumbers") or []),
        records={str(r.get("key")): str(r.get("value")) for r in node.get("records") or []},
        refs=[(str(r.get("foreignId")), str(r.get("system"))) for r in node.get("externalSystemsReferences") or []],
        emergency=bool(node.get("emergencyContact")),
        roles=len(node.get("roles") or []),
    )


def _system_kind(system: str) -> str:
    return system if system in ("HUBSPOT", "UNKNOWN") else "other"


def run_probe(child_id: str, confirm: bool, rest, gql, out: Callable[[str], None] = print,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> Result:
    """Run the probe. Raises ProbeRefused (nothing written) if a guard fails."""
    if not confirm:
        raise ProbeRefused("refusing without --confirm; nothing was written")
    if not UUID_RE.match(str(child_id).strip().lower()):
        raise ProbeRefused("--child-id is not a UUID; nothing was written")
    child_id = child_id.strip()
    check_documents()
    verify_test_child(rest, child_id)
    out(f"CAP: at most {MAX_CONTACTS} contacts and {MAX_WRITES} write requests; each write once, never retried")

    result = Result()
    stamp = str(int(time.time()))
    foreign_a, foreign_b = f"9{stamp}1", f"9{stamp}2"          # fake numeric ids, never printed

    # ---- the only write path, built after the guards ---------------------- #
    def send(doc: Path, operation: str, variables: dict):
        """One write request. Returns (response body or None, error type or None). Never retried."""
        if result.writes >= MAX_WRITES:
            result.stopped = "write cap reached"
            return None, "cap"
        if doc == CREATE_DOC and len(result.created_ids) >= MAX_CONTACTS:
            result.stopped = "contact cap reached"
            return None, "cap"
        assert doc in WRITE_DOCS
        result.writes += 1
        try:
            return gql.execute(query_path=doc, variables=variables, operation_name=operation), None
        except (GraphQLError, GraphQLHTTPError) as exc:
            return None, type(exc).__name__

    def read_contacts() -> list[dict]:
        nodes, cursor = [], None
        for _ in range(50):
            body = gql.execute(query_path=READ_DOC, variables={"childIds": [child_id], "next": cursor},
                               operation_name=READ_OP)
            listing = ((body.get("data") or {}).get("contacts") or {}).get("list") or {}
            nodes += [n for n in listing.get("result") or [] if isinstance(n, dict)]
            cursor = listing.get("next")
            if not cursor:
                break
        return nodes

    def read_one(contact_id: str) -> Snapshot | None:
        for node in read_contacts():
            if str(node.get("id")) == contact_id:
                return _snapshot(node)
        return None

    def lookup(foreign: str) -> list:
        body = gql.execute(query_path=LOOKUP_DOC, variables={"foreignId": foreign, "system": SYSTEM},
                           operation_name=LOOKUP_OP)
        listing = ((body.get("data") or {}).get("contacts") or {}).get("listByExternalSystemReference") or {}
        return [n for n in listing.get("result") or [] if isinstance(n, dict)]

    def poll(foreign: str, wanted: int):
        """(seconds until at least `wanted` contacts appear or None, the count seen last)."""
        started, seen = clock(), 0
        while True:
            seen = len(lookup(foreign))
            if seen >= wanted:
                return round(clock() - started, 1), seen
            if clock() - started >= POLL_LIMIT_SECONDS:
                return None, seen
            sleep(POLL_SECONDS)

    def contact_input(label: str, foreign: str) -> dict:
        return {
            "name": f"AAProbe Ref{label}",
            "email": f"probe-ref-{label.lower()}@example.com",
            "phoneNumbers": [{"value": "+447700900123", "phoneType": "MOBILE"}],
            "records": [{"key": "TITLE", "value": "AAProbe"}, {"key": "OCCUPATION", "value": "AAProbe"}],
            "childIds": [child_id],
            "roles": [],
            "emergencyContact": False,
            "externalSystemsReferences": [{"foreignId": foreign, "system": SYSTEM}],
        }

    def create(label: str, foreign: str):
        """Create a probe contact. An error does not mean it did not run: settle by reading."""
        body, error = send(CREATE_DOC, CREATE_OP, {"contacts": [contact_input(label, foreign)]})
        contact_id = None
        if body is not None:
            created = (((body.get("data") or {}).get("contacts") or {}).get("create")) or []
            contact_id = str(created[0]["id"]) if created and created[0].get("id") else None
        elif error != "cap":
            email = contact_input(label, foreign)["email"]
            for node in read_contacts():
                if str(node.get("email") or "").casefold() == email:
                    contact_id = str(node["id"])
        if contact_id:
            result.created_ids.append(contact_id)
        return contact_id, error

    rows = result.rows
    # ---- 1. create A ------------------------------------------------------ #
    a_id, error = create("A", foreign_a)
    rows["1_create_accepted"] = a_id is not None and error is None
    if error:
        rows["1_create_error_type"] = error
    if a_id is None:
        result.stopped = result.stopped or "contact A was not created"
        return _finish(result, out)
    snap = read_one(a_id)
    rows["1_reference_present_after_create"] = bool(snap and any(f == foreign_a for f, _s in snap.refs))
    systems = [s for f, s in (snap.refs if snap else []) if f == foreign_a]
    rows["1_system_returned"] = _system_kind(systems[0]) if systems else "none"

    # ---- 2. lookup by reference -------------------------------------------- #
    seconds, count = poll(foreign_a, 1)
    rows["2_lookup_seconds_until_seen"] = seconds if seconds is not None else "not_seen_in_60s"
    rows["2_lookup_count"] = count

    # ---- 3. add RELATION through the single-field records argument --------- #
    body, error = send(UPDATE_RECORDS_DOC, UPDATE_RECORDS_OP,
                       {"records": [{"contactId": a_id, "record": [{"key": "RELATION", "value": "Parent"}]}]})
    rows["3_records_update_accepted"] = body is not None and error is None
    after = read_one(a_id)
    rows["3_relation_present"] = bool(after and after.records.get("RELATION"))
    rows["3_title_survived"] = bool(after and after.records.get("TITLE"))
    rows["3_occupation_survived"] = bool(after and after.records.get("OCCUPATION"))
    if after is None or after.roles:
        result.stopped = "contact A could not be re-read, or has roles: not echoing an unknown contact"
        return _finish(result, out)

    # ---- 4. add a second reference (full update: everything else echoed) ---- #
    before = after
    update = {
        "id": a_id, "name": before.name, "email": before.email,
        "phoneNumbers": [{"value": v, "phoneType": t} for v, t in before.phones],
        "records": [{"key": k, "value": v} for k, v in before.records.items()],
        "roles": [], "emergencyContact": before.emergency,
        "externalSystemsReferences": [{"foreignId": f, "system": s} for f, s in before.refs]
                                     + [{"foreignId": foreign_b, "system": SYSTEM}],
    }
    body, error = send(UPDATE_CONTACT_DOC, UPDATE_CONTACT_OP, {"contacts": [update]})
    rows["4_reference_update_accepted"] = body is not None and error is None
    final = read_one(a_id)
    rows["4_reference_count_after"] = len(final.refs) if final else 0
    rows["4_first_reference_survived"] = bool(final and any(f == foreign_a for f, _s in final.refs))
    rows["4_second_reference_present"] = bool(final and any(f == foreign_b for f, _s in final.refs))
    rows["4_name_unchanged"] = bool(final and final.name == before.name)
    rows["4_phones_unchanged"] = bool(final and final.phones == before.phones)
    rows["4_email_unchanged"] = bool(final and final.email == before.email)
    rows["4_records_unchanged"] = bool(final and final.records == before.records)

    # ---- 5. a second contact with the SAME HubSpot id ------------------------ #
    b_id, error = create("B", foreign_a)
    rows["5_second_contact_created"] = b_id is not None
    if b_id is not None:
        seconds, count = poll(foreign_a, 2)
        rows["5_lookup_returns_count"] = count
        rows["5_shared_id_seconds_until_both"] = seconds if seconds is not None else "not_both_in_60s"
        rows["5_shared_id_allowed"] = count >= 2
    return _finish(result, out)


def _finish(result: Result, out) -> Result:
    out("")
    out("RESULT (booleans, counts, seconds; no values):")
    for key, value in result.rows.items():
        out(f"  {key}: {value}")
    if result.stopped:
        out(f"STOPPED: {result.stopped}")
    out(f"write requests sent: {result.writes}")
    out("CREATED CONTACT IDS (delete these in Famly by hand; the public schema has no contact delete):")
    for contact_id in result.created_ids:
        out(f"  {contact_id}")
    return result
