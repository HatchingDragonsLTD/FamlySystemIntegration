"""Split a child's placement into contiguous dated segments.

A placement runs from `start` to an optional `leave` date and can carry two
overlays: a FUNDING period and an ADVANCED period. `build_segments` turns that
into the ordered list of stretches over which both flags are constant, so each
stretch can be priced on its own.

PURE: no I/O, no Famly calls, no clock. Dates are ISO `YYYY-MM-DD` strings and
every END date is INCLUSIVE (a period ending 2026-03-31 covers 31 March).

RULES, each pinned by tests:

  * CLAMP. Funding and advanced are clamped to [start, leave]. A period lying
    wholly outside that range is dropped, with a warning.
  * CUT at each period start and at the day AFTER each period end; merge
    neighbouring segments whose flags are identical.
  * The segments are contiguous and never overlap: each segment starts the day
    after the previous one ends, the first `from` is `start`, and the last `to`
    is `leave` (None when there is no leave date).
  * Funding that starts on or before `start` makes the FIRST segment funded.
  * HARD ERRORS (`TimelineError`, a ValueError): a malformed date, leave before
    start, a period whose end is before its start, a period with only one date.
    These are raised rather than warned about because a guess here would be a
    wrong price.

A period given as `(None, None)` is treated as "no period", the same as None.
"""

import re
from datetime import date, timedelta
from typing import Any

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimelineError(ValueError):
    """The inputs cannot describe a timeline."""


def _parse(value: Any, label: str) -> date:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        raise TimelineError(f"{label} must be an ISO date (YYYY-MM-DD), got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise TimelineError(f"{label} is not a real date: {value!r}") from exc


def _parse_period(period: Any, label: str) -> tuple[date, date] | None:
    """`(from, to)` as dates, None for no period; raises on a malformed one."""
    if period is None:
        return None
    try:
        first, last = period
    except (TypeError, ValueError) as exc:
        raise TimelineError(f"{label} must be a (from, to) pair, got {period!r}") from exc

    if first is None and last is None:
        return None
    if first is None or last is None:
        raise TimelineError(f"{label} has only one date: {period!r}")

    start, end = _parse(first, f"{label} from"), _parse(last, f"{label} to")
    if end < start:
        raise TimelineError(f"{label} ends ({last}) before it starts ({first})")
    return start, end


def _clamp(
    period: tuple[date, date] | None,
    label: str,
    start: date,
    leave: date | None,
    warnings: list[str],
) -> tuple[date, date] | None:
    """The part of `period` inside [start, leave], or None if there is none.

    Only the left edge is clamped when there is no leave date.
    """
    if period is None:
        return None
    first, last = period
    if last < start or (leave is not None and first > leave):
        window = f"{start.isoformat()} to {leave.isoformat() if leave else 'open end'}"
        warnings.append(
            f"{label} {first.isoformat()} to {last.isoformat()} is wholly outside "
            f"the placement ({window}); ignored"
        )
        return None
    first = max(first, start)
    if leave is not None:
        last = min(last, leave)
    return first, last


def build_segments(
    start: str,
    leave: str | None = None,
    funding: tuple[str, str] | None = None,
    advanced: tuple[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """The placement as ordered segments, plus warnings.

    Returns `(segments, warnings)`. Each segment is
    `{"from": iso, "to": iso | None, "funded": bool, "advanced": bool}`.
    Raises `TimelineError` on the hard errors listed in the module docstring.
    """
    begin = _parse(start, "start")
    end = None if leave is None else _parse(leave, "leave")
    if end is not None and end < begin:
        raise TimelineError(f"leave ({leave}) is before start ({start})")

    # Validate BOTH periods before clamping either, so a bad advanced period is
    # reported even when funding is dropped.
    parsed_funding = _parse_period(funding, "funding")
    parsed_advanced = _parse_period(advanced, "advanced")

    warnings: list[str] = []
    funded_span = _clamp(parsed_funding, "funding", begin, end, warnings)
    advanced_span = _clamp(parsed_advanced, "advanced", begin, end, warnings)

    # Every date a segment can start on: the placement start, each period's
    # first day, and the day after each period's last day.
    cuts = {begin}
    for span in (funded_span, advanced_span):
        if span is None:
            continue
        cuts.add(span[0])
        if span[1] != date.max:
            cuts.add(span[1] + timedelta(days=1))
    starts = sorted(c for c in cuts if end is None or c <= end)

    def inside(span: tuple[date, date] | None, day: date) -> bool:
        return span is not None and span[0] <= day <= span[1]

    segments: list[dict] = []
    for index, first in enumerate(starts):
        last = starts[index + 1] - timedelta(days=1) if index + 1 < len(starts) else end
        flags = (inside(funded_span, first), inside(advanced_span, first))
        if segments and (segments[-1]["funded"], segments[-1]["advanced"]) == flags:
            segments[-1]["to"] = None if last is None else last.isoformat()
            continue
        segments.append(
            {
                "from": first.isoformat(),
                "to": None if last is None else last.isoformat(),
                "funded": flags[0],
                "advanced": flags[1],
            }
        )
    return segments, warnings
