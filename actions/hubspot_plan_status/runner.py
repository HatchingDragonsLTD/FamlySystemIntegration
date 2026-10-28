"""Backfill `famly_plan_status = plan_created` on HubSpot deals from what Famly shows (`--from-famly`).

The rules are deliberately narrow, because the status drives workflows (a CS task on needs_attention):

  * it writes ONLY `plan_created`, and ONLY where a live read of the child's plans (GET only) shows a plan;
  * it writes ONLY where the deal's status is EMPTY: a non-empty status (plan_created or needs_attention) is
    never overwritten, so an open needs_attention is never wiped by a backfill;
  * it never writes `needs_attention` and never writes or clears the note;
  * a deal with no `famly_childid`, a plan list that cannot be read, or no plan at all is skipped;
  * at most MAX_DEALS (60) writes per run: with --file more than 60 deals is refused, with --operations the
    rest are reported `deferred` (run again);
  * a dry run unless `confirm`; one PATCH per deal, never retried; counts, ids and codes only.

`--operations` uses the shared Leave Date scope (`onboard_audit.scope`). The reading helpers are the ones
`hubspot-set-plan-uploaded` uses (`hubspot_plan_uploaded.runner`), so the two commands read deals the same way.
"""

from actions.hubspot_plan_uploaded import runner as plan_uploaded
from core import hubspot
from core.hubspot import HubSpotDealWriter, HubSpotError

MAX_DEALS = plan_uploaded.MAX_DEALS
FileRefused = plan_uploaded.FileRefused
parse_deal_ids = plan_uploaded.parse_deal_ids

PROPERTY_MISSING, VALUE_NOT_OFFERED = "property_missing", "value_not_offered"
NOT_FOUND, READ_FAILED = plan_uploaded.NOT_FOUND, plan_uploaded.READ_FAILED
NO_CHILD, HAS_STATUS, NO_PLAN, PLAN_UNREADABLE = "no_child", "skipped_has_status", "no_plan", "plan_unreadable"
WOULD_SET, SET, DEFERRED, REJECTED = "would_set", "set", "deferred", "rejected"


def preflight(writer: HubSpotDealWriter) -> str | None:
    """None when `famly_plan_status` exists and (if it is a dropdown) offers `plan_created`, else a code."""
    definition = writer.deal_property_definitions().get(hubspot.PLAN_STATUS_PROPERTY)
    if definition is None:
        return PROPERTY_MISSING
    offered = definition.get("options") or []
    if offered and hubspot.PLAN_STATUS_CREATED not in offered:
        return VALUE_NOT_OFFERED
    return None


def operations_states(writer, scope=None, report=None) -> list:
    """(deal id, famly_childid, famly_plan_status) for the Operations deals in scope."""
    return plan_uploaded.operations_states(writer, scope, report, property_name=hubspot.PLAN_STATUS_PROPERTY)


def file_states(writer, ids: list[str]) -> list:
    """(deal id, famly_childid, famly_plan_status) for each id, or (deal id, code) when it could not be read."""
    out = []
    for deal_id in ids:
        try:
            child_id, status, _note = writer.read_plan_status_state(deal_id)
            out.append((deal_id, child_id, status))
        except HubSpotError as exc:
            out.append((deal_id, NOT_FOUND if exc.status_code == 404 else READ_FAILED))
    return out


def backfill(writer, has_plan, states: list, confirm: bool, cap: int = MAX_DEALS) -> dict:
    """Decide and (with `confirm`) write. `has_plan(child_id)` is True or False, or raises. Returns counts and
    per-deal codes (ids and codes only)."""
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
        _deal, child_id, status = entry
        if not child_id:
            results[deal_id] = NO_CHILD
        elif status:
            results[deal_id] = HAS_STATUS                     # never overwritten, whatever it says
        else:
            try:
                found = has_plan(child_id)
            except Exception:  # noqa: BLE001 - an unreadable plan list changes nothing
                results[deal_id] = PLAN_UNREADABLE
                continue
            if not found:
                results[deal_id] = NO_PLAN
            elif not confirm:
                results[deal_id] = WOULD_SET
            elif written >= cap:
                results[deal_id] = DEFERRED
            else:
                try:
                    writer.write_plan_status(deal_id, hubspot.PLAN_STATUS_CREATED)    # one PATCH, never retried
                    results[deal_id] = SET
                    written += 1
                except (HubSpotError, ValueError):
                    results[deal_id] = REJECTED
    summary: dict[str, int] = {}
    for code in results.values():
        summary[code] = summary.get(code, 0) + 1
    return {"mode": "write" if confirm else "dry_run", "deals": len(results), "results": dict(sorted(summary.items())),
            "written": written, "cap": cap, "perDeal": results if confirm else None}
