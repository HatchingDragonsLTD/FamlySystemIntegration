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
import time
import zlib
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
# Replaced by an adjusted copy under a new preview_id. The original must not
# also be approvable, or the same plan could be committed twice over.
STATUS_SUPERSEDED = "superseded"
# A chain preview whose commit failed. Unlike an ordinary preview it is NOT
# returned to pending: the chain stops there and nothing is retried, so the
# same Approve button cannot be clicked again into a second write.
STATUS_FAILED = "failed"

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
    # The pricing group the computed plan priced against. Needed to attach a
    # totalAdjustments entry later, which must name the same group.
    "pricing_group_id": "TEXT",
    # The estimate as first previewed. Kept so a re-price can be COMPARED
    # against it -- otherwise an adjustment Famly silently ignored looks
    # identical to one that worked.
    "monthly_estimate": "REAL",
    # Famly operation #3 (add a second plan alongside an existing one). When
    # set, a commit of this preview must send oldPlanId/replaceOldPlan --
    # recorded here so approval.py does not have to rebuild or guess them at
    # commit time. NULL/0 for an ordinary create, which is every preview
    # stored before these columns existed.
    "old_plan_id": "TEXT",
    "replace_old_plan": "INTEGER",
    # create_timeline: which chain this preview belongs to, and which of its
    # segments it is. NULL for every ordinary preview.
    "chain_id": "TEXT",
    "chain_index": "INTEGER",
    # queue_timeline: which queued job this preview belongs to and which of its
    # plans it is, so a human approving a HELD plan can release the job. NULL
    # for every other preview.
    "job_id": "TEXT",
    "job_index": "INTEGER",
}

# create_timeline's chains (see actions/create_timeline/chains.py, which owns
# the reads and writes). The table lives in this file so one connection, one
# WAL and one migration path serve both. The payload carries no names -- see
# the engine's allow-list.
_CHAINS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chains (
    chain_id   TEXT PRIMARY KEY,
    child_id   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    segments   TEXT NOT NULL,
    next_index INTEGER NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

# Columns added to `chains` after it first shipped (none yet; the same in-place
# pattern as _ADDED_COLUMNS).
_CHAINS_ADDED_COLUMNS: dict[str, str] = {}

# queue_timeline's jobs (see actions/queue_timeline/jobs.py, which owns the
# reads and writes) and the queue's small key/value state (paused flag,
# consecutive failures, commits per day). Same file, same migration path.
_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    job_key     TEXT NOT NULL,
    child_id    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    segments    TEXT NOT NULL,
    next_index  INTEGER NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    preview_id  TEXT,
    digest_sent INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""

_JOBS_ADDED_COLUMNS: dict[str, str] = {}

# ENQUEUEING IS CONCURRENCY-SAFE BECAUSE THE DATABASE DECIDES, not a check made
# beforehand: two gunicorn workers handling webhooks at once both INSERT, and
# exactly one wins. Two indexes do it:
#
#   * the idempotency key is unique among LIVE jobs (queued, running, held or
#     done): an identical request while one is active or finished is the same
#     job. A failed or cancelled job does not hold its key, so the same request
#     can be submitted again and gets a new job;
#   * a child can have at most ONE active job (queued, running or held).
_JOBS_KEY_UNIQUE = """
CREATE UNIQUE INDEX IF NOT EXISTS jobs_live_job_key ON jobs (job_key)
WHERE status IN ('queued', 'running', 'held', 'done')
"""

_JOBS_ONE_ACTIVE_PER_CHILD = """
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_child
ON jobs (child_id) WHERE status IN ('queued', 'running', 'held')
"""

# Earlier key indexes, replaced by the one above: dropped when found.
_JOBS_OLD_KEY_INDEXES = ("jobs_one_live_per_key", "jobs_job_key_unique")

# The audit trail of the TEMPORARY delete_plans action (actions/delete_plans):
# one row per decision -- the child, the plan (null for a child-level row), when,
# the mode and the outcome. Written by that action only.
_DELETE_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS delete_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id    TEXT NOT NULL,
    plan_id     TEXT,
    at          TEXT NOT NULL,
    mode        TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    detail      TEXT,
    reported_at TEXT
)
"""

# `reported_at` is when a row went into a delete_plans Slack digest (NULL until
# then): a row is posted in at most one digest. Added in place to a log that
# predates it.
_DELETE_LOG_ADDED_COLUMNS = {"reported_at": "TEXT"}

_QUEUE_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_state (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

# A child can have at most ONE active chain. Enforced by the database, not just
# checked beforehand, so two create_timeline requests arriving together cannot
# both start one.
_CHAINS_ONE_ACTIVE = """
CREATE UNIQUE INDEX IF NOT EXISTS chains_one_active_per_child
ON chains (child_id) WHERE status = 'active'
"""


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
    pricing_group_id: str | None = None
    # The estimate as first previewed, so a re-price can be compared with it.
    monthly_estimate: float | None = None
    # Famly operation #3 params. old_plan_id is None for an ordinary create;
    # replace_old_plan is only meaningful alongside it.
    old_plan_id: str | None = None
    replace_old_plan: bool = False
    # create_timeline: the chain this preview is one plan of, and its position
    # (0-based). None for an ordinary preview.
    chain_id: str | None = None
    chain_index: int | None = None
    # queue_timeline: the job and position this preview is for. None otherwise.
    job_id: str | None = None
    job_index: int | None = None

    @property
    def is_expired(self) -> bool:
        return self.status == STATUS_EXPIRED

    @property
    def is_committed(self) -> bool:
        return self.status == STATUS_COMMITTED


def open_store():
    """A connection to the store, committed on success and always closed.

    The same context manager every function here uses, exposed so
    `actions/create_timeline/chains.py` shares the file, the WAL and the schema
    rather than opening its own.
    """
    return _open()


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


def _schema_version() -> int:
    """A number that changes whenever the schema definitions below do.

    Stored in the file's `user_version`, so a connection can tell in one cheap
    READ whether the schema is current -- and only a connection that finds it
    stale takes the write lock to fix it.
    """
    definitions = repr(
        (
            _SCHEMA,
            _CHAINS_SCHEMA,
            _CHAINS_ONE_ACTIVE,
            _JOBS_SCHEMA,
            _QUEUE_STATE_SCHEMA,
            _DELETE_LOG_SCHEMA,
            _JOBS_KEY_UNIQUE,
            _JOBS_ONE_ACTIVE_PER_CHILD,
            _JOBS_OLD_KEY_INDEXES,
            sorted(_ADDED_COLUMNS.items()),
            sorted(_CHAINS_ADDED_COLUMNS.items()),
            sorted(_JOBS_ADDED_COLUMNS.items()),
            sorted(_DELETE_LOG_ADDED_COLUMNS.items()),
        )
    )
    return (zlib.crc32(definitions.encode("utf-8")) & 0x7FFFFFFF) or 1


def _connect() -> sqlite3.Connection:
    """Open the store, creating the file and schema on first use.

    Safe for several processes opening it at once (two gunicorn workers, the
    queue worker): the busy timeout makes writers wait for each other, and the
    schema is created and migrated under ONE write lock by whichever connection
    gets there first -- the others find it done.
    """
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = _schema_version()
    if connection.execute("PRAGMA user_version").fetchone()[0] == version:
        return  # current: nothing to write, no lock taken

    # Concurrent workers: WAL lets a reader run while another writes. It is
    # persistent in the file, so this is only ever needed once; switching can
    # briefly find the file busy while another connection does the same.
    for attempt in range(100):
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError:
            if attempt == 99:
                raise
            time.sleep(0.05)

    connection.execute("BEGIN IMMEDIATE")  # waits (busy timeout) for the write lock
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] == version:
            connection.execute("COMMIT")  # another connection finished while we waited
            return

        connection.execute(_SCHEMA)
        connection.execute(_CHAINS_SCHEMA)
        connection.execute(_JOBS_SCHEMA)
        connection.execute(_QUEUE_STATE_SCHEMA)
        connection.execute(_DELETE_LOG_SCHEMA)
        _migrate(connection)
        connection.execute(_CHAINS_ONE_ACTIVE)
        for old_index in _JOBS_OLD_KEY_INDEXES:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (old_index,),
            ).fetchone():
                connection.execute(f"DROP INDEX {old_index}")
        connection.execute(_JOBS_KEY_UNIQUE)
        connection.execute(_JOBS_ONE_ACTIVE_PER_CHILD)
        connection.execute(f"PRAGMA user_version = {version}")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _migrate(connection: sqlite3.Connection) -> None:
    """Add any column missing from an existing store.

    CREATE TABLE IF NOT EXISTS leaves an older table untouched, so a store
    written before a column existed would break on read. Adding in place keeps
    the pending previews already in it.
    """
    for table, added in (
        ("previews", _ADDED_COLUMNS),
        ("chains", _CHAINS_ADDED_COLUMNS),
        ("jobs", _JOBS_ADDED_COLUMNS),
        ("delete_log", _DELETE_LOG_ADDED_COLUMNS),
    ):
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column, column_type in added.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )
                logger.info("Preview store: added column %s.%s", table, column)


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
        pricing_group_id=_column(row, "pricing_group_id"),
        monthly_estimate=_column(row, "monthly_estimate"),
        old_plan_id=_column(row, "old_plan_id"),
        replace_old_plan=bool(_column(row, "replace_old_plan")),
        chain_id=_column(row, "chain_id"),
        chain_index=_column(row, "chain_index"),
        job_id=_column(row, "job_id"),
        job_index=_column(row, "job_index"),
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
    pricing_group_id: str | None = None,
    monthly_estimate: float | None = None,
    old_plan_id: str | None = None,
    replace_old_plan: bool = False,
    chain_id: str | None = None,
    chain_index: int | None = None,
    job_id: str | None = None,
    job_index: int | None = None,
) -> None:
    """Record a previewed plan as pending approval.

    Overwrites any existing row for the same preview_id (ids are uuid4, so this
    is a re-save rather than a collision).

    `site_code`/`institution_id` are metadata: stored so a preview carries its
    site context, read by nothing in the commit path.

    `old_plan_id`/`replace_old_plan`: when this preview added a plan alongside
    an existing one (Famly operation #3), these are read back at commit time
    (see `claim_for_commit`/`approval.py`) so the write uses the SAME params
    the approver's preview used. None/False (the defaults) for an ordinary
    create, unchanged from before these parameters existed.

    `chain_id`/`chain_index`: set when this preview is one plan of a
    create_timeline chain, so the approval that commits it can advance the
    chain. An adjusted copy carries them forward. None for everything else.
    """
    with _open() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO previews "
            "(preview_id, plan_body, child_id, version, created_at, status, "
            "site_code, institution_id, pricing_group_id, monthly_estimate, "
            "old_plan_id, replace_old_plan, chain_id, chain_index, "
            "job_id, job_index) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                preview_id,
                json.dumps(plan_body),
                child_id,
                version,
                _now(),
                STATUS_PENDING,
                site_code,
                institution_id,
                pricing_group_id,
                monthly_estimate,
                old_plan_id,
                int(bool(replace_old_plan)),
                chain_id,
                chain_index,
                job_id,
                job_index,
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


def latest_for_chain(chain_id: str, chain_index: int) -> StoredPreview | None:
    """The newest preview stored for one plan of a chain, or None.

    Newest, because an adjusted copy replaces the original under a new id. Goes
    through `get`, so a pending one past its TTL comes back marked expired.
    """
    with _open() as connection:
        row = connection.execute(
            "SELECT preview_id FROM previews WHERE chain_id = ? AND chain_index = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (chain_id, chain_index),
        ).fetchone()
    return get(row["preview_id"]) if row is not None else None


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
        connection.execute("DELETE FROM chains")
        connection.execute("DELETE FROM jobs")
        connection.execute("DELETE FROM queue_state")
        connection.execute("DELETE FROM delete_log")
