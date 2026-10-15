"""LIVE, READ-ONLY Famly reads for the onboarding dry run.

Never the roster cache (`roster_cache.db`): the existence checks here must see
Famly as it is now (see the warning in `actions/pull_roster/store.py`). Every
call is a GraphQL query on Famly's PUBLIC API; there is no mutation anywhere in
this package.

Names, emails and phone numbers read here are held in memory for the run only.
They are never returned in a plan, logged or stored.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from actions.pull_roster.runner import ROSTER_TIME_RANGE
from actions.pull_roster.store import _normalise  # the roster's trim + casefold
from actions.urn_lookup import runner as urn_runner
from core.client import GraphQLClient

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
CHILD_BY_EXT_REF_QUERY = HERE / "child_by_ext_ref.graphql"
CHILD_CONTACTS_QUERY = HERE / "child_contacts.graphql"
SITE_CHILDREN_QUERY = HERE / "site_children.graphql"

CHILD_BY_EXT_REF_OP = "OnboardChildByExternalRef"
CHILD_CONTACTS_OP = "OnboardChildContacts"
SITE_CHILDREN_OP = "OnboardSiteChildren"

HUBSPOT_SYSTEM = "HUBSPOT"


GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def normalise_email(value: Any) -> str:
    """Trim + casefold (the roster's convention), and treat googlemail.com as
    gmail.com. FOR COMPARISON ONLY: the address itself is never rewritten
    anywhere. No other fuzzy matching: dots and "+tags" in the local part count."""
    email = _normalise(value)
    local, at, domain = email.rpartition("@")
    if at and domain in GMAIL_DOMAINS:
        return f"{local}@gmail.com"
    return email


def local_part(value: Any) -> str:
    """The normalised part before the @ ("" if there is none)."""
    email = normalise_email(value)
    local, at, _ = email.rpartition("@")
    return local if at else ""


@dataclass(frozen=True)
class SiteChild:
    """A child at a site, for the duplicate check. Held in memory only."""

    child_id: str
    birthday: str | None
    full_name: str


class FamlyReads:
    """The live reads, over one public-API client. `urn_roster()`
    are fetched at most once per instance (one dry-run invocation), so a batch of
    deals makes one of each rather than one per deal."""

    def __init__(self, client: GraphQLClient | None = None, site_ids: list[str] | None = None):
        self._client = client
        self._site_ids = site_ids
        self._urn_roster: list | None = None
        self._site_children: dict[str, list[SiteChild]] = {}

    def _public(self) -> GraphQLClient:
        if self._client is None:
            self._client = urn_runner._public_client()
        return self._client

    def _sites(self) -> list[str]:
        if self._site_ids is None:
            self._site_ids = urn_runner._all_institution_ids()
        return self._site_ids

    # -- child lookup --------------------------------------------------- #
    def children_by_deal_id(self, deal_id: str) -> list[str]:
        """Child ids whose external system reference is HUBSPOT = `deal_id`."""
        body = self._public().execute(
            query_path=CHILD_BY_EXT_REF_QUERY,
            variables={"foreignId": str(deal_id), "system": HUBSPOT_SYSTEM},
            operation_name=CHILD_BY_EXT_REF_OP,
        )
        listing = _dig(body, "data", "children", "listByExternalSystemReference")
        rows = listing.get("result") if isinstance(listing, dict) else None
        return _distinct(row.get("id") for row in rows or [] if isinstance(row, dict))

    def children_by_urn(self, urn: str) -> list[str]:
        """Child ids whose `externalId` is `urn`: the existing LIVE urn search
        in actions/urn_lookup (`fetch_roster`, never its TTL cache)."""
        if self._urn_roster is None:
            self._urn_roster = urn_runner.fetch_roster(self._public())
        return _distinct(row.id for row in urn_runner.find_matches(urn, self._urn_roster))

    def child_exists(self, child_id: str) -> bool:
        """Whether a child with this id exists in Famly NOW (any site, past and
        future included), from the same live roster the urn fallback uses.
        (`children.listByChildIds` was not used: it answers HTTP 500 for an
        unknown id, which cannot be told apart from an outage.)"""
        if self._urn_roster is None:
            self._urn_roster = urn_runner.fetch_roster(self._public())
        return any(row.id == child_id for row in self._urn_roster)

    def children_at_site(self, site_id: str) -> list[SiteChild]:
        """Every child at `site_id`, live, past and future included. Once per
        site per run."""
        if site_id in self._site_children:
            return self._site_children[site_id]

        out: list[SiteChild] = []
        cursor: str | None = None
        while True:
            body = self._public().execute(
                query_path=SITE_CHILDREN_QUERY,
                variables={
                    "siteIds": [site_id],
                    "next": cursor,
                    "timeRange": dict(ROSTER_TIME_RANGE),
                },
                operation_name=SITE_CHILDREN_OP,
            )
            listing = _dig(body, "data", "children", "listBySiteIds")
            if not isinstance(listing, dict):
                break
            for node in listing.get("result") or []:
                if not isinstance(node, dict) or not node.get("id"):
                    continue
                name = node.get("name")
                out.append(
                    SiteChild(
                        child_id=str(node["id"]),
                        birthday=(str(node["birthday"])[:10] if node.get("birthday") else None),
                        full_name=str(name.get("fullName") or "") if isinstance(name, dict) else "",
                    )
                )
            cursor = listing.get("next") or None
            if not cursor:
                break

        self._site_children[site_id] = out
        return out

    # -- contacts -------------------------------------------------------- #
    def child_contacts(self, child_id: str) -> list[tuple[str, str]]:
        """(contact id, normalised email) for each contact on the child."""
        out: list[tuple[str, str]] = []
        cursor: str | None = None
        while True:
            body = self._public().execute(
                query_path=CHILD_CONTACTS_QUERY,
                variables={"childIds": [child_id], "next": cursor},
                operation_name=CHILD_CONTACTS_OP,
            )
            listing = _dig(body, "data", "contacts", "list")
            if not isinstance(listing, dict):
                break
            for row in listing.get("result") or []:
                if isinstance(row, dict) and row.get("id"):
                    out.append((str(row["id"]), normalise_email(row.get("email"))))
            cursor = listing.get("next") or None
            if not cursor:
                break
        return out


def _dig(body: Any, *keys: str) -> Any:
    node = body
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _distinct(values) -> list[str]:
    return list(dict.fromkeys(str(v) for v in values if v))
