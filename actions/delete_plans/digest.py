"""The delete_plans end-of-run Slack digest -- part of the TEMPORARY action.

Instead of a message per child, `delete_plans` posts ONE digest. Only three
things still post at once (see runner.py): an unexpected result that stops a
child, a tripped pause and a daily-cap stop. Everything else lands here.

WHEN IT POSTS
  * after each child is processed, if EVERY child on the allowlist now has an
    unreported `delete_log` row (`post_if_complete`);
  * `python main.py delete-digest` posts whatever is unreported, listing any
    allowlist child with no row as "Not received";
  * `delete-digest --if-quiet N` posts only if there are unreported rows and the
    newest is at least N minutes old, so it can run from cron every few minutes.

A ROW IS POSTED IN AT MOST ONE DIGEST. Rows are CLAIMED by stamping `reported_at`
before anything is posted, so two digests starting together cannot both report
a row; a digest whose Slack post fails entirely gives its claim back. A row that
arrives after a digest has claimed its rows belongs to the next digest.

NAMES. Each child's name is looked up AT DIGEST TIME with the existing 3-second
`child_lookup` and falls back to the bare id on any failure. It is never stored
(not in `delete_log`, not in a response). It is never fetched during a webhook
request either: the digest a webhook triggers (`post_if_complete`) uses ids
only; only the CLI digest fetches names.

TEXT. A header with the mode and the counts, a section per category with one
line per child (name, the id in code formatting, the number of plans deleted;
the reason for a failure; the plans that WOULD be deleted in a dry run), and the
backup folder at the end. Over Slack's size limit it is split into numbered
messages (1/2, 2/2).
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from actions.plan_write import preview_store
from integrations import child_lookup, slack

from . import runner

logger = logging.getLogger(__name__)

# A conservative size for one Slack message (Slack truncates far above this, and
# recommends staying near 4,000 characters).
SLACK_LIMIT = 3500

# The log outcomes that END a child's request -> the digest category they fall in.
TERMINAL_CATEGORY = {
    "done": "deleted",
    "dry_run": "deleted",  # in a dry run: "would delete"
    "nothing_to_delete": "nothing",
    "partial": "partial",
    "stopped": "failed",
    "error": "failed",
    "refused": "refused",
    "not_allowlisted": "refused",
}

# (key, title, header wording). "not_received" has no log row by definition.
CATEGORIES = [
    ("deleted", "Deleted", "deleted"),
    ("nothing", "Nothing to delete", "nothing to delete"),
    ("partial", "Partly deleted or skipped", "partly deleted or skipped"),
    ("failed", "Failed", "failed"),
    ("refused", "Refused", "refused"),
    ("not_received", "Not received", "not received"),
]


@dataclass
class Line:
    """One child's place in the digest."""

    child_id: str
    category: str
    deleted: int = 0
    skipped: int = 0
    reason: str = ""
    plans: list = field(default_factory=list)  # dry run: what would be deleted
    dry_run: bool = False


@dataclass
class DigestResult:
    posted: bool = False
    rows: int = 0
    messages: int = 0
    reason: str = ""


# --------------------------------------------------------------------------- #
# Claiming rows
# --------------------------------------------------------------------------- #
def unreported_rows() -> list[dict]:
    with preview_store.open_store() as connection:
        rows = connection.execute(
            "SELECT * FROM delete_log WHERE reported_at IS NULL ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _claim(rows: list[dict], now: datetime) -> list[dict]:
    """Stamp `reported_at` on these rows and return the ones THIS call claimed.

    The UPDATE only touches rows that are still unreported, and the claim token
    is unique to this call, so a concurrent digest cannot claim the same row.
    """
    if not rows:
        return []
    token = f"{now.isoformat()}#{uuid.uuid4().hex[:8]}"
    marks = ",".join("?" for _ in rows)
    with preview_store.open_store() as connection:
        connection.execute(
            f"UPDATE delete_log SET reported_at = ? WHERE reported_at IS NULL AND id IN ({marks})",
            (token, *[r["id"] for r in rows]),
        )
        claimed = connection.execute(
            "SELECT * FROM delete_log WHERE reported_at = ? ORDER BY id", (token,)
        ).fetchall()
    result = [dict(r) for r in claimed]
    for row in result:
        row["_token"] = token
    return result


def _release(rows: list[dict]) -> None:
    """Give a claim back (the digest could not be posted at all)."""
    if not rows:
        return
    token = rows[0]["_token"]
    with preview_store.open_store() as connection:
        connection.execute("UPDATE delete_log SET reported_at = NULL WHERE reported_at = ?", (token,))


# --------------------------------------------------------------------------- #
# Turning rows into lines
# --------------------------------------------------------------------------- #
def _detail(row: dict):
    try:
        return json.loads(row.get("detail") or "")
    except (TypeError, ValueError):
        return None


def summarise(rows: list[dict]) -> dict[str, Line]:
    """One Line per child: how its LATEST request in these rows ended."""
    lines: dict[str, Line] = {}
    seen: set[str] = set()
    for row in rows:
        child = row["child_id"]
        seen.add(child)
        category = TERMINAL_CATEGORY.get(row["outcome"])
        if category is None:
            continue  # a step along the way, not how the request ended

        detail = _detail(row)
        line = Line(child_id=child, category=category)
        if row["outcome"] == "dry_run":
            line.dry_run = True
            line.plans = detail if isinstance(detail, list) else []
            line.deleted = len(line.plans)
        elif isinstance(detail, dict):
            line.deleted = int(detail.get("deleted") or 0)
            line.skipped = int(detail.get("skipped") or 0)
            line.reason = str(detail.get("message") or "")
        else:
            line.reason = str(row.get("detail") or "")
        if row["outcome"] in ("error", "not_allowlisted"):
            line.reason = str(row.get("detail") or "")
        lines[child] = line  # rows are in id order: the latest wins

    for child in seen - set(lines):
        # Rows but no ending (it should not happen): say so rather than hide it.
        lines[child] = Line(child, "failed", reason="no final result was recorded")
    return lines


def _label(child_id: str, name: str | None) -> str:
    if name:
        return f"{slack._slack_safe_name(name)} (`{child_id}`)"
    return f"`{child_id}`"


def _plan_text(plan: dict) -> str:
    estimate = plan.get("monthlyEstimate")
    return (
        f"{plan.get('from') or 'unknown'} to {plan.get('to') or 'open-ended'} "
        f"({'?' if estimate is None else estimate})"
    )


def _line_text(line: Line, name: str | None) -> str:
    who = _label(line.child_id, name)
    if line.category == "not_received":
        return f"• {who} — no request received"
    if line.dry_run and line.category == "deleted":
        plans = "; ".join(_plan_text(p) for p in line.plans)
        return f"• {who} — would delete {line.deleted} plan(s): {plans}"
    if line.category == "deleted":
        return f"• {who} — {line.deleted} plan(s) deleted"
    if line.category == "nothing":
        return f"• {who} — no plans"
    if line.category == "partial":
        return f"• {who} — {line.deleted} plan(s) deleted; {line.skipped} skipped (no CanDeletePlan)"
    if line.category in ("failed", "refused"):
        deleted = f" ({line.deleted} deleted first)" if line.deleted else ""
        return f"• {who} — {line.reason}{deleted}"
    return f"• {who}"


def build_lines(
    lines: dict[str, Line], not_received: list[str], names: dict[str, str | None], run_mode: str
) -> list[str]:
    """The digest as text lines: header, one section per category, the backup path."""
    everyone = list(lines.values()) + [Line(c, "not_received") for c in not_received]
    counts = {key: sum(1 for l in everyone if l.category == key) for key, _, _ in CATEGORIES}

    dry = run_mode == runner.MODE_DRY_RUN
    parts = []
    for key, _title, wording in CATEGORIES:
        label = "would delete" if (dry and key == "deleted") else wording
        parts.append(f"{counts[key]} {label}")
    out = [
        f":wastebasket: *delete_plans digest* — {run_mode.replace('_', ' ')} — "
        f"{len(everyone)} child(ren): " + ", ".join(parts)
    ]

    for key, title, _ in CATEGORIES:
        members = [l for l in everyone if l.category == key]
        if not members:
            continue
        heading = "Would delete (dry run)" if (dry and key == "deleted") else title
        out += ["", f"*{heading}* ({len(members)})"]
        out += [_line_text(l, names.get(l.child_id)) for l in members]

    out += ["", f"Backups: `{runner.backup_dir()}`"]
    return out


def chunk(lines: list[str], limit: int = SLACK_LIMIT) -> list[str]:
    """Split text lines into messages of at most `limit` characters.

    Over the limit, each message starts with its place ("(1/2)", "(2/2)"). Lines
    are never split mid-line unless one line alone is too long.
    """
    room = limit - 12  # for the "(k/n) " prefix
    pieces: list[str] = []
    for line in lines:
        while len(line) > room:
            pieces.append(line[:room])
            line = line[room:]
        pieces.append(line)

    messages: list[list[str]] = [[]]
    size = 0
    for piece in pieces:
        if size and size + len(piece) + 1 > room:
            messages.append([])
            size = 0
        messages[-1].append(piece)
        size += len(piece) + 1

    texts = ["\n".join(m).strip("\n") for m in messages]
    if len(texts) == 1:
        return texts
    return [f"({i}/{len(texts)}) {t}" for i, t in enumerate(texts, start=1)]


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #
def _modes(rows: list[dict]) -> str:
    modes = {r["mode"] for r in rows if TERMINAL_CATEGORY.get(r["outcome"])}
    if len(modes) == 1:
        return modes.pop()
    return "mixed" if modes else runner.mode()


def post_digest(*, names: bool, now: datetime | None = None, if_quiet_minutes: float | None = None) -> DigestResult:
    """Post one digest of every unreported row.

    `names` is True only for the CLI. `if_quiet_minutes` posts only when the
    newest unreported row is at least that old.
    """
    now = now or runner._now()
    pending = unreported_rows()
    if not pending:
        return DigestResult(reason="nothing unreported")

    if if_quiet_minutes is not None:
        newest = max(datetime.fromisoformat(r["at"]) for r in pending)
        if (now - newest).total_seconds() < if_quiet_minutes * 60:
            return DigestResult(rows=len(pending), reason=f"the newest row is under {if_quiet_minutes:g} minute(s) old")

    claimed = _claim(pending, now)
    if not claimed:
        return DigestResult(reason="another digest took these rows")

    lines = summarise(claimed)
    reported = {c.lower() for c in lines}
    not_received = sorted(c for c in runner.allowlist() if c not in reported)

    looked_up: dict[str, str | None] = {}
    if names:
        for child in list(lines) + not_received:
            looked_up[child] = child_lookup.lookup_child_name(child)  # never raises; None on failure

    text = build_lines(lines, not_received, looked_up, _modes(claimed))
    messages = chunk(text)

    delivered = sum(1 for message in messages if slack.post_notice(message))
    if delivered == 0:
        _release(claimed)  # nothing reached Slack: the rows wait for the next digest
        return DigestResult(rows=len(claimed), reason="Slack did not accept the digest")
    return DigestResult(posted=True, rows=len(claimed), messages=delivered)


def post_if_complete(now: datetime | None = None) -> DigestResult:
    """After a child: post the digest if every allowlist child has an unreported row.

    Called from the webhook, so it fetches NO names.
    """
    expected = runner.allowlist()
    if not expected:
        return DigestResult(reason="the allowlist is empty")
    have = {r["child_id"].lower() for r in unreported_rows()}
    if not expected <= have:
        return DigestResult(reason="not every allowlist child has reported yet")
    return post_digest(names=False, now=now)
