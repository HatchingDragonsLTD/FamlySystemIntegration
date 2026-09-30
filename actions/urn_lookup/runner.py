"""TEMPORARY BACKFILL UTILITY -- resolve a HubSpot deal's `urn` to a Famly
`childId` by matching against Famly's child roster.

DELETE THIS WHOLE PACKAGE (`actions/urn_lookup/`) once the one-off/periodic
backfill it exists for is done, along with its one-line entry in
`web_registry.py`. Grep for "urn_lookup" to find every place it is wired in.

This module never writes to Famly or HubSpot: `fetch_roster`/`get_roster` are
read-only GraphQL queries, and matching is a pure in-memory comparison. The
web layer (`web.py`) just reports the resolved childId (or the lack of one)
in the standard envelope; a HubSpot workflow step is what actually writes it
back into the deal property.

DIFFERENT ENDPOINT THAN THE ORIGINAL VERSION OF THIS FILE USED. The query
this action shipped with (`children { list(next: $next) { ... } } }`) was
never live-tested and does not exist on Famly's INTERNAL app API at
FAMLY_GRAPHQL_URL -- that schema has no bulk way to list a child's
`externalId` at all (only one child at a time, via
`childProfile.externalId(childId)`). Confirmed live on 2026-09-30 the same
way actions/pull_roster/runner.py was: Famly's SEPARATE PUBLIC API
(https://famlyapi.famly.co/v1/graphql, same access token) has
`children.listBySiteIds(siteIds, nextToken: ChildCursor)`, which does return
`externalId` and `name.fullName` in bulk. `_public_client()` below mirrors
`pull_roster`'s helper of the same name -- see that module's docstring for
the full internal-vs-public API story.

`siteIds` is a REQUIRED argument on the public API, unlike the old (broken)
query -- so fetching "the whole roster" now means every institutionId
reference/catalogue.json's `sites` section knows about, passed in one call
(confirmed live that several siteIds in one call return every match across
all of them). This is a deliberate, narrow fix: it keeps this action's
existing "no institution filter, one flat roster" behaviour by reusing the
same catalogue every other pull already reads, rather than adding a new
per-institution mode to a utility slated for deletion.

Server-importable: no argparse, no Flask.
"""

import dataclasses
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.client import GraphQLClient
from core.config import load_config
from integrations import catalogue

logger = logging.getLogger(__name__)

QUERY_PATH = Path(__file__).with_name("query.graphql")
OPERATION_NAME = "UrnLookupChildren"

# Famly's separate public API. Same env var and default pull_roster uses --
# both actions hit the same endpoint for the same reason (see module
# docstring above).
DEFAULT_PUBLIC_GRAPHQL_URL = "https://famlyapi.famly.co/v1/graphql"


def public_graphql_url() -> str:
    override = os.environ.get("FAMLY_PUBLIC_GRAPHQL_URL", "").strip()
    return override or DEFAULT_PUBLIC_GRAPHQL_URL


def _public_client() -> GraphQLClient:
    """A GraphQLClient pointed at the public API, same access token.

    Mirrors actions/pull_roster/runner.py's helper of the same name: only
    `graphql_url` is swapped from the Config `GraphQLClient()` would otherwise
    build, so this still reads FAMLY_ACCESS_TOKEN like every other action and
    never touches FAMLY_GRAPHQL_URL.
    """
    base_config = load_config()
    public_config = dataclasses.replace(base_config, graphql_url=public_graphql_url())
    return GraphQLClient(config=public_config)


def _all_institution_ids() -> list[str]:
    """Every institutionId reference/catalogue.json's sites section has.

    Raises:
        ValueError: no site in the catalogue has an institutionId configured
            -- the public API's `siteIds` argument is required, so there is
            nothing this backfill could fetch. Surfaced clearly rather than
            silently querying with an empty list.
    """
    ids: list[str] = []
    for entry in catalogue.load_sites().values():
        institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if institution_id and institution_id not in ids:
            ids.append(institution_id)

    if not ids:
        raise ValueError(
            "no institutionId configured for any site in reference/catalogue.json "
            "-- urn_lookup has nothing to query"
        )

    return ids


# How long the in-memory roster cache stays fresh before a lookup refetches
# it. 15 minutes by default: this backfill is typically driven by a BATCH of
# webhooks arriving close together, so the goal is one fetch per batch rather
# than one per request -- the first call in a window fetches fresh, every
# other call within the same window reuses it, and a call after the window
# expires triggers exactly one refetch and starts the window over.
DEFAULT_CACHE_TTL_SECONDS = 900


def cache_ttl_seconds() -> float:
    """The configured TTL, read fresh each time (like preview_store.ttl_hours())
    so a changed environment variable takes effect without a restart."""
    raw = os.environ.get("URN_LOOKUP_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "URN_LOOKUP_CACHE_TTL_SECONDS=%r is not a number; using %s",
            raw,
            DEFAULT_CACHE_TTL_SECONDS,
        )
        return DEFAULT_CACHE_TTL_SECONDS


# Note on the cache below: it is MODULE-LEVEL, IN-MEMORY, PER-PROCESS. Under
# gunicorn each worker imports this module separately and keeps its own copy
# -- there is no cross-worker sharing or invalidation. That is fine here: the
# child roster changes slowly, a handful of workers independently refetching
# on their own clocks costs nothing that matters for a backfill, and there is
# no shared store worth the complexity for a temporary utility.
_cache_rows: list["ChildRow"] | None = None
_cache_fetched_at: float | None = None


@dataclass
class ChildRow:
    """One roster entry -- only the fields this action actually uses."""

    id: str | None = None
    external_id: str | None = None
    full_name: str | None = None


def _parse_child(node: Any) -> ChildRow | None:
    if not isinstance(node, dict):
        return None
    name = node.get("name")
    full_name = name.get("fullName") if isinstance(name, dict) else None
    return ChildRow(
        id=node.get("id"),
        external_id=node.get("externalId"),
        full_name=full_name,
    )


def _fetch_page(
    client: GraphQLClient, site_ids: list[str], next_cursor: str | None
) -> tuple[list[ChildRow], str | None]:
    """One page of the roster: the parsed rows, and the next cursor (or None)."""
    body = client.execute(
        query_path=QUERY_PATH,
        variables={"siteIds": site_ids, "next": next_cursor},
        operation_name=OPERATION_NAME,
    )

    data = body.get("data") if isinstance(body, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    listing = children.get("listBySiteIds") if isinstance(children, dict) else None
    result = listing.get("result") if isinstance(listing, dict) else None
    cursor = listing.get("next") if isinstance(listing, dict) else None

    rows = [
        row for row in (_parse_child(node) for node in (result or [])) if row is not None
    ]
    return rows, (cursor or None)


def fetch_roster(client: GraphQLClient | None = None) -> list[ChildRow]:
    """Fetch the FULL child roster (every institutionId configured in
    reference/catalogue.json, in one paginated pull), following `next` until
    it comes back empty/null.

    Args:
        client: optional GraphQLClient, mainly for tests. Defaults to a
            client pointed at the PUBLIC API (see `_public_client`), not the
            one every other (internal-API) action here uses.

    Raises:
        ValueError: no site in the catalogue has an institutionId configured
            (see `_all_institution_ids`).
        GraphQLError: the response carried a GraphQL `errors` array.
            Propagated deliberately, uncaught: a failure here must stop the
            fetch and surface clearly (see `core.client.GraphQLClient.execute`),
            never be guessed around or silently treated as "no more results".
        GraphQLHTTPError: a non-200 HTTP response. Same reasoning.
    """
    client = client or _public_client()
    site_ids = _all_institution_ids()

    rows: list[ChildRow] = []
    cursor: str | None = None

    while True:
        page, cursor = _fetch_page(client, site_ids, cursor)
        rows.extend(page)
        if not cursor:
            break

    return rows


def get_roster(
    client: GraphQLClient | None = None, *, force_refresh: bool = False
) -> list[ChildRow]:
    """The cached roster, refetching when stale (or when `force_refresh`).

    `client` and `force_refresh` exist mainly for tests; the web handler calls
    this with neither.
    """
    global _cache_rows, _cache_fetched_at

    if force_refresh or not _cache_is_fresh():
        _cache_rows = fetch_roster(client)
        _cache_fetched_at = time.monotonic()
        logger.info("urn_lookup: refetched the child roster (%d rows)", len(_cache_rows))

    return _cache_rows


def _cache_is_fresh() -> bool:
    return (
        _cache_rows is not None
        and _cache_fetched_at is not None
        and (time.monotonic() - _cache_fetched_at) < cache_ttl_seconds()
    )


def _reset_cache_for_tests() -> None:
    """Clear the cache. Tests only -- never called by the app."""
    global _cache_rows, _cache_fetched_at
    _cache_rows = None
    _cache_fetched_at = None


def _normalise(value: Any) -> str:
    """Trim and casefold. `""` for anything that is not a usable string."""
    return str(value).strip().casefold() if isinstance(value, str) and value.strip() else ""


def find_matches(urn: Any, roster: list[ChildRow]) -> list[ChildRow]:
    """Children whose externalId matches `urn`, normalised on both sides.

    A child with an empty/null externalId is skipped entirely -- there is
    nothing on their side to compare against, so it can never match (and
    never collide with another equally-empty one).
    """
    target = _normalise(urn)
    if not target:
        return []

    return [
        row for row in roster if row.external_id and _normalise(row.external_id) == target
    ]
