"""The real monthly estimate of the plan in force today.

WHY: the plan list (`GET /v2/plans/?childId=...`) carries a `monthlyEstimate` that leaves out
the automatic charges and re-priced funding. Famly's own screen gets the true figure by re-running the
plan as a PREVIEW (`PUT /v2/plans/{id}?preview=true&version=3`), whose response `plan.monthlyEstimate`
is what the user sees. This module does that call for audits.

THE ONLY PUT IN THIS CODEBASE. It can only ever preview:
  * `PreviewOnlyRest` is the one place that sends it. Its single method `preview_put` builds the query
    string itself (`preview=true`, `version=3`); the caller cannot pass a query string, a verb or a path.
  * It refuses a body whose plan id is not the id in the path, and an id that is not a plain id.
  * Nothing here imports or calls the commit path (plan_write.runner.commit, approval.py).
  * tests/test_plan_estimate.py scans the source tree: no other module may send a PUT.

THE BODY is built from the plan the list call returned (`build_preview_body`): the same fields Famly's
screen sends. `from` is today (or the plan's own start if that is later), because the screen previews
from today, not from the plan's first day. Nothing is guessed: a missing id raises PreviewBuildError.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable

PLANS_PATH = "/v2/plans"
PREVIEW_VERSION = "3"
_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class PreviewBuildError(ValueError):
    """The plan does not carry what a preview body needs. Nothing was sent."""


class PreviewOnlyRest:
    """The only PUT: a preview of an existing plan. Counts its requests."""

    def __init__(self, client):
        self._client = client
        self.calls = 0

    def preview_put(self, plan_id: str, body: dict) -> Any:
        if not isinstance(plan_id, str) or not _ID.match(plan_id):
            raise PreviewBuildError("plan id is not a plain id")
        plan = body.get("plan") if isinstance(body, dict) else None
        if not isinstance(plan, dict) or plan.get("id") != plan_id:
            raise PreviewBuildError("the body's plan id is not the plan being previewed")
        self.calls += 1
        # The query string is fixed here and nowhere else: this call can never persist anything.
        return self._client.request("PUT", f"{PLANS_PATH}/{plan_id}",
                                    params={"preview": "true", "version": PREVIEW_VERSION}, json_body=body)


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _today(today) -> date:
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, str):
        return date.fromisoformat(today[:10])
    return today or date.today()


def active_plan(plans: list, today=None):
    """The plan in force today: from <= today and (open-ended, or today < to; `to` is exclusive).
    The latest-starting one if several cover today. None when none does (all future, or all past)."""
    day = _today(today)
    covering = []
    for plan in plans:
        start = _as_date(plan.from_)
        end = _as_date(plan.to) if plan.to else None
        if start is not None and start <= day and (end is None or day < end):
            covering.append((start, plan))
    return max(covering, key=lambda pair: pair[0])[1] if covering else None


def _need(node: dict, key: str, where: str) -> Any:
    value = node.get(key)
    if value in (None, ""):
        raise PreviewBuildError(f"{where} has no {key}")
    return value


def _part(node: dict) -> dict:
    term_only = bool(node.get("termTimeOnly"))
    return {
        "ordering": node.get("ordering", 0),
        "planPartId": _need(node, "planPartId", "a plan part"),
        "billingProfileId": _need(node, "billingProfileId", "a plan part"),
        "sessionBookings": [
            {"sessionId": _need(s, "sessionId", "a session booking"), "fundable": s.get("fundable"),
             "day": _need(s, "day", "a session booking"), "start": s.get("start"), "end": s.get("end")}
            for s in node.get("sessionBookings") or []
        ],
        "chargeRuleExemptions": node.get("chargeRuleExemptions") or [],
        "productBookings": node.get("productBookings") or [],
        "billing": {k: (node.get("billing") or {}).get(k) for k in ("id", "invoices", "weeksOfCare", "title")},
        "termTimeOnly": term_only,
        "attendanceScheduleId": node.get("attendanceScheduleId"),
        # Famly's screen sends no term schedule for a plan that is not term-time-only, although the list
        # call returns one (the attendance schedule's id): same here.
        "termScheduleId": node.get("termScheduleId") if term_only else None,
        "totalAdjustments": node.get("totalAdjustments") or [],
        "discounts": node.get("discounts") or [],
    }


def build_preview_body(plan, today=None) -> dict:
    """`{"plan": {...}}` for previewing an existing plan, mapped straight from what the list call
    returned (`plan.raw`). Raises PreviewBuildError, sending nothing, when an id is missing."""
    raw = plan.raw if isinstance(getattr(plan, "raw", None), dict) else {}
    plan_id = _need(raw, "id", "the plan")
    child_id = _need(raw, "childId", "the plan")
    parts = raw.get("planParts")
    if not isinstance(parts, list) or not parts:
        raise PreviewBuildError("the plan has no plan parts")
    start = _as_date(raw.get("from"))
    if start is None:
        raise PreviewBuildError("the plan has no start date")
    day = _today(today)
    settings = raw.get("publicFundingSettings")
    body = {
        "id": plan_id,
        "childId": child_id,
        "pricingGroupId": raw.get("pricingGroupId"),
        "ruleGroupId": raw.get("ruleGroupId"),
        "note": raw.get("note"),
        "from": max(start, day).isoformat(),
        "to": raw.get("to"),
        "planParts": [_part(p) for p in parts if isinstance(p, dict)],
    }
    if isinstance(settings, dict):
        keep = {k: settings[k] for k in ("method", "hours", "minutes", "fundingTypeIds", "maxFundedMinutes")
                if k in settings}
        if settings.get("grantIds"):
            keep["grantIds"] = settings["grantIds"]
        body["publicFundingSettings"] = keep
    return {"plan": body}


def previewed_estimate(plan, today, previewer: PreviewOnlyRest) -> float:
    """Famly's own monthly estimate for `plan` as of today. Raises on a build error or a response
    with no numeric estimate; the caller decides what to do (the audit falls back and says so)."""
    body = build_preview_body(plan, today)
    response = previewer.preview_put(body["plan"]["id"], body)
    value = ((response or {}).get("plan") or {}).get("monthlyEstimate") if isinstance(response, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreviewBuildError("the preview carried no monthly estimate")
    return float(value)


def estimator(previewer: PreviewOnlyRest) -> Callable:
    """The callable the audit takes: (plan, today) -> float."""
    return lambda plan, today=None: previewed_estimate(plan, today, previewer)
