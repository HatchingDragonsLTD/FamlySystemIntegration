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

Server-importable: no argparse, no Flask.
"""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.client import GraphQLClient

logger = logging.getLogger(__name__)

QUERY_PATH = Path(__file__).with_name("query.graphql")
OPERATION_NAME = "Children"

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
    client: GraphQLClient, next_cursor: str | None
) -> tuple[list[ChildRow], str | None]:
    """One page of the roster: the parsed rows, and the next cursor (or None)."""
    body = client.execute(
        query_path=QUERY_PATH,
        variables={"next": next_cursor},
        operation_name=OPERATION_NAME,
    )

    data = body.get("data") if isinstance(body, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    listing = children.get("list") if isinstance(children, dict) else None
    result = listing.get("result") if isinstance(listing, dict) else None
    cursor = listing.get("next") if isinstance(listing, dict) else None

    rows = [
        row for row in (_parse_child(node) for node in (result or [])) if row is not None
    ]
    return rows, (cursor or None)


def fetch_roster(client: GraphQLClient | None = None) -> list[ChildRow]:
    """Fetch the FULL child roster, following `next` until it comes back
    empty/null.

    Raises:
        GraphQLError: the response carried a GraphQL `errors` array --
            including, on the very first (cursor-less) call, one naming a
            missing/required argument this query does not supply. Propagated
            deliberately, uncaught: a failure here must stop the fetch and
            surface clearly (see `core.client.GraphQLClient.execute`), never
            be guessed around or silently treated as "no more results".
        GraphQLHTTPError: a non-200 HTTP response. Same reasoning.
    """
    client = client or GraphQLClient()

    rows: list[ChildRow] = []
    cursor: str | None = None

    while True:
        page, cursor = _fetch_page(client, cursor)
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
