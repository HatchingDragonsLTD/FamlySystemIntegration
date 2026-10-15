"""ONE-OFF PROBE: try Famly's `contacts.create` on ONE verified TEST child.

DELETE THIS PACKAGE (`actions/probe_contact_create/`) and its line in main.py
once the open questions in docs/famly_onboarding_pipeline_design.md section 9
(NameInput format, empty roles, address/phone formats) are answered.

THIS IS AN EXPLICIT, NARROW EXCEPTION to "never call a write against Famly
outside tests" (CLAUDE.md): it may call `contacts.create` on a single child that
has been verified to be a test child, and nothing else. It never runs from a
web action, a queue or any other command. Every guard below must pass BEFORE the
first write, and there is exactly one write path (`_create`, built only after
the guards):

  1. `--confirm` is required.
  2. The child's internal summary (GET /v2/children/{id}/summary, the same auth
     the other internal calls use) must say isTestChild == true. Anything else,
     or a summary that cannot be read, is a refusal with no write.
  3. At most MAX_CONTACTS (8) contacts are created per run, and a bounded number
     of attempts are made, so a run of rejections cannot loop.

The probe uses test values only (AAProbe / ContactOne, example.com emails, a
UK test mobile number), so printing them is fine. Nothing here touches the
real roster, the commit path or the plan queue.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.client import GraphQLError, GraphQLHTTPError
from core.rest_client import RestHTTPError

HERE = Path(__file__).parent
CREATE_QUERY = HERE / "create_contact.graphql"
READ_QUERY = HERE / "read_contacts.graphql"
INTROSPECT_QUERY = HERE / "introspect.graphql"

CREATE_OP = "ProbeContactCreate"
READ_OP = "ProbeReadContacts"
INTROSPECT_OP = "ProbeIntrospect"

# Hard cap on contacts created in one run.
MAX_CONTACTS = 8
# Hard cap on create requests (accepted or rejected) in one run.
MAX_ATTEMPTS = 16

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

FIRST, LAST = "AAProbe", "ContactOne"
ORDINALS = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"]

# Name attempts, in order. NameInput is a custom scalar whose input format is
# not documented.
NAME_STYLES = ("object_first_last", "object_first_last_full", "plain_string")

PHONES = ("+447700900123", "07700 900123", "+44 7700 900123")
COUNTRIES = ("United Kingdom", "GB")

REFUSED_NOT_TEST_CHILD = "not a test child"


class ProbeRefused(Exception):
    """A guard failed. No write was made."""


@dataclass
class ProbeResult:
    created: list[dict] = field(default_factory=list)  # {"id", "email", "label"}
    accepted: dict = field(default_factory=dict)  # format kind -> accepted values
    rejected: list[dict] = field(default_factory=list)  # {"label", "error"}
    attempts: int = 0


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def _find_all(node: Any, key: str) -> list:
    """Every value stored under `key` anywhere in a JSON document."""
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
    """True only if the summary names isTestChild and EVERY occurrence is the
    boolean true. Missing, false, a string or null all mean "not a test child"."""
    values = _find_all(summary, "isTestChild")
    return bool(values) and all(v is True for v in values)


def verify_test_child(rest, child_id: str) -> dict:
    """GET the child's summary and require isTestChild. Returns the summary.

    Raises:
        ProbeRefused: not a test child, or the summary could not be read.
    """
    try:
        summary = rest.get(f"/v2/children/{child_id}/summary")
    except RestHTTPError as exc:
        raise ProbeRefused(
            f"could not verify the child (HTTP {exc.status_code}); no write made"
        ) from None
    if not is_test_child(summary):
        raise ProbeRefused(REFUSED_NOT_TEST_CHILD)
    return summary


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #
def make_name(style: str, first: str, last: str):
    if style == "object_first_last":
        return {"firstName": first, "lastName": last}
    if style == "object_first_last_full":
        return {"firstName": first, "lastName": last, "fullName": f"{first} {last}"}
    return f"{first} {last}"


def make_contact(child_id: str, ordinal: int, style: str, phone: str, country: str) -> dict:
    """One ContactInput: empty roles, a probe email, a MOBILE phone, a RELATION
    record, not an emergency contact, a UK address with no state."""
    word = ORDINALS[ordinal - 1]
    return {
        "name": make_name(style, FIRST, f"Contact{word}"),
        "email": f"probe-{word.lower()}@example.com",
        "phoneNumbers": [{"value": phone, "phoneType": "MOBILE"}],
        "records": [{"key": "RELATION", "value": "Parent"}],
        "childIds": [child_id],
        "roles": [],
        "emergencyContact": False,
        "address": {
            "street": "1 Test Street",
            "zip": "SE16 1AA",
            "city": "London",
            "country": country,
        },
    }


def error_text(exc: Exception) -> str:
    """Exactly what Famly returned for a rejected request."""
    if isinstance(exc, GraphQLError):
        return json.dumps(exc.errors)
    if isinstance(exc, GraphQLHTTPError):
        return f"HTTP {exc.status_code}: {exc.body[:2000]}"
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
def run_probe(
    child_id: str,
    confirm: bool,
    rest,
    gql,
    out: Callable[[str], None] = print,
) -> ProbeResult:
    """Run the probe against `child_id`.

    Raises:
        ProbeRefused: a guard failed. Nothing has been written.
    """
    # ---- guards: every one before any write ------------------------------ #
    if not confirm:
        raise ProbeRefused("refusing without --confirm; nothing was written")
    if not UUID_RE.match(str(child_id).strip().lower()):
        raise ProbeRefused("--child-id is not a UUID; nothing was written")
    child_id = child_id.strip()
    verify_test_child(rest, child_id)

    out(f"CAP: at most {MAX_CONTACTS} contacts are created per run "
        f"(and at most {MAX_ATTEMPTS} create requests)")
    result = ProbeResult()

    # ---- the single write path, built only after the guards -------------- #
    def _create(contact: dict) -> list:
        body = gql.execute(
            query_path=CREATE_QUERY,
            variables={"contacts": [contact]},
            operation_name=CREATE_OP,
        )
        return body["data"]["contacts"]["create"]

    def attempt(label: str, ordinal: int, style: str, phone: str, country: str):
        """One create request. Returns the new contact id, or None if rejected
        or a cap stopped it."""
        if len(result.created) >= MAX_CONTACTS:
            out(f"[{label}] SKIPPED: contact cap ({MAX_CONTACTS}) reached")
            return None
        if result.attempts >= MAX_ATTEMPTS:
            out(f"[{label}] SKIPPED: attempt cap ({MAX_ATTEMPTS}) reached")
            return None
        contact = make_contact(child_id, ordinal, style, phone, country)
        result.attempts += 1
        out(f"[{label}] SENDING name={json.dumps(contact['name'])} phone={phone!r} "
            f"country={country!r}")
        try:
            created = _create(contact)
        except (GraphQLError, GraphQLHTTPError) as exc:
            text = error_text(exc)
            result.rejected.append({"label": label, "error": text})
            out(f"[{label}] REJECTED: {text}")
            return None
        out(f"[{label}] ACCEPTED: {json.dumps(created)}")
        contact_id = created[0]["id"] if created else None
        result.created.append({"id": contact_id, "email": contact["email"], "label": label})
        return contact_id

    _print_schema(gql, out)

    # ---- 1. name attempts, with the phone/country fallbacks -------------- #
    winner = None
    phone, country = PHONES[0], COUNTRIES[0]
    for style in NAME_STYLES:
        phone, country = PHONES[0], COUNTRIES[0]
        contact_id = attempt(f"name:{style}", 1, style, phone, country)
        # An address-country or phone rejection is retried in place (same
        # contact slot) before the name style is written off.
        for _ in range(4):
            if contact_id or not result.rejected:
                break
            text = result.rejected[-1]["error"].lower()
            if "country" in text and country != COUNTRIES[1]:
                country = COUNTRIES[1]
            elif "phone" in text and phone != PHONES[-1]:
                phone = PHONES[PHONES.index(phone) + 1]
            else:
                break
            contact_id = attempt(f"name:{style}+fallback", 1, style, phone, country)
        if contact_id:
            winner = style
            result.accepted = {"name": [style], "phone": [phone], "country": [country]}
            _readback(child_id, contact_id, result.created[-1]["email"], style, rest, gql, out)
            break

    if winner is None:
        out("NO NAME FORMAT WAS ACCEPTED; stopping")
        return _finish(result, out)

    # ---- 2/3. the other phone and country formats, one contact each ------ #
    base_phone, base_country = result.accepted["phone"][0], result.accepted["country"][0]
    ordinal = 2
    for other_phone in (p for p in PHONES if p != base_phone):
        cid = attempt(f"phone:{other_phone}", ordinal, winner, other_phone, base_country)
        ordinal += 1
        if cid:
            result.accepted["phone"].append(other_phone)
            _readback(child_id, cid, result.created[-1]["email"], winner, rest, gql, out)
    for other_country in (c for c in COUNTRIES if c != base_country):
        cid = attempt(f"country:{other_country}", ordinal, winner, base_phone, other_country)
        ordinal += 1
        if cid:
            result.accepted["country"].append(other_country)
            _readback(child_id, cid, result.created[-1]["email"], winner, rest, gql, out)

    return _finish(result, out)


def _finish(result: ProbeResult, out) -> ProbeResult:
    out("")
    out("ACCEPTED FORMATS: " + json.dumps(result.accepted))
    out("REJECTED: " + json.dumps(result.rejected))
    out("CREATED CONTACT IDS (delete these in Famly):")
    for item in result.created:
        out(f"  {item['id']}  {item['email']}  [{item['label']}]")
    return result


# --------------------------------------------------------------------------- #
# Read-only introspection and readback
# --------------------------------------------------------------------------- #
def _type_name(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return node.get("name") or f"{node.get('kind')}<{_type_name(node.get('ofType'))}>"


def _print_schema(gql, out) -> None:
    """Print the contact-create mutation and ContactInput from the live schema."""
    body = gql.execute(query_path=INTROSPECT_QUERY, variables={}, operation_name=INTROSPECT_OP)
    data = body.get("data") or {}
    out("SCHEMA: contacts { ... } mutation fields:")
    for field_ in (data.get("mutation") or {}).get("fields") or []:
        args = ", ".join(f"{a['name']}: {_type_name(a['type'])}" for a in field_.get("args") or [])
        out(f"  {field_['name']}({args}) -> {_type_name(field_['type'])}")
    out("SCHEMA: ContactInput:")
    for field_ in (data.get("input") or {}).get("inputFields") or []:
        out(f"  {field_['name']}: {_type_name(field_['type'])}")


def _readback(child_id, contact_id, email, style, rest, gql, out) -> None:
    """Read the new contact back two ways and print how it was stored."""
    stored = None
    cursor = None
    while stored is None:
        body = gql.execute(
            query_path=READ_QUERY,
            variables={"childIds": [child_id], "next": cursor},
            operation_name=READ_OP,
        )
        listing = ((body.get("data") or {}).get("contacts") or {}).get("list") or {}
        for node in listing.get("result") or []:
            if node.get("id") == contact_id:
                stored = node
                break
        cursor = listing.get("next")
        if stored is not None or not cursor:
            break

    out(f"READBACK contacts.list for {contact_id}: {json.dumps(stored)}")
    if stored:
        name = stored.get("name") or {}
        split = bool(name.get("firstName")) and bool(name.get("lastName"))
        out(f"  name stored as first/last split: {'yes' if split else 'no'} "
            f"(firstName={name.get('firstName')!r}, lastName={name.get('lastName')!r}, "
            f"fullName={name.get('fullName')!r})")
        out(f"  stored phoneNumbers: {json.dumps(stored.get('phoneNumbers'))}")
        out(f"  stored address: {json.dumps(stored.get('address'))}")
        out(f"  roles: {json.dumps(stored.get('roles'))}  "
            f"roleInvitations: {json.dumps(stored.get('roleInvitations'))}")

    # Re-verify the child (still a test child) and read the contact's login state.
    summary = verify_test_child(rest, child_id)
    relation = _find_relation(summary, email)
    out(f"SUMMARY relation for {email}: {json.dumps(relation)}")
    if isinstance(relation, dict):
        out(f"  loginId={relation.get('loginId')!r} onApp={relation.get('onApp')!r} "
            f"roles2={json.dumps(relation.get('roles2'))}")


def _find_relation(node: Any, email: str):
    """The summary dict that carries this email (its contact/relation entry)."""
    if isinstance(node, dict):
        if str(node.get("email") or "").strip().casefold() == email.casefold():
            return node
        for value in node.values():
            found = _find_relation(value, email)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_relation(item, email)
            if found is not None:
                return found
    return None
