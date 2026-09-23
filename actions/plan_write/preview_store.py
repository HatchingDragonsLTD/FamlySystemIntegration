"""Durable record of previewed plans, so an approval commits exactly what was
previewed.

The Slack Approve button carries only a `preview_id`. Without this store the
commit would have to rebuild the plan from the original payload, and any change
in between -- a price change, an edit to the catalogue, a code change -- would
mean committing something the approver never saw. So the exact
`{"plan": {...}}` body is kept here, keyed by `preview_id`, and that is what
gets written.

SQLite via stdlib `sqlite3`: no dependency, and safe across the multiple
gunicorn workers a JSON file would race between.

Holds child plan data, so the database lives outside the repo tree (gitignored;
see PREVIEW_STORE_PATH).

No argparse, no Flask: importable and testable anywhere.

Environment:
    PREVIEW_STORE_PATH  optional path to the SQLite file. Defaults to
                        var/preview_store.sqlite3 under the project root.
    PREVIEW_TTL_HOURS   optional, default 24. An approval older than this is
                        refused: stale pricing must never be committed silently.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "var" / "preview_store.sqlite3"
DEFAULT_TTL_HOURS = 24

# Statuses a stored preview moves through.
STATUS_PENDING = "pending"
STATUS_COMMITTING = "committing"
STATUS_COMMITTED = "committed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS previews (
    preview_id TEXT PRIMARY KEY,
    plan_body  TEXT NOT NULL,
    child_id   TEXT,
    version    INTEGER,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL
)
"""

# Columns added after the table first shipped. A deployed store already has
# rows, so they are added in place rather than by recreating the table.
_ADDED_COLUMNS = {
    "site_code": "TEXT",
    "institution_id": "TEXT",
}


@dataclass
class StoredPreview:
    """One previewed plan, as stored."""

    preview_id: str
    plan_body: dict = field(default_factory=dict)
    child_id: str | None = None
    version: int | None = None
    created_at: str | None = None
    status: str = STATUS_PENDING
    # Site context, informational. Recorded so a stored preview carries which
    # site it belongs to; nothing in the commit path reads it.
    site_code: str | None = None
    institution_id: str | None = None

    @property
    def is_expired(self) -> bool:
        return self.status == STATUS_EXPIRED

    @property
    def is_committed(self) -> bool:
        return self.status == STATUS_COMMITTED


def store_path() -> Path:
    """The SQLite file in use."""
    override = os.environ.get("PREVIEW_STORE_PATH", "").strip()
    return Path(override) if override else DEFAULT_STORE_PATH


def ttl_hours() -> float:
    """How long an approval stays valid."""
    raw = os.environ.get("PREVIEW_TTL_HOURS", "").strip()
    if not raw:
        return DEFAULT_TTL_HOURS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "PREVIEW_TTL_HOURS=%r is not a number; using %s",
            raw,
            DEFAULT_TTL_HOURS,
        )
        return DEFAULT_TTL_HOURS


@contextmanager
def _open():
    """Open the store, commit on success, and ALWAYS close.

    `with sqlite3.connect(...)` commits the transaction but leaves the
    connection open -- which leaks a file handle on every call, one per request
    in the server.
    """
    connection = _connect()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _connect() -> sqlite3.Connection:
    """Open the store, creating the file and schema on first use."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    # Concurrent workers: WAL lets a reader run while another writes.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(_SCHEMA)
    _migrate(connection)
    connection.commit()
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Add any column missing from an existing store.

    CREATE TABLE IF NOT EXISTS leaves an older table untouched, so a store
    written before a column existed would break on read. Adding in place keeps
    the pending previews already in it.
    """
    existing = {
        row["name"] for row in connection.execute("PRAGMA table_info(previews)")
    }
    for column, column_type in _ADDED_COLUMNS.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE previews ADD COLUMN {column} {column_type}"
            )
            logger.info("Preview store: added column %s", column)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _column(row: sqlite3.Row, name: str):
    """Read a column that an older store may not have."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _row_to_preview(row: sqlite3.Row) -> StoredPreview:
    try:
        plan_body = json.loads(row["plan_body"])
    except (TypeError, ValueError):
        plan_body = {}

    return StoredPreview(
        preview_id=row["preview_id"],
        plan_body=plan_body if isinstance(plan_body, dict) else {},
        child_id=row["child_id"],
        version=row["version"],
        created_at=row["created_at"],
        status=row["status"],
        site_code=_column(row, "site_code"),
        institution_id=_column(row, "institution_id"),
    )


def _has_expired(created_at: str | None) -> bool:
    if not created_at:
        return True
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(hours=ttl_hours())


def save(
    preview_id: str,
    plan_body: dict,
    child_id: str | None,
    version: int | None,
    site_code: str | None = None,
    institution_id: str | None = None,
) -> None:
    """Record a previewed plan as pending approval.

    Overwrites any existing row for the same preview_id (ids are uuid4, so this
    is a re-save rather than a collision).

    `site_code`/`institution_id` are metadata: stored so a preview carries its
    site context, read by nothing in the commit path.
    """
    with _open() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO previews "
            "(preview_id, plan_body, child_id, version, created_at, status, "
            "site_code, institution_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                preview_id,
                json.dumps(plan_body),
                child_id,
                version,
                _now(),
                STATUS_PENDING,
                site_code,
                institution_id,
            ),
        )

    logger.info(
        "Stored preview preview_id=%s child_id=%s", preview_id, child_id
    )


def get(preview_id: str) -> StoredPreview | None:
    """Fetch a stored preview.

    Returns None when the id is unknown. An entry past its TTL is marked
    expired (persisted) and returned with `status == "expired"`, so a caller can
    tell "never existed" apart from "too old" and say so to the approver.

    An approval clicked days later must not commit against stale pricing.
    """
    if not preview_id:
        return None

    with _open() as connection:
        row = connection.execute(
            "SELECT * FROM previews WHERE preview_id = ?", (preview_id,)
        ).fetchone()

    if row is None:
        return None

    stored = _row_to_preview(row)

    # Only a pending entry can expire: a committed or rejected one keeps its
    # outcome, which is the more useful thing to report.
    if stored.status == STATUS_PENDING and _has_expired(stored.created_at):
        mark(preview_id, STATUS_EXPIRED)
        stored.status = STATUS_EXPIRED

    return stored


def mark(preview_id: str, status: str) -> bool:
    """Set a preview's status. Returns True when a row was updated."""
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE previews SET status = ? WHERE preview_id = ?",
            (status, preview_id),
        )
        updated = cursor.rowcount > 0

    if updated:
        logger.info("Preview preview_id=%s marked %s", preview_id, status)
    return updated


def claim_for_commit(preview_id: str) -> StoredPreview | None:
    """Atomically take ownership of a pending preview, for committing.

    DOUBLE-CLICK SAFETY. Two Approve clicks arriving together would both read
    status "pending" and both write a plan, creating a duplicate REAL plan in
    Famly. This moves pending -> committing in a single conditional UPDATE, so
    exactly one caller can win.

    Returns:
        The claimed preview, or None when it was not claimable -- unknown,
        expired, already committed, rejected, or claimed by another worker. The
        caller should then re-read it with `get` to explain why.
    """
    stored = get(preview_id)
    if stored is None or stored.status != STATUS_PENDING:
        return None

    with _open() as connection:
        cursor = connection.execute(
            "UPDATE previews SET status = ? WHERE preview_id = ? AND status = ?",
            (STATUS_COMMITTING, preview_id, STATUS_PENDING),
        )
        if cursor.rowcount == 0:
            # Another worker claimed it between the read and this update.
            return None

    stored.status = STATUS_COMMITTING
    return stored


def release_claim(preview_id: str) -> None:
    """Return a claimed preview to pending after a failed commit attempt.

    A Famly error is usually transient or fixable, so the approver should be
    able to click again rather than being locked out by a dangling claim.
    """
    with _open() as connection:
        connection.execute(
            "UPDATE previews SET status = ? WHERE preview_id = ? AND status = ?",
            (STATUS_PENDING, preview_id, STATUS_COMMITTING),
        )


def describe(stored: StoredPreview | None) -> str:
    """A short phrase for why a preview is not commitable, for Slack."""
    if stored is None:
        return "not found"
    return stored.status


def purge(preview_id: str) -> None:
    """Delete one entry. Housekeeping/tests only."""
    with _open() as connection:
        connection.execute("DELETE FROM previews WHERE preview_id = ?", (preview_id,))


def _reset_for_tests() -> None:
    """Drop every row. Tests only -- never called by the app."""
    with _open() as connection:
        connection.execute("DELETE FROM previews")
