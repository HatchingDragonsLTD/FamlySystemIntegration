"""READ-ONLY bill payer logic for the onboarding pipeline: scan a site's live bill
payers, match on canonical email, and decide what the bill payer stage would do.

Nothing here writes. The decision is shared by the dry run / plan and by the worker's
write stage (which recomputes it immediately before writing).

PRIVACY: emails, names and phones are held in memory only. The `Decision` that goes
into a plan carries a status, a flag code, a bill payer id, a match COUNT and the NAMES
of fields that differ (never values).

Facts this relies on (live probes):
  * `includeDeleted` defaults to TRUE on the server and must always be passed
    explicitly; the scan passes FALSE, and still ignores any payer with `deletedAt`.
  * a site has about 110 to 135 live payers (3 pages, about 3 seconds).
  * the canonical email is trim + lower case, googlemail.com = gmail.com, and a plus
    tag is NOT stripped.
  * existing families use multiplier 1.0 or 0.0 only; a share is never edited.
"""

import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from actions.onboard import phone as phone_mod
from actions.onboard_dry_run.famly_reads import normalise_email
from core.client import GraphQLError, GraphQLHTTPError

HERE = Path(__file__).parent
SCAN_QUERY = HERE / "bill_payers_scan.graphql"
SCAN_OP = "OnboardBillPayers"
DETAIL_ENV = "ONBOARD_BILL_PAYER_DETAIL_SELECTION"
FLAG_ENV = "ONBOARD_BILL_PAYER"

MAX_PAGES = 30                 # a site is about 3 pages; a runaway cursor must end
SCAN_DEADLINE_SECONDS = 90     # the whole scan, on top of the client's per-request timeout

# --- the codes (all have a sentence in actions/onboard/messages.py) ---------------- #
CREATED = "bill_payer_created"
ATTACHED = "bill_payer_attached"
PRESENT = "bill_payer_present"
AMBIGUOUS = "bill_payer_ambiguous"
NO_CONTACT = "bill_payer_no_contact"
NO_EMAIL = "bill_payer_no_email"
CHILD_LINKED_ELSEWHERE = "bill_payer_child_linked_elsewhere"
PLAN_CHANGED = "bill_payer_plan_changed"
OUTCOME_UNKNOWN = "bill_payer_outcome_unknown"
READBACK_MISMATCH = "bill_payer_readback_mismatch"
WRITE_REJECTED = "bill_payer_write_rejected"
SCAN_FAILED = "bill_payer_scan_failed"
DIFFERS = "bill_payer_differs"
CREATE_UNCONFIRMED = "bill_payer_create_unconfirmed"   # an earlier write was unconfirmed and the scan does not show it
NAME_INCOMPLETE = "contact_name_incomplete"        # an existing code: a create needs a name

ALL_CODES = (CREATED, ATTACHED, PRESENT, AMBIGUOUS, NO_CONTACT, NO_EMAIL, CHILD_LINKED_ELSEWHERE,
             PLAN_CHANGED, OUTCOME_UNKNOWN, READBACK_MISMATCH, WRITE_REJECTED, SCAN_FAILED, DIFFERS,
             CREATE_UNCONFIRMED)
SUCCESS_CODES = (CREATED, ATTACHED, PRESENT)

# plan statuses
WOULD_CREATE, WOULD_ATTACH, STATUS_PRESENT, FLAGGED = "would_create", "would_attach", "present", "flagged"


def enabled() -> bool:
    """ONBOARD_BILL_PAYER=on. Anything else (and the default) is off."""
    return os.environ.get(FLAG_ENV, "").strip().lower() == "on"


class ScanFailed(Exception):
    """The site's bill payers could not be read completely. Nothing is written."""


@dataclass(frozen=True)
class Payer:
    id: str
    email: str                                   # canonical; memory only
    children: dict = field(default_factory=dict)  # child id -> multiplier (None if unreadable)
    details: dict = field(default_factory=dict, repr=False)   # memory only


def _detail_selection() -> str:
    return os.environ.get(DETAIL_ENV, "").strip()


def scan_site(client, site_id: str, clock=time.monotonic) -> list[Payer]:
    """Every LIVE bill payer at one site, all pages. `includeDeleted` is passed
    explicitly as false. Raises ScanFailed on any error, a page cap or the deadline."""
    text = SCAN_QUERY.read_text(encoding="utf-8").replace("# DETAILS", _detail_selection())
    handle = tempfile.NamedTemporaryFile("w", suffix=".graphql", delete=False, encoding="utf-8")
    try:
        handle.write(text)
        handle.close()
        path = Path(handle.name)
        started = clock()
        payers: list[Payer] = []
        cursor = None
        for _page in range(MAX_PAGES):
            if clock() - started > SCAN_DEADLINE_SECONDS:
                raise ScanFailed("deadline")
            try:
                body = client.execute(
                    query_path=path,
                    variables={"siteIds": [site_id], "next": cursor, "includeDeleted": False},
                    operation_name=SCAN_OP,
                )
            except (GraphQLError, GraphQLHTTPError, requests.RequestException, TimeoutError,
                    ConnectionError, ValueError):
                raise ScanFailed("request") from None        # type only: a body can echo data
            listing = (((body or {}).get("data") or {}).get("billPayers") or {}).get("listBySiteIds")
            if not isinstance(listing, dict):
                raise ScanFailed("shape")
            for node in listing.get("result") or []:
                payer = _payer(node)
                if payer is not None:
                    payers.append(payer)
            cursor = listing.get("next") or None
            if not cursor:
                return payers
        raise ScanFailed("page cap")
    finally:
        os.unlink(handle.name)


_BASE = {"billPayerId", "email", "deletedAt", "children"}


def _payer(node: Any) -> Payer | None:
    if not isinstance(node, dict) or not node.get("billPayerId"):
        return None
    if node.get("deletedAt"):                       # archived: ignored, whatever the flag said
        return None
    children = {}
    for item in node.get("children") or []:
        if isinstance(item, dict) and item.get("childId"):
            try:
                multiplier = float((item.get("share") or {}).get("multiplier"))
            except (TypeError, ValueError):
                multiplier = None
            children[str(item["childId"])] = multiplier
    return Payer(
        id=str(node["billPayerId"]),
        email=normalise_email(node.get("email")),
        children=children,
        details={k: v for k, v in node.items() if k not in _BASE},
    )


def lists_child(payer: Payer, child_id: str) -> bool:
    return child_id in payer.children


def lists_child_at(payer: Payer, child_id: str, multiplier: float) -> bool:
    value = payer.children.get(child_id)
    return value is not None and abs(value - multiplier) < 1e-9


def bills_the_child(payer: Payer, child_id: str) -> bool:
    """Whether the payer lists the child with a multiplier above 0. A multiplier that
    cannot be read counts as billing (the double billing guard errs that way)."""
    if child_id not in payer.children:
        return False
    value = payer.children[child_id]
    return value is None or value > 0


# --------------------------------------------------------------------------- #
# Differences (names only, never values)
# --------------------------------------------------------------------------- #
def _squash(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _payer_name(details: dict) -> str:
    raw = details.get("name")
    if isinstance(raw, dict):
        raw = raw.get("fullName") or " ".join(
            str(raw.get(k) or "") for k in ("firstName", "lastName"))
    return _squash(raw)


def _payer_phones(details: dict) -> set[str]:
    out: set[str] = set()
    for key, value in details.items():
        if "phone" not in key.lower():
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = item.get("value") if isinstance(item, dict) else item
            normalised = phone_mod.normalise_uk(text) if isinstance(text, str) else None
            if normalised:
                out.add(normalised)
    return out


def differences(contact, payer: Payer) -> list[str]:
    """Field NAMES (`name`, `phone`) where the payer already in Famly differs from the
    HubSpot contact. A field the scan did not select, or that is empty on either side,
    is not compared. Nothing is ever overwritten."""
    props = contact.props or {}
    found = []
    contact_name = _squash(f"{props.get('firstname') or ''} {props.get('lastname') or ''}")
    famly_name = _payer_name(payer.details)
    if contact_name and famly_name and contact_name != famly_name:
        found.append("name")
    contact_phone = phone_mod.pick_phone(props)[2]
    famly_phones = _payer_phones(payer.details)
    if contact_phone and famly_phones and contact_phone not in famly_phones:
        found.append("phone")
    return found


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    status: str                       # would_create | would_attach | present | flagged
    flag: str | None = None           # a code from ALL_CODES (or NAME_INCOMPLETE) when flagged
    bill_payer_id: str | None = None  # the matched payer, when exactly one
    matches: int = 0                  # the candidate count
    differs: list[str] = field(default_factory=list)

    def base(self) -> tuple:
        """What the plan approved: the decision, the matched id and the candidate count."""
        return (self.status, self.flag, self.bill_payer_id, self.matches)

    def as_plan(self) -> dict:
        plan = {"status": self.status, "flag": self.flag, "billPayerId": self.bill_payer_id,
                "matches": self.matches}
        if self.differs:
            plan["differs"] = list(self.differs)        # outside the hash: see runner.hashed_plan
        return plan


def decide(contacts, payers: list[Payer] | None, child_id: str | None = None) -> Decision:
    """The bill payer decision. `contacts` are the deal's DealContacts; `payers` is the
    site's live payers, or None when the scan failed; `child_id` is known when the child
    already exists (or has just been created)."""
    bill_payer_contacts = [c for c in contacts if c.is_bill_payer]
    if len(bill_payer_contacts) != 1:
        return Decision(FLAGGED, NO_CONTACT)
    contact = bill_payer_contacts[0]
    email = normalise_email(contact.email)
    if not email:
        return Decision(FLAGGED, NO_EMAIL)
    if payers is None:
        return Decision(FLAGGED, SCAN_FAILED)

    matches = [p for p in payers if p.email == email]
    if len(matches) >= 2:
        return Decision(FLAGGED, AMBIGUOUS, matches=len(matches))
    matched = matches[0] if matches else None

    if child_id:
        if matched is not None and lists_child(matched, child_id):
            return Decision(STATUS_PRESENT, None, matched.id, 1, differences(contact, matched))
        elsewhere = [p for p in payers if (matched is None or p.id != matched.id)
                     and bills_the_child(p, child_id)]
        if elsewhere:
            return Decision(FLAGGED, CHILD_LINKED_ELSEWHERE, matched.id if matched else None,
                            len(matches))
    if matched is None:
        if not contact.has_first_and_last_name:
            return Decision(FLAGGED, NAME_INCOMPLETE, None, 0)
        return Decision(WOULD_CREATE, None, None, 0)
    return Decision(WOULD_ATTACH, None, matched.id, 1, differences(contact, matched))
