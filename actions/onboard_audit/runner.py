"""onboard-audit: for children ALREADY in Famly, does the child carry what HubSpot holds, and
does it have the plans HubSpot implies? READ-ONLY, end to end.

  * HubSpot: GET requests and the deal SEARCH (a POST that only reads) through
    `ReadOnlyHubSpot`, which refuses any other request before it is sent.
  * Famly GraphQL: documents are checked to contain no mutation (`ReadOnlyGraphQL`).
  * Famly REST: only `get` is exposed (`ReadOnlyRest`).

The output is ids and codes only. Names, dates of birth, emails and every other value are
compared in memory and never returned, printed, logged or written to the CSV.

WHAT IS COMPARED (codes):
  child     child_name_differs, child_name_not_comparable, child_dob_differs, child_group_differs,
            child_start_differs, child_end_differs, child_urn_differs, child_urn_missing (the
            Famly child has no URN), deal_urn_missing (the deal has none), child_wrong_site,
            no_hubspot_reference, expected_group_not_derivable, deal_dates_unreadable,
            child_ambiguous
  contacts  contact_missing, contact_no_email, bill_payer_contact_missing,
            contact_relation_missing (+ the sub-code contact_relation_empty when the contact HAS a
            RELATION record with a blank value, which the planner can repair), bill_payer_not_emergency,
            contact_matched_via_additional_email (info)   (skipped for child-only stages; parent names
            and phones are NOT compared: Famly is the source of truth after onboarding)
  payer     bill_payer_missing, bill_payer_not_linked, bill_payer_share_not_full,
            bill_payer_ambiguous, double_billing_risk, bill_payer_no_contact, bill_payer_no_email,
            bill_payer_scan_failed, and for the deal's bill payer contact: bill_payer_recipient_present
            (it is an invoice recipient of the child's payer) / bill_payer_recipient_missing (the payer
            exists and lists the child, but the contact is not a recipient; read from the payer scan's
            invoiceRecipients, no extra request)   (skipped for child-only stages)
  plans     plan_missing, plan_overlap_or_duplicate

            plan_matches | plan_differs (+ plan_dates_differ, plan_sessions_differ, plan_fees_differ) |
            plan_not_derivable (+ plan_input_dates_missing, plan_input_sessions_missing,
            plan_input_fees_missing, plan_session_catalogue_gap): the expected plan is read from
            the deal's own properties with NO preview request (see compare_plan)
"""

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from actions.onboard_dry_run import bill_payers as payer_scan
from actions.onboard_dry_run import derive, links
from actions.onboard_dry_run.famly_reads import normalise_email
from actions.pull_roster.runner import ROSTER_TIME_RANGE
from core import hubspot as hubspot_cfg
from core.hubspot import HubSpotClient, HubSpotError

logger = logging.getLogger(__name__)

CHILDREN_QUERY = Path(__file__).parent / "audit_children.graphql"
CHILDREN_OP = "AuditSiteChildren"
SEARCH_PATH = "/crm/v3/objects/deals/search"
OPERATIONS = "operations"

FOUND_DEAL_REFERENCE, FOUND_FAMLY_CHILDID, FOUND_URN, NOT_FOUND = (
    "deal_reference", "famly_childid", "urn", "not_found")
CHILD_MISSING = "child_missing"
CHILD_AMBIGUOUS = "child_ambiguous"


class NotReadOnly(RuntimeError):
    """A request that is not a read was attempted. Nothing was sent."""


# --------------------------------------------------------------------------- #
# The read-only guards (and the request counters)
# --------------------------------------------------------------------------- #
class ReadOnlyHubSpot(HubSpotClient):
    """HubSpotClient that refuses everything but GET and the deal SEARCH, and counts calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def _call(self, method, path, params=None, json_body=None):
        if not (method == "GET" or (method == "POST" and path == SEARCH_PATH)):
            raise NotReadOnly(f"{method} is not a read")
        self.calls += 1
        return super()._call(method, path, params, json_body)


class ReadOnlyGraphQL:
    """Wraps a GraphQL client; refuses any document that contains a mutation; counts requests."""

    def __init__(self, client):
        self._client = client
        self.calls = 0

    def execute(self, query_path, variables, operation_name):
        text = "\n".join(
            line for line in Path(query_path).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")).lower()
        if re.search(r"\bmutation\b\s*[\w{(]", text.replace("mutationtype", "")):
            raise NotReadOnly(f"{operation_name} is not a read-only document")
        self.calls += 1
        return self._client.execute(query_path=query_path, variables=variables,
                                    operation_name=operation_name)


class ReadOnlyRest:
    """Only `get` is exposed: there is no way to send anything else through this."""

    def __init__(self, client):
        self._client = client
        self.calls = 0

    def get(self, path, params=None, **kwargs):
        self.calls += 1
        return self._client.get(path, params=params, **kwargs)


# --------------------------------------------------------------------------- #
# Famly side: one pull per site, everything in memory
# --------------------------------------------------------------------------- #
@dataclass
class FamlyContact:
    email: str = field(default="", repr=False)
    emergency: bool = False
    relation: bool = False
    id: str = ""                    # the Famly contact id (ids are allowed in output)
    relation_empty: bool = False    # a RELATION record exists but its value is blank


@dataclass
class FamlyChild:
    id: str
    external_id: str = field(default="", repr=False)
    birthday: str = field(default="", repr=False)
    full_name: str = field(default="", repr=False)
    group_id: str | None = None
    relations: list = field(default_factory=list, repr=False)      # (site id, first day, last day)
    references: list = field(default_factory=list, repr=False)    # (system upper, foreign id)
    contacts: list = field(default_factory=list, repr=False)


def _tokens(value: Any) -> frozenset:
    return frozenset(str(value or "").casefold().split())


def fetch_children(gql, site_ids: list[str]) -> dict[str, FamlyChild]:
    """Every child at every site, past and future, merged by id. One paginated pull per site."""
    out: dict[str, FamlyChild] = {}
    for site_id in site_ids:
        cursor = None
        for _ in range(500):
            body = gql.execute(
                query_path=CHILDREN_QUERY,
                variables={"siteIds": [site_id], "next": cursor, "timeRange": dict(ROSTER_TIME_RANGE)},
                operation_name=CHILDREN_OP)
            listing = (((body or {}).get("data") or {}).get("children") or {}).get("listBySiteIds") or {}
            for node in listing.get("result") or []:
                if not isinstance(node, dict) or not node.get("id"):
                    continue
                child = out.setdefault(str(node["id"]), FamlyChild(id=str(node["id"])))
                child.external_id = str(node.get("externalId") or "").strip()
                child.birthday = str(node.get("birthday") or "")[:10]
                name = node.get("name") or {}
                child.full_name = str(name.get("fullName") or "") if isinstance(name, dict) else ""
                group = node.get("currentGroup")
                child.group_id = str(group["id"]) if isinstance(group, dict) and group.get("id") else None
                child.relations = [
                    (str(((r.get("site") or {}).get("siteId")) or ""), str(r.get("firstDay") or "")[:10],
                     str(r.get("lastDay") or "")[:10])
                    for r in node.get("sitesRelation") or [] if isinstance(r, dict)]
                child.references = [
                    (str(r.get("system") or "").upper(), str(r.get("foreignId") or "").strip())
                    for r in node.get("externalSystemsReferences") or [] if isinstance(r, dict)]
                child.contacts = [
                    FamlyContact(
                        email=normalise_email(c.get("email")),
                        emergency=bool(c.get("emergencyContact")),
                        relation=any(isinstance(rec, dict) and rec.get("key") == "RELATION" and rec.get("value")
                                     for rec in c.get("records") or []),
                        id=str(c.get("id") or ""),
                        relation_empty=any(isinstance(rec, dict) and rec.get("key") == "RELATION"
                                           and not str(rec.get("value") or "").strip()
                                           for rec in c.get("records") or []))
                    for c in node.get("contacts") or [] if isinstance(c, dict)]
            cursor = listing.get("next") or None
            if not cursor:
                break
    return out


class Index:
    """Lookups over the pulled children, by deal reference, recorded id and URN."""

    def __init__(self, children: dict[str, FamlyChild]):
        self.children = children
        self.by_deal: dict[str, list[str]] = {}
        self.by_urn: dict[str, list[str]] = {}
        self.contact_children: dict[str, set] = {}      # contact email -> the children it is on (memory only)
        for child in children.values():
            for contact in child.contacts:
                if contact.email:
                    self.contact_children.setdefault(contact.email, set()).add(child.id)
            for system, foreign in child.references:
                if system == "HUBSPOT" and foreign:
                    self.by_deal.setdefault(foreign.casefold(), []).append(child.id)
            if child.external_id:
                self.by_urn.setdefault(child.external_id.casefold(), []).append(child.id)

    def find(self, deal: dict, deal_id: str) -> tuple[str, list[str]]:
        """(found_via, child ids): deal reference first, then the deal's famly_childid, then URN."""
        ids = list(dict.fromkeys(self.by_deal.get(str(deal_id).casefold(), [])))
        if ids:
            return FOUND_DEAL_REFERENCE, ids
        recorded = str(deal.get("famly_childid") or "").strip()
        if recorded and recorded in self.children:
            return FOUND_FAMLY_CHILDID, [recorded]
        urn = str(deal.get("child_urn") or "").strip().casefold()
        ids = list(dict.fromkeys(self.by_urn.get(urn, []))) if urn else []
        if ids:
            return FOUND_URN, ids
        return NOT_FOUND, []


# --------------------------------------------------------------------------- #
# The comparisons (booleans and codes only)
# --------------------------------------------------------------------------- #
def expected_for(deal: dict, stage: str | None) -> dict:
    """What the PLANNER would derive for this deal (same derivation functions), as booleans'
    inputs: {site_code, site_id, group_id, start, end}. A part that cannot be derived is None
    and reported as a code by the caller."""
    out: dict[str, Any] = {"site_code": None, "site_id": None, "group_id": None, "start": None,
                           "end": None, "codes": []}
    try:
        out["site_code"], out["site_id"] = derive.site_for_location(deal.get("location_of_interest"))
    except derive.Hold:
        out["codes"].append("expected_site_not_derivable")
    try:
        start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
        end, _defaulted = derive.resolve_end_date(deal)
        out["start"] = start.isoformat() if start else None
        out["end"] = end.isoformat() if end else None
    except ValueError:
        out["codes"].append("deal_dates_unreadable")
    try:
        title = (derive.VOYAGERS if derive.is_left_nursery(stage)
                 else derive.group_title(deal.get("child_date_of_birth"), deal.get("actual_start_date_1")))
        if out["site_code"]:
            out["group_id"] = derive.group_id_for(out["site_code"], title)
    except derive.Hold:
        out["codes"].append("expected_group_not_derivable")
    return out


def child_codes(deal: dict, deal_id: str, stage: str | None, child: FamlyChild) -> list[str]:
    codes: list[str] = []
    want = expected_for(deal, stage)
    codes += want["codes"]

    first = str(deal.get(hubspot_cfg.CHILD_NAME_PROPERTIES[0]) or "") if hubspot_cfg.CHILD_NAME_PROPERTIES else ""
    last = (str(deal.get(hubspot_cfg.CHILD_NAME_PROPERTIES[1]) or "")
            if len(hubspot_cfg.CHILD_NAME_PROPERTIES) > 1 else "")
    deal_name = f"{first} {last}"
    deal_tokens = _tokens(deal_name)
    if not deal_tokens:
        codes.append("child_name_not_comparable")
    elif deal_tokens != _tokens(child.full_name):
        codes.append("child_name_differs")
        codes.append(name_difference(deal_name, child.full_name))

    try:
        dob = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
    except ValueError:
        dob = None
    if dob is None or dob.isoformat() != child.birthday:
        codes.append("child_dob_differs")
        codes.append(dob_difference(dob, child.birthday))

    if want["group_id"] is not None and child.group_id != want["group_id"]:
        codes.append("child_group_differs")

    urn = str(deal.get("child_urn") or "").strip()
    if urn and not child.external_id:
        codes.append("child_urn_missing")
    elif urn and urn.casefold() != child.external_id.casefold():
        codes.append("child_urn_differs")
    elif not urn:
        codes.append("deal_urn_missing")

    if not any(system == "HUBSPOT" and foreign.casefold() == str(deal_id).casefold()
               for system, foreign in child.references):
        codes.append("no_hubspot_reference")

    if want["site_id"] is not None:
        mine = [r for r in child.relations if r[0] == want["site_id"]]
        if not mine:
            codes.append("child_wrong_site")
        else:
            _site, first_day, last_day = mine[0]
            if want["start"] is not None and first_day != want["start"]:
                codes.append("child_start_differs")
                codes.append(date_gap("child_start", first_day, want["start"]))
            if want["start"] is not None and (last_day or None) != (want["end"] or None):
                codes.append("child_end_differs")
                codes.append(date_gap("child_end", last_day, want["end"]))
    return codes


# --------------------------------------------------------------------------- #
# Sub-codes: HOW a value differs, never the values. Compared in memory only.
# --------------------------------------------------------------------------- #
def dob_difference(expected, famly_value: str) -> str:
    """`dob_day_month_swapped` | `dob_year_differs` | `dob_month_differs` | `dob_day_differs` |
    `dob_other`, from the deal's date (a `date`, or None) and Famly's ISO string."""
    try:
        actual = date.fromisoformat(str(famly_value or "")[:10])
    except ValueError:
        return "dob_other"
    if expected is None:
        return "dob_other"
    if (expected.year == actual.year and expected.day == actual.month and expected.month == actual.day
            and expected.day != expected.month):
        return "dob_day_month_swapped"
    same = (expected.year == actual.year, expected.month == actual.month, expected.day == actual.day)
    if same == (False, True, True):
        return "dob_year_differs"
    if same == (True, False, True):
        return "dob_month_differs"
    if same == (True, True, False):
        return "dob_day_differs"
    return "dob_other"


def name_difference(deal_name: str, famly_name: str) -> str:
    """`name_case_or_spacing_only` | `name_punctuation_only` | `name_one_contains_the_other` (a
    middle name or an extra word) | `name_different`. Words are compared, never printed."""
    a, b = str(deal_name or "").casefold(), str(famly_name or "").casefold()
    if "".join(a.split()) == "".join(b.split()):
        return "name_case_or_spacing_only"
    strip = lambda text: "".join(ch for ch in text if ch.isalnum())          # noqa: E731
    if strip(a) == strip(b):
        return "name_punctuation_only"
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and (ta <= tb or tb <= ta):
        return "name_one_contains_the_other"
    return "name_different"


def date_gap(prefix: str, famly_value: str | None, expected: str | None) -> str:
    """The size and direction of a start/end difference, as one code, e.g.
    `child_start_famly_later_1_to_7_days`. When a side has no date there is no size."""
    if not famly_value:
        return f"{prefix}_famly_open_ended"
    if not expected:
        return f"{prefix}_deal_has_no_date"
    try:
        days = (date.fromisoformat(famly_value[:10]) - date.fromisoformat(expected[:10])).days
    except ValueError:
        return f"{prefix}_gap_unreadable"
    direction = "famly_later" if days > 0 else "famly_earlier"
    size = abs(days)
    bucket = "1_to_7_days" if size <= 7 else "8_to_31_days" if size <= 31 else "over_31_days"
    return f"{prefix}_{direction}_{bucket}"


def find_contact(child: FamlyChild, contact) -> tuple["FamlyContact | None", bool]:
    """The child's Famly contact for a deal contact: the primary email first, then any additional
    address. (contact or None, whether it was found only through an additional address)."""
    primary = normalise_email(contact.email)
    emails = (set(contact.emails) | {primary}) - {""}
    found = next((c for c in child.contacts if c.email and c.email == primary), None)
    if found is not None:
        return found, False
    found = next((c for c in child.contacts if c.email and c.email in emails), None)
    return found, found is not None


def contact_codes(deal_contacts, child: FamlyChild, index: "Index | None" = None) -> list[str]:
    """For each HubSpot contact on the deal, by canonical email. No names, no phones.

    `contact_missing` also says why: `child_has_no_contacts`,
    `child_has_contacts_but_email_not_matched`, and (cheap, from the same pull)
    `email_matches_a_contact_on_a_different_child_of_the_same_family`."""
    codes: list[str] = []
    for contact in deal_contacts:
        primary = normalise_email(contact.email)
        emails = set(contact.emails) | {primary}              # the primary and the additional addresses
        emails.discard("")
        if not emails:
            codes.append("contact_no_email")
            if contact.is_bill_payer:
                codes.append("bill_payer_contact_missing")
            continue
        # the primary address first (as before), then any additional address of the HubSpot contact
        found, via_additional = find_contact(child, contact)
        if via_additional:
            codes.append("contact_matched_via_additional_email")        # info: rescued by a secondary address
        if found is None:
            codes.append("contact_missing")
            codes.append("child_has_contacts_but_email_not_matched" if child.contacts else "child_has_no_contacts")
            if index is not None and any(index.contact_children.get(e, set()) - {child.id} for e in emails):
                codes.append("email_matches_a_contact_on_a_different_child_of_the_same_family")
            if contact.is_bill_payer:
                codes.append("bill_payer_contact_missing")
            continue
        if not found.relation:
            codes.append("contact_relation_missing")
            if found.relation_empty:
                codes.append("contact_relation_empty")      # sub-code: a blank RELATION, repairable by the planner
        if contact.is_bill_payer and not found.emergency:
            codes.append("bill_payer_not_emergency")
    return codes


def recipient_codes(deal_contacts, payers, child: FamlyChild) -> list[str]:
    """For the deal's bill payer contact: `bill_payer_recipient_present` when it is an invoice
    recipient of the child's payer, `bill_payer_recipient_missing` when the payer exists (one live payer
    matched by the contact's emails, listing this child) and the contact is not. No code when there is no
    such payer (the payer codes cover that) or the contact is not on the child (contact codes cover that).
    Read from `Payer.recipients`, which the payer scan already carries."""
    contact = next((c for c in deal_contacts if c.is_bill_payer), None)
    if contact is None or payers is None:
        return []
    decision = payer_scan.decide(deal_contacts, payers, child.id)
    if decision.status != payer_scan.STATUS_PRESENT:
        return []
    payer = next((p for p in payers if p.id == decision.bill_payer_id), None)
    famly_contact, _via = find_contact(child, contact)
    if payer is None or famly_contact is None or not famly_contact.id:
        return []
    return ["bill_payer_recipient_present" if famly_contact.id in payer.recipients
            else "bill_payer_recipient_missing"]


def payer_codes(deal_contacts, payers, child_id: str) -> list[str]:
    """The planner's own decision, mapped to audit codes, plus sub-codes from the child's
    payer links (ids and shares are compared in memory; only codes come out)."""
    decision = payer_scan.decide(deal_contacts, payers, child_id)
    codes: list[str] = []
    live = payers or []
    matched = next((p for p in live if p.id == decision.bill_payer_id), None)
    if matched is None:
        # the planner's matched payer is only set when exactly one has the contact's email
        contact = next((c for c in deal_contacts if c.is_bill_payer), None)
        emails = (set(contact.emails) | {normalise_email(contact.email)}) - {""} if contact is not None else set()
        same_email = [p for p in live if p.email in emails]
        matched = same_email[0] if len(same_email) == 1 else None

    def share(payer):
        return payer.children.get(child_id) if payer is not None else None

    billing = [p for p in live if payer_scan.bills_the_child(p, child_id)]
    others = [p for p in billing if matched is None or p.id != matched.id]

    if decision.status == payer_scan.STATUS_PRESENT:
        value = share(matched)
        if matched is not None and not payer_scan.lists_child_at(matched, child_id, 1.0):
            codes.append("bill_payer_share_not_full")
            if value is not None and value <= 0:
                codes.append("bill_payer_share_zero")
            elif value is not None and 0 < value < 1:
                codes.append("bill_payer_share_partial")
    elif decision.status == payer_scan.WOULD_CREATE:
        codes.append("bill_payer_missing")
    elif decision.status == payer_scan.WOULD_ATTACH:
        codes += ["bill_payer_not_linked", "bill_payer_share_row_missing"]
    else:
        codes += {
            payer_scan.AMBIGUOUS: ["bill_payer_ambiguous"],
            payer_scan.CHILD_LINKED_ELSEWHERE: ["double_billing_risk"],
            payer_scan.NO_CONTACT: ["bill_payer_no_contact"],
            payer_scan.NO_EMAIL: ["bill_payer_no_email"],
            payer_scan.SCAN_FAILED: ["bill_payer_scan_failed"],
            payer_scan.NAME_INCOMPLETE: ["bill_payer_missing"],
        }.get(decision.flag, [f"bill_payer_{decision.flag}"])

    if payers is not None:
        if not billing:
            codes.append("bill_payer_child_has_no_live_payer")
        elif len(billing) > 1:
            codes.append("bill_payer_child_has_multiple_live_payers")
        if others:
            codes.append("bill_payer_other_payer_billing")
            # the double billing split: who bills at what, against the payer the deal expects
            other_full = any((share(p) is None) or abs(share(p) - 1.0) < 1e-9 for p in others)
            expected_value = share(matched)
            expected_full = expected_value is not None and abs(expected_value - 1.0) < 1e-9
            expected_zero = matched is None or expected_value is None or expected_value <= 0
            if "double_billing_risk" not in codes and matched is not None and bills_positive(matched, child_id):
                codes.append("double_billing_risk")           # the expected payer AND another both bill
            if "double_billing_risk" in codes:
                if other_full and expected_zero:
                    codes.append("other_payer_full_and_expected_zero")
                elif other_full and expected_full:
                    codes.append("both_full")
                else:
                    codes.append("other")
    return codes


def bills_positive(payer, child_id: str) -> bool:
    value = payer.children.get(child_id)
    return value is not None and value > 0


def _end_of(value: str | None) -> str:
    return value or "9999-12-31"


# How close the plan's current monthly estimate must be to the deal's `billable_fees`
# (relative). billable_fees is "around what the fees should be", so this is a loose check.
FEE_TOLERANCE = 0.10
_DAY_KEYS = (("monday", "MONDAY"), ("tuesday", "TUESDAY"), ("wednesday", "WEDNESDAY"),
             ("thursday", "THURSDAY"), ("friday", "FRIDAY"))


def _slot_by_session(institution: str) -> dict[str, str]:
    """Famly session id -> slot (morning / afternoon / full_day) for one institution, over every
    funded variant and the non-funded branch of the local session catalogue. Raises
    SessionCatalogueError when the institution is not in it."""
    from integrations import session_catalogue as catalogue

    entry = catalogue._find_institution((catalogue._read().get("institutions") or {}), institution)
    if not isinstance(entry, dict):
        raise catalogue.SessionCatalogueError("institution not in the session catalogue")
    nodes = [entry.get("non_funded")] + list((entry.get("funded") or {}).values())
    out: dict[str, str] = {}
    for node in nodes:
        for slot, uuid in (node or {}).items():
            if uuid and slot in catalogue.VALID_SLOTS:
                out[str(uuid)] = slot
    return out


def fee_bucket(estimate: float, fees: float) -> str:
    """How far the plan's monthly estimate is from `billable_fees`, as ONE bucket code (the
    first that fits): within 1 pound, under 2 percent, under 10 percent (FEE_TOLERANCE),
    over 10 percent. Counts of these codes are the summary's fee buckets."""
    gap = abs(estimate - fees)
    if gap <= 1.0:
        return "plan_fee_within_1_pound"
    if gap < 0.02 * max(abs(fees), 1.0):
        return "plan_fee_under_2_percent"
    if gap <= FEE_TOLERANCE * max(abs(fees), 1.0):
        return "plan_fee_under_10_percent"
    return "plan_fee_over_10_percent"


def _booked(plan) -> list:
    bookings = [b for part in plan.plan_parts for b in part.session_bookings]
    return bookings or list(plan.session_bookings)


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return None


def compare_plan(plans: list, deal: dict, institution: str | None, today=None) -> list[str]:
    """Compare the child's existing plan with what the DEAL implies, as codes. No request is
    made and nothing is previewed: the expected plan is read straight from the deal.

      dates     the plan's `from` is `actual_start_date_1`, its exclusive `to` is the day after
                the deal's `end_date` (open-ended when the deal has none);
      sessions  the SET of (weekday, slot) from the deal's monday..friday properties, against the
                plan's session bookings (a booking's slot is found from its session id in the
                local session catalogue);
      fees      the plan's current monthly estimate within FEE_TOLERANCE of `billable_fees`.

    Codes: plan_matches | plan_differs + plan_dates_differ / plan_sessions_differ /
    plan_fees_differ | plan_not_derivable + plan_input_dates_missing / plan_input_sessions_missing /
    plan_input_fees_missing / plan_session_catalogue_gap."""
    from actions.plan_write import hubspot_flatten
    from integrations import session_catalogue as catalogue

    try:
        start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
        last_day = derive.parse_hubspot_date(deal.get("end_date"))
    except ValueError:
        start = last_day = None
    expected_from = start.isoformat() if start else None
    # which of the child's plans: the one starting on the expected day, else the latest-starting
    chosen = next((p for p in plans if str(p.from_ or "")[:10] == expected_from), None) \
        or sorted(plans, key=lambda p: str(p.from_ or ""))[-1]

    differs: list[str] = []
    missing: list[str] = []
    buckets: list[str] = []

    if expected_from is None:
        missing.append("plan_input_dates_missing")
    else:
        expected_to = hubspot_flatten.exclusive_end(last_day.isoformat()) if last_day else None
        if str(chosen.from_ or "")[:10] != expected_from or (str(chosen.to or "")[:10] or None) != expected_to:
            differs.append("plan_dates_differ")

    wanted = set()
    unreadable = False
    for key, enum in _DAY_KEYS:
        slot = catalogue.normalise_slot(deal.get(key))
        if not slot:
            continue
        if slot not in catalogue.VALID_SLOTS:
            unreadable = True
        wanted.add((enum, slot))
    if not wanted or unreadable:
        missing.append("plan_input_sessions_missing")
    else:
        try:
            slots = _slot_by_session(institution or "")
        except catalogue.SessionCatalogueError:
            missing.append("plan_session_catalogue_gap")
        else:
            actual = {(str(b.day or "").upper(), slots.get(str(b.session_id), "?")) for b in _booked(chosen)}
            if actual != wanted:
                differs.append("plan_sessions_differ")

    fees = _number(deal.get("billable_fees"))
    if fees is None:
        missing.append("plan_input_fees_missing")
    else:
        estimate = _number(chosen.current_monthly_estimate(today))
        if estimate is None or abs(estimate - fees) > FEE_TOLERANCE * max(abs(fees), 1.0):
            differs.append("plan_fees_differ")
        if estimate is not None:
            buckets.append(fee_bucket(estimate, fees))

    if differs:
        return ["plan_differs", *differs, *missing, *buckets]
    if missing:
        return ["plan_not_derivable", *missing, *buckets]
    return ["plan_matches", *buckets]


def plan_codes(result, deal: dict | None = None, institution: str | None = None, today=None) -> list[str]:
    """From the child's EXISTING plans: none at all, plans that overlap or repeat each other
    (Famly's plan `to` is exclusive; an open end counts as far in the future), and, given the
    deal, the comparison with what the deal implies (`compare_plan`)."""
    plans = list(result.plans)
    if not plans:
        return ["plan_missing"]
    codes: list[str] = []
    spans = sorted((str(p.from_ or "")[:10], _end_of(str(p.to or "")[:10] or None)) for p in plans)
    for (a_from, a_to), (b_from, b_to) in zip(spans, spans[1:]):
        if b_from < a_to:                    # the next plan starts before the previous one ends
            codes.append("plan_overlap_or_duplicate")
            break
    if deal is not None:
        codes += compare_plan(plans, deal, institution, today)
    return codes


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    deal_id: str
    site_code: str | None = None
    stage: str | None = None
    child_id: str | None = None
    found_via: str = NOT_FOUND
    codes: list[str] = field(default_factory=list)


def operations_deals(hubspot, stages: dict[str, tuple[str, str]], stage_names: list[str] | None) -> list[dict]:
    """Every deal in the Operations pipeline (optionally only these stage labels): ONE stage-list
    read plus one search page per 100 deals."""
    wanted = {derive.norm_stage(s) for s in stage_names or []}
    ids = [sid for sid, (pipeline, stage) in stages.items()
           if derive.norm_stage(pipeline) == OPERATIONS and (not wanted or derive.norm_stage(stage) in wanted)]
    if not ids:
        return []
    rows = hubspot.search_deals(
        [{"propertyName": "dealstage", "operator": "IN", "values": ids}], list(hubspot_cfg.DEAL_PROPERTIES))
    return rows


def run(hubspot, gql, rest, *, deal_ids: list[str] | None = None, operations: bool = False,
        stage_names: list[str] | None = None, site_ids: list[str] | None = None,
        include_left: bool = False, deals: list[dict] | None = None) -> tuple[dict, list[Row]]:
    """The audit. Exactly one of `deal_ids` / `operations` / `deals`. Returns (summary, rows).

    `deals`: deal dicts the caller already read (the operations search rows), audited as given with no
    further per-deal HubSpot read (onboard-quality-check passes its in-scope subset).

    `include_left`: plans of a CHILD-ONLY stage (Left Nursery) are neither read nor compared
    unless it is set (plan_missing for them is reported only with it)."""
    from actions.read_child_plans import runner as plans_runner

    stages = hubspot.stage_names()
    if deals is not None:
        pairs = [(str(d["id"]), d) for d in deals]
    elif operations:
        pairs = [(str(d["id"]), d) for d in operations_deals(hubspot, stages, stage_names)]
    else:
        pairs = []
        for deal_id in deal_ids or []:
            try:
                pairs.append((deal_id, hubspot.get_deal(deal_id)))
            except HubSpotError as exc:
                pairs.append((deal_id, {"_error": exc.status_code or 0}))

    if site_ids is None:
        from actions.urn_lookup import runner as urn_runner
        site_ids = urn_runner._all_institution_ids()
    index = Index(fetch_children(gql, site_ids))
    labels = None
    payers_by_site: dict[str, Any] = {}

    rows: list[Row] = []
    for deal_id, deal in pairs:
        row = Row(deal_id=deal_id)
        rows.append(row)
        if "_error" in deal:
            row.codes.append("deal_not_readable")
            continue
        _pipeline, stage = stages.get(str(deal.get("dealstage")), ("", ""))
        row.stage = stage or None
        try:
            row.site_code = derive.site_for_location(deal.get("location_of_interest"))[0]
        except derive.Hold:
            row.site_code = None

        row.found_via, child_ids = index.find(deal, deal_id)
        if not child_ids:
            row.codes.append(CHILD_MISSING)
            continue
        if len(child_ids) > 1:
            row.child_id = None
            row.codes.append(CHILD_AMBIGUOUS)
            continue
        row.child_id = child_ids[0]
        child = index.children[row.child_id]
        row.codes += child_codes(deal, deal_id, stage, child)

        if not derive.is_child_only_stage(stage):
            try:
                if labels is None:
                    labels = links.load_labels(hubspot)
                deal_contacts = links.deal_contacts(hubspot, labels, deal_id)
            except HubSpotError:
                row.codes.append("contacts_not_readable")
                deal_contacts = None
            if deal_contacts is not None:
                row.codes += contact_codes(deal_contacts, child, index)
                site_id = None
                try:
                    site_id = derive.site_for_location(deal.get("location_of_interest"))[1]
                except derive.Hold:
                    pass
                if site_id is not None:
                    if site_id not in payers_by_site:                  # once per site per run
                        from actions.onboard_dry_run.famly_reads import FamlyReads
                        payers_by_site[site_id] = FamlyReads(gql, site_ids=site_ids).bill_payers(site_id)
                    row.codes += payer_codes(deal_contacts, payers_by_site[site_id], child.id)
                    row.codes += recipient_codes(deal_contacts, payers_by_site[site_id], child)

        if derive.is_child_only_stage(stage) and not include_left:
            continue
        try:
            row.codes += plan_codes(plans_runner.run(child.id, client=rest), deal, row.site_code)
        except Exception as exc:  # noqa: BLE001 - one child's plan read must not stop the audit
            logger.warning("onboard-audit: plan read failed (%s)", type(exc).__name__)
            row.codes.append("plans_not_readable")

    return summarise(rows, hubspot, gql, rest), rows


def summarise(rows: list[Row], hubspot, gql, rest) -> dict:
    by_code = Counter(code for r in rows for code in r.codes)
    return {
        "mode": "read_only",
        "dealsRead": len(rows),
        "childrenFoundBy": dict(sorted(Counter(r.found_via for r in rows).items())),
        "childMissing": by_code.get(CHILD_MISSING, 0),
        "byCode": dict(sorted(by_code.items())),
        "byCodeByStage": {code: dict(sorted(Counter(r.stage or "unknown" for r in rows if code in r.codes).items()))
                          for code in sorted(by_code)},
        "feeBuckets": {code.removeprefix("plan_fee_"): n for code, n in sorted(by_code.items())
                       if code.startswith("plan_fee_") and code != "plan_fees_differ"},
        "bySite": dict(sorted(Counter(r.site_code or "unknown" for r in rows).items())),
        "byStage": dict(sorted(Counter(r.stage or "unknown" for r in rows).items())),
        "requests": {"hubspot": getattr(hubspot, "calls", None), "famlyGraphql": getattr(gql, "calls", None),
                     "famlyRest": getattr(rest, "calls", None)},
        "planComparison": "dates, sessions (monday..friday) and fees (billable_fees, within "
                          f"{int(FEE_TOLERANCE * 100)}% of the current monthly estimate); no preview, no write",
    }


def write_csv(path: str, rows: list[Row], only_codes=None) -> int:
    """dealId, siteCode, stage, childId, found_via, codes (`;`-joined). Ids and codes only.
    `only_codes`: keep only rows that carry ANY of these codes (a base code or a sub-code).
    Returns the number of rows written."""
    wanted = {c.strip() for c in only_codes or [] if c and c.strip()}
    rows = [r for r in rows if not wanted or wanted & set(r.codes)]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["dealId", "siteCode", "stage", "childId", "found_via", "codes"])
        for r in rows:
            writer.writerow([r.deal_id, r.site_code or "", r.stage or "", r.child_id or "", r.found_via,
                             ";".join(dict.fromkeys(r.codes))])
    return len(rows)
