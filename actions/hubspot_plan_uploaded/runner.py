"""One-off: set `famly_plan_uploaded` to true or false on a list of HubSpot deals.

The ONLY caller of `HubSpotDealWriter.write_plan_uploaded`. Counts, ids and codes only.

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
