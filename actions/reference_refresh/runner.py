"""reference-refresh: refresh the five GENERATED reference files safely, on a schedule.

The five files (see integrations/reference_files.py) live in REFERENCE_DIR on the droplet. This command:

  1. copies the live files into REFERENCE_DIR/.staging and runs the existing pulls (`pull-references`)
     against the STAGED copies (the pulls only read Famly; every path they write is a staged one);
  2. if any pull reports an error for any institution: replaces nothing, alerts, exits 1;
  3. validates each staged file: valid JSON, not empty, and its leaf entry count (session, group, product
     and default ids) has not dropped by more than 20 percent against the live file. It also refuses a
     staged institution_defaults.json that LACKS a half-day add-on pair the live file has (a removal), and a
     sessions pull whose UNMATCHED set holds a session id that is not in the known set recorded beside the
     reference files (see below). If anything fails the whole refresh is refused, every live file is kept,
     and one alert says why (counts, and ids for unmatched sessions);
  4. otherwise backs up each file that changed (a timestamped .bak next to it), puts each staged file in
     place with an atomic os.replace (a failure part-way restores the files already replaced), then prunes
     the backups to the newest KEEP_BACKUPS per file;
  5. posts one Slack summary (ids and counts only) when something changed, with a clearly marked line
     when institution_defaults.json changed (it changes what plans cost). Nothing is posted when nothing
     changed.

`--dry-run` stops after validation and the diff: nothing is replaced and nothing is posted; it also reports,
per institution, the unmatched sessions, the ambiguous slots and the half-pair status.

UNMATCHED SESSIONS. A session whose title does not parse, or two sessions claiming one slot
(`ambiguous_slot`), is unmatched: nothing is written for it, so its slot keeps the id it had (possibly stale)
or stays empty. The ids that are known and reviewed are kept in KNOWN_FILE_NAME in REFERENCE_DIR.
  * no unmatched sessions: passes, whether or not a known set is recorded;
  * unmatched sessions and no recorded set: refused until `--record-unmatched-baseline` records them;
  * a recorded set: refused only for an id that is NOT in it (Slack lists the new ids and counts);
  * `--accept-unmatched ID` (repeatable) adds a reviewed id: it must be unmatched now, and it is logged.
After a successful run the set becomes (known and still unmatched) plus the accepted ids, so an id that was
resolved and breaks again is new again. The set is written only after a successful run, never on a refusal.
Product and group unmatched stay warnings (log and report).

Never touched: catalogue.json and funding_rules.json (hand-maintained) or any file outside the five.
Ids, codes and counts only in the log and in Slack; the pulls' own logging is unchanged. Exit 0 on success
or no change, 1 on any refusal or error (including a failed Slack post after a replace, so the log shows
it). Run it under `flock -n /tmp/reference_refresh.lock` (see the README).
"""

import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from actions.pull_references import runner as pull_references
from integrations import reference_files, slack

from . import messages

logger = logging.getLogger(__name__)

STAGING_DIR_NAME = ".staging"
KNOWN_FILE_NAME = "unmatched_sessions_known.json"
KEEP_BACKUPS = 5
MAX_DROP_PERCENT = 20
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
PRICING_FILE = "institution_defaults"
HALF_FIELDS = ("halfMealsProductId", "halfActivitiesProductId")
AMBIGUOUS_SLOT = "ambiguous_slot"


# --------------------------------------------------------------------------- #
# Reading and comparing the files
# --------------------------------------------------------------------------- #
def load(path: Path):
    """The parsed JSON, or None when the file is missing or not valid JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _walk(node, prefix, out):
    for key, value in node.items():
        if isinstance(value, dict):
            _walk(value, prefix + (str(key),), out)
        elif value is not None and not (isinstance(value, str) and not value.strip()):
            out[prefix + (str(key),)] = value


def leaves(data) -> dict | None:
    """{(institution, *path): value} for every filled leaf under `institutions`, or None when the shape is
    not {"institutions": {...}}. A blank string or null is not an entry. `_comment` is not an entry."""
    institutions = data.get("institutions") if isinstance(data, dict) else None
    if not isinstance(institutions, dict):
        return None
    out: dict = {}
    for code, node in institutions.items():
        if isinstance(node, dict):
            _walk(node, (str(code),), out)
    return out


def diff(old: dict, new: dict) -> dict[str, dict[str, list[str]]]:
    """institution -> {added, removed, changed: [ids]} (the id is the dotted key path under the institution)."""
    out: dict[str, dict[str, list[str]]] = {}

    def bucket(code):
        return out.setdefault(code, {"added": [], "removed": [], "changed": []})
    for key in sorted(set(old) | set(new)):
        code, path = key[0], ".".join(key[1:]) or key[0]
        if key not in old:
            bucket(code)["added"].append(path)
        elif key not in new:
            bucket(code)["removed"].append(path)
        elif old[key] != new[key]:
            bucket(code)["changed"].append(path)
    return out


def validate(name: str, staged, old_leaves: dict | None) -> tuple[int, str | None]:
    """(new entry count, refusal reason or None). Dropping by exactly 20 percent is allowed."""
    new_leaves = leaves(staged)
    if staged is None:
        return 0, "not valid JSON"
    if new_leaves is None:
        return 0, "not an institutions file"
    new = len(new_leaves)
    if new == 0:
        return 0, "empty"
    old = len(old_leaves or {})
    if old and (old - new) * 100 > old * MAX_DROP_PERCENT:
        return new, f"entries dropped by more than {MAX_DROP_PERCENT} percent"
    return new, None


def pull_problems(outcome) -> dict[str, list[str]]:
    """pull name -> institution codes or codes, for every pull that reported an error."""
    problems: dict[str, list[str]] = {name: ["pull_failed"] for name in outcome.errors}
    for name, result in outcome.results.items():
        items = list(sorted(getattr(result, "institutions_failed", None) or {}))
        items += [f"{code} (skipped)" for code in sorted(getattr(result, "institutions_skipped", None) or {})]
        items += [f"{code} (add-on products)" for code in sorted(getattr(result, "addon_products_failed", None) or {})]
        if getattr(result, "fetch_error", None):
            items.append("fetch_failed")
        if getattr(result, "titles_error", None):
            items.append("titles_unreadable")
        if items:
            problems.setdefault(name, []).extend(items)
    return problems


# --------------------------------------------------------------------------- #
# Half pair, unmatched sessions and the known set
# --------------------------------------------------------------------------- #
def _filled(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _half_pair(data, code: str) -> bool:
    """True when `code`'s addonProducts holds BOTH half ids."""
    institutions = data.get("institutions") if isinstance(data, dict) else None
    entry = institutions.get(code) if isinstance(institutions, dict) else None
    addon = entry.get("addonProducts") if isinstance(entry, dict) else None
    return isinstance(addon, dict) and all(_filled(addon.get(field)) for field in HALF_FIELDS)


def half_pair_removed(live, staged) -> list[str]:
    """Institution codes whose live file has a half pair that the staged file lacks."""
    institutions = live.get("institutions") if isinstance(live, dict) else None
    if not isinstance(institutions, dict):
        return []
    return sorted(str(code) for code in institutions if _half_pair(live, code) and not _half_pair(staged, code))


def half_pair_status(staged, outcome) -> dict[str, str]:
    """Per institution: present | failed_kept (the pull failed and the file still has them) | failed_missing | absent."""
    institutions = staged.get("institutions") if isinstance(staged, dict) else None
    failed = set(getattr(outcome.results.get("products"), "half_products_failed", None) or {})
    status = {}
    for code in sorted(institutions or {}):
        has = _half_pair(staged, code)
        if code in failed:
            status[str(code)] = "failed_kept" if has else "failed_missing"
        else:
            status[str(code)] = "present" if has else "absent"
    return status


def unmatched_sessions(outcome) -> tuple[dict[str, list[str]], dict[str, int]]:
    """({institution: unmatched session ids}, {institution: ambiguous slot count}) from the sessions pull. A
    session with no id is keyed '<institution>:no_id'. Ids and counts only."""
    result = outcome.results.get("sessions")
    ids: dict[str, set[str]] = {}
    slots: dict[str, set[str]] = {}
    for item in getattr(result, "unmatched", None) or []:
        code = str(getattr(item, "institution", "") or "?")
        ids.setdefault(code, set()).add(str(getattr(item, "session_id", None) or f"{code}:no_id"))
        if getattr(item, "reason", None) == AMBIGUOUS_SLOT and getattr(item, "slot", None):
            slots.setdefault(code, set()).add(item.slot)
    return ({c: sorted(v) for c, v in sorted(ids.items())}, {c: len(v) for c, v in sorted(slots.items())})


def load_known(path: Path) -> set[str] | None:
    """The recorded known unmatched session ids, or None when no set has been recorded (missing or unreadable)."""
    data = load(path)
    ids = data.get("ids") if isinstance(data, dict) else None
    return {str(i) for i in ids} if isinstance(ids, list) else None


def save_known(path: Path, ids: set[str], now: datetime) -> None:
    """Write the known set atomically (a temp file beside it, then os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".new")
    temp.write_text(json.dumps({"recorded": now.strftime(STAMP_FORMAT), "ids": sorted(ids)}, indent=2) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def _by_institution(current: dict[str, list[str]], only: set[str]) -> str:
    parts = []
    for code, ids in current.items():
        mine = [i for i in ids if i in only]
        if mine:
            shown = ", ".join(mine[:messages.MAX_IDS])
            parts.append(f"{code} {len(mine)} ({shown}{' ...' if len(mine) > messages.MAX_IDS else ''})")
    return "; ".join(parts)


def unmatched_refusal(current: dict[str, list[str]], known: set[str] | None, accept: set[str],
                      baseline: bool) -> dict | None:
    """A refusal item, or None. See the module text for the rules."""
    every = {i for ids in current.values() for i in ids}
    bad_accept = sorted(accept - every)
    if bad_accept:
        return {"file": "session_catalogue", "old": 0, "new": 0, "reason": "accepted id is not unmatched",
                "text": f"--accept-unmatched named an id that is not unmatched now: {', '.join(bad_accept)}"}
    if not every:
        return None
    if known is None and not baseline:
        return {"file": "session_catalogue", "old": 0, "new": len(every), "reason": "no baseline",
                "text": f"{len(every)} unmatched session(s) and no baseline recorded; review them, then run with "
                        f"--record-unmatched-baseline: {_by_institution(current, every)}"}
    allowed = (known or set()) | accept | (every if baseline else set())
    new = every - allowed
    if new:
        return {"file": "session_catalogue", "old": 0, "new": len(new), "reason": "new unmatched sessions",
                "text": f"{len(new)} NEW unmatched session(s) ({len(every) - len(new)} known): "
                        f"{_by_institution(current, new)}"}
    return None


# --------------------------------------------------------------------------- #
# Running the pulls against the staged copies
# --------------------------------------------------------------------------- #
@contextmanager
def staged_environment(staged: dict[str, Path], staging: Path):
    """Point every one of the five files, and REFERENCE_DIR, at the staging directory (the per-file variables
    win over REFERENCE_DIR, so each is set), then restore the environment."""
    names = [reference_files.REFERENCE_DIR_VAR, *reference_files.ENV_VARS.values()]
    saved = {name: os.environ.get(name) for name in names}
    try:
        os.environ[reference_files.REFERENCE_DIR_VAR] = str(staging)
        for key, path in staged.items():
            os.environ[reference_files.ENV_VARS[key]] = str(path)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def count_calls():
    """Count the HTTP calls the pulls make (the clients use requests.post and requests.request)."""
    import requests

    counter = {"n": 0}
    original_post, original_request = requests.post, requests.request

    def post(*args, **kwargs):
        counter["n"] += 1
        return original_post(*args, **kwargs)

    def request(*args, **kwargs):
        counter["n"] += 1
        return original_request(*args, **kwargs)
    requests.post, requests.request = post, request
    try:
        yield counter
    finally:
        requests.post, requests.request = original_post, original_request


# --------------------------------------------------------------------------- #
# Backups, replace and prune
# --------------------------------------------------------------------------- #
def backup_path(path: Path, stamp: str) -> Path:
    return path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")


def prune_backups(path: Path, keep: int = KEEP_BACKUPS) -> int:
    """Delete all but the newest `keep` backups of THIS file (by the stamp in the name). Only names of the
    form <stem>.<stamp>.bak<suffix> are ever considered, so nothing else in the directory is touched."""
    pattern = re.compile(rf"^{re.escape(path.stem)}\.(\d{{8}}T\d{{6}}Z)\.bak{re.escape(path.suffix)}$")
    found = sorted((m.group(1), p) for p in path.parent.iterdir() if (m := pattern.match(p.name)))
    removed = 0
    for _stamp, old in found[:-keep] if keep else found:
        try:
            old.unlink()
            removed += 1
        except OSError:
            logger.warning("reference-refresh: could not remove an old backup of %s", path.stem)
    return removed


def _restore(path: Path, backup: Path | None) -> None:
    """Put a replaced file back from its backup (or remove it when it did not exist before)."""
    if backup is None:
        path.unlink(missing_ok=True)
        return
    temp = path.with_name(path.name + ".restore")
    shutil.copy2(backup, temp)
    os.replace(temp, path)


def apply(changed: list[str], staged: dict[str, Path], live: dict[str, Path], stamp: str, replace=os.replace):
    """Back up and replace every changed file, all or nothing. Returns (ok, failed file or None, restored)."""
    backups: dict[str, Path | None] = {}
    prepared: dict[str, Path] = {}
    try:
        for name in changed:
            target = live[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            backups[name] = None
            if target.exists():
                backups[name] = backup_path(target, stamp)
                shutil.copy2(target, backups[name])
            prepared[name] = target.with_name(target.name + ".new")
            shutil.copy2(staged[name], prepared[name])
    except Exception:  # noqa: BLE001 - nothing has been replaced yet
        for temp in prepared.values():
            temp.unlink(missing_ok=True)
        return False, "backup", 0

    replaced: list[str] = []
    for name in changed:
        try:
            replace(prepared[name], live[name])
            replaced.append(name)
        except Exception:  # noqa: BLE001 - restore what was already replaced
            restored = 0
            for done in replaced:
                try:
                    _restore(live[done], backups[done])
                    restored += 1
                except Exception:  # noqa: BLE001
                    logger.error("reference-refresh: could not restore %s", done)
            for temp in prepared.values():
                temp.unlink(missing_ok=True)
            return False, name, restored
    return True, None, len(replaced)


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def _post(post, text: str) -> bool:
    try:
        return bool(post(text))
    except Exception as exc:  # noqa: BLE001 - Slack must never raise
        logger.warning("reference-refresh: Slack post failed (%s)", type(exc).__name__)
        return False


def _result(started: float, **fields) -> dict:
    fields.setdefault("posted", False)
    fields["seconds"] = round(time.monotonic() - started, 1)
    fields["exit"] = 0 if fields.pop("ok") else 1
    return fields


def run(*, dry_run: bool = False, post=None, pull=None, replace=os.replace, now: datetime | None = None,
        accept_unmatched=(), record_baseline: bool = False, known_path: Path | None = None) -> dict:
    """Run the refresh. Returns counts and codes only; `exit` is the process exit code."""
    started = time.monotonic()
    post = post or slack.post_notice
    pull = pull or pull_references.pull_all_references
    mode = "dry_run" if dry_run else "write"
    accept = {str(i).strip() for i in accept_unmatched if str(i).strip()}

    directory = reference_files.reference_dir()
    if directory is None:
        logger.error("reference-refresh: REFERENCE_DIR is not set; refusing to run (it would write into the repo)")
        return _result(started, ok=False, mode=mode, code="reference_dir_not_set")
    known_path = known_path or directory / KNOWN_FILE_NAME

    live = reference_files.all_paths()
    staging = directory / STAGING_DIR_NAME
    staged = {name: staging / reference_files.file_name(name) for name in reference_files.NAMES}
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for name in reference_files.NAMES:
            if live[name].exists():
                shutil.copy2(live[name], staged[name])

        try:
            with staged_environment(staged, staging), count_calls() as calls:
                outcome = pull()
        except Exception as exc:  # noqa: BLE001 - reported by type only
            logger.error("reference-refresh: the pulls raised (%s)", type(exc).__name__)
            problems = {"refresh": [type(exc).__name__]}
            posted = False if dry_run else _post(post, messages.stopped(problems))
            return _result(started, ok=False, mode=mode, code="pulls_failed", problems=problems, posted=posted)
        famly_calls = calls["n"]

        problems = pull_problems(outcome)
        if problems:
            logger.error("reference-refresh: pulls reported errors %s; nothing replaced", problems)
            posted = False if dry_run else _post(post, messages.stopped(problems))
            return _result(started, ok=False, mode=mode, code="pulls_failed", problems=problems,
                           famlyCalls=famly_calls, posted=posted)

        files: dict[str, dict] = {}
        refusals: list[dict] = []
        changes: dict[str, dict[str, dict[str, list[str]]]] = {}
        for name in reference_files.NAMES:
            old_data, new_data = load(live[name]), load(staged[name])
            old_leaves = leaves(old_data) or {}
            count, reason = validate(name, new_data, old_leaves)
            files[name] = {"old": len(old_leaves), "new": count, "refused": reason}
            if not reason and name == PRICING_FILE:
                removed = half_pair_removed(old_data, new_data)
                if removed:
                    reason = f"half-day add-on pair removed for {', '.join(removed)}"
                    files[name]["refused"] = reason
            if reason:
                refusals.append({"file": name, "old": len(old_leaves), "new": count, "reason": reason})
                continue
            if new_data != old_data:
                changes[name] = diff(old_leaves, leaves(new_data))
                files[name]["changed"] = True
        counts = {name: {k: sum(len(c[k]) for c in per.values()) for k in ("added", "removed", "changed")}
                  for name, per in changes.items()}

        # Unmatched sessions: only an id outside the recorded known set refuses.
        current, ambiguous = unmatched_sessions(outcome)
        known = load_known(known_path)
        every = {i for ids in current.values() for i in ids}
        refusal = unmatched_refusal(current, known, accept, record_baseline)
        if refusal:
            refusals.append(refusal)
        report = {
            "unmatchedSessions": {code: len(ids) for code, ids in current.items()},
            "ambiguousSlots": ambiguous,
            "halfPair": half_pair_status(load(staged[PRICING_FILE]), outcome),
            "newUnmatchedSessions": len(every - (known or set()) - accept),
            "knownSetRecorded": known is not None,
            "productsUnmatched": len(getattr(outcome.results.get("products"), "unmatched", None) or []),
            "groupsUnmatched": len(getattr(outcome.results.get("groups"), "unmatched", None) or []),
        }
        if report["productsUnmatched"] or report["groupsUnmatched"]:
            logger.warning("reference-refresh: unmatched products=%d groups=%d (warnings only)",
                           report["productsUnmatched"], report["groupsUnmatched"])

        if refusals:
            logger.error("reference-refresh: refused %s",
                         [(r["file"], r["old"], r["new"], r["reason"]) for r in refusals])
            posted = False if dry_run else _post(post, messages.refused(refusals))
            return _result(started, ok=False, mode=mode, code="refused", files=files, report=report,
                           famlyCalls=famly_calls, posted=posted)

        # What the known set becomes after a successful run: the ids still unmatched that were known, plus the
        # ones just accepted or baselined. Written only after the replace has succeeded, never on a refusal.
        next_known = ((known or set()) & every) | accept | (every if record_baseline else set())
        write_known = not dry_run and (next_known != known if known is not None else record_baseline)
        for accepted in sorted(accept):
            logger.info("reference-refresh: accepted unmatched session id=%s", accepted)

        if dry_run or not changes:
            if write_known:
                save_known(known_path, next_known, now or datetime.now(timezone.utc))
                logger.info("reference-refresh: known unmatched set recorded (%d id(s))", len(next_known))
            logger.info("reference-refresh: %s, %d file(s) would change", mode, len(changes))
            return _result(started, ok=True, mode=mode, code="dry_run" if dry_run else "no_change", files=files,
                           changes=counts, report=report, famlyCalls=famly_calls)

        stamp = (now or datetime.now(timezone.utc)).strftime(STAMP_FORMAT)
        changed = [name for name in reference_files.NAMES if name in changes]
        ok, failed, count = apply(changed, staged, live, stamp, replace=replace)
        if not ok:
            logger.error("reference-refresh: replace failed at %s; restored %d file(s)", failed, count)
            posted = _post(post, messages.replace_failed(failed, count))
            return _result(started, ok=False, mode=mode, code="replace_failed", files=files, report=report,
                           famlyCalls=famly_calls, posted=posted)

        if write_known:
            save_known(known_path, next_known, now or datetime.now(timezone.utc))
            logger.info("reference-refresh: known unmatched set recorded (%d id(s))", len(next_known))
        pruned = sum(prune_backups(live[name]) for name in reference_files.NAMES)
        pricing = {code: sorted(kinds["added"] + kinds["removed"] + kinds["changed"])
                   for code, kinds in changes.get(PRICING_FILE, {}).items()}
        posted = _post(post, messages.summary(changes, pricing))
        logger.info("reference-refresh: replaced %s; pruned %d backup(s); famly calls %d", changed, pruned, famly_calls)
        return _result(started, ok=posted, mode=mode, code="replaced" if posted else "replaced_post_failed",
                       files=files, changes=counts, report=report, famlyCalls=famly_calls, pruned=pruned,
                       posted=posted)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
