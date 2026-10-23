"""The child URN service: the returning-child guard, the numbers already in use, and the
race-free allocation. Reads HubSpot (deal search) and Famly (the live roster and every
site's children); the only write is the allocation row in the local store.

Names and dates of birth are compared IN MEMORY and never returned: a candidate is reported
by Famly child id, deal id and URN only.
"""

import logging

from actions.onboard_dry_run import derive, urn as urn_fmt
from actions.onboard_dry_run.runner import _hubspot_child_names, _names_match
from core import hubspot as hubspot_cfg
from core.hubspot import HubSpotError

from . import store

logger = logging.getLogger(__name__)


class UrnService:
    def __init__(self, hubspot, famly):
        self._hubspot = hubspot
        self._famly = famly
        self._hubspot_numbers: dict[str, int] | None = None
        self._famly_numbers: dict[str, int] | None = None

    # -- what is already in use ------------------------------------------ #
    def _highest_hubspot(self) -> dict[str, int]:
        """Highest NEW-format number per site suffix on any deal's child_urn. One search over
        the deals that have a child_urn (a handful of pages), memoised per instance."""
        if self._hubspot_numbers is None:
            top: dict[str, int] = {}
            rows = self._hubspot.search_deals(
                [{"propertyName": "child_urn", "operator": "HAS_PROPERTY"}], ["child_urn"])
            for row in rows:
                parsed = urn_fmt.parse_urn(row.get("child_urn"))
                if parsed:
                    top[parsed[0]] = max(top.get(parsed[0], 0), parsed[1])
            self._hubspot_numbers = top
        return self._hubspot_numbers

    def _highest_famly(self) -> dict[str, int]:
        """Same, over the live Famly roster's externalId (ONE paginated pull of every site,
        the same fetch the URN lookup uses, memoised per instance)."""
        if self._famly_numbers is None:
            top: dict[str, int] = {}
            for child in self._famly.urn_roster():
                parsed = urn_fmt.parse_urn(child.external_id)
                if parsed:
                    top[parsed[0]] = max(top.get(parsed[0], 0), parsed[1])
            self._famly_numbers = top
        return self._famly_numbers

    def floor(self, suffix: str) -> int:
        return max(self._highest_hubspot().get(suffix, 0), self._highest_famly().get(suffix, 0))

    def _in_use(self, urn: str) -> bool:
        """The candidate exists as a child_urn in HubSpot or an externalId in Famly NOW."""
        if self._hubspot.search_deals(
                [{"propertyName": "child_urn", "operator": "EQ", "value": urn}], ["child_urn"]):
            return True
        return bool(self._famly.children_by_urn(urn))

    def _first_free(self, suffix: str):
        def first_free(number: int) -> int:
            while True:
                if number > urn_fmt.MAX_NUMBER:
                    raise ValueError("urn sequence exhausted")
                if not self._in_use(urn_fmt.format_urn(suffix, number)):
                    return number
                number += 1
        return first_free

    # -- the service ------------------------------------------------------ #
    def own(self, deal_id: str) -> str | None:
        """The URN already allocated to this deal (its own earlier allocation), or None."""
        held = store.urn_allocation_for_deal(deal_id)
        return urn_fmt.format_urn(held[0], held[1]) if held else None

    def peek(self, site_code: str) -> tuple[str, int]:
        """(suffix, the number that WOULD be used next). Reserves nothing."""
        suffix = urn_fmt.site_suffix(site_code)
        start = max(store.urn_max_allocated(suffix), self.floor(suffix)) + 1
        return suffix, self._first_free(suffix)(start)

    def allocate(self, deal_id: str, job_id: str | None, site_code: str) -> str:
        """Reserve (or get back) this deal's URN. Raises on any read failure: nothing is
        reserved from a guess."""
        suffix = urn_fmt.site_suffix(site_code)
        held_suffix, number = store.allocate_urn(
            deal_id, job_id, suffix, self._first_free(suffix), floor=self.floor(suffix))
        return urn_fmt.format_urn(held_suffix, number)

    def returning_candidates(self, deal: dict, deal_id: str, own_urn: str | None = None) -> dict:
        """Possible existing children for this child, as ids only:
        {"childIds": [...], "urns": [...], "dealIds": [...]}.

        (a) Famly, ALL sites, past and future: same date of birth and a matching name (the
        possible-duplicate rule, widened from one site to every site);
        (b) HubSpot: OTHER deals with the same child name and date of birth that have a
        child_urn. The deal itself and its own allocation are ignored."""
        try:
            dob = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
        except ValueError:
            dob = None
        names = _hubspot_child_names(deal)
        child_ids: list[str] = []
        urns: list[str] = []
        deal_ids: list[str] = []
        if dob is None:
            return {"childIds": [], "urns": [], "dealIds": []}

        for site_id in self._famly.site_ids():
            for child in self._famly.children_at_site(site_id):
                if child.birthday != dob.isoformat():
                    continue
                if names and not _names_match(child.full_name, names, deal):
                    continue
                if own_urn and child.external_id and child.external_id.strip().upper() == own_urn:
                    continue
                if child.child_id not in child_ids:
                    child_ids.append(child.child_id)
                if child.external_id and child.external_id not in urns:
                    urns.append(child.external_id)

        first, last = (str(deal.get(p) or "").strip() for p in hubspot_cfg.CHILD_NAME_PROPERTIES[:2]) \
            if len(hubspot_cfg.CHILD_NAME_PROPERTIES) >= 2 else ("", "")
        if first and last:
            props = ["child_urn", "child_date_of_birth", "dealname", *hubspot_cfg.CHILD_NAME_PROPERTIES]
            for row in self._hubspot.search_deals(
                    [{"propertyName": hubspot_cfg.CHILD_NAME_PROPERTIES[0], "operator": "EQ", "value": first},
                     {"propertyName": hubspot_cfg.CHILD_NAME_PROPERTIES[1], "operator": "EQ", "value": last},
                     {"propertyName": "child_urn", "operator": "HAS_PROPERTY"}], props):
                if row["id"] == str(deal_id):
                    continue
                try:
                    other = derive.parse_hubspot_date(row.get("child_date_of_birth"))
                except ValueError:
                    continue
                if other != dob:
                    continue
                if own_urn and str(row.get("child_urn") or "").strip().upper() == own_urn:
                    continue
                deal_ids.append(row["id"])
                urn = str(row.get("child_urn") or "").strip()
                if urn and urn not in urns:
                    urns.append(urn)
        return {"childIds": child_ids, "urns": urns, "dealIds": deal_ids}
