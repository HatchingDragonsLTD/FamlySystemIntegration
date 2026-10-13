"""Durable record of a create_timeline chain.

A chain is the ordered set of plans one new starter needs (see
`actions/plan_write/timeline.py`), created ONE AT A TIME, each behind its own
Slack approval. This module is only the record of where a chain has got to; the
flow that moves it along is `engine.py`.

Stored in the same SQLite file as the previews (`preview_store`), so a chain and
the previews that belong to it can never disagree about which database they are
in. The table and its migration live in `preview_store.py`.

A row holds:

    chain_id    uuid4
    child_id    the Famly child
    payload     the ORIGINAL request, restricted to an allow-list of the fields
                the plan builder reads -- so it carries no names, whatever else
                the caller sent (see `engine.ALLOWED_FIELDS`)
    segments    [{from, to, funded, advanced, plan_id}], in order. `plan_id` is
                null until that segment's plan has been committed
    next_index  how many segments are committed: the segment at this index is
                the one awaiting approval (== len(segments) once all are done)
    status      active | complete | stopped
    created_at  UTC ISO timestamp

At most ONE chain per child can be active, enforced by a unique index in the
database (so two simultaneous requests cannot both start one).

No Flask, no Famly: importable and testable anywhere.
"""

import copy
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from actions.plan_write import preview_store

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_COMPLETE = "complete"
STATUS_STOPPED = "stopped"


@dataclass
class Chain:
    """One chain, as stored."""

    chain_id: str
    child_id: str
    payload: dict = field(default_factory=dict)
    segments: list = field(default_factory=list)
    next_index: int = 0
    status: str = STATUS_ACTIVE
    created_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def last_index(self) -> int:
        return len(self.segments) - 1

    def committed(self) -> list[tuple[int, dict]]:
        """(index, segment) for every segment whose plan has been committed."""
        return [(i, s) for i, s in enumerate(self.segments) if i < self.next_index]

    def not_committed(self) -> list[tuple[int, dict]]:
        """(index, segment) for every segment that has no committed plan."""
        return [(i, s) for i, s in enumerate(self.segments) if i >= self.next_index]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_chain(row: sqlite3.Row) -> Chain:
    def load(text, default):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default

    return Chain(
        chain_id=row["chain_id"],
        child_id=row["child_id"],
        payload=load(row["payload"], {}),
        segments=load(row["segments"], []),
        next_index=row["next_index"],
        status=row["status"],
        created_at=row["created_at"],
    )


def create(chain_id: str, child_id: str, payload: dict, segments: list[dict]) -> bool:
    """Record a new, active chain with no plan committed yet.

    Returns False -- and writes nothing -- when the child already has an active
    chain. That is decided by the database's unique index, so it holds when two
    requests race.
    """
    stored_segments = [{**segment, "plan_id": None} for segment in segments]
    try:
        with preview_store.open_store() as connection:
            connection.execute(
                "INSERT INTO chains "
                "(chain_id, child_id, payload, segments, next_index, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    chain_id,
                    child_id,
                    json.dumps(payload),
                    json.dumps(stored_segments),
                    0,
                    STATUS_ACTIVE,
                    _now(),
                ),
            )
    except sqlite3.IntegrityError:
        return False

    logger.info(
        "Chain created chain_id=%s child_id=%s segments=%d",
        chain_id,
        child_id,
        len(segments),
    )
    return True


def get(chain_id: str) -> Chain | None:
    """Fetch a chain, or None when the id is unknown."""
    if not chain_id:
        return None
    with preview_store.open_store() as connection:
        row = connection.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
    return _row_to_chain(row) if row is not None else None


def find_active(child_id: str) -> Chain | None:
    """The child's active chain, if there is one."""
    if not child_id:
        return None
    with preview_store.open_store() as connection:
        row = connection.execute(
            "SELECT * FROM chains WHERE child_id = ? AND status = ?",
            (child_id, STATUS_ACTIVE),
        ).fetchone()
    return _row_to_chain(row) if row is not None else None


def mark_committed(chain_id: str, index: int, plan_id: str) -> Chain | None:
    """Record that segment `index` is committed as `plan_id`.

    Moves `next_index` on to `index + 1`. Applied ONLY to an active chain whose
    next segment really is `index` -- the plan is already in Famly by the time
    this is called, so a mismatch is returned as None for the caller to report,
    never papered over.

    Returns the updated chain, or None when it was not applied.
    """
    with preview_store.open_store() as connection:
        row = connection.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if row is None:
            return None

        chain = _row_to_chain(row)
        if not chain.is_active or chain.next_index != index:
            return None
        if not 0 <= index < len(chain.segments):
            return None

        segments = copy.deepcopy(chain.segments)
        segments[index]["plan_id"] = plan_id
        cursor = connection.execute(
            "UPDATE chains SET segments = ?, next_index = ? "
            "WHERE chain_id = ? AND status = ? AND next_index = ?",
            (json.dumps(segments), index + 1, chain_id, STATUS_ACTIVE, index),
        )
        if cursor.rowcount == 0:
            return None

    chain.segments = segments
    chain.next_index = index + 1
    logger.info(
        "Chain chain_id=%s segment %d committed as plan_id=%s", chain_id, index, plan_id
    )
    return chain


def finish(chain_id: str, status: str) -> bool:
    """Move an ACTIVE chain to `complete` or `stopped`.

    Only an active chain moves, so a chain that already finished keeps the
    outcome it had. Returns True when this call changed it.
    """
    if status not in (STATUS_COMPLETE, STATUS_STOPPED):
        raise ValueError(f"cannot finish a chain as {status!r}")
    with preview_store.open_store() as connection:
        cursor = connection.execute(
            "UPDATE chains SET status = ? WHERE chain_id = ? AND status = ?",
            (status, chain_id, STATUS_ACTIVE),
        )
        changed = cursor.rowcount > 0

    if changed:
        logger.info("Chain chain_id=%s marked %s", chain_id, status)
    return changed
