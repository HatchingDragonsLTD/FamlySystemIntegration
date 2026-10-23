"""Durable record of onboarding jobs: the `onboard_jobs` table.

Follows the queue_timeline jobs-table conventions (SQLite via stdlib `sqlite3`,
WAL, a busy timeout so several processes can share it, created on first use, an
atomic INSERT that lets a UNIQUE INDEX decide races, `created_at` stamped by
SQLite inside the INSERT so ordering follows the real insert order). It is its
OWN file (ONBOARD_STORE_PATH, default var/onboard_jobs.sqlite3, gitignored)
rather than a table in preview_store, so the existing plan queue's schema and
migration are untouched.

A row holds ids, codes and hashes ONLY. Never a name, email, phone, address or
date of birth; `last_error_code` and `outcomes` are codes, never a message that
could carry data.

    id                 uuid4
    deal_id            the HubSpot deal
    status             queued | held | awaiting_approval | creating_child |
                       creating_contacts | done | dry_done | needs_review | failed
    mode               off | dry | live, as the job was last processed
    plan_hash          hash of the plan JSON last shown (no personal data in it)
    approved_hash      the plan_hash a human approved in Slack
    child_id           the Famly child, once known
    attempts           how many times a worker has taken the job
    last_error_code    a code
    approval_ref       the Slack message ref of the approval request
    reask_count        how many times the plan CHANGED after an approval and was asked again
    plan_sections      short hash per plan section at the last ask (no personal data)
    bill_payer_id      the Famly bill payer id the bill payer stage matched, created or attached
    approval_nonce     random token of the CURRENT approval request; only its buttons count
    child_create_sent  1 once a child create request has been SENT for this job:
                       a job never sends a second one
    outcomes           JSON list of contact outcome codes
    claimed_at         set while a worker holds the job
    source             cli | webhook: how the job was enqueued. A `webhook` job
                       must pass the worker's stage check (Operations pipeline,
                       Deposit Pending) or it is held `stage_not_eligible`

ONE ACTIVE JOB PER DEAL is enforced by a UNIQUE PARTIAL INDEX, not just in code:
active = queued, awaiting_approval, creating_child, creating_contacts. `held`,
`needs_review`, `failed`, `done` and `dry_done` are finished (a person acts, then
re-enqueues), so a fresh job can start. Re-enqueueing a deal that is `done`
creates no job.

No Flask, no Famly: importable and testable anywhere.
"""

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "var" / "onboard_jobs.sqlite3"

QUEUED = "queued"
HELD = "held"
AWAITING_APPROVAL = "awaiting_approval"
CREATING_CHILD = "creating_child"
CREATING_CONTACTS = "creating_contacts"
DONE = "done"
DRY_DONE = "dry_done"
NEEDS_REVIEW = "needs_review"
FAILED = "failed"
ALL_STATUSES = (
    QUEUED, HELD, AWAITING_APPROVAL, CREATING_CHILD, CREATING_CONTACTS,
    DONE, DRY_DONE, NEEDS_REVIEW, FAILED,
)
ACTIVE = (QUEUED, AWAITING_APPROVAL, CREATING_CHILD, CREATING_CONTACTS)

# A claimed job whose worker died is taken again after this long.
STALE_CLAIM_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS onboard_jobs (
    id                TEXT PRIMARY KEY,
    deal_id           TEXT NOT NULL,
    status            TEXT NOT NULL,
    mode              TEXT NOT NULL,
    plan_hash         TEXT,
    approved_hash     TEXT,
    child_id          TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error_code   TEXT,
    approval_ref      TEXT,
    child_create_sent INTEGER NOT NULL DEFAULT 0,
    outcomes          TEXT,
    claimed_at        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'cli',
    approval_nonce    TEXT,
    reask_count       INTEGER NOT NULL DEFAULT 0,
    plan_sections     TEXT,
    bill_payer_id     TEXT,
    file_hash         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS onboard_one_active_per_deal
    ON onboard_jobs (deal_id)
    WHERE status IN ('queued', 'awaiting_approval', 'creating_child', 'creating_contacts');
CREATE INDEX IF NOT EXISTS onboard_jobs_by_status ON onboard_jobs (status, created_at);
"""

# Columns that update() may set.
_UPDATABLE = {
    "plan_hash", "approved_hash", "child_id", "last_error_code", "approval_ref",
    "outcomes", "mode", "approval_nonce", "bill_payer_id",
}


@dataclass
class Job:
    id: str
    deal_id: str
    status: str
    mode: str
    plan_hash: str | None = None
    approved_hash: str | None = None
    child_id: str | None = None
    attempts: int = 0
    last_error_code: str | None = None
    approval_ref: str | None = None
    child_create_sent: bool = False
    outcomes: list | None = None
    created_at: str | None = None
    updated_at: str | None = None
    source: str = "cli"
    approval_nonce: str | None = None
    claimed_at: str | None = None
    reask_count: int = 0
    plan_sections: dict | None = None
    bill_payer_id: str | None = None
    file_hash: str | None = None     # source `file`: a hash of the ids file (never its contents)


def store_path() -> Path:
    override = os.environ.get("ONBOARD_STORE_PATH", "").strip()
    return Path(override) if override else DEFAULT_STORE_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _open():
    """Open the store, commit on success, and ALWAYS close."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        _migrate(connection)
        yield connection
        connection.commit()
    finally:
        connection.close()


# (column, DDL) added after the first release, in the order they were added.
_ADDED_COLUMNS = (
    ("source", "TEXT NOT NULL DEFAULT 'cli'"),
    ("approval_nonce", "TEXT"),
    ("reask_count", "INTEGER NOT NULL DEFAULT 0"),
    ("plan_sections", "TEXT"),
    ("bill_payer_id", "TEXT"),
    ("file_hash", "TEXT"),
)


def _migrate(connection: sqlite3.Connection) -> None:
    """Add columns a store created before they existed does not have.

    Safe when several processes open the store at once (two gunicorn workers, the
    onboard worker, ...): the existing columns are read first, and an ADD COLUMN
    that loses the race to another process ("duplicate column name") is the column
    being there already, so it is ignored. Any other error is raised.
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(onboard_jobs)")}
    for name, ddl in _ADDED_COLUMNS:
        if name in columns:
            continue
        try:
            connection.execute(f"ALTER TABLE onboard_jobs ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


SOURCES = ("cli", "webhook", "file")


def _to_job(row: sqlite3.Row) -> Job:
    try:
        outcomes = json.loads(row["outcomes"]) if row["outcomes"] else None
    except ValueError:
        outcomes = None
    try:
        sections = json.loads(row["plan_sections"]) if row["plan_sections"] else None
    except ValueError:
        sections = None
    return Job(
        id=row["id"], deal_id=row["deal_id"], status=row["status"], mode=row["mode"],
        plan_hash=row["plan_hash"], approved_hash=row["approved_hash"],
        child_id=row["child_id"], attempts=row["attempts"],
        last_error_code=row["last_error_code"], approval_ref=row["approval_ref"],
        child_create_sent=bool(row["child_create_sent"]), outcomes=outcomes,
        created_at=row["created_at"], updated_at=row["updated_at"],
        source=row["source"], approval_nonce=row["approval_nonce"],
        claimed_at=row["claimed_at"], reask_count=row["reask_count"], plan_sections=sections,
        bill_payer_id=row["bill_payer_id"], file_hash=row["file_hash"],
    )


# --------------------------------------------------------------------------- #
# Enqueue
# --------------------------------------------------------------------------- #
CREATED = "created"
ALREADY_QUEUED = "already_queued"
ALREADY_DONE = "already_done"


@dataclass
class Enqueued:
    outcome: str
    job: Job


def enqueue(deal_id: str, mode: str, source: str = "cli", file_hash: str | None = None) -> Enqueued:
    """Enqueue a job for `deal_id`, with ONE atomic INSERT.

    * A deal that already has a `done` job creates no job (`already_done`).
    * A deal with an active job returns it (`already_queued`): the unique
      partial index, not a check made first, decides that.
    * Otherwise a new `queued` job. A `needs_review`, `failed`, `held` or
      `dry_done` job does not block a fresh one; its first action is the live
      plan, which sees what is already in Famly.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown job source: {source!r}")
    if (source == "file") != bool(file_hash):
        raise ValueError("a file job needs a file hash, and only a file job has one")
    deal_id = str(deal_id).strip()
    with _open() as connection:
        done = connection.execute(
            "SELECT * FROM onboard_jobs WHERE deal_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (deal_id, DONE),
        ).fetchone()
        if done is not None:
            return Enqueued(ALREADY_DONE, _to_job(done))

    job_id = str(uuid.uuid4())
    now = _now()
    try:
        with _open() as connection:
            connection.execute(
                "INSERT INTO onboard_jobs (id, deal_id, status, mode, created_at, updated_at, source, file_hash) "
                "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?)",
                (job_id, deal_id, QUEUED, mode, now, source, file_hash or None),
            )
    except sqlite3.IntegrityError:
        active = active_for_deal(deal_id)
        if active is not None:
            return Enqueued(ALREADY_QUEUED, active)
        raise
    return Enqueued(CREATED, get(job_id))


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get(job_id: str) -> Job | None:
    with _open() as connection:
        row = connection.execute("SELECT * FROM onboard_jobs WHERE id = ?", (job_id,)).fetchone()
    return _to_job(row) if row else None


def active_for_deal(deal_id: str) -> Job | None:
    marks = ",".join("?" for _ in ACTIVE)
    with _open() as connection:
        row = connection.execute(
            f"SELECT * FROM onboard_jobs WHERE deal_id = ? AND status IN ({marks})",
            (str(deal_id), *ACTIVE),
        ).fetchone()
    return _to_job(row) if row else None


def latest_for_deal(deal_id: str) -> Job | None:
    with _open() as connection:
        row = connection.execute(
            "SELECT * FROM onboard_jobs WHERE deal_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (str(deal_id),),
        ).fetchone()
    return _to_job(row) if row else None


def status_counts_for_deal(deal_id: str) -> dict[str, int]:
    with _open() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS n FROM onboard_jobs WHERE deal_id = ? GROUP BY status",
            (str(deal_id),),
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}


def all_rows_as_text() -> str:
    """Every row of the table as text. Tests only: to scan for personal data."""
    with _open() as connection:
        rows = connection.execute("SELECT * FROM onboard_jobs").fetchall()
    return json.dumps([dict(row) for row in rows], default=str)


# --------------------------------------------------------------------------- #
# Worker transitions
# --------------------------------------------------------------------------- #
def claim_next() -> Job | None:
    """Take the OLDEST queued job, atomically. A claim older than
    STALE_CLAIM_SECONDS is treated as abandoned and may be taken again."""
    now = _now()
    stale_before = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - STALE_CLAIM_SECONDS, timezone.utc
    ).isoformat()
    with _open() as connection:
        row = connection.execute(
            "SELECT id FROM onboard_jobs WHERE status = ? AND "
            "(claimed_at IS NULL OR claimed_at < ?) ORDER BY created_at, rowid LIMIT 1",
            (QUEUED, stale_before),
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            "UPDATE onboard_jobs SET claimed_at = ?, attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = ? AND (claimed_at IS NULL OR claimed_at < ?)",
            (now, now, row["id"], QUEUED, stale_before),
        )
        if cursor.rowcount != 1:
            return None
    return get(row["id"])


def set_status(job_id: str, status: str, **fields) -> None:
    """Set the status (and any updatable columns) and release the claim."""
    assert status in ALL_STATUSES, status
    _check_fields(fields)
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = [_encode(name, value) for name, value in fields.items()]
    with _open() as connection:
        connection.execute(
            f"UPDATE onboard_jobs SET status = ?, claimed_at = NULL, updated_at = ?"
            f"{', ' + columns if columns else ''} WHERE id = ?",
            (status, _now(), *values, job_id),
        )


def update(job_id: str, **fields) -> None:
    """Update columns without changing the status or the claim."""
    _check_fields(fields)
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = [_encode(name, value) for name, value in fields.items()]
    with _open() as connection:
        connection.execute(
            f"UPDATE onboard_jobs SET {columns}, updated_at = ? WHERE id = ?",
            (*values, _now(), job_id),
        )


def transition(job_id: str, from_status: str, to_status: str, **fields) -> bool:
    """Atomic compare-and-set: change the status only if it is still
    `from_status`. Returns whether this call did it (so a double click, or a click
    on a job that already moved on, does nothing the second time)."""
    assert to_status in ALL_STATUSES, to_status
    _check_fields(fields)
    columns = ", ".join(f"{name} = ?" for name in fields)
    values = [_encode(name, value) for name, value in fields.items()]
    with _open() as connection:
        cursor = connection.execute(
            f"UPDATE onboard_jobs SET status = ?, claimed_at = NULL, updated_at = ?"
            f"{', ' + columns if columns else ''} WHERE id = ? AND status = ?",
            (to_status, _now(), *values, job_id, from_status),
        )
    return cursor.rowcount == 1


def consume_hold_nonce(job_id: str, nonce: str) -> bool:
    """Spend a held job's Retry nonce: atomic, so only ONE press can win. True only if
    the job is still `held` and the nonce is still the one of the notice pressed."""
    if not nonce:
        return False
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE onboard_jobs SET approval_nonce = NULL, updated_at = ? "
            "WHERE id = ? AND status = ? AND approval_nonce = ?",
            (_now(), job_id, HELD, nonce),
        )
    return cursor.rowcount == 1


def begin_approval_wait(job_id: str, claimed_at: str | None, plan_hash: str, nonce: str,
                        sections: dict | None = None, reask: bool = False) -> bool:
    """Move a job to `awaiting_approval` ONLY if it is still `queued` and still held
    by the claim this run took (`claimed_at` is unchanged). A run that lagged behind
    a click, a re-claim or the end of the job changes nothing and returns False, so
    it cannot clear an approval or drag a moved-on job back to waiting."""
    if claimed_at is None:
        return False
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE onboard_jobs SET status = ?, claimed_at = NULL, updated_at = ?, "
            "plan_hash = ?, approved_hash = NULL, approval_nonce = ?, plan_sections = ?, "
            "reask_count = reask_count + ? "
            "WHERE id = ? AND status = ? AND claimed_at = ?",
            (AWAITING_APPROVAL, _now(), plan_hash, nonce,
             json.dumps(sections) if sections is not None else None, 1 if reask else 0,
             job_id, QUEUED, claimed_at),
        )
    return cursor.rowcount == 1


def set_approval_ref(job_id: str, nonce: str, ref: str) -> bool:
    """Record the Slack ref of the request, only while that request is the current one."""
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE onboard_jobs SET approval_ref = ?, updated_at = ? "
            "WHERE id = ? AND status = ? AND approval_nonce = ?",
            (ref, _now(), job_id, AWAITING_APPROVAL, nonce),
        )
    return cursor.rowcount == 1


def mark_child_create_sent(job_id: str) -> bool:
    """Record, BEFORE the request goes out, that this job is sending its child
    create. Returns False if it already did: a job never sends a second one."""
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE onboard_jobs SET child_create_sent = 1, updated_at = ? "
            "WHERE id = ? AND child_create_sent = 0",
            (_now(), job_id),
        )
    return cursor.rowcount == 1


def created_child_ids_for_deal(deal_id: str) -> frozenset[str]:
    """Child ids that a job for THIS deal recorded as the result of a create it
    SENT (`child_create_sent` and a persisted `child_id`). The job history is how
    the plan knows a child was created by the pipeline rather than found:

      * a job that merely found an existing child sets `child_id` but never
        `child_create_sent`, so it does not count;
      * a job for another deal does not count, even for the same child id.
    """
    with _open() as connection:
        rows = connection.execute(
            "SELECT DISTINCT child_id FROM onboard_jobs "
            "WHERE deal_id = ? AND child_create_sent = 1 AND child_id IS NOT NULL",
            (str(deal_id),),
        ).fetchall()
    return frozenset(row["child_id"] for row in rows)


def last_outcome_for(deal_id: str, child_id: str, key: str, exclude_job_id: str) -> str | None:
    """The most recent recorded outcome CODE for `key` (a HubSpot contact id, or
    `billpayer`) from an EARLIER job for this deal on this same child, or None.

    Outcomes are stored as `<key>:<code>` strings. A job that never got as far as
    recording outcomes (it was interrupted) says nothing, and a job for another child
    does not count."""
    with _open() as connection:
        rows = connection.execute(
            "SELECT outcomes FROM onboard_jobs WHERE deal_id = ? AND child_id = ? AND id != ? "
            "AND outcomes IS NOT NULL ORDER BY created_at DESC, rowid DESC",
            (str(deal_id), child_id, exclude_job_id),
        ).fetchall()
    prefix = f"{key}:"
    for row in rows:
        try:
            outcomes = json.loads(row["outcomes"])
        except (TypeError, ValueError):
            continue
        for entry in reversed(outcomes if isinstance(outcomes, list) else []):
            if isinstance(entry, str) and entry.startswith(prefix):
                return entry[len(prefix):]
    return None


def recover_in_flight() -> int:
    """A job found mid-write (creating_child / creating_contacts) means a worker
    died after it may have written. It is NEVER replayed: it becomes
    `needs_review` (`interrupted`). Re-enqueueing starts a fresh job whose live
    plan sees whatever landed. Returns how many were moved."""
    with _open() as connection:
        cursor = connection.execute(
            "UPDATE onboard_jobs SET status = ?, last_error_code = ?, claimed_at = NULL, "
            "updated_at = ? WHERE status IN (?, ?)",
            (NEEDS_REVIEW, "interrupted", _now(), CREATING_CHILD, CREATING_CONTACTS),
        )
    return cursor.rowcount


def _check_fields(fields: dict) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"not an updatable column: {sorted(bad)}")


def _encode(name: str, value):
    if name == "outcomes" and value is not None:
        return json.dumps(value)
    return value


def _reset_for_tests() -> None:
    with _open() as connection:
        connection.execute("DELETE FROM onboard_jobs")
