"""Batch onboarding from a file of HubSpot deal ids (onboard-enqueue --file,
onboard-dry-run --file). Ids, counts and codes only: no names, emails or file contents.

FILE ENQUEUE IS CLI-STYLE. It does NOT apply the webhook's Deposit Pending stage check
(a backfill deal may be past it); what it validates instead is that the deal exists, is in
the Operations pipeline and is not closed lost.

THE SAFETY GATE. Writing live still needs a deal to be allowed. A job enqueued from a file
records source `file` and a hash of the file (never its contents), and
`config.allowed_for_job` accepts exactly those ids (not test ids, and only for a job
enqueued live). Every deal still gets its own Slack approval (Gate A).
"""

import hashlib
import re
import sys
from collections import Counter

from actions.onboard_dry_run import derive
from core.hubspot import HubSpotError

from . import config, store

SOURCE = "file"
_ID = re.compile(r"[0-9]{1,20}")

# validation codes
DEAL_NOT_FOUND = "deal_not_found"
NOT_OPERATIONS = "not_operations_pipeline"
CLOSED_LOST = "closed_lost"
READ_FAILED = "hubspot_read_failed"

# skip reasons
SKIP_DONE = "already_done"
SKIP_LIVE = "already_queued_or_running"
SKIP_DRY_DONE = "already_dry_run"
SKIP_TEST = "test_deal_not_allowed_by_file"
SKIP_NEEDS_PERSON = "needs_a_person"

NOTE = ("File enqueue is CLI-style: the webhook's Deposit Pending stage check is NOT applied. "
        "Each deal still needs its own Slack approval.")


def parse_ids(text: str) -> tuple[list[str], int | None]:
    """(ids de-duplicated in file order, the first bad line number or None). Blank lines
    and `#` comments are ignored; a line must be digits only."""
    ids: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        if not _ID.fullmatch(content):
            return [], number
        ids.append(content)
    return list(dict.fromkeys(ids)), None


def read_file(path: str) -> tuple[list[str], str]:
    """(ids, a sha256 of the file). Stops the whole command, naming the line NUMBER only."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        print("cannot read the ids file; nothing was queued", file=sys.stderr)
        raise SystemExit(3) from None
    ids, bad = parse_ids(raw.decode("utf-8", errors="replace"))
    if bad is not None:
        print(f"line {bad}: not a deal id (digits only); nothing was queued", file=sys.stderr)
        raise SystemExit(3)
    if not ids:
        print("the ids file has no deal ids; nothing was queued", file=sys.stderr)
        raise SystemExit(3)
    return ids, hashlib.sha256(raw).hexdigest()


def require_scope(limit, all_: bool) -> None:
    """Never run a whole file by accident: --limit N or --all, exactly one."""
    if bool(all_) == (limit is not None):
        print("a --file run needs exactly one of --limit N or --all; nothing was done",
              file=sys.stderr)
        raise SystemExit(3)
    if limit is not None and limit < 1:
        print("--limit must be at least 1; nothing was done", file=sys.stderr)
        raise SystemExit(3)


def validate(hubspot, ids: list[str]) -> tuple[list[str], dict[str, str], Counter]:
    """Read-only. (valid ids in file order, {invalid id: code}, counts by `pipeline / stage`)."""
    try:
        stages = hubspot.stage_names()
    except HubSpotError:
        stages = None
    valid: list[str] = []
    invalid: dict[str, str] = {}
    by_stage: Counter = Counter()
    for deal_id in ids:
        if stages is None:
            invalid[deal_id] = READ_FAILED
            continue
        try:
            deal = hubspot.get_deal(deal_id)
        except HubSpotError as exc:
            invalid[deal_id] = DEAL_NOT_FOUND if exc.status_code == 404 else READ_FAILED
            continue
        pipeline, stage = stages.get(str(deal.get("dealstage")), ("", ""))
        by_stage[f"{pipeline or '?'} / {stage or '?'}"] += 1
        if derive.norm_stage(pipeline) != "operations":
            invalid[deal_id] = NOT_OPERATIONS
        elif derive.norm_stage(stage) == "closed lost":
            invalid[deal_id] = CLOSED_LOST
        else:
            valid.append(deal_id)
    return valid, invalid, by_stage


def classify(deal_id: str, mode: str, include_review: bool) -> tuple[str | None, str | None]:
    """(None, None) if the deal is eligible, else (skip reason, last error code or None),
    by the deal's LATEST job."""
    latest = store.latest_for_deal(deal_id)
    if mode == config.MODE_LIVE and deal_id in config.test_deal_ids():
        return SKIP_TEST, None
    if latest is None:
        return None, None
    if latest.status == store.DONE:
        return SKIP_DONE, None
    if latest.status in store.ACTIVE:
        return SKIP_LIVE, None
    if latest.status == store.DRY_DONE:
        # Eligible for a LIVE run; a second dry run of the same id would only repeat itself
        # (and would stop --limit from moving on through the file).
        return (SKIP_DRY_DONE, None) if mode == config.MODE_DRY else (None, None)
    if include_review:
        return None, None
    return SKIP_NEEDS_PERSON, latest.last_error_code or latest.status


def enqueue_file(hubspot, ids: list[str], file_hash: str, mode: str, *, limit, skip_invalid: bool,
                 include_review: bool) -> dict:
    """Validate, apply eligibility and queue. Returns the printable summary."""
    valid, invalid, by_stage = validate(hubspot, ids)
    summary = {
        "mode": mode, "note": NOTE, "idsRead": len(ids), "valid": len(valid),
        "invalid": dict(sorted(invalid.items())), "byStage": dict(sorted(by_stage.items())),
        "queued": 0, "skipped": {}, "needsAPerson": {}, "queuedIds": [],
    }
    if invalid and not skip_invalid:
        summary["blocked"] = "invalid_ids (nothing queued; fix them or pass --skip-invalid)"
        return summary

    skipped: Counter = Counter()
    needs: Counter = Counter()
    for deal_id in valid:
        if limit is not None and summary["queued"] >= limit:
            break
        reason, code = classify(deal_id, mode, include_review)
        if reason:
            skipped[reason] += 1
            if reason == SKIP_NEEDS_PERSON:
                needs[code] += 1
            continue
        result = store.enqueue(deal_id, mode, source=SOURCE, file_hash=file_hash)
        if result.outcome == store.CREATED:
            summary["queued"] += 1
            summary["queuedIds"].append(deal_id)
        elif result.outcome == store.ALREADY_QUEUED:
            skipped[SKIP_LIVE] += 1
        else:
            skipped[SKIP_DONE] += 1
    summary["skipped"] = dict(sorted(skipped.items()))
    summary["needsAPerson"] = dict(sorted(needs.items()))
    return summary
