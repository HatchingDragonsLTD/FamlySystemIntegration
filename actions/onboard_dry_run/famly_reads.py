"""LIVE, READ-ONLY Famly reads for the onboarding dry run.

Never the roster cache (`roster_cache.db`): the existence checks here must see
Famly as it is now (see the warning in `actions/pull_roster/store.py`). Every
call is a GraphQL query on Famly's PUBLIC API; there is no mutation anywhere in
this package.

Names, emails and phone numbers read here are held in memory for the run only.
They are never returned in a plan, logged or stored: `ContactCopy` carries only
a comparison fingerprint, not the values.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
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
CONTACTS_SCAN_QUERY = HERE / "contacts_scan.graphql"

CHILD_BY_EXT_REF_OP = "OnboardChildByExternalRef"
CHILD_CONTACTS_OP = "OnboardChildContacts"
CONTACTS_SCAN_OP = "OnboardContactsScan"

HUBSPOT_SYSTEM = "HUBSPOT"


def normalise_email(value: Any) -> str:
    return _normalise(value)


@dataclass(frozen=True)
class ContactCopy:
    """One contact record on some child. No personal values: `fingerprint` is a
    hash of the comparable fields, so two copies can be compared for
    differences without the values leaving memory."""

    contact_id: str
    child_id: str
    last_modified: datetime | None
    fingerprint: str


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _fingerprint(node: dict) -> str:
    name = node.get("name")
    full_name = name.get("fullName") if isinstance(name, dict) else ""
    phones = sorted(
        str(p.get("value") or "").strip()
        for p in node.get("phoneNumbers") or []
        if isinstance(p, dict)
    )
    material = "\x1f".join([_normalise(full_name), *phones])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class FamlyReads:
    """The live reads, over one public-API client. `scan()` and `urn_roster()`
    are fetched at most once per instance (one dry-run invocation), so a batch of
    deals makes one of each rather than one per deal."""

    def __init__(self, client: GraphQLClient | None = None, site_ids: list[str] | None = None):
        self._client = client
        self._site_ids = site_ids
        self._scan: dict[str, list[ContactCopy]] | None = None
        self._urn_roster: list | None = None

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
                    out.append((str(row["id"]), _normalise(row.get("email"))))
            cursor = listing.get("next") or None
            if not cursor:
                break
        return out

    def copies_of_email(self, email: str, exclude_child_id: str | None) -> list[ContactCopy]:
        """Every contact record carrying `email` on a child other than
        `exclude_child_id`, one per distinct contact id."""
        target = _normalise(email)
        if not target:
            return []
        found: dict[str, ContactCopy] = {}
        for copy in self._scan_index().get(target, []):
            if copy.child_id != exclude_child_id and copy.contact_id not in found:
                found[copy.contact_id] = copy
        return list(found.values())

    def _scan_index(self) -> dict[str, list[ContactCopy]]:
        if self._scan is not None:
            return self._scan

        index: dict[str, list[ContactCopy]] = {}
        cursor: str | None = None
        while True:
            body = self._public().execute(
                query_path=CONTACTS_SCAN_QUERY,
                variables={
                    "siteIds": self._sites(),
                    "next": cursor,
                    "timeRange": dict(ROSTER_TIME_RANGE),
                },
                operation_name=CONTACTS_SCAN_OP,
            )
            listing = _dig(body, "data", "children", "listBySiteIds")
            if not isinstance(listing, dict):
                break
            for child in listing.get("result") or []:
                if not isinstance(child, dict) or not child.get("id"):
                    continue
                for node in child.get("contacts") or []:
                    if not isinstance(node, dict) or not node.get("id"):
                        continue
                    email = _normalise(node.get("email"))
                    if not email:
                        continue
                    index.setdefault(email, []).append(
                        ContactCopy(
                            contact_id=str(node["id"]),
                            child_id=str(child["id"]),
                            last_modified=_parse_timestamp(node.get("lastModifiedAt")),
                            fingerprint=_fingerprint(node),
                        )
                    )
            cursor = listing.get("next") or None
            if not cursor:
                break

        self._scan = index
        logger.info("onboard_dry_run: live contact scan done (%d distinct emails)", len(index))
        return index


def newest(copies: list[ContactCopy]) -> ContactCopy:
    """The most recently modified copy. A copy with no timestamp ranks oldest;
    ties break on contact id so the choice is deterministic."""
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def key(copy: ContactCopy):
        stamp = copy.last_modified
        if stamp is None:
            stamp = floor
        elif stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (stamp, copy.contact_id)

    return max(copies, key=key)


def copies_differ(copies: list[ContactCopy]) -> bool:
    return len({copy.fingerprint for copy in copies}) > 1


def _dig(body: Any, *keys: str) -> Any:
    node = body
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _distinct(values) -> list[str]:
    return list(dict.fromkeys(str(v) for v in values if v))
