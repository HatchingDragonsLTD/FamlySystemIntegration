"""Automatic rounding of a plan's monthly totals. PURE: no I/O, no Famly.

A family is billed a plan's monthly estimate plus its `totalAdjustments`. This
module decides the small adjustment that moves an estimate to a tidy figure --
the same mechanism as a manual Adjust & Approve, never more than 1.00 in size.

THE RULE (`round_target`), all in `Decimal`, floats converted through `str` so
269.03 is 269.03 and not 269.0299999...:

  * for each step in SNAP_STEPS, find the nearest multiple of that step (a tie
    rounds up);
  * a multiple whose distance from the estimate is at most SNAP_WINDOW is a
    CANDIDATE;
  * take the closest candidate; on a tie, the one from the LARGER step;
  * no candidate: round to the nearest whole pound, halves up;
  * an estimate of 0 or less (or one that is not a number) is not rounded, and
    neither is one that would round DOWN to 0 (a positive estimate is never
    billed as nothing).

`adjustment = target - estimate`, to 2 decimals. Its size is at most
SNAP_WINDOW (snapping) or 0.50 (a whole pound), so it can never exceed the 1.00
the manual adjustment allows.

THE KNOBS live in one place, here: SNAP_STEPS and SNAP_WINDOW.

A plan has one `planState` per pricing period, each priced under its own
pricing group, and a `totalAdjustments` entry names ONE group. So the rounding
of a plan is one entry per DISTINCT group (`plan_rounding`). If two periods
share a group but have different estimates, one entry cannot be right for both,
so that plan is not rounded and the reason is returned.
"""

import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# The multiples an estimate snaps to, and how close it must be to snap. Change
# them here and nowhere else.
SNAP_STEPS = (10, 7.5, 5)
SNAP_WINDOW = 1.00

AUTO_NOTE_PREFIX = "Auto-rounded:"

_PENNY = Decimal("0.01")
_ONE = Decimal(1)
_NOTE_DATE = re.compile(r",\s*(\d{4}-\d{2}-\d{2})\s*$")


def to_decimal(value: Any) -> Decimal | None:
    """A number as a Decimal via its string, or None when it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        number = Decimal(str(value).strip())
    except Exception:  # noqa: BLE001 - anything unparseable is "not a number"
        return None
    return number if number.is_finite() else None


def _nearest_multiple(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(_ONE, rounding=ROUND_HALF_UP) * step


def round_target(estimate: Any) -> Decimal | None:
    """The figure `estimate` is rounded to, or None when it is not rounded."""
    value = to_decimal(estimate)
    if value is None or value <= 0:
        return None

    window = to_decimal(SNAP_WINDOW)
    best: tuple[Decimal, Decimal, Decimal] | None = None  # (distance, -step, target)
    for step in (to_decimal(s) for s in SNAP_STEPS):
        target = _nearest_multiple(value, step)
        distance = abs(target - value)
        if distance > window or target <= 0:
            continue  # too far, or would round a positive estimate down to nothing
        # Closest first; on a tie the LARGER step (hence the negated step).
        candidate = (distance, -step, target)
        if best is None or candidate[:2] < best[:2]:
            best = candidate

    if best is not None:
        return best[2].quantize(_PENNY)
    whole = value.quantize(_ONE, rounding=ROUND_HALF_UP)
    return whole.quantize(_PENNY) if whole > 0 else None


def adjustment_for(estimate: Any) -> Decimal | None:
    """`target - estimate` to 2 decimals, or None when the estimate is not rounded."""
    value = to_decimal(estimate)
    target = round_target(estimate)
    if value is None or target is None:
        return None
    return (target - value).quantize(_PENNY, rounding=ROUND_HALF_UP)


@dataclass
class Entry:
    """One `totalAdjustments` entry: a pricing group and its adjustment."""

    group: str
    estimate: Decimal
    target: Decimal
    adjustment: Decimal


@dataclass
class PeriodRounding:
    """One pricing period's rounding, for display."""

    from_: str | None
    estimate: Decimal
    target: Decimal


@dataclass
class RoundingPlan:
    entries: list[Entry] = field(default_factory=list)
    periods: list[PeriodRounding] = field(default_factory=list)
    # Why the plan is NOT being rounded (None when it is, or there is nothing
    # to do).
    reason: str | None = None


def plan_rounding(periods: list[tuple[Any, Any, Any]]) -> RoundingPlan:
    """The rounding for a plan, from its periods as (from, pricingGroupId, estimate).

    One entry per distinct pricing group, taken from that group's estimate; a
    zero adjustment is skipped. A period with no group or no usable estimate,
    or a group whose periods disagree on the estimate, refuses the WHOLE plan
    (`reason` set, nothing rounded) -- a partial rounding would leave the
    family billed some periods tidied and some not.
    """
    if not periods:
        return RoundingPlan(reason="the plan has no pricing periods")

    by_group: dict[str, Decimal] = {}
    parsed: list[tuple[Any, str, Decimal]] = []
    for start, group, estimate in periods:
        value = to_decimal(estimate)
        if not isinstance(group, str) or not group.strip() or value is None:
            return RoundingPlan(
                reason="a pricing period has no pricingGroupId or monthlyEstimate"
            )
        value = value.quantize(_PENNY, rounding=ROUND_HALF_UP)
        group = group.strip()
        if group in by_group and by_group[group] != value:
            return RoundingPlan(
                reason=(
                    f"pricing group {group} is shared by periods with different "
                    f"estimates ({by_group[group]} and {value})"
                )
            )
        by_group[group] = value
        parsed.append((start, group, value))

    entries: list[Entry] = []
    adjustments: dict[str, Entry] = {}
    for group, estimate in by_group.items():
        adjustment = adjustment_for(estimate)
        if adjustment is None or adjustment == 0:
            continue
        entry = Entry(group, estimate, round_target(estimate), adjustment)
        entries.append(entry)
        adjustments[group] = entry

    rounded = [
        PeriodRounding(start, adjustments[group].estimate, adjustments[group].target)
        for start, group, _ in parsed
        if group in adjustments
    ]
    return RoundingPlan(entries=entries, periods=rounded)


# --------------------------------------------------------------------------- #
# The body and the note
# --------------------------------------------------------------------------- #
def _signed(value: Any) -> str:
    return f"{to_decimal(value).quantize(_PENNY, rounding=ROUND_HALF_UP):+.2f}"


def note_line(adjustments: list[Any], day: date | str | None = None) -> str:
    """`Auto-rounded: +0.09 / -0.45, 2026-10-13`."""
    when = day.isoformat() if isinstance(day, date) else (day or date.today().isoformat())
    return f"{AUTO_NOTE_PREFIX} {' / '.join(_signed(a) for a in adjustments)}, {when}"


def _first_part(body: dict) -> dict | None:
    parts = (body.get("plan") or {}).get("planParts")
    return parts[0] if isinstance(parts, list) and parts and isinstance(parts[0], dict) else None


def _set_note_line(plan: dict, line: str | None) -> None:
    """Replace any Auto-rounded line in the plan's note with `line` (or drop it)."""
    existing = plan.get("note")
    kept = [
        text
        for text in (existing.splitlines() if isinstance(existing, str) else [])
        if text.strip() and not text.strip().startswith(AUTO_NOTE_PREFIX)
    ]
    if line:
        kept.append(line)
    plan["note"] = "\n".join(kept)


def with_entries(body: dict, entries: list[Entry], day: date | str | None = None) -> dict:
    """A copy of `body` carrying the rounding: the adjustments and the note.

    An entry replaces any existing one for the same group, so a group never has
    two. The note gets (or has rewritten) one `Auto-rounded:` line.
    """
    adjusted = deepcopy(body)
    part = _first_part(adjusted)
    if part is None or not entries:
        return adjusted

    groups = {e.group for e in entries}
    existing = part.get("totalAdjustments")
    kept = [
        e
        for e in (existing if isinstance(existing, list) else [])
        if not (isinstance(e, dict) and e.get("pricingGroupId") in groups)
    ]
    part["totalAdjustments"] = kept + [
        {"pricingGroupId": e.group, "adjustment": float(e.adjustment)} for e in entries
    ]
    _set_note_line(adjusted["plan"], note_line([e.adjustment for e in entries], day))
    return adjusted


def without_group(body: dict, group: str) -> dict:
    """A copy of `body` with every adjustment for `group` removed.

    Used when a MANUAL adjustment takes over a group: its auto entry goes (never
    two for one group) and the `Auto-rounded:` note line is rewritten to list
    only the entries that remain -- or removed when none do. The line keeps its
    original date.
    """
    adjusted = deepcopy(body)
    part = _first_part(adjusted)
    if part is None:
        return adjusted

    existing = part.get("totalAdjustments")
    if not isinstance(existing, list):
        return adjusted

    remaining = [
        e for e in existing if not (isinstance(e, dict) and e.get("pricingGroupId") == group)
    ]
    if len(remaining) == len(existing):
        return adjusted
    part["totalAdjustments"] = remaining

    plan = adjusted["plan"]
    note = plan.get("note") if isinstance(plan.get("note"), str) else ""
    had_auto = any(t.strip().startswith(AUTO_NOTE_PREFIX) for t in note.splitlines())
    if had_auto:
        dated = next(
            (m.group(1) for t in note.splitlines() if (m := _NOTE_DATE.search(t))), None
        )
        _set_note_line(
            plan,
            note_line([e["adjustment"] for e in remaining if isinstance(e, dict)], dated)
            if remaining
            else None,
        )
    return adjusted


def check_saved(plan: Any) -> list[str]:
    """Problems with an auto-rounded plan as Famly saved it. Pure.

    For a plan whose note carries an `Auto-rounded:` line, each saved period's
    estimate plus its group's adjustment must equal the target the rule gives
    for that estimate, within 1p. A group whose saved adjustment is not one of
    those listed in the note was replaced by a manual adjustment on purpose and
    is left alone. Returns human-readable problems; empty means fine.
    """
    note = getattr(plan, "note", None)
    line = next(
        (
            t.strip()
            for t in (note.splitlines() if isinstance(note, str) else [])
            if t.strip().startswith(AUTO_NOTE_PREFIX)
        ),
        None,
    )
    if line is None:
        return []

    listed = {
        part.strip()
        for part in _NOTE_DATE.sub("", line[len(AUTO_NOTE_PREFIX):]).split("/")
        if part.strip()
    }

    saved: dict[str, Decimal] = {}
    for part in (getattr(plan, "plan_parts", None) or [])[:1]:
        for entry in getattr(part, "total_adjustments", None) or []:
            if isinstance(entry, dict) and to_decimal(entry.get("adjustment")) is not None:
                saved[entry.get("pricingGroupId")] = to_decimal(entry["adjustment"])

    problems = []
    for state in getattr(plan, "plan_states", None) or []:
        estimate = to_decimal(getattr(state, "monthly_estimate", None))
        group = getattr(state, "pricing_group_id", None)
        target = round_target(estimate)
        if estimate is None or target is None:
            continue
        adjustment = saved.get(group, Decimal(0))
        if group in saved and _signed(adjustment) not in listed:
            continue  # taken over by a manual adjustment
        if abs(estimate + adjustment - target) > _PENNY:
            problems.append(
                f"period from {str(getattr(state, 'from_', None) or 'unknown')[:10]}: "
                f"estimate {estimate} + adjustment {adjustment} = "
                f"{estimate + adjustment}, expected {target}"
            )
    return problems
