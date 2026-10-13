"""Clamp a plan's funded hours to the hours Famly can actually fund.

A funded plan asks for hours from the server's funding rules (`funding_rules`,
e.g. 33h for a 30-hour term-time-only entitlement). Famly funds only as many as
the child's scheduled sessions allow, reports that as `publicFunding` on each
pricing period (`planState`), and warns about the shortfall. When the hours
asked for exceed the hours applied, this brings the request DOWN to what is
applied, so the plan is previewed, approved and billed on a figure Famly agrees
with, instead of carrying a warning for every part-week child.

THE RULE, after the preview and before auto-rounding:

  1. Famly's APPLIED funded time is read from each period's `publicFunding`
     (see `applied_minutes`) and the SMALLEST is taken. Requested minutes is
     `round(hours * 60)`.
  2. If requested minus applied is MORE THAN 1 minute (Famly works in whole
     minutes, so 1 or less is rounding) the request is clamped: new hours =
     applied minutes / 60, in `Decimal`. `maxFundedMinutes` is untouched.
  3. Hours are NEVER raised. If applied is 0 or less nothing is clamped and
     Famly's own warning stays, so the queue holds the plan.
  4. The clamped body is re-previewed ONCE. Every period's funded amount and
     monthly estimate must equal the original within 1p, the shortfall warning
     must be gone and no other new warning may appear -- otherwise the clamp is
     DISCARDED and the original body and its warning stand.
  5. On success the plan note records it and the Slack summary says so.

The server's funding rules are never written to: the clamp lives in the body
that is stored and committed, so a manual Adjust & Approve keeps it.

Nothing here commits. The clamped body is stored like any other and committed
by the ordinary approval, behind every guard it already has.
"""

import copy
import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import rounding, runner

logger = logging.getLogger(__name__)

# Whole minutes of difference that are rounding, not a shortfall.
ROUNDING_MINUTES = 1

# How far a re-priced figure may drift from the original and still be "equal".
MONEY_TOLERANCE = Decimal("0.01")

# Warnings are matched on the lowercased title/message. The funding-limit note
# is information, not a problem: it never counts as a shortfall and never makes
# a plan unclean (the queue reads this too).
SHORTFALL_FRAGMENTS = ("funding", "funded")
INFORMATIONAL_TITLE_FRAGMENTS = ("funding limit",)

NOTE_PREFIX = "Funded hours set to scheduled hours:"

_EIGHT_PLACES = Decimal("0.00000001")
_HOURS_SHOWN = Decimal("0.01")


@dataclass
class Outcome:
    """What the clamp did to one plan."""

    body: dict
    preview: Any
    applied: bool = False
    hours: Decimal | None = None  # the hours the plan now asks for
    entitlement_hours: Decimal | None = None  # the hours it asked for before
    reason: str | None = None  # why a clamp was discarded (worth telling a human)


def format_hours(hours: Decimal) -> str:
    """27.5 -> "27.5", 33 -> "33", 24.58333333 -> "24.58"."""
    text = f"{hours.quantize(_HOURS_SHOWN, rounding=ROUND_HALF_UP):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def funded_settings(body: Any) -> dict | None:
    plan = body.get("plan") if isinstance(body, dict) else None
    settings = plan.get("publicFundingSettings") if isinstance(plan, dict) else None
    return settings if isinstance(settings, dict) else None


def requested_minutes(hours: Any) -> int | None:
    """`round(hours * 60)`, half up, in Decimal; None when there is no figure."""
    value = rounding.to_decimal(hours)
    if value is None:
        return None
    return int((value * 60).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def applied_minutes(public_funding: Any) -> Decimal | None:
    """Famly's applied funded time for ONE period, in minutes; None when unreported.

    `publicFunding` reports a duration as `hours`, `minutes` and `seconds`
    components (the plans API shows it as e.g. 15h 30m), so the total is
    `hours * 60 + minutes + seconds / 60` -- reading `minutes` alone would take
    27h 30m for half an hour. Where only hours are given it is `hours * 60`.
    A `minutes` of 60 or more cannot be a minutes component, so it is read as
    the TOTAL, which also covers an API that reports total minutes there.
    """
    if public_funding is None:
        return None
    hours = rounding.to_decimal(getattr(public_funding, "hours", None))
    minutes = rounding.to_decimal(getattr(public_funding, "minutes", None))
    seconds = rounding.to_decimal(getattr(public_funding, "seconds", None))
    if hours is None and minutes is None and seconds is None:
        return None
    if minutes is not None and minutes >= 60:
        return minutes
    zero = Decimal(0)
    return (hours or zero) * 60 + (minutes or zero) + (seconds or zero) / 60


def smallest_applied(plan: Any) -> Decimal | None:
    """The smallest applied funded time across the plan's periods, in minutes."""
    values = [
        applied_minutes(getattr(state, "public_funding", None))
        for state in (getattr(plan, "plan_states", None) or [])
    ]
    values = [v for v in values if v is not None]
    return min(values) if values else None


def _is_local(warning: Any) -> bool:
    raw = getattr(getattr(warning, "warning", warning), "raw", None)
    return isinstance(raw, dict) and raw.get("source") == "local"


def _text(warning: Any) -> str:
    inner = getattr(warning, "warning", warning)
    return f"{getattr(inner, 'title', '') or ''} {getattr(inner, 'message', '') or ''}".lower()


def is_informational(warning: Any) -> bool:
    title = (getattr(getattr(warning, "warning", warning), "title", None) or "").lower()
    return any(fragment in title for fragment in INFORMATIONAL_TITLE_FRAGMENTS)


def is_shortfall(warning: Any) -> bool:
    """A FAMLY warning about funding that is not the informational limit note."""
    return (
        not _is_local(warning)
        and not is_informational(warning)
        and any(fragment in _text(warning) for fragment in SHORTFALL_FRAGMENTS)
    )


def _key(warning: Any) -> tuple:
    inner = getattr(warning, "warning", warning)
    return (getattr(inner, "title", None), getattr(inner, "message", None))


def _periods(plan: Any) -> list[tuple[Any, Any, Any]]:
    return [
        (
            s.from_,
            s.monthly_estimate,
            getattr(getattr(s, "public_funding", None), "amount", None),
        )
        for s in (getattr(plan, "plan_states", None) or [])
    ]


def _close(was: Any, now: Any) -> bool:
    was, now = rounding.to_decimal(was), rounding.to_decimal(now)
    if was is None and now is None:
        return True
    return was is not None and now is not None and abs(now - was) <= MONEY_TOLERANCE


def _verification_problem(original: Any, repriced: Any) -> str | None:
    """Why the re-priced plan is not the same plan minus the shortfall, or None."""
    before, after = _periods(original.plan), _periods(repriced.plan)
    if len(before) != len(after):
        return f"it re-priced as {len(after)} period(s), not {len(before)}"

    for (start, estimate, amount), (_, new_estimate, new_amount) in zip(before, after):
        when = str(start)[:10]
        if not _close(amount, new_amount):
            return f"the funded amount for the period from {when} moved ({amount} -> {new_amount})"
        if not _close(estimate, new_estimate):
            return f"the monthly estimate for the period from {when} moved ({estimate} -> {new_estimate})"

    if any(is_shortfall(w) for w in repriced.warnings):
        return "the funding shortfall warning is still there"

    known = {_key(w) for w in original.warnings}
    new = [w for w in repriced.warnings if _key(w) not in known]
    if new:
        return f"it raised {len(new)} new warning(s)"
    return None


def _with_hours_and_note(body: dict, hours: Decimal, line: str) -> dict:
    clamped = copy.deepcopy(body)
    clamped["plan"]["publicFundingSettings"]["hours"] = float(hours)
    existing = clamped["plan"].get("note")
    existing = existing.strip() if isinstance(existing, str) else ""
    clamped["plan"]["note"] = f"{existing}\n{line}" if existing else line
    return clamped


def apply(
    plan_body: dict,
    original: Any,
    version: int,
    *,
    old_plan_id: str | None = None,
    replace_old_plan: bool = False,
    today: date | None = None,
) -> Outcome:
    """Clamp the plan's funded hours to what Famly applies, or leave it as it was.

    Args:
        plan_body: the previewed body (returned untouched when not clamped).
        original: the `ParsedPlanResult` of that preview.
        version / old_plan_id / replace_old_plan: passed to the verification
            preview so it is the same request apart from the hours.
    """
    unchanged = Outcome(body=plan_body, preview=original)

    settings = funded_settings(plan_body)
    if settings is None:
        return unchanged

    asked = requested_minutes(settings.get("hours"))
    applied = smallest_applied(getattr(original, "plan", None))
    if asked is None or applied is None:
        return unchanged
    if applied <= 0:
        logger.info("FUNDED HOURS NOT CLAMPED: Famly applies %s minutes; its warning stands", applied)
        return unchanged
    if asked - applied <= ROUNDING_MINUTES:
        return unchanged  # rounding, or nothing short

    new_hours = (applied / 60).quantize(_EIGHT_PLACES, rounding=ROUND_HALF_UP)
    entitlement = rounding.to_decimal(settings.get("hours"))
    if new_hours * 60 >= asked:  # never raise, never leave the hours as they were
        return unchanged

    line = (
        f"{NOTE_PREFIX} {format_hours(new_hours)}h (entitlement "
        f"{format_hours(entitlement)}h), {(today or date.today()).isoformat()}"
    )
    clamped = _with_hours_and_note(plan_body, new_hours, line)

    try:
        repriced = runner.preview(
            clamped, version, old_plan_id=old_plan_id, replace_old_plan=replace_old_plan
        )
        problem = _verification_problem(original, repriced)
    except Exception as exc:  # noqa: BLE001 - a failed check means no clamp
        problem = f"the verification preview failed ({type(exc).__name__})"

    if problem:
        logger.warning("FUNDED HOURS CLAMP DISCARDED: %s", problem)
        return Outcome(body=plan_body, preview=original, reason=problem)

    # The re-priced plan IS the plan now: its Famly warnings replace the
    # original's, keeping only the ones this codebase raised itself.
    repriced.warnings = [w for w in original.warnings if _is_local(w)] + list(repriced.warnings)
    return Outcome(
        body=clamped,
        preview=repriced,
        applied=True,
        hours=new_hours,
        entitlement_hours=entitlement,
    )
