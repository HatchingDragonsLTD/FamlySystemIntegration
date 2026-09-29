"""Local SQLite cache of Famly children/contacts/bill-payer identifying
fields, refreshed daily by `actions/pull_roster`.

HOLDS REAL CHILD/FAMILY DATA (child ids, contact emails, bill payer emails).
Treated exactly like `.env`: gitignored, never committed, and must never be
included in any backup or export that leaves the server. See `.gitignore` and
the README section on this cache.

WHAT THIS CACHE IS FOR: matching an already-known identifier (a HubSpot deal's
urn, a contact's email, a bill payer's email) against Famly ids, for the
upcoming contact/child-creation pipeline to link records it is about to write.

WHAT THIS CACHE MUST NEVER BE USED FOR -- READ THIS BEFORE WIRING IT INTO
ANYTHING: deciding whether a child, contact or bill payer ALREADY EXISTS
before creating one. That existence check MUST be a LIVE Famly call made at
CREATION TIME, per the idempotency design already agreed elsewhere. This
cache can be up to 24 hours stale (it refreshes once a day): using it for a
pre-creation existence check risks either creating a duplicate of something
Famly already has (the cache missed a same-day creation) or skipping a
creation for something that no longer exists (the cache missed a same-day
deletion). When the contact/child-creation pipeline is built, the existence
check there must be marked with a comment referencing this paragraph, and
must call Famly directly -- never `find_child_by_external_id`,
`find_contact_by_email` or `find_bill_payer_by_email` below for that purpose.

Three tables, one per entity, each holding only the identifying field this
cache exists for (no names, no addresses, no financial details -- minimal
field, by design, matching what `actions/pull_roster/runner.py` fetches):

    children(famly_id, external_id, institution, fetched_at)
    contacts(famly_id, email, institution, fetched_at)
    bill_payers(famly_id, email, institution, fetched_at)

`institution` is the site code the row was last refreshed under (see
reference/catalogue.json), so a refresh of one institution can replace just
its own rows without touching another institution's -- the same
merge-not-wipe principle `actions/pull_sessions` and friends already apply to
their JSON files, adapted here to a per-institution DELETE+INSERT.

SQLite via stdlib `sqlite3`, matching `actions/plan_write/preview_store.py`'s
pattern (no dependency, safe across concurrent workers).

Environment:
    ROSTER_CACHE_PATH  optional path to the SQLite file. Defaults to
                       var/roster_cache.db under the project root (the same
                       gitignored `var/` directory preview_store.sqlite3
                       lives in).
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "var" / "roster_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS children (
    famly_id    TEXT PRIMARY KEY,
    external_id TEXT,
    institution TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    famly_id    TEXT PRIMARY KEY,
    email       TEXT,
    institution TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bill_payers (
    famly_id    TEXT PRIMARY KEY,
    email       TEXT,
    institution TEXT,
    fetched_at  TEXT NOT NULL
);
"""


@dataclass
class ChildRow:
    id: str | None = None
    external_id: str | None = None


@dataclass
class ContactRow:
    id: str | None = None
    email: str | None = None


@dataclass
class BillPayerRow:
    id: str | None = None
    email: str | None = None


def store_path() -> Path:
    """The SQLite file in use."""
    override = os.environ.get("ROSTER_CACHE_PATH", "").strip()
    return Path(override) if override else DEFAULT_STORE_PATH


@contextmanager
def _open(path: Path | None = None):
    """Open the store, commit on success, and ALWAYS close."""
    connection = _connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store, creating the file and schema on first use."""
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def replace_institution(
    institution: str,
    *,
    children: list[ChildRow] | None = None,
    contacts: list[ContactRow] | None = None,
    bill_payers: list[BillPayerRow] | None = None,
    path: Path | None = None,
) -> None:
    """Replace one institution's rows in every table, in a single transaction.

    Existing rows for OTHER institutions are untouched -- this is the
    "refresh one, leave the rest" merge a partial or `--institution`-filtered
    run relies on. A row with no id is skipped (nothing to key it by).
    """
    fetched_at = _now()

    with _open(path) as connection:
        connection.execute("DELETE FROM children WHERE institution = ?", (institution,))
        for row in children or []:
            if not row.id:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO children (famly_id, external_id, institution, "
                "fetched_at) VALUES (?, ?, ?, ?)",
                (row.id, row.external_id, institution, fetched_at),
            )

        connection.execute("DELETE FROM contacts WHERE institution = ?", (institution,))
        for row in contacts or []:
            if not row.id:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO contacts (famly_id, email, institution, "
                "fetched_at) VALUES (?, ?, ?, ?)",
                (row.id, row.email, institution, fetched_at),
            )

        connection.execute(
            "DELETE FROM bill_payers WHERE institution = ?", (institution,)
        )
        for row in bill_payers or []:
            if not row.id:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO bill_payers (famly_id, email, institution, "
                "fetched_at) VALUES (?, ?, ?, ?)",
                (row.id, row.email, institution, fetched_at),
            )

    logger.info(
        "roster_cache: refreshed institution=%s children=%d contacts=%d bill_payers=%d",
        institution,
        len(children or []),
        len(contacts or []),
        len(bill_payers or []),
    )


def _normalise(value: Any) -> str:
    """Trim and casefold, the same convention `actions.urn_lookup` uses."""
    return str(value).strip().casefold() if isinstance(value, str) and value.strip() else ""


def _find(table: str, column: str, value: Any, path: Path | None = None) -> list[dict]:
    """Every row in `table` whose `column` normalises to match `value`.

    Read-and-filter-in-Python, not a SQL WHERE clause: casefold handles more
    Unicode cases correctly than SQLite's built-in LOWER(), and this table is
    small enough (a childcare setting's roster, not a national one) that a
    full scan costs nothing worth optimising away.
    """
    target = _normalise(value)
    if not target:
        return []

    with _open(path) as connection:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()

    return [dict(row) for row in rows if _normalise(row[column]) == target]


def find_child_by_external_id(urn: Any, path: Path | None = None) -> list[dict]:
    """Cached children whose `external_id` matches `urn`, normalised.

    LOOKUP ONLY -- see the module docstring. Never use this to decide whether
    a child already exists before creating one.
    """
    return _find("children", "external_id", urn, path)


def find_contact_by_email(email: Any, path: Path | None = None) -> list[dict]:
    """Cached contacts whose `email` matches, normalised.

    LOOKUP ONLY -- see the module docstring. Never use this to decide whether
    a contact already exists before creating one.
    """
    return _find("contacts", "email", email, path)


def find_bill_payer_by_email(email: Any, path: Path | None = None) -> list[dict]:
    """Cached bill payers whose `email` matches, normalised.

    LOOKUP ONLY -- see the module docstring. Never use this to decide whether
    a bill payer already exists before creating one.
    """
    return _find("bill_payers", "email", email, path)


def _reset_for_tests(path: Path | None = None) -> None:
    """Drop every row from every table. Tests only -- never called by the app."""
    with _open(path) as connection:
        connection.execute("DELETE FROM children")
        connection.execute("DELETE FROM contacts")
        connection.execute("DELETE FROM bill_payers")
