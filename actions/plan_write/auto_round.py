"""Apply automatic rounding to a previewed plan, and verify it with Famly.

`rounding.py` is the pure rule. This is the part that touches Famly and Slack:
after a plan has been previewed, work out the adjustment for each of its pricing
groups, put them in the body that will be stored and committed, and PROVE they
do what they should by previewing the adjusted body once more.

The verification is the safety net. The adjusted body is kept only if the second
preview (a) prices every period at the SAME monthly estimate as before, within
1p, and (b) raises no warning the first did not. Otherwise the rounding is
discarded -- the plan goes ahead unrounded -- and a Slack notice says why.

Nothing here commits. The adjusted body is stored like any other and committed
by the ordinary approval, behind every guard it already has.

Switch: `auto_round` in the payload (true/false) wins; when it is absent,
AUTO_ROUND_DEFAULT=true turns it on. Off otherwise.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from integrations import slack

from . import hubspot_flatten, rounding, runner

logger = logging.getLogger(__name__)

# How far a re-priced period's estimate may drift from the original and still
# count as unchanged.
ESTIMATE_TOLERANCE = Decimal("0.01")


@dataclass
class Outcome:
    """What auto-rounding did to one plan."""

    body: dict
    applied: bool = False
    periods: list[rounding.PeriodRounding] = field(default_factory=list)
    # Why it was not applied, when that is worth telling the approver.
    reason: str | None = None


def enabled(payload: Any) -> bool:
    """Whether to auto-round: the payload's `auto_round` wins over the env default."""
    flat = payload if isinstance(payload, dict) else {}
    raw = flat.get("auto_round")
    if raw is not None and str(raw).strip() != "":
        return hubspot_flatten._as_bool(raw)
    return os.environ.get("AUTO_ROUND_DEFAULT", "").strip().lower() in ("1", "true", "yes", "on")


def _periods(plan: Any) -> list[tuple[Any, Any, Any]]:
    return [
        (s.from_, s.pricing_group_id, s.monthly_estimate)
        for s in (getattr(plan, "plan_states", None) or [])
    ]


def _warning_keys(warnings: list) -> set[tuple]:
    keys = set()
    for item in warnings or []:
        inner = getattr(item, "warning", item)
        keys.add((getattr(inner, "title", None), getattr(inner, "message", None)))
    return keys


def _verification_problem(original_result: Any, repriced: Any) -> str | None:
    """Why the re-priced plan is not the same plan, or None when it is."""
    before = _periods(original_result.plan)
    after = _periods(repriced.plan)
    if len(before) != len(after):
        return f"it re-priced as {len(after)} period(s), not {len(before)}"

    for (start, _, was), (_, _, now) in zip(before, after):
        was, now = rounding.to_decimal(was), rounding.to_decimal(now)
        if was is None or now is None or abs(now - was) > ESTIMATE_TOLERANCE:
            return f"the estimate for the period from {str(start)[:10]} moved ({was} -> {now})"

    new = _warning_keys(repriced.warnings) - _warning_keys(original_result.warnings)
    if new:
        return f"it raised {len(new)} new warning(s)"
    return None


def apply(
    plan_body: dict,
    original_result: Any,
    version: int,
    *,
    old_plan_id: str | None = None,
    replace_old_plan: bool = False,
    today: date | None = None,
) -> Outcome:
    """Round the plan, or leave it exactly as it was.

    Args:
        plan_body: the body that was previewed (returned untouched when not
            rounded).
        original_result: the `ParsedPlanResult` of that preview.
        version / old_plan_id / replace_old_plan: passed to the verification
            preview so it is the same request apart from the adjustments.
    """
    plan = getattr(original_result, "plan", None)
    plan_rounding = rounding.plan_rounding(_periods(plan))

    if plan_rounding.reason:
        logger.info("AUTO-ROUND SKIPPED: %s", plan_rounding.reason)
        return Outcome(body=plan_body, reason=plan_rounding.reason)
    if not plan_rounding.entries:
        return Outcome(body=plan_body)

    adjusted = rounding.with_entries(plan_body, plan_rounding.entries, today)

    try:
        repriced = runner.preview(
            adjusted, version, old_plan_id=old_plan_id, replace_old_plan=replace_old_plan
        )
        problem = _verification_problem(original_result, repriced)
    except Exception as exc:  # noqa: BLE001 - a failed check means no rounding
        problem = f"the verification preview failed ({type(exc).__name__})"

    if problem:
        logger.warning("AUTO-ROUND DISCARDED: %s", problem)
        slack.post_notice(
            f":warning: Auto-rounding was discarded — {problem}. The plan is being "
            f"offered for approval unrounded."
        )
        return Outcome(body=plan_body, reason=problem)

    return Outcome(body=adjusted, applied=True, periods=plan_rounding.periods)
