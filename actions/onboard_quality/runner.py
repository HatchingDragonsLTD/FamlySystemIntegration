"""onboard-quality-check: the read-only onboard-audit, run for the deals that matter, with a diff against the
last run and a Slack summary. READ-ONLY end to end: the clients are the audit's read-only wrappers, and this
package imports no writer. Ids and codes only: no names, emails, phones or addresses anywhere.

  scope   every Operations deal not in Left Nursery, plus Left Nursery deals whose end_date is within the
          last LEFT_NURSERY_WINDOW_DAYS days;
  tiers   see config.py;
  state   {dealId: [alert codes]} in a JSON file beside ONBOARD_STORE_PATH, diffed with the previous run;
  Slack   new issues (never more than MAX_IDS_PER_CODE ids per code), nothing when nothing is new, a
          one-line baseline summary, the Monday digest, a short failure notice. A Slack failure never
          raises, but when this run had something to post and the post failed the state file is NOT
          advanced (`postFailed`; the CLI exits 1), so the same issues are announced next run.
"""

import json
import logging
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from actions.onboard import messages, notifier, store
from actions.onboard_audit import runner as audit
from actions.onboard_dry_run import derive
from integrations import reference_files

from . import config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def in_scope(deal: dict, stage: str | None, today: date) -> bool:
    """Operations deals are given; Left Nursery counts only while its end_date is recent."""
    if derive.norm_stage(stage or "") != derive.norm_stage(config.LEFT_NURSERY_STAGE):
        return True
    try:
        end = derive.parse_hubspot_date(deal.get("end_date"))
    except ValueError:
        return False
    return end is not None and end >= today - timedelta(days=config.LEFT_NURSERY_WINDOW_DAYS)


def scope_deals(hubspot, today: date) -> list[dict]:
    """The in-scope Operations deals (one stage-list read plus one search page per 100 deals)."""
    stages = hubspot.stage_names()
    deals = audit.operations_deals(hubspot, stages, None)
    return [d for d in deals
            if in_scope(d, stages.get(str(d.get("dealstage")), ("", ""))[1], today)]


# --------------------------------------------------------------------------- #
# Tiers, state and the diff
# --------------------------------------------------------------------------- #
def alert_codes_of(row) -> list[str]:
    """The row's alert-tier codes (de-duplicated, sorted). `child_missing` is dropped for a stage that is
    not yet expected to have a child."""
    out = []
    for code in dict.fromkeys(row.codes):
        if config.tier(code) != config.ALERT:
            continue
        if code == audit.CHILD_MISSING and (row.stage or "") in config.CHILD_NOT_YET_EXPECTED_STAGES:
            continue
        out.append(code)
    return sorted(out)


def classify(rows) -> tuple[dict[str, list[str]], dict[str, dict[str, int]]]:
    """({deal id: alert codes}, {tier: {code: count}}) for the alert and count tiers."""
    alerts: dict[str, list[str]] = {}
    tiers = {config.ALERT: Counter(), config.COUNT: Counter()}
    for row in rows:
        codes = alert_codes_of(row)
        if codes:
            alerts[row.deal_id] = codes
        for code in codes:
            tiers[config.ALERT][code] += 1
        for code in dict.fromkeys(row.codes):
            if config.tier(code) == config.COUNT:
                tiers[config.COUNT][code] += 1
    return alerts, {name: dict(counts) for name, counts in tiers.items()}


def diff(previous: dict[str, list[str]], current: dict[str, list[str]]):
    """(new: {code: [deal ids]}, resolved count, persisting count), over (deal, code) pairs."""
    before = {(d, c) for d, codes in previous.items() for c in codes}
    now = {(d, c) for d, codes in current.items() for c in codes}
    new: dict[str, list[str]] = {}
    for deal, code in sorted(now - before):
        new.setdefault(code, []).append(deal)
    return new, len(before - now), len(before & now)


def state_path() -> Path:
    return store.store_path().parent / config.STATE_FILE_NAME


def load_state(path: Path) -> dict[str, list[str]] | None:
    """The previous run's {deal id: codes}, or None when there is none (the first ever run)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(d): [str(c) for c in codes] for d, codes in data["alerts"].items()}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None


def save_state(path: Path, alerts: dict[str, list[str]], today: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runDate": today.isoformat(), "alerts": alerts}, indent=1, sort_keys=True),
                    encoding="utf-8")


def write_report(rows, reports_dir: Path, today: date) -> Path:
    """reports/quality_YYYY-MM-DD.csv (the onboard-audit CSV columns), keeping the last KEEP_REPORTS."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / f"quality_{today.isoformat()}.csv"
    audit.write_csv(str(target), rows)
    for old in sorted(reports_dir.glob("quality_*.csv"), reverse=True)[config.KEEP_REPORTS:]:
        old.unlink()
    return target


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def reference_ages(now_ts: float | None = None) -> dict[str, int | None]:
    """Age in whole days of each of the five generated reference files (None when the file is missing)."""
    now_ts = time.time() if now_ts is None else now_ts
    ages: dict[str, int | None] = {}
    for name, path in reference_files.all_paths().items():
        try:
            ages[name] = max(0, int((now_ts - path.stat().st_mtime) // 86400))
        except OSError:
            ages[name] = None
    return ages


def check_reference_freshness(post, now_ts: float | None = None) -> list[str]:
    """Log the resolved reference directory and each file's age, and post ONE line when any file is older
    than REFERENCE_MAX_AGE_DAYS (or missing). Separate from the state logic: a failed post here changes
    nothing else. Returns the stale file names."""
    ages = reference_ages(now_ts)
    directory = reference_files.reference_dir() or reference_files.REPO_DIR
    logger.info("onboard-quality-check: reference dir=%s ages_days=%s", directory,
                {name: ("missing" if age is None else age) for name, age in ages.items()})
    stale = {name: age for name, age in ages.items() if age is None or age > config.REFERENCE_MAX_AGE_DAYS}
    if stale:
        _post(post, messages.quality_reference_stale(stale, config.REFERENCE_MAX_AGE_DAYS))
    return sorted(stale)


def _post(post, text: str) -> bool:
    """Post to Slack; a failure is logged (the type only) and never raised."""
    try:
        return bool(post(text))
    except Exception as exc:  # noqa: BLE001 - Slack must never fail the run
        logger.warning("onboard-quality-check: Slack post failed (%s)", type(exc).__name__)
        return False


def run(hubspot, gql, rest, *, baseline: bool = False, digest: bool = False, post=None,
        today: date | None = None, state_file: Path | None = None, reports_dir: Path | str | None = None,
        site_ids=None) -> dict:
    """Run the check. Returns counts only. If the audit raises, a short failure notice is posted (the error
    type only) and the exception is re-raised for a non-zero exit; the previous state is left alone."""
    today = today or date.today()
    post = post or notifier.SlackNotifier().notice
    state_file = state_file or state_path()
    reports_dir = Path(reports_dir or config.REPORTS_DIR)
    try:
        deals = scope_deals(hubspot, today)
        _summary, rows = audit.run(hubspot, gql, rest, deals=deals, include_left=True, site_ids=site_ids)
    except Exception as exc:  # noqa: BLE001 - reported by its type only, then re-raised
        _post(post, messages.quality_failed(type(exc).__name__))
        raise

    alerts, tiers = classify(rows)
    previous = load_state(state_file)
    first_run = previous is None
    new, resolved, persisting = diff(previous or {}, alerts)
    report = write_report(rows, reports_dir, today)
    requests = {"hubspot": getattr(hubspot, "calls", None), "famlyGraphql": getattr(gql, "calls", None),
                "famlyRest": getattr(rest, "calls", None)}
    logger.info("onboard-quality-check: deals=%d alert_issues=%d new=%d resolved=%d persisting=%d requests=%s",
                len(rows), sum(len(v) for v in alerts.values()), sum(len(v) for v in new.values()),
                resolved, persisting, requests)

    open_by_code = tiers[config.ALERT]
    posted = False
    has_post = bool(baseline or first_run or new or digest)
    if baseline or first_run:
        posted = _post(post, messages.quality_baseline(len(rows), sum(open_by_code.values()), len(open_by_code)))
    elif new or digest:
        refs = {code: [notifier.deal_ref(d) for d in ids] for code, ids in new.items()}
        posted = _post(post, messages.quality_report(
            refs, open_by_code, resolved, persisting, config.MAX_IDS_PER_CODE,
            digest_tiers=tiers if digest else None))
    # State advances only once what this run found has been announced (or there was nothing to announce), so
    # a failed post leaves the previous state and the same issues are new again next run.
    post_failed = has_post and not posted
    if post_failed:
        logger.warning("onboard-quality-check: Slack post failed, state left unchanged")
    else:
        save_state(state_file, alerts, today)
    stale_reference = check_reference_freshness(post)         # its own line; never touches the state logic above
    return {"mode": "baseline" if (baseline or first_run) else "digest" if digest else "alerts",
            "postFailed": post_failed, "referenceStale": stale_reference,
            "dealsChecked": len(rows), "openAlertIssues": sum(open_by_code.values()),
            "new": sum(len(v) for v in new.values()), "resolved": resolved, "persisting": persisting,
            "tiers": {name: dict(sorted(counts.items())) for name, counts in tiers.items()},
            "posted": posted, "report": str(report), "requests": requests}
