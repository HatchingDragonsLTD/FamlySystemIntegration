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
            contact_relation_missing, bill_payer_not_emergency   (skipped for child-only stages;
            parent names and phones are NOT compared: Famly is the source of truth after onboarding)
  payer     bill_payer_missing, bill_payer_not_linked, bill_payer_share_not_full,
            bill_payer_ambiguous, double_billing_risk, bill_payer_no_contact, bill_payer_no_email,
            bill_payer_scan_failed   (skipped for child-only stages)
  plans     plan_missing, plan_overlap_or_duplicate

NOT BUILT (needs guidance): comparing the plans with the plan HubSpot implies. See docs/status.md.
"""

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
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
        for child in children.values():
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
    deal_tokens = _tokens(f"{first} {last}")
    if not deal_tokens:
        codes.append("child_name_not_comparable")
    elif deal_tokens != _tokens(child.full_name):
        codes.append("child_name_differs")

    try:
        dob = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
    except ValueError:
        dob = None
    if dob is None or dob.isoformat() != child.birthday:
        codes.append("child_dob_differs")

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
            if want["start"] is not None and (last_day or None) != (want["end"] or None):
                codes.append("child_end_differs")
    return codes


def contact_codes(deal_contacts, child: FamlyChild) -> list[str]:
    """For each HubSpot contact on the deal, by canonical email. No names, no phones."""
    codes: list[str] = []
    on_child = {c.email: c for c in child.contacts if c.email}
    for contact in deal_contacts:
        email = normalise_email(contact.email)
        if not email:
            codes.append("contact_no_email")
            if contact.is_bill_payer:
                codes.append("bill_payer_contact_missing")
            continue
        found = on_child.get(email)
        if found is None:
            codes.append("contact_missing")
            if contact.is_bill_payer:
                codes.append("bill_payer_contact_missing")
            continue
        if not found.relation:
            codes.append("contact_relation_missing")
        if contact.is_bill_payer and not found.emergency:
            codes.append("bill_payer_not_emergency")
    return codes


def payer_codes(deal_contacts, payers, child_id: str) -> list[str]:
    """The planner's own decision, mapped to audit codes."""
    decision = payer_scan.decide(deal_contacts, payers, child_id)
    if decision.status == payer_scan.STATUS_PRESENT:
        matched = next((p for p in payers or [] if p.id == decision.bill_payer_id), None)
        if matched is not None and not payer_scan.lists_child_at(matched, child_id, 1.0):
            return ["bill_payer_share_not_full"]
        return []
    if decision.status == payer_scan.WOULD_CREATE:
        return ["bill_payer_missing"]
    if decision.status == payer_scan.WOULD_ATTACH:
        return ["bill_payer_not_linked"]
    return {
        payer_scan.AMBIGUOUS: ["bill_payer_ambiguous"],
        payer_scan.CHILD_LINKED_ELSEWHERE: ["double_billing_risk"],
        payer_scan.NO_CONTACT: ["bill_payer_no_contact"],
        payer_scan.NO_EMAIL: ["bill_payer_no_email"],
        payer_scan.SCAN_FAILED: ["bill_payer_scan_failed"],
        payer_scan.NAME_INCOMPLETE: ["bill_payer_missing"],
    }.get(decision.flag, [f"bill_payer_{decision.flag}"])


def _end_of(value: str | None) -> str:
    return value or "9999-12-31"


def plan_codes(result) -> list[str]:
    """From the child's EXISTING plans only: none at all, or plans that overlap or repeat each
    other (Famly's plan `to` is exclusive; an open end counts as far in the future)."""
    plans = list(result.plans)
    if not plans:
        return ["plan_missing"]
    spans = sorted((str(p.from_ or "")[:10], _end_of(str(p.to or "")[:10] or None)) for p in plans)
    for (a_from, a_to), (b_from, b_to) in zip(spans, spans[1:]):
        if b_from < a_to:                    # the next plan starts before the previous one ends
            return ["plan_overlap_or_duplicate"]
    return []


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
        stage_names: list[str] | None = None, site_ids: list[str] | None = None) -> tuple[dict, list[Row]]:
    """The audit. Exactly one of `deal_ids` / `operations`. Returns (summary, rows)."""
    from actions.read_child_plans import runner as plans_runner

    stages = hubspot.stage_names()
    if operations:
        deals = operations_deals(hubspot, stages, stage_names)
        pairs = [(str(d["id"]), d) for d in deals]
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
                row.codes += contact_codes(deal_contacts, child)
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

        try:
            row.codes += plan_codes(plans_runner.run(child.id, client=rest))
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
        "bySite": dict(sorted(Counter(r.site_code or "unknown" for r in rows).items())),
        "byStage": dict(sorted(Counter(r.stage or "unknown" for r in rows).items())),
        "requests": {"hubspot": getattr(hubspot, "calls", None), "famlyGraphql": getattr(gql, "calls", None),
                     "famlyRest": getattr(rest, "calls", None)},
        "planComparison": "not_run: the expected plan is not derived (see docs/status.md); "
                          "plan_missing and plan_overlap_or_duplicate come from the existing plans only",
    }


def write_csv(path: str, rows: list[Row]) -> None:
    """dealId, siteCode, stage, childId, found_via, codes (`;`-joined). Ids and codes only."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["dealId", "siteCode", "stage", "childId", "found_via", "codes"])
        for r in rows:
            writer.writerow([r.deal_id, r.site_code or "", r.stage or "", r.child_id or "", r.found_via,
                             ";".join(dict.fromkeys(r.codes))])
