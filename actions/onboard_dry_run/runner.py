"""PHASE 1 of the onboarding pipeline, DRY RUN ONLY: for a HubSpot deal, report
what a live run would do about the child and the parent contacts in Famly.

NO WRITES, to Famly or HubSpot. Every call is a read: the HubSpot client has
only a GET path, and the Famly reads are GraphQL queries (see famly_reads.py).
There is no web action and no queue here yet (design doc, section 10).

NEVER THE ROSTER CACHE. Whether a child or contact already exists is decided
from LIVE Famly reads, as the warning in actions/pull_roster/store.py requires.

PERSONAL DATA. Names and emails are read from HubSpot/Famly at run time and held
in memory for this call only (CLAUDE.md). A plan contains ids, codes and counts:
no names, emails, phone numbers or dates of birth. Nothing here logs them.

HOLDS. Anything that could put a child in the wrong room, link the wrong child
or pick between candidates is a HOLD with a reason code, never a guess:
site_unmapped, site_id_missing, group_missing, group_ambiguous, dob_missing,
start_date_missing, date_invalid, dates_inconsistent, child_ambiguous,
contact_no_email, contact_ambiguous_on_child, bill_payer_unclear, deal_not_found.
A deal with any hold is `held`; one without is `ready`; one a read failed for is
`error`.
"""

import logging
from collections import Counter
from typing import Any

from actions.onboard_dry_run import derive, links
from actions.onboard_dry_run.famly_reads import (
    FamlyReads,
    copies_differ,
    newest,
    normalise_email,
)
from core.client import GraphQLError, GraphQLHTTPError
from core.hubspot import HubSpotClient, HubSpotError

logger = logging.getLogger(__name__)

# Child outcomes.
CHILD_FOUND = "child_found"
CHILD_WOULD_CREATE = "child_would_create"
CHILD_AMBIGUOUS = "child_ambiguous"

# Contact statuses.
PRESENT = "present"
WOULD_COPY = "would_copy"
WOULD_CREATE = "would_create"
NO_EMAIL = "no_email"
AMBIGUOUS_ON_CHILD = "ambiguous_on_child"

# Hold reasons owned by this module (derive.py owns the rest).
DEAL_NOT_FOUND = "deal_not_found"
CHILD_AMBIGUOUS_HOLD = "child_ambiguous"
CONTACT_NO_EMAIL = "contact_no_email"
CONTACT_AMBIGUOUS_ON_CHILD = "contact_ambiguous_on_child"
BILL_PAYER_UNCLEAR = "bill_payer_unclear"


class DryRun:
    """One invocation. Holds the per-run memoised reads (label map, stage names,
    live scans) so a batch of deals does each of them once."""

    def __init__(self, hubspot: HubSpotClient, famly: FamlyReads):
        self._hubspot = hubspot
        self._famly = famly
        self._labels: links.LabelMap | None = None
        self._stages: dict[str, tuple[str, str]] | None = None

    def labels(self) -> links.LabelMap:
        if self._labels is None:
            self._labels = links.load_labels(self._hubspot)
        return self._labels

    def _stage_names(self) -> dict[str, tuple[str, str]]:
        if self._stages is None:
            self._stages = self._hubspot.stage_names()
        return self._stages

    # ------------------------------------------------------------------ #
    def plan_deal(self, deal_id: str) -> dict:
        """The dry-run plan for one deal. Never raises for a read failure: that
        becomes `status: error` so one bad deal cannot stop a batch."""
        deal_id = str(deal_id).strip()
        plan: dict[str, Any] = {"dealId": deal_id, "holds": [], "warnings": []}
        try:
            self._plan(deal_id, plan)
        except HubSpotError as exc:
            if exc.status_code == 404:
                plan["holds"].append({"reason": DEAL_NOT_FOUND})
            else:
                plan["error"] = {"source": "hubspot", "status": exc.status_code}
        except (GraphQLError, GraphQLHTTPError) as exc:
            # Never the message: it can echo a response body.
            plan["error"] = {"source": "famly", "type": type(exc).__name__}

        plan["status"] = (
            "error" if "error" in plan else "held" if plan["holds"] else "ready"
        )
        return plan

    def _plan(self, deal_id: str, plan: dict) -> None:
        deal = self._hubspot.get_deal(deal_id)
        holds, warnings = plan["holds"], plan["warnings"]

        plan["stage"] = self._stage(deal)
        site_code = self._derive_site(deal, plan)
        self._derive_group(deal, site_code, plan)
        plan["child"] = self._child(deal, deal_id, plan)
        plan["contacts"] = self._contacts(deal_id, plan["child"], plan)
        plan["quotes"], quote_warnings = links.quote_summary(
            self._hubspot, self.labels(), deal_id
        )
        warnings.extend(quote_warnings)

        # Cross-check only; never decides anything.
        recorded = (deal.get("famly_childid") or "").strip()
        found = plan["child"].get("famlyChildId")
        if recorded and found and recorded != found:
            warnings.append("famly_childid_differs_from_lookup")
        elif recorded and not found and plan["child"]["status"] == CHILD_WOULD_CREATE:
            warnings.append("famly_childid_recorded_but_child_not_found")

    # -- derivations ---------------------------------------------------- #
    def _stage(self, deal: dict) -> dict:
        pipeline, stage = self._stage_names().get(
            str(deal.get("dealstage")), ("", "")
        )
        amount = (deal.get("deposit_amount") or "").strip()
        return {
            "pipeline": pipeline or None,
            "name": stage or None,
            "depositState": derive.deposit_state(pipeline, stage),
            "depositAmount": amount or None,
        }

    def _derive_site(self, deal: dict, plan: dict) -> str | None:
        try:
            code, institution_id = derive.site_for_location(deal.get("location_of_interest"))
        except derive.Hold as hold:
            plan["holds"].append({"reason": hold.reason, **hold.detail})
            return None
        plan["siteCode"] = code
        plan["siteId"] = institution_id
        return code

    def _derive_group(self, deal: dict, site_code: str | None, plan: dict) -> None:
        try:
            title = derive.group_title(
                deal.get("child_date_of_birth"), deal.get("actual_start_date_1")
            )
        except derive.Hold as hold:
            plan["holds"].append({"reason": hold.reason, **hold.detail})
            return
        plan["groupTitle"] = title
        if site_code is None:
            return  # the site hold already explains it; no room can be looked up
        try:
            plan["groupId"] = derive.group_id_for(site_code, title)
        except derive.Hold as hold:
            plan["holds"].append({"reason": hold.reason, **hold.detail})

    # -- child ----------------------------------------------------------- #
    def _child(self, deal: dict, deal_id: str, plan: dict) -> dict:
        ids = self._famly.children_by_deal_id(deal_id)
        via = "external_system_reference"

        if not ids:
            urn = (deal.get("child_urn") or "").strip()
            via = "urn"
            if urn:
                ids = self._famly.children_by_urn(urn)
            else:
                plan["warnings"].append("child_urn_missing")

        if len(ids) > 1:
            plan["holds"].append({"reason": CHILD_AMBIGUOUS_HOLD, "childIds": ids, "via": via})
            return {"status": CHILD_AMBIGUOUS, "candidateIds": ids, "via": via}
        if ids:
            return {"status": CHILD_FOUND, "famlyChildId": ids[0], "via": via}
        return {"status": CHILD_WOULD_CREATE}

    # -- contacts -------------------------------------------------------- #
    def _contacts(self, deal_id: str, child: dict, plan: dict) -> list[dict]:
        deal_contacts = links.deal_contacts(self._hubspot, self.labels(), deal_id)
        holds, warnings = plan["holds"], plan["warnings"]

        bill_payers = sum(1 for c in deal_contacts if c.is_bill_payer)
        if bill_payers != 1:
            holds.append(
                {
                    "reason": BILL_PAYER_UNCLEAR,
                    "billPayerCount": bill_payers,
                    "contactCount": len(deal_contacts),
                    "direction": self.labels().bill_payer_direction,
                }
            )

        emails = [normalise_email(c.email) for c in deal_contacts if c.email]
        if len(emails) != len(set(emails)):
            warnings.append("duplicate_email_on_deal")

        child_id = child.get("famlyChildId")
        on_child = self._famly.child_contacts(child_id) if child_id else []

        out = []
        for contact in deal_contacts:
            entry = {"hubspotContactId": contact.hubspot_id, "isBillPayer": contact.is_bill_payer}
            if not contact.email:
                entry["status"] = NO_EMAIL
                holds.append({"reason": CONTACT_NO_EMAIL, "hubspotContactId": contact.hubspot_id})
                out.append(entry)
                continue

            target = normalise_email(contact.email)
            here = list(dict.fromkeys(cid for cid, mail in on_child if mail == target))
            if len(here) == 1:
                entry.update(status=PRESENT, famlyContactId=here[0])
            elif len(here) > 1:
                entry.update(status=AMBIGUOUS_ON_CHILD, candidateIds=here)
                holds.append(
                    {
                        "reason": CONTACT_AMBIGUOUS_ON_CHILD,
                        "hubspotContactId": contact.hubspot_id,
                        "contactIds": here,
                    }
                )
            else:
                copies = self._famly.copies_of_email(contact.email, child_id)
                if copies:
                    entry.update(
                        status=WOULD_COPY,
                        famlyContactId=newest(copies).contact_id,
                        copyCount=len(copies),
                        copiesDiffer=copies_differ(copies),
                    )
                else:
                    entry["status"] = WOULD_CREATE
            out.append(entry)
        return out


def summarise(plans: list[dict]) -> dict:
    """Counts only: nothing per deal, no ids."""
    holds = Counter(h["reason"] for p in plans for h in p.get("holds", []))
    contacts = Counter(c["status"] for p in plans for c in p.get("contacts", []))
    children = Counter(
        p["child"]["status"] for p in plans if isinstance(p.get("child"), dict)
    )
    deposits = Counter(
        p["stage"]["depositState"] for p in plans if isinstance(p.get("stage"), dict)
    )
    status = Counter(p["status"] for p in plans)
    return {
        "deals": len(plans),
        "ready": status.get("ready", 0),
        "held": status.get("held", 0),
        "error": status.get("error", 0),
        "children": dict(children),
        "contacts": dict(contacts),
        "holdReasons": dict(holds),
        "depositStates": dict(deposits),
    }


def run(
    deal_ids: list[str],
    hubspot: HubSpotClient | None = None,
    famly: FamlyReads | None = None,
) -> dict:
    """Dry-run every deal id. Output: the association labels found, one plan per
    deal and a counts-only summary."""
    dry = DryRun(hubspot or HubSpotClient(), famly or FamlyReads())
    unique = list(dict.fromkeys(str(i).strip() for i in deal_ids if str(i).strip()))

    try:
        labels_report = dry.labels().report()
    except HubSpotError as exc:
        labels_report = {"error": {"source": "hubspot", "status": exc.status_code}}

    plans = [dry.plan_deal(deal_id) for deal_id in unique]
    result = {
        "mode": "dry_run",
        "writes": 0,
        "associationLabels": labels_report,
        "plans": plans,
        "summary": summarise(plans),
    }
    logger.info("onboard_dry_run: %s", result["summary"])
    return result
