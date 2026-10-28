"""Durable record of queued timeline jobs, plus the queue's small shared state.

A job is one starter's whole timeline (see `actions/create_timeline`), accepted
now and built later by the worker. This module is only the record -- the `jobs`
and `queue_state` tables live in the same SQLite file as the previews
(`preview_store`), with their schema and migration there.

A job row:

    job_id       uuid4
    job_key      sha256 of child id + start date + a hash of the allow-listed
                 payload: the idempotency key (unique among live jobs)
    child_id
    payload      the allow-listed request (no names -- see engine.ALLOWED_FIELDS)
    segments     [{from, to, funded, advanced, plan_id, committed_at, dry_run}]
    next_index   how many plans are committed: the plan at this index is next
    status       queued | running | done | dry_done | held | failed | cancelled
    detail       the last reason: why held / failed / cancelled
    preview_id   the stored preview a HELD job waits on a human for
    digest_sent  whether a Slack digest has already reported it

ENQUEUEING is a single atomic INSERT, and the database -- not a check made first --
decides races (see the two unique indexes in preview_store.py):

    * the idempotency key is unique among LIVE jobs (queued, running, held, done):
      an identical request while one is active or finished is the same job
      (`already_queued`). A failed, cancelled or DRY-RUN job does not hold its
      key, so the same request can be sent again and makes a new job;
    * a child can have ONE active job (queued, running or held): a different
      request for a child that has one is refused (`active_conflict`).

Jobs are taken OLDEST FIRST: by `created_at` (stamped by SQLite inside the INSERT,
so it follows the order the inserts really happened in), then by row.

A job moves:  queued -> running -> done | dry_done | held | failed | cancelled, and
held -> queued when a human approves the held plan. `running` is only ever the
state of the single worker's current job; a worker that finds one on startup
resumes it by READ-BACK, never by replaying (see worker.recover).

`queue_state` holds: `paused` ("1"), `consecutive_failures`, `commits:<UTC day>`
(commit attempts that day), and the day a cap notice was posted.

No Flask, no Famly: importable and testable anywhere.
"""

import copy
import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from actions.plan_write import preview_store
from actions.plan_write.hubspot_intake import DEAL_ID_KEYS

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
# A dry run finished: every segment was previewed and checked, and NOTHING was
# committed. Terminal like `done`, but deliberately NOT a live key holder -- a
# dry run is a rehearsal, so the same request must be able to come back and be
# run for real. See `find_live_by_key` and the jobs_live_job_key index.
STATUS_DRY_DONE = "dry_done"
STATUS_HELD = "held"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
ALL_STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_DRY_DONE,
    STATUS_HELD,
    STATUS_FAILED,
    STATUS_CANCELLED,
)
# Statuses that end a job: nothing more will happen to it.
FINISHED = (STATUS_DONE, STATUS_DRY_DONE, STATUS_FAILED, STATUS_CANCELLED)

# Statuses that HOLD the idempotency key. `dry_done` is absent on purpose, and
# must stay absent: it is what lets a rehearsed request be sent again for real.
KEY_HOLDING = (STATUS_QUEUED, STATUS_RUNNING, STATUS_HELD, STATUS_DONE)


@dataclass
class Job:
    job_id: str
    job_key: str
    child_id: str
    payload: dict = field(default_factory=dict)
    segments: list = field(default_factory=list)
    next_index: int = 0
    status: str = STATUS_QUEUED
    detail: str | None = None
    preview_id: str | None = None
    digest_sent: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def last_index(self) -> int:
        return len(self.segments) - 1

    @property
    def institution(self) -> str:
        return str(self.payload.get("institution") or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_key(child_id: str, start: str, payload: dict) -> str:
    """The idempotency key: child, start date and a hash of the payload.

    The HubSpot deal id keys are left out of the hash: they are carried for the famly_plan_uploaded flag,
    not part of what the request IS, so every key stays exactly what it was before they were carried."""
    hashed = {k: v for k, v in payload.items() if k not in DEAL_ID_KEYS}
    digest = hashlib.sha256(
        json.dumps(hashed, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return hashlib.sha256(f"{child_id}|{start}|{digest}".encode("utf-8")).hexdigest()


def _row_to_job(row: sqlite3.Row) -> Job:
    def load(text, default):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default

    return Job(
        job_id=row["job_id"],
        job_key=row["job_key"],
        child_id=row["child_id"],
        payload=load(row["payload"], {}),
        segments=load(row["segments"], []),
        next_index=row["next_index"],
        status=row["status"],
        detail=row["detail"],
        preview_id=row["preview_id"],
        digest_sent=bool(row["digest_sent"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
CREATED = "created"
ALREADY_QUEUED = "already_queued"
ACTIVE_CONFLICT = "active_conflict"


@dataclass
class Enqueued:
    """What an enqueue did: `job` is the new job, or the existing one it hit."""

    job: Job
    outcome: str  # CREATED | ALREADY_QUEUED | ACTIVE_CONFLICT


def create(child_id: str, start: str, payload: dict, segments: list[dict]) -> Enqueued:
    """Enqueue a job with ONE atomic INSERT, and let the database settle races.

    No read comes first: the INSERT either succeeds or violates a unique index.

      * the idempotency-key index  -> the existing LIVE job, outcome
        ALREADY_QUEUED (checked first: an identical request is the same job
        whatever else is true);
      * the one-active-job-per-child index -> the child's active job, outcome
        ACTIVE_CONFLICT -- nothing was created.

    `created_at` is stamped by SQLite inside the INSERT, so it follows the order
    inserts really happened in even when requests race.
    """
    key = job_key(child_id, start, payload)
    stored = [{**s, "plan_id": None} for s in segments]

    # A violation names a job that can have finished by the time we look for it
    # (a rare race); in that case the INSERT is simply tried again.
    for _attempt in range(3):
        job_id = str(uuid.uuid4())
        now = _now()
        try:
            with preview_store.open_store() as connection:
                connection.execute(
                    "INSERT INTO jobs (job_id, job_key, child_id, payload, segments, "
                    "next_index, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
                    (job_id, key, child_id, json.dumps(payload), json.dumps(stored),
                     STATUS_QUEUED, now),
                )
        except sqlite3.IntegrityError:
            existing = find_live_by_key(key)
            if existing is not None:
                return Enqueued(existing, ALREADY_QUEUED)
            active = find_active_for_child(child_id)
            if active is not None:
                return Enqueued(active, ACTIVE_CONFLICT)
            continue

        logger.info("Job queued job_id=%s child_id=%s segments=%d", job_id, child_id, len(segments))
        return Enqueued(get(job_id), CREATED)

    raise RuntimeError("could not enqueue: the database kept refusing the job")


def get(job_id: str) -> Job | None:
    if not job_id:
        return None
    with preview_store.open_store() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row is not None else None


def find_live_by_key(key: str) -> Job | None:
    """The job holding an idempotency key, if any (see KEY_HOLDING).

    A failed or cancelled job is not returned: it no longer holds its key.
    NEITHER IS A DRY RUN -- `dry_done` committed nothing, so the same request
    has to be able to come back and be run for real.

    The statuses here mirror the jobs_live_job_key partial index exactly; the
    index is what actually decides a race, and this only reports who won.
    """
    marks = ", ".join("?" for _ in KEY_HOLDING)
    with preview_store.open_store() as connection:
        row = connection.execute(
            f"SELECT * FROM jobs WHERE job_key = ? AND status IN ({marks})",
            (key, *KEY_HOLDING),
        ).fetchone()
    return _row_to_job(row) if row is not None else None


def find_active_for_child(child_id: str) -> Job | None:
    """The child's active (queued, running or held) job, if there is one."""
    with preview_store.open_store() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE child_id = ? AND status IN (?, ?, ?)",
            (child_id, STATUS_QUEUED, STATUS_RUNNING, STATUS_HELD),
        ).fetchone()
    return _row_to_job(row) if row is not None else None


def list_jobs(status: str | None = None) -> list[Job]:
    """Jobs, oldest first, optionally of one status."""
    with preview_store.open_store() as connection:
        if status:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, rowid", (status,)
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid").fetchall()
    return [_row_to_job(r) for r in rows]


def counts() -> dict[str, int]:
    with preview_store.open_store() as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    found = {r["status"]: r["n"] for r in rows}
    return {status: found.get(status, 0) for status in ALL_STATUSES}


def claim_next() -> Job | None:
    """Take the OLDEST queued job and mark it running, atomically."""
    with preview_store.open_store() as connection:
        row = connection.execute(
            "SELECT job_id FROM jobs WHERE status = ? ORDER BY created_at, rowid LIMIT 1",
            (STATUS_QUEUED,),
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ? AND status = ?",
            (STATUS_RUNNING, _now(), row["job_id"], STATUS_QUEUED),
        )
        if cursor.rowcount == 0:
            return None
    return get(row["job_id"])


def set_status(
    job_id: str,
    status: str,
    detail: str | None = None,
    *,
    preview_id: str | None = None,
    only_from: tuple[str, ...] | None = None,
) -> bool:
    """Move a job to `status`. `only_from` restricts which states may move.

    A finished job (done/failed/cancelled) never moves again.
    """
    allowed = only_from or tuple(s for s in ALL_STATUSES if s not in FINISHED)
    marks = ", ".join("?" for _ in allowed)
    with preview_store.open_store() as connection:
        cursor = connection.execute(
            f"UPDATE jobs SET status = ?, detail = ?, preview_id = ?, updated_at = ? "
            f"WHERE job_id = ? AND status IN ({marks})",
            (status, detail, preview_id, _now(), job_id, *allowed),
        )
        changed = cursor.rowcount > 0
    if changed:
        logger.info("Job job_id=%s -> %s %s", job_id, status, detail or "")
    return changed


def record_segment(job_id: str, index: int, **fields) -> Job | None:
    """Merge `fields` into segment `index` (plan_id, committed_at, dry_run...)."""
    with preview_store.open_store() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        job = _row_to_job(row)
        if not 0 <= index < len(job.segments):
            return None
        segments = copy.deepcopy(job.segments)
        segments[index].update(fields)
        connection.execute(
            "UPDATE jobs SET segments = ?, updated_at = ? WHERE job_id = ?",
            (json.dumps(segments), _now(), job_id),
        )
    job.segments = segments
    return job


def mark_committed(job_id: str, index: int, plan_id: str | None) -> bool:
    """Segment `index` is committed as `plan_id`: advance `next_index`.

    Applied only when `index` really is the job's next plan, so a late or
    repeated report cannot move it twice. Returns True when applied.
    """
    with preview_store.open_store() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return False
        job = _row_to_job(row)
        if job.next_index != index or not 0 <= index < len(job.segments):
            return False
        segments = copy.deepcopy(job.segments)
        segments[index]["plan_id"] = plan_id
        segments[index]["committed_at"] = _now()
        cursor = connection.execute(
            "UPDATE jobs SET segments = ?, next_index = ?, updated_at = ? "
            "WHERE job_id = ? AND next_index = ?",
            (json.dumps(segments), index + 1, _now(), job_id, index),
        )
        return cursor.rowcount > 0


def mark_digested(job_ids: list[str]) -> None:
    with preview_store.open_store() as connection:
        for job_id in job_ids:
            connection.execute("UPDATE jobs SET digest_sent = 1 WHERE job_id = ?", (job_id,))


def undigested_done() -> list[Job]:
    """Finished jobs not yet reported in a digest -- dry runs included.

    A dry run is exactly what someone wants to read in the digest: it says what
    WOULD have happened. Excluding it would make the rehearsal invisible.
    """
    with preview_store.open_store() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) AND digest_sent = 0 "
            "ORDER BY created_at, rowid",
            (STATUS_DONE, STATUS_DRY_DONE),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


# --------------------------------------------------------------------------- #
# Hooks for the Slack approval of a HELD plan (called by approval.py)
# --------------------------------------------------------------------------- #
def release_after_approval(job_id: str, index: int, plan_id: str | None) -> bool:
    """A human approved the HELD plan and it committed: record it, requeue the job.

    The worker then verifies it by read-back like any other commit before it
    goes on. Only a job that is actually held on that plan is released.
    """
    job = get(job_id)
    if job is None or job.status != STATUS_HELD or job.next_index != index:
        return False
    if not mark_committed(job_id, index, plan_id):
        return False
    return set_status(job_id, STATUS_QUEUED, None, only_from=(STATUS_HELD,))


def cancel_after_rejection(job_id: str, index: int, user) -> bool:
    """A human rejected the HELD plan: the job is cancelled. Nothing in Famly moves."""
    job = get(job_id)
    if job is None or job.status != STATUS_HELD or job.next_index != index:
        return False
    return set_status(
        job_id,
        STATUS_CANCELLED,
        f"the held plan was rejected by {user or 'unknown'}",
        only_from=(STATUS_HELD,),
    )


# --------------------------------------------------------------------------- #
# Queue state
# --------------------------------------------------------------------------- #
def state_get(key: str, default: str | None = None) -> str | None:
    with preview_store.open_store() as connection:
        row = connection.execute("SELECT value FROM queue_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def state_set(key: str, value: str | None) -> None:
    with preview_store.open_store() as connection:
        if value is None:
            connection.execute("DELETE FROM queue_state WHERE key = ?", (key,))
        else:
            connection.execute(
                "INSERT INTO queue_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def state_increment(key: str) -> int:
    """Add one to a counter atomically and return the new value."""
    with preview_store.open_store() as connection:
        connection.execute(
            "INSERT INTO queue_state (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
            (key,),
        )
        row = connection.execute("SELECT value FROM queue_state WHERE key = ?", (key,)).fetchone()
    return int(row["value"])


def is_paused() -> bool:
    return state_get("paused") == "1"


def set_paused(paused: bool) -> None:
    state_set("paused", "1" if paused else None)


def consecutive_failures() -> int:
    return int(state_get("consecutive_failures", "0") or 0)


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


def commits_today(now: datetime | None = None) -> int:
    return int(state_get(f"commits:{utc_day(now)}", "0") or 0)
