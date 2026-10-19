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
contact_no_email, contact_ambiguous_on_child, contact_unmatched_on_existing_child,
bill_payer_unclear, famly_childid_not_found, child_possible_duplicate,
end_date_before_start, deal_not_found.
A deal with any hold is `held`; one without is `ready`; one a read failed for is
`error`.
"""

import logging
from collections import Counter
from typing import Any

from actions.onboard_dry_run import bill_payers, derive, links
from actions.onboard_dry_run.famly_reads import (
    FamlyReads,
    local_part,
    normalise_email,
)
from core.client import GraphQLError, GraphQLHTTPError
from core import hubspot as hubspot_config
from core.hubspot import HubSpotClient, HubSpotError

logger = logging.getLogger(__name__)

# Child outcomes.
CHILD_FOUND = "child_found"
CHILD_WOULD_CREATE = "child_would_create"
CHILD_AMBIGUOUS = "child_ambiguous"

# Contact statuses.
PRESENT = "present"
WOULD_CREATE = "would_create"
UNMATCHED_ON_EXISTING_CHILD = "contact_unmatched_on_existing_child"
NO_EMAIL = "no_email"
AMBIGUOUS_ON_CHILD = "ambiguous_on_child"
CHILD_UNRESOLVED = "child_unresolved"

# Hold reasons owned by this module (derive.py owns the rest).
DEAL_NOT_FOUND = "deal_not_found"
CHILD_AMBIGUOUS_HOLD = "child_ambiguous"
CONTACT_NO_EMAIL = "contact_no_email"
CONTACT_AMBIGUOUS_ON_CHILD = "contact_ambiguous_on_child"
BILL_PAYER_UNCLEAR = "bill_payer_unclear"
FAMLY_CHILDID_NOT_FOUND = "famly_childid_not_found"
CHILD_POSSIBLE_DUPLICATE = "child_possible_duplicate"
CHILD_NAME_INCOMPLETE = "child_name_incomplete"


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
    def plan_deal(
        self,
        deal_id: str,
        context: dict | None = None,
        created_child_ids=None,
    ) -> dict:
        """The dry-run plan for one deal. Never raises for a read failure: that
        becomes `status: error` so one bad deal cannot stop a batch.

        `context`, if given, is filled with `deal` (the raw allow-listed deal
        properties) and `contacts` (the DealContacts, with their raw properties):
        personal data for the phase-2 writer, held in memory only. It is never
        part of the returned plan.

        `created_child_ids` is the job HISTORY for this deal: the child ids that
        a job (an earlier one, or this one) recorded as the result of a create it
        sent. A child found by the live lookup on the deal id whose id is in that
        set has origin `created_by_pipeline`; any other child, including one found
        only by the URN fallback or one no job record mentions, is `existing` and
        handled more strictly: see `_existing_child_contact`."""
        deal_id = str(deal_id).strip()
        plan: dict[str, Any] = {"dealId": deal_id, "holds": [], "warnings": []}
        context = context if context is not None else {}
        try:
            self._plan(deal_id, plan, context, created_child_ids)
        except HubSpotError as exc:
            if exc.status_code == 404:
                plan["holds"].append({"reason": DEAL_NOT_FOUND})
            else:
                plan["error"] = {"source": "hubspot", "status": exc.status_code}
        except (GraphQLError, GraphQLHTTPError) as exc:
            # Never the message: it can echo a response body.
            plan["error"] = {"source": "famly", "type": type(exc).__name__}

        plan["warnings"] = list(dict.fromkeys(plan["warnings"]))
        plan["status"] = (
            "error" if "error" in plan else "held" if plan["holds"] else "ready"
        )
        return plan

    def _plan(
        self, deal_id: str, plan: dict, context: dict, created_child_ids=None
    ) -> None:
        deal = self._hubspot.get_deal(deal_id)
        context["deal"] = deal
        warnings = plan["warnings"]

        plan["stage"] = self._stage(deal)
        left_nursery = derive.is_left_nursery(plan["stage"]["name"])
        site_code = self._derive_site(deal, plan)
        self._derive_group(deal, site_code, left_nursery, plan)
        self._derive_dates(deal, left_nursery, plan)
        plan["child"] = self._child(deal, deal_id, plan)
        if plan["child"]["status"] == CHILD_FOUND:
            # `created_by_pipeline` needs BOTH: found by the deal-id reference (a
            # pipeline-created child always carries it) and an id the job history
            # says a create produced. Everything else is `existing`.
            plan["child"]["origin"] = (
                "created_by_pipeline"
                if plan["child"]["via"] == "external_system_reference"
                and plan["child"]["famlyChildId"] in (created_child_ids or ())
                else "existing"
            )
        if plan["child"]["status"] == CHILD_WOULD_CREATE:
            self._check_creatable(deal, plan)
            self._check_possible_duplicate(deal, plan)
        plan["contacts"] = self._contacts(deal_id, plan["child"], plan, context)
        if bill_payers.enabled() and plan.get("siteId"):
            self._bill_payer(plan, context)
        plan["quotes"], quote_warnings = links.quote_summary(
            self._hubspot, self.labels(), deal_id
        )
        warnings.extend(quote_warnings)

        if plan["quotes"]["count"] == 0:
            warnings.append("no_quote")
        if plan["stage"]["depositState"] in (derive.DEPOSIT_UNPAID, derive.DEPOSIT_PAID) and (
            plan["stage"]["depositAmount"] is None
        ):
            warnings.append("deposit_amount_missing")

    def _bill_payer(self, plan: dict, context: dict) -> None:
        """The bill payer decision at PLAN time (ONBOARD_BILL_PAYER=on): the child's site
        only, live payers, canonical email. Flags never hold the plan: they are warnings
        and the stage ends `needs_review`. The plan carries a status, a flag code, the
        matched id and a match count (ids and counts only)."""
        child = plan["child"]
        child_id = child.get("famlyChildId") if child.get("status") == CHILD_FOUND else None
        payers = self._famly.bill_payers(plan["siteId"])
        decision = bill_payers.decide(context.get("contacts") or [], payers, child_id)
        plan["billPayer"] = decision.as_plan()
        if decision.flag:
            plan["warnings"].append(decision.flag)

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

    def _derive_group(
        self, deal: dict, site_code: str | None, left_nursery: bool, plan: dict
    ) -> None:
        if left_nursery:
            # A child who has left is placed in Voyagers whatever their age, so
            # the age rule is skipped (and a missing date of birth is not a hold).
            title = derive.VOYAGERS
            plan["groupReason"] = derive.LEFT_NURSERY_VOYAGERS
        else:
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

    def _derive_dates(self, deal: dict, left_nursery: bool, plan: dict) -> None:
        """firstDay = actual_start_date_1, lastDay = the deal's `end_date`, or, when
        it is empty, the first 31 August on or after the child's 4th birthday. The
        plan says only whether each is present and whether the last day was
        DEFAULTED, never the dates.

        HOLDS: a Left Nursery deal without a start date; an end date (given or
        defaulted) BEFORE the start date (`end_date_before_start`, e.g. a child
        already past the default date when they start); an unparseable date.
        WARNING: an end date (given or defaulted) before today on a deal that is not
        Left Nursery (`end_date_in_past`): Famly would treat the child as inactive.
        A past end date is expected for Left Nursery, so it says nothing there."""
        holds, warnings = plan["holds"], plan["warnings"]

        def hold_once(reason: str) -> None:
            if reason not in [h["reason"] for h in holds]:
                holds.append({"reason": reason})

        try:
            start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
            end, defaulted = derive.resolve_end_date(deal)
        except ValueError:
            hold_once(derive.DATE_INVALID)
            start, end, defaulted = None, None, False

        plan["dates"] = {
            "firstDayPresent": start is not None,
            "lastDayPresent": end is not None,
            "lastDayDefaulted": defaulted,
        }

        if left_nursery and start is None and not deal.get("actual_start_date_1"):
            hold_once(derive.START_DATE_MISSING)
        if start is not None and end is not None and end < start:
            hold_once(derive.END_DATE_BEFORE_START)
        if end is not None and not left_nursery and end < derive.today():
            warnings.append("end_date_in_past")

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

        # The id HubSpot already holds must be a real child. Checked live; if it
        # is not, nothing about this deal can be trusted, so HOLD.
        recorded = (deal.get("famly_childid") or "").strip()
        if recorded and not self._famly.child_exists(recorded):
            plan["holds"].append({"reason": FAMLY_CHILDID_NOT_FOUND, "famlyChildId": recorded})
            return {"status": CHILD_UNRESOLVED, "via": via}

        if ids:
            if recorded and recorded != ids[0]:
                plan["warnings"].append("famly_childid_differs_from_lookup")
            return {"status": CHILD_FOUND, "famlyChildId": ids[0], "via": via}
        if recorded:
            # No reference or urn match, but the deal's own recorded child exists.
            return {"status": CHILD_FOUND, "famlyChildId": recorded, "via": "famly_childid"}
        return {"status": CHILD_WOULD_CREATE}

    def _check_creatable(self, deal: dict, plan: dict) -> None:
        """What a child create needs that the room and site rules do not already
        hold for: a first and last name and a date of birth."""
        holds = plan["holds"]
        reasons = [h["reason"] for h in holds]
        if not (
            str(deal.get("child_first_name") or "").strip()
            and str(deal.get("child_last_name") or "").strip()
        ):
            holds.append({"reason": CHILD_NAME_INCOMPLETE})
        try:
            dob = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
        except ValueError:
            dob = None
        if dob is None and not any(
            r in reasons for r in (derive.DOB_MISSING, derive.DATE_INVALID)
        ):
            holds.append({"reason": derive.DOB_MISSING})

    def _check_possible_duplicate(self, deal: dict, plan: dict) -> None:
        """Before a child would be created: any child at the same site with the
        same date of birth AND a matching name is a HOLD (ids only). Names and
        dates are compared in memory and never output or logged."""
        site_id = plan.get("siteId")
        try:
            dob = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
        except ValueError:
            dob = None
        if not site_id or dob is None:
            plan["warnings"].append("duplicate_check_skipped")
            return

        same_day = [
            c for c in self._famly.children_at_site(site_id) if c.birthday == dob.isoformat()
        ]
        names = _hubspot_child_names(deal)
        if not names:
            # No name to compare: every same-site, same-birth-date child counts.
            plan["warnings"].append("child_name_unavailable_for_duplicate_check")
            matches = same_day
        else:
            matches = [c for c in same_day if _names_match(c.full_name, names, deal)]
        if matches:
            plan["holds"].append(
                {"reason": CHILD_POSSIBLE_DUPLICATE, "childIds": [c.child_id for c in matches]}
            )

    # -- contacts -------------------------------------------------------- #
    def _contacts(
        self, deal_id: str, child: dict, plan: dict, context: dict | None = None
    ) -> list[dict]:
        deal_contacts = links.deal_contacts(self._hubspot, self.labels(), deal_id)
        if context is not None:
            context["contacts"] = deal_contacts
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
            entry = {
                "hubspotContactId": contact.hubspot_id,
                "isBillPayer": contact.is_bill_payer,
                "labels": contact.labels,
                "phoneSource": contact.phone_source,
                "phoneType": links.phone_type(contact.phone_source),
                "hasEmail": bool(contact.email),
                "hasCompleteAddress": contact.has_complete_address,
                "hasFirstAndLastName": contact.has_first_and_last_name,
                # Only the bill payer is the emergency contact.
                "emergencyContact": contact.is_bill_payer,
                "records": [{"key": "RELATION", "value": "Parent"}],
            }
            if contact.phone_source == "none":
                warnings.append("phone_missing")
            if not contact.has_complete_address:
                warnings.append("address_incomplete")
            if not contact.email:
                entry["status"] = NO_EMAIL
                holds.append({"reason": CONTACT_NO_EMAIL, "hubspotContactId": contact.hubspot_id})
            elif child["status"] == CHILD_FOUND:
                self._existing_child_contact(
                    contact, on_child, entry, holds,
                    own_child=child.get("origin") == "created_by_pipeline",
                )
            elif child["status"] == CHILD_WOULD_CREATE:
                self._new_child_contact(contact, entry)
            else:
                entry["status"] = CHILD_UNRESOLVED
            out.append(entry)
        return out

    def _existing_child_contact(
        self, contact, on_child, entry: dict, holds: list, own_child: bool = False
    ) -> None:
        """The child exists. A parent already on it (canonical email: trimmed,
        lower-cased, googlemail.com = gmail.com) is `present`.

        One that is not:
          * on a child that ALREADY EXISTED when the job started: a HOLD,
            `contact_unmatched_on_existing_child`, with the ids of contacts on the
            child that share the part before the @ (ids only: evidence for a
            person, never a match). A contact is never added to a child somebody
            else created on a guess.
          * on a child the PIPELINE created (per the job history): `would_create`,
            as the child was ours; this is how a re-run after a partial failure
            completes the contacts that are missing.
        """
        target = normalise_email(contact.email)
        here = sorted(dict.fromkeys(cid for cid, mail in on_child if mail == target),
                      key=links.id_sort_key)
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
        elif own_child:
            entry["status"] = WOULD_CREATE
        else:
            wanted = local_part(contact.email)
            candidates = sorted(
                dict.fromkeys(
                    cid for cid, mail in on_child if wanted and local_part(mail) == wanted
                ),
                key=links.id_sort_key,
            )
            entry.update(status=UNMATCHED_ON_EXISTING_CHILD, candidateIds=candidates)
            holds.append(
                {
                    "reason": UNMATCHED_ON_EXISTING_CHILD,
                    "hubspotContactId": contact.hubspot_id,
                    "candidateIds": candidates,
                }
            )

    def _new_child_contact(self, contact, entry: dict) -> None:
        """The child does not exist yet, so every contact is created."""
        entry["status"] = WOULD_CREATE


def _hubspot_child_names(deal: dict) -> list[str]:
    """The child's name from the deal, as comparable strings (in memory only)."""
    values = [
        " ".join(str(deal.get(p) or "").split()) for p in hubspot_config.CHILD_NAME_PROPERTIES
    ]
    values = [v for v in values if v]
    if not values:
        return []
    return list(dict.fromkeys([" ".join(values), *values]))


def _tokens(name: str) -> frozenset:
    return frozenset(name.casefold().split())


def _names_match(famly_name: str, hubspot_names: list[str], deal: dict) -> bool:
    """A Famly child's name equals the deal's child name (same words, any
    order), or appears in the deal name. In memory only."""
    famly_tokens = _tokens(famly_name)
    if not famly_tokens:
        return False
    if any(_tokens(n) == famly_tokens for n in hubspot_names):
        return True
    dealname = " ".join(str(deal.get("dealname") or "").casefold().split())
    return " ".join(famly_name.casefold().split()) in dealname


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


# Contact fields that stay in the plan for DISPLAY and diagnostics but are NOT part
# of what Gate A approves. `labels` is the raw list of association labels on a
# contact's link to the deal. The only label-derived value that changes behaviour is
# `isBillPayer`, which stays in the hashed part, so a label's order or an irrelevant
# extra label can never change the digest.
#
# IF A LATER PHASE WRITES DIFFERENTLY DEPENDING ON ANOTHER LABEL (for example the
# non-parent label that decides app access), THAT LABEL MUST BE ADDED BACK TO THE
# HASHED PART, as a SORTED SET (e.g. a sorted `appAccessLabels` list on the entry).
UNHASHED_CONTACT_FIELDS = ("labels",)


def hashed_plan(plan: dict) -> dict:
    """The part of the plan that is hashed: the plan without the display-only
    contact fields. Everything else, every list in it in canonical order, is in."""
    view = dict(plan)
    if isinstance(view.get("billPayer"), dict):
        # The field NAMES that differ are shown, not approved: only the decision, the
        # matched id and the candidate count are part of the digest.
        view["billPayer"] = {k: v for k, v in view["billPayer"].items() if k != "differs"}
    if isinstance(view.get("contacts"), list):
        view["contacts"] = [
            {k: v for k, v in c.items() if k not in UNHASHED_CONTACT_FIELDS}
            if isinstance(c, dict) else c
            for c in view["contacts"]
        ]
    return view


def plan_hash(plan: dict) -> str:
    """A hash of the plan JSON (see `hashed_plan`). The plan holds ids, codes and
    counts only (no personal data), so the hash is safe to store; if anything that
    affects what is written, or that the approver saw, changes, it changes."""
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(hashed_plan(plan), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
