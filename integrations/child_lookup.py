"""Look up ONE child's full name, live, for the Slack approval messages.

Slack previews used to identify the child by a bare UUID, which an approver
cannot check by eye. `lookup_child_name` fetches the child's full name from
Famly's PUBLIC API (`children { listByChildIds }` -- the public client pattern
`actions/pull_roster` uses: the same access token, a different endpoint,
`FAMLY_PUBLIC_GRAPHQL_URL`) so the message can read
"Alex Smith (`724d7d15-...`)".

HARD RULES, each pinned by tests:

  * NAME ONLY, ONE CHILD PER CALL. One id in, `id` + `name.fullName` selected,
    nothing else requested (see child_lookup.graphql).
  * NEVER STORED. No cache, no module-level state, nothing written: the name
    is fetched for one message, rendered, and dropped. It is not put in
    `preview_store`, and nothing here logs it -- a failure logs only the
    exception TYPE (never its text, which can echo a response body).
  * NEVER BLOCKS OR BREAKS A PREVIEW. A HARD TOTAL DEADLINE bounds the wait,
    and ANY failure -- the deadline expiring, a network error, an HTTP error, a
    GraphQL error, a missing token, an unexpected shape, no matching row --
    returns None, which every caller renders as the bare id exactly as before.
    This never raises.
  * NEVER TRUSTS A MISMATCHED ROW. The id in the response must equal the id
    asked for; a different child's name beside this child's id would be worse
    than no name.

THE DEADLINE. `requests` only offers per-phase timeouts (connect, then each
read), and a server that keeps dripping bytes can hold a read open far beyond
either, so no `requests` timeout can promise a TOTAL wait. The request therefore
runs on a worker thread and the caller waits on it for at most
`LOOKUP_DEADLINE_SECONDS` in total -- connect, send and read together. When that
expires the caller gets None immediately and the worker is ABANDONED: it is a
daemon thread (it cannot hold up shutdown), its result -- if one ever arrives --
is dropped without being stored, cached or logged, and the per-request timeout
handed to `requests` (the same number) makes it end on its own shortly after.
A healthy call takes ~0.1s.
"""

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Any

from core.client import GraphQLClient
from core.config import load_config

logger = logging.getLogger(__name__)

QUERY_PATH = Path(__file__).with_name("child_lookup.graphql")
OPERATION_NAME = "ChildName"

# The hard cap on the WHOLE lookup, in seconds. See the module docstring.
LOOKUP_DEADLINE_SECONDS = 3

# Also handed to `requests` (per phase), so an ABANDONED worker thread ends by
# itself soon after the deadline instead of lingering. Not what bounds the wait:
# the deadline above is.
LOOKUP_TIMEOUT_SECONDS = LOOKUP_DEADLINE_SECONDS


class LookupDeadlineExceeded(TimeoutError):
    """The whole lookup did not finish inside LOOKUP_DEADLINE_SECONDS."""


def _public_client(timeout: float = LOOKUP_TIMEOUT_SECONDS) -> GraphQLClient:
    """A GraphQLClient on the public API with a short timeout.

    The same construction as `actions/pull_roster/runner._public_client` --
    same Config, only `graphql_url` swapped, so FAMLY_GRAPHQL_URL (which every
    internal-API action relies on) is untouched -- plus the timeout. The
    endpoint comes from pull_roster's own `public_graphql_url`, so there is one
    definition of it. Imported here rather than at module level to keep this
    module free of an import-time dependency on another action.
    """
    from actions.pull_roster.runner import public_graphql_url

    config = dataclasses.replace(load_config(), graphql_url=public_graphql_url())
    return GraphQLClient(config=config, timeout=timeout)


def _execute_within(client: Any, variables: dict, remaining: float) -> Any:
    """`client.execute(...)`, abandoned if it takes longer than `remaining`.

    Runs the request on a daemon worker thread and waits for it at most
    `remaining` seconds -- a TOTAL bound, unlike a `requests` timeout. Whatever
    the worker raises is re-raised here; on expiry `LookupDeadlineExceeded` is
    raised and the worker is left to finish on its own. It writes only to a
    private box nobody reads after that, and logs nothing, so a late response
    -- which may well carry the child's name -- is simply dropped.
    """
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["body"] = client.execute(
                query_path=QUERY_PATH,
                variables=variables,
                operation_name=OPERATION_NAME,
            )
        except Exception as exc:  # noqa: BLE001 - handed back to the caller below
            box["error"] = exc

    worker = threading.Thread(target=work, name="child-name-lookup", daemon=True)
    worker.start()
    worker.join(max(0.0, remaining))

    if worker.is_alive():
        raise LookupDeadlineExceeded(
            f"no answer within {LOOKUP_DEADLINE_SECONDS}s"
        )
    if "error" in box:
        raise box["error"]
    return box["body"]


def _extract_name(body: Any, child_id: str) -> str | None:
    """The full name of the row that IS `child_id`, else None."""
    data = body.get("data") if isinstance(body, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    listing = children.get("listByChildIds") if isinstance(children, dict) else None
    rows = listing.get("result") if isinstance(listing, dict) else None
    if not isinstance(rows, list):
        return None

    wanted = child_id.strip().lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or row_id.strip().lower() != wanted:
            continue
        name = row.get("name")
        full_name = name.get("fullName") if isinstance(name, dict) else None
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()
    return None


def lookup_child_name(child_id: Any, client: GraphQLClient | None = None) -> str | None:
    """The child's full name, or None -- which callers show as the bare id.

    Never raises, never caches, never logs the name, and never takes longer than
    LOOKUP_DEADLINE_SECONDS in total; see the module docstring.

    Args:
        child_id: the Famly child id.
        client: optional client, for tests. Defaults to a short-timeout client
            on the public API.
    """
    if not isinstance(child_id, str) or not child_id.strip():
        return None

    started = time.monotonic()
    try:
        body = _execute_within(
            client or _public_client(),
            {"childIds": [child_id.strip()]},
            LOOKUP_DEADLINE_SECONDS - (time.monotonic() - started),
        )
        name = _extract_name(body, child_id)
    except Exception as exc:  # noqa: BLE001 - a name is a nicety; it must never break a preview
        logger.warning(
            "Child name lookup failed (%s); showing the id instead", type(exc).__name__
        )
        return None

    if name is None:
        logger.warning("Child name lookup found no matching child; showing the id instead")
    return name
