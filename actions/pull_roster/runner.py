"""Daily pull of Famly's children/contacts/bill-payer identifying fields into
the local `roster_cache.db` -- see store.py's module docstring for what this
cache is (and is NOT) for.

DIFFERENT ENDPOINT THAN EVERY OTHER ACTION HERE. Every other GraphQL action in
this project (staff_credentials, pull_groups, urn_lookup) talks to Famly's
INTERNAL app API at `FAMLY_GRAPHQL_URL` (default `https://app.famly.co/graphql`)
-- the same one the Famly web app itself uses. That schema has NO way to bulk
-list children with their `externalId` (it's only obtainable one child at a
time, via `childProfile.externalId(childId)`), and no `contacts`/`billPayers`
list queries shaped like the ones this pull needs.

Famly's SEPARATE PUBLIC API (https://famlyapi.famly.co/v1/graphql, same access
token) does have them, confirmed live on 2026-09-29:

    children { listBySiteIds(siteIds, nextToken: ChildCursor) {
        result { id externalId contacts { id email } }  next } }
    billPayers { listBySiteIds(siteIds, nextToken: BillPayerCursor) {
        result { billPayerId email }  next } }

`siteIds` accepts the same institutionId UUIDs reference/catalogue.json
already stores (confirmed by calling it with them). Contacts are fetched
NESTED inside the children query rather than via the public API's separate
`contacts { list(childIds, nextToken) }` (which requires childIds up front
anyway) -- one call per page already returns every contact for every child on
it, so there is no second round trip needed.

`_public_client()` builds a GraphQLClient pointed at this second endpoint
without disturbing `FAMLY_GRAPHQL_URL`, which every other action still reads
for the internal API.

Institutions come from `reference/catalogue.json`'s `sites` section, same as
pull_sessions/pull_groups. Per-institution failure isolation, `--institution`
filtering, dry-run and a backup-before-write all match those pulls'
conventions -- adapted here to a SQLite file (backup = a timestamped file
copy) instead of a merged JSON document (see store.replace_institution for the
per-institution DELETE+INSERT that plays the JSON pulls' "merge" role).
"""

import dataclasses
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.client import GraphQLClient, GraphQLError, GraphQLHTTPError
from core.config import load_config
from integrations import catalogue
from integrations.catalogue import UnknownInstitutionError  # re-exported; see there

from . import store

logger = logging.getLogger(__name__)

CHILDREN_QUERY_PATH = Path(__file__).with_name("children_query.graphql")
BILL_PAYERS_QUERY_PATH = Path(__file__).with_name("bill_payers_query.graphql")

CHILDREN_OPERATION_NAME = "PullRosterChildren"
BILL_PAYERS_OPERATION_NAME = "PullRosterBillPayers"

# Famly's separate public API. Overridable, same convention as
# FAMLY_GRAPHQL_URL/FAMLY_REST_BASE_URL in core/config.py.
DEFAULT_PUBLIC_GRAPHQL_URL = "https://famlyapi.famly.co/v1/graphql"


def public_graphql_url() -> str:
    override = os.environ.get("FAMLY_PUBLIC_GRAPHQL_URL", "").strip()
    return override or DEFAULT_PUBLIC_GRAPHQL_URL


def _public_client() -> GraphQLClient:
    """A GraphQLClient pointed at the public API, same access token.

    Built from the same Config `core.client.GraphQLClient()` would otherwise
    use, with only `graphql_url` swapped -- so this reads FAMLY_ACCESS_TOKEN
    exactly like every other action, and never touches FAMLY_GRAPHQL_URL
    (which every internal-API action still relies on).
    """
    base_config = load_config()
    public_config = dataclasses.replace(base_config, graphql_url=public_graphql_url())
    return GraphQLClient(config=public_config)


def _normalise_institution_code(code: str) -> str:
    return (code or "").strip().upper()


def _parse_child_and_contacts(
    node: Any,
) -> tuple[store.ChildRow | None, list[store.ContactRow]]:
    if not isinstance(node, dict):
        return None, []

    child = store.ChildRow(id=node.get("id"), external_id=node.get("externalId"))

    contacts = []
    for contact_node in node.get("contacts") or []:
        if not isinstance(contact_node, dict):
            continue
        contacts.append(
            store.ContactRow(id=contact_node.get("id"), email=contact_node.get("email"))
        )

    return child, contacts


def fetch_children_and_contacts(
    site_ids: list[str], client: GraphQLClient | None = None
) -> tuple[list[store.ChildRow], list[store.ContactRow]]:
    """Every child (and every one of their contacts) across `site_ids`.

    Contacts are de-duplicated by id: the same contact (e.g. a parent with two
    children at the same site) would otherwise appear once per child.

    Raises:
        GraphQLError / GraphQLHTTPError: propagated deliberately, uncaught.
    """
    client = client or _public_client()

    children: list[store.ChildRow] = []
    contacts_by_id: dict[str, store.ContactRow] = {}
    cursor: str | None = None

    while True:
        body = client.execute(
            query_path=CHILDREN_QUERY_PATH,
            variables={"siteIds": site_ids, "next": cursor},
            operation_name=CHILDREN_OPERATION_NAME,
        )

        data = body.get("data") if isinstance(body, dict) else None
        listing = (
            (data or {}).get("children", {}).get("listBySiteIds")
            if isinstance(data, dict)
            else None
        )
        if not isinstance(listing, dict):
            break

        for node in listing.get("result") or []:
            child, node_contacts = _parse_child_and_contacts(node)
            if child is not None and child.id:
                children.append(child)
            for contact in node_contacts:
                if contact.id and contact.id not in contacts_by_id:
                    contacts_by_id[contact.id] = contact

        cursor = listing.get("next") or None
        if not cursor:
            break

    return children, list(contacts_by_id.values())


def _parse_bill_payer(node: Any) -> store.BillPayerRow | None:
    if not isinstance(node, dict):
        return None
    return store.BillPayerRow(id=node.get("billPayerId"), email=node.get("email"))


def fetch_bill_payers(
    site_ids: list[str], client: GraphQLClient | None = None
) -> list[store.BillPayerRow]:
    """Every bill payer across `site_ids`, following `next` until empty/null.

    Raises:
        GraphQLError / GraphQLHTTPError: propagated deliberately, uncaught.
    """
    client = client or _public_client()

    rows: list[store.BillPayerRow] = []
    cursor: str | None = None

    while True:
        body = client.execute(
            query_path=BILL_PAYERS_QUERY_PATH,
            variables={"siteIds": site_ids, "next": cursor},
            operation_name=BILL_PAYERS_OPERATION_NAME,
        )

        data = body.get("data") if isinstance(body, dict) else None
        listing = (
            (data or {}).get("billPayers", {}).get("listBySiteIds")
            if isinstance(data, dict)
            else None
        )
        if not isinstance(listing, dict):
            break

        rows.extend(
            row for row in (_parse_bill_payer(n) for n in listing.get("result") or []) if row
        )

        cursor = listing.get("next") or None
        if not cursor:
            break

    return rows


@dataclass
class InstitutionRoster:
    children: list[store.ChildRow] = field(default_factory=list)
    contacts: list[store.ContactRow] = field(default_factory=list)
    bill_payers: list[store.BillPayerRow] = field(default_factory=list)


@dataclass
class PullResult:
    # code -> what was fetched for it this run. Nothing is written to the
    # store here -- see `write_store`.
    rosters: dict[str, InstitutionRoster] = field(default_factory=dict)
    institutions_pulled: list[str] = field(default_factory=list)
    institutions_skipped: dict[str, str] = field(default_factory=dict)
    institutions_failed: dict[str, str] = field(default_factory=dict)


def pull_all(
    client: GraphQLClient | None = None, institutions: list[str] | None = None
) -> PullResult:
    """Pull children/contacts/bill-payers for every institution in
    catalogue.json's sites.

    Nothing is written to the store here -- see `write_store`. A per-
    institution Famly failure (GraphQLError/GraphQLHTTPError) does not abort
    the run: it is recorded under `institutions_failed` and every other
    institution still gets pulled, same as `pull_sessions`.

    Args:
        client: optional GraphQLClient, mainly for tests. Defaults to a
            client pointed at the PUBLIC API (see `_public_client`), not the
            one every other action here uses.
        institutions: optional site codes restricting the pull, same
            case-insensitive matching and same UnknownInstitutionError-before-
            any-call behaviour as pull_sessions/pull_groups.

    Returns:
        A PullResult ready to hand to `write_store`.

    Raises:
        UnknownInstitutionError: `institutions` named a code catalogue.json's
            sites do not have.
    """
    sites = catalogue.load_sites()

    if institutions is not None:
        requested = [_normalise_institution_code(code) for code in institutions]
        unknown = [code for code in requested if code not in sites]
        if unknown:
            raise UnknownInstitutionError(
                f"unknown institution code(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(sites)) or 'none configured'})"
            )
        sites = {code: sites[code] for code in dict.fromkeys(requested)}

    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}
    pulled: list[str] = []
    rosters: dict[str, InstitutionRoster] = {}

    for code, entry in sites.items():
        institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if not institution_id:
            skipped[code] = "no institutionId configured in catalogue.json"
            continue

        try:
            children, contacts = fetch_children_and_contacts([institution_id], client)
            bill_payers = fetch_bill_payers([institution_id], client)
        except (GraphQLError, GraphQLHTTPError) as exc:
            failed[code] = str(exc)
            continue

        rosters[code] = InstitutionRoster(
            children=children, contacts=contacts, bill_payers=bill_payers
        )
        pulled.append(code)

    return PullResult(
        rosters=rosters,
        institutions_pulled=pulled,
        institutions_skipped=skipped,
        institutions_failed=failed,
    )


def backup_path(path: Path) -> Path:
    """A timestamped sibling path, e.g. roster_cache.2026-09-29T120000Z.bak.db."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")


def write_store(result: PullResult, path: Path | None = None) -> Path | None:
    """Back up the existing store file, then replace each pulled institution's
    rows.

    The backup happens BEFORE any write, unconditionally, whenever the file
    already exists -- a bad pull must always be revertable by hand, same
    promise as the JSON reference pulls make about their files.

    An institution NOT in `result.rosters` (skipped or failed this run) is
    left completely untouched -- see `store.replace_institution`.

    Returns:
        The backup path, or None when there was no existing file to back up.
    """
    path = path or store.store_path()
    backup = None

    if path.exists():
        backup = backup_path(path)
        shutil.copy2(path, backup)

    for code, roster in result.rosters.items():
        store.replace_institution(
            code,
            children=roster.children,
            contacts=roster.contacts,
            bill_payers=roster.bill_payers,
            path=path,
        )

    return backup


def summary_payload(
    result: PullResult, *, backup: Path | None = None, dry_run: bool = False
) -> dict:
    """A CLI-friendly summary: counts per institution, no row contents."""
    return {
        "dryRun": dry_run,
        "institutionsPulled": result.institutions_pulled,
        "writtenPerInstitution": {
            code: {
                "children": len(roster.children),
                "contacts": len(roster.contacts),
                "billPayers": len(roster.bill_payers),
            }
            for code, roster in result.rosters.items()
        },
        "institutionsSkipped": result.institutions_skipped,
        "institutionsFailed": result.institutions_failed,
        "backupFile": str(backup) if backup else None,
    }
