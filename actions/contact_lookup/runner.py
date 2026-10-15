"""Resolve a contact's email to the existing Famly contact id and bill payer id.

THIS IS A LOOKUP, for linking HubSpot records to Famly ids that already exist
(a HubSpot workflow writes the returned ids onto the contact as properties).
IT MUST NEVER BE USED AS THE EXISTENCE CHECK for the future contact-creation
pipeline. "Does this contact/bill payer already exist?" before creating one has
to be a live Famly call made at creation time -- see the warning in
`actions/pull_roster/store.py`. The cache-first lookup here can answer from
data up to a day old, and its live fallback is itself held for a few minutes,
so neither is fit to gate a write.

Read-only. Data access is all `actions/pull_roster`:

  1. the roster cache (`store.find_contact_by_email` /
     `store.find_bill_payer_by_email`), which only `pull-roster` writes;
  2. if EITHER id has no cache match, one live fetch of every site in
     reference/catalogue.json via `fetch_children_and_contacts` and
     `fetch_bill_payers`, matched in memory. Live rows are NOT written to the
     cache; they are held in a per-process cache with a TTL
     (CONTACT_LOOKUP_LIVE_TTL_SECONDS, default 300) so a burst of misses makes
     one fetch rather than one per webhook.

Each id is resolved separately: the cache result is used when the cache found
it, and live only fills an id the cache did not. A live failure raises
`LiveFetchError` -- an incomplete search is never reported as "not found".
Multiple DISTINCT ids for one email are never guessed between; the caller
sees every id and treats it as ambiguous.

Server-importable: no argparse, no Flask.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from actions.pull_roster import runner as pull_roster
from actions.pull_roster import store
from core.client import GraphQLClient, GraphQLError, GraphQLHTTPError
from integrations import catalogue

logger = logging.getLogger(__name__)

DEFAULT_LIVE_TTL_SECONDS = 300

SOURCE_CACHE = "cache"
SOURCE_LIVE = "live"
SOURCE_CACHE_AND_LIVE = "cache+live"


class LiveFetchError(Exception):
    """The live Famly fetch failed, so the search is incomplete."""


@dataclass
class LookupResult:
    """Distinct ids found for one email. Zero ids = not matched; two or more =
    ambiguous."""

    contact_ids: list[str] = field(default_factory=list)
    bill_payer_ids: list[str] = field(default_factory=list)
    source: str = SOURCE_CACHE


def normalise_email(value: Any) -> str:
    """Trim + casefold, via the roster store's own convention. `""` if unusable."""
    return store._normalise(value)


def live_ttl_seconds() -> float:
    """Read fresh each time so a changed environment variable needs no restart."""
    raw = os.environ.get("CONTACT_LOOKUP_LIVE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_LIVE_TTL_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "CONTACT_LOOKUP_LIVE_TTL_SECONDS is not a number; using %s",
            DEFAULT_LIVE_TTL_SECONDS,
        )
        return DEFAULT_LIVE_TTL_SECONDS


# MODULE-LEVEL, IN-MEMORY, PER-PROCESS, like urn_lookup's roster cache: each
# gunicorn worker keeps its own copy. The lock makes a burst of concurrent
# misses within one worker share a single fetch.
_live_lock = threading.Lock()
_live_contacts: list[store.ContactRow] | None = None
_live_bill_payers: list[store.BillPayerRow] | None = None
_live_fetched_at: float | None = None


def _live_is_fresh() -> bool:
    return (
        _live_contacts is not None
        and _live_fetched_at is not None
        and (time.monotonic() - _live_fetched_at) < live_ttl_seconds()
    )


def _reset_live_cache_for_tests() -> None:
    """Tests only -- never called by the app."""
    global _live_contacts, _live_bill_payers, _live_fetched_at
    with _live_lock:
        _live_contacts = None
        _live_bill_payers = None
        _live_fetched_at = None


def _live_client() -> GraphQLClient:
    return pull_roster._public_client()


def _all_institution_ids() -> list[str]:
    ids: list[str] = []
    for entry in catalogue.load_sites().values():
        institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if institution_id and institution_id not in ids:
            ids.append(institution_id)
    return ids


def _fetch_live() -> tuple[list[store.ContactRow], list[store.BillPayerRow]]:
    """Every contact and bill payer across every catalogued site, or raise."""
    site_ids = _all_institution_ids()
    if not site_ids:
        raise LiveFetchError("no institutionId configured in reference/catalogue.json")

    try:
        client = _live_client()
        _, contacts = pull_roster.fetch_children_and_contacts(site_ids, client)
        bill_payers = pull_roster.fetch_bill_payers(site_ids, client)
    except (GraphQLError, GraphQLHTTPError) as exc:
        raise LiveFetchError(f"live Famly fetch failed: {exc}") from exc

    return contacts, bill_payers


def _get_live() -> tuple[list[store.ContactRow], list[store.BillPayerRow]]:
    global _live_contacts, _live_bill_payers, _live_fetched_at

    with _live_lock:
        if not _live_is_fresh():
            contacts, bill_payers = _fetch_live()
            _live_contacts, _live_bill_payers = contacts, bill_payers
            _live_fetched_at = time.monotonic()
            logger.info(
                "contact_lookup: live fetch (%d contacts, %d bill payers)",
                len(contacts),
                len(bill_payers),
            )
        return _live_contacts, _live_bill_payers


def _distinct_ids(ids) -> list[str]:
    """Distinct non-empty ids, in first-seen order."""
    return list(dict.fromkeys(i for i in ids if i))


def _match_rows(rows, target: str) -> list[str]:
    return _distinct_ids(
        row.id for row in rows if row.email and normalise_email(row.email) == target
    )


def lookup(email: str) -> LookupResult:
    """Distinct contact and bill payer ids for `email` (already validated).

    Raises:
        LiveFetchError: a live fetch was needed and failed.
    """
    target = normalise_email(email)

    contact_ids = _distinct_ids(r["famly_id"] for r in store.find_contact_by_email(target))
    bill_payer_ids = _distinct_ids(
        r["famly_id"] for r in store.find_bill_payer_by_email(target)
    )

    if contact_ids and bill_payer_ids:
        return LookupResult(contact_ids, bill_payer_ids, SOURCE_CACHE)

    cache_found_any = bool(contact_ids or bill_payer_ids)
    live_contacts, live_bill_payers = _get_live()

    if not contact_ids:
        contact_ids = _match_rows(live_contacts, target)
    if not bill_payer_ids:
        bill_payer_ids = _match_rows(live_bill_payers, target)

    source = SOURCE_CACHE_AND_LIVE if cache_found_any else SOURCE_LIVE
    return LookupResult(contact_ids, bill_payer_ids, source)
