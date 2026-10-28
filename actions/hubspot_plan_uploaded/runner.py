"""Set `famly_plan_uploaded` on a list of HubSpot deals: to a chosen value (`run`), or to what Famly
shows (`reconcile`, `--from-famly`). Counts, ids and codes only.

Callers of `HubSpotDealWriter.write_plan_uploaded`: this command and, after a confirmed commit,
`actions/plan_write/plan_flag.py` (the server owns the flag).

  * pre-flight: the deal property definitions are listed first; the property must exist and be a
    boolean checkbox, else nothing is read or written (a code is returned);
  * dry run (no --confirm): each deal's current value is READ; nothing is written;
  * --confirm: only deals whose current value differs from --value are written, one PATCH per deal,
    sent once and never retried; a deal is not re-read afterwards (the next dry run shows it);
  * at most MAX_DEALS deals per file, checked before any request.
"""

import csv
import io
import re

from core.hubspot import PLAN_UPLOADED_PROPERTY, HubSpotDealWriter, HubSpotError

MAX_DEALS = 60
VALUES = ("true", "false")

CHANGED, UNCHANGED, NOT_FOUND, REJECTED, READ_FAILED = (
    "changed", "unchanged", "not_found", "rejected", "read_failed")
PROPERTY_MISSING, PROPERTY_NOT_BOOLEAN = "property_missing", "property_not_boolean"

_ID = re.compile(r"\d+")


class FileRefused(Exception):
    """The file cannot be used; `code` says why. Nothing was sent."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_deal_ids(text: str) -> list[str]:
    """Deal ids (de-duplicated, in file order) from a plain id file (digits per line, blank lines
    and `#` comments ignored) or the onboard-audit CSV (the `dealId` column; the rest is ignored)."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    if "dealid" in first.lower() and "," in first:
        rows = csv.DictReader(io.StringIO(text))
        column = next((n for n in rows.fieldnames or [] if n.strip().lower() == "dealid"), None)
        raw = [(row.get(column) or "").strip() for row in rows] if column else []
    else:
        raw = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    ids: list[str] = []
    for number, value in enumerate(v for v in raw if v):
        if not _ID.fullmatch(value):
            raise FileRefused("bad_deal_id")
        ids.append(value)
    if not ids:
        raise FileRefused("no_deal_ids")
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_DEALS:
        raise FileRefused("too_many_deals")
    return ids


def preflight(writer: HubSpotDealWriter) -> str | None:
    """None when `famly_plan_uploaded` exists and is a boolean checkbox, else a code."""
    definition = writer.deal_property_definitions().get(PLAN_UPLOADED_PROPERTY)
    if definition is None:
        return PROPERTY_MISSING
    if definition.get("type") not in ("bool", "boolean") and definition.get("fieldType") != "booleancheckbox":
        return PROPERTY_NOT_BOOLEAN
    return None


def operations_states(writer, scope=None, report: dict | None = None,
                      property_name: str = PLAN_UPLOADED_PROPERTY) -> list[tuple[str, str | None, str | None]]:
    """(deal id, famly_childid, value of `property_name`) for the deals of the Operations pipeline: ONE
    stage-list read plus one search page per 100 deals. `scope` (an `onboard_audit.scope.Scope`) applies the
    shared operations scope (Leave Date cutoff); None = every deal in any stage, as before. `report`, when
    given, receives the scope counts under "scope"."""
    from actions.onboard_audit import scope as scope_mod
    from actions.onboard_dry_run import derive

    stages = writer.stage_names()
    ids = [sid for sid, (pipeline, _stage) in stages.items() if derive.norm_stage(pipeline) == "operations"]
    if not ids:
        return []
    rows = writer.search_deals([{"propertyName": "dealstage", "operator": "IN", "values": ids}],
                               ["famly_childid", property_name, "end_date", "dealstage"])
    if scope is not None:
        rows, counts = scope_mod.apply(rows, stages, scope)
        if report is not None:
            report["scope"] = counts

    def clean(value):
        return None if value in (None, "") else str(value).strip() or None
    return [(str(r["id"]), clean(r.get("famly_childid")), clean(r.get(property_name))) for r in rows]


def file_states(writer, ids: list[str]) -> list[tuple[str, str | None, str | None] | tuple[str, str]]:
    """(deal id, famly_childid, flag) for each id, or (deal id, code) when the deal could not be read."""
    out = []
    for deal_id in ids:
        try:
            child_id, flag = writer.read_plan_state(deal_id)
            out.append((deal_id, child_id, flag))
        except HubSpotError as exc:
            out.append((deal_id, NOT_FOUND if exc.status_code == 404 else READ_FAILED))
    return out


def reconcile(writer, has_plan, states: list, confirm: bool, cap: int = MAX_DEALS) -> dict:
    """Make the flag say what Famly says. For each deal with a `famly_childid`, `has_plan(child_id)`
    (a read of the child's plans: True, False, or raises) gives the wanted value: true when the child has
    a plan, false when not. A plan that cannot be read changes nothing. Dry run unless `confirm`; with it
    only deals whose flag DIFFERS are written (one PATCH each, never retried), at most `cap` per run (the
    rest are `deferred`). Per-deal results are ids and codes only."""
    stopped = preflight(writer)
    if stopped:
        return {"mode": "stopped", "stopped": stopped, "written": 0}
    results: dict[str, str] = {}
    written = 0
    for entry in states:
        deal_id = entry[0]
        if len(entry) == 2:
            results[deal_id] = entry[1]                       # not_found | read_failed
            continue
        _deal, child_id, flag = entry
        if not child_id:
            results[deal_id] = "no_child"
            continue
        try:
            wanted = "true" if has_plan(child_id) else "false"
        except Exception:  # noqa: BLE001 - an unreadable plan list changes nothing
            results[deal_id] = "plan_unreadable"
            continue
        if _state(flag) == wanted:
            results[deal_id] = UNCHANGED
        elif not confirm:
            results[deal_id] = f"would_set_{wanted}"
        elif written >= cap:
            results[deal_id] = "deferred"
        else:
            try:
                writer.write_plan_uploaded(deal_id, wanted)
                results[deal_id] = f"{CHANGED}_{wanted}"
                written += 1
            except (HubSpotError, ValueError):
                results[deal_id] = REJECTED
    summary: dict[str, int] = {}
    for code in results.values():
        summary[code] = summary.get(code, 0) + 1
    return {"mode": "write" if confirm else "dry_run", "deals": len(results), "results": dict(sorted(summary.items())),
            "written": written, "cap": cap,
            "perDeal": results if confirm else None}


def _state(current: str | None) -> str:
    return "empty" if current is None else current.strip().lower()


def run(writer: HubSpotDealWriter, ids: list[str], value: str, confirm: bool) -> dict:
    """Returns counts and per-deal codes. Raises nothing for a single deal's failure."""
    if value not in VALUES:
        raise FileRefused("bad_value")
    if len(ids) > MAX_DEALS:
        raise FileRefused("too_many_deals")
    stopped = preflight(writer)
    if stopped:
        return {"mode": "stopped", "stopped": stopped, "written": 0, "deals": {}}

    current_counts = {"true": 0, "false": 0, "empty": 0, "other": 0, "not_found": 0, "read_failed": 0}
    results: dict[str, str] = {}
    written = 0
    for deal_id in ids:
        try:
            state = _state(writer.read_plan_uploaded(deal_id))
        except HubSpotError as exc:
            code = NOT_FOUND if exc.status_code == 404 else READ_FAILED
            current_counts[code] += 1
            results[deal_id] = code
            continue
        current_counts[state if state in ("true", "false", "empty") else "other"] += 1
        if state == value:
            results[deal_id] = UNCHANGED
        elif not confirm:
            results[deal_id] = "would_change"
        else:
            try:
                writer.write_plan_uploaded(deal_id, value)
                results[deal_id] = CHANGED
                written += 1
            except (HubSpotError, ValueError):
                results[deal_id] = REJECTED
    summary = {code: sum(1 for r in results.values() if r == code)
               for code in ("would_change", CHANGED, UNCHANGED, NOT_FOUND, REJECTED, READ_FAILED)
               if any(r == code for r in results.values())}
    return {
        "mode": "write" if confirm else "dry_run",
        "value": value,
        "deals": len(ids),
        "currently": {k: v for k, v in current_counts.items() if v},
        "results": summary,
        "written": written,
        "perDeal": results if confirm else None,
    }
