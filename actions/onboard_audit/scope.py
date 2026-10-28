"""Which Operations deals a `--operations` run covers: ONE shared rule for every command that reads the whole
pipeline (onboard-audit, hubspot-set-plan-uploaded, hubspot-set-plan-status). onboard-quality-check keeps its
own scope (Left Nursery within 45 days of its end date) and does not use this.

A deal in the Operations pipeline is IN SCOPE when

  * its `end_date` (Leave Date) is on or after the cutoff, or
  * its `end_date` is empty and its stage is not Left Nursery.

A deal that left before the cutoff, a Left Nursery deal with no end date, and a deal whose `end_date` cannot
be read are excluded; an unreadable date is reported (a count, and the deal ids in the log), never guessed.

The cutoff is ONBOARD_SCOPE_LEAVE_AFTER (YYYY-MM-DD), default 2026-01-01; `--leave-after YYYY-MM-DD` overrides
it for one run and `--all-dates` restores the old behaviour (every deal in the pipeline). Counts only in the
output: how many were included and excluded, and by which reason.
"""

import logging
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date

from actions.onboard_dry_run import derive

logger = logging.getLogger(__name__)

ENV_VAR = "ONBOARD_SCOPE_LEAVE_AFTER"
DEFAULT_CUTOFF = date(2026, 1, 1)
LEFT_NURSERY = "Left Nursery"

INCLUDED_ON_OR_AFTER = "included_leave_on_or_after_cutoff"
INCLUDED_NO_LEAVE_DATE = "included_no_leave_date"
INCLUDED_ALL_DATES = "included_all_dates"
EXCLUDED_BEFORE = "excluded_leave_before_cutoff"
EXCLUDED_LEFT_NO_DATE = "excluded_left_nursery_no_leave_date"
EXCLUDED_UNPARSEABLE = "excluded_unparseable_leave_date"


class ScopeError(ValueError):
    """The cutoff could not be read (a bad --leave-after, a bad variable, or both flags together)."""


@dataclass(frozen=True)
class Scope:
    cutoff: date | None            # None: every date (the old behaviour)


def _parse_cutoff(text: str, source: str) -> date:
    text = text.strip()
    try:
        if len(text) != 10:
            raise ValueError(text)
        return date.fromisoformat(text)
    except ValueError:
        raise ScopeError(f"{source} must be a date like 2026-01-01") from None


def resolve(leave_after: str | None = None, all_dates: bool = False, environ=None) -> Scope:
    """The scope for one run: --all-dates, else --leave-after, else ONBOARD_SCOPE_LEAVE_AFTER, else the default."""
    if all_dates and leave_after:
        raise ScopeError("--all-dates and --leave-after cannot be used together")
    if all_dates:
        return Scope(None)
    if leave_after:
        return Scope(_parse_cutoff(leave_after, "--leave-after"))
    raw = (environ if environ is not None else os.environ).get(ENV_VAR, "").strip()
    return Scope(_parse_cutoff(raw, ENV_VAR) if raw else DEFAULT_CUTOFF)


def classify(deal: dict, stage_label: str | None, scope: Scope) -> str:
    """The reason code for one deal (an `included_*` or `excluded_*` constant)."""
    if scope.cutoff is None:
        return INCLUDED_ALL_DATES
    try:
        end = derive.parse_hubspot_date(deal.get("end_date"))
    except ValueError:
        return EXCLUDED_UNPARSEABLE
    if end is None:
        left = derive.norm_stage(stage_label or "") == derive.norm_stage(LEFT_NURSERY)
        return EXCLUDED_LEFT_NO_DATE if left else INCLUDED_NO_LEAVE_DATE
    return INCLUDED_ON_OR_AFTER if end >= scope.cutoff else EXCLUDED_BEFORE


def apply(deals: list[dict], stages: dict[str, tuple[str, str]], scope: Scope) -> tuple[list[dict], dict]:
    """(the deals in scope, counts). `stages`: stage id -> (pipeline label, stage label), as `stage_names()`.
    Counts: {"cutoff", "included", "excluded", "byReason": {reason: n}}. Ids of unreadable dates go to the log."""
    kept: list[dict] = []
    reasons: Counter = Counter()
    unreadable: list[str] = []
    for deal in deals:
        stage = stages.get(str(deal.get("dealstage")), ("", ""))[1]
        reason = classify(deal, stage, scope)
        reasons[reason] += 1
        if reason.startswith("included"):
            kept.append(deal)
        elif reason == EXCLUDED_UNPARSEABLE:
            unreadable.append(str(deal.get("id")))
    if unreadable:
        logger.warning("operations scope: %d deal(s) excluded, end_date not readable: %s",
                       len(unreadable), ", ".join(unreadable[:50]))
    counts = {"cutoff": scope.cutoff.isoformat() if scope.cutoff else "all_dates",
              "included": len(kept), "excluded": len(deals) - len(kept),
              "byReason": dict(sorted(reasons.items()))}
    return kept, counts


def add_arguments(parser) -> None:
    """The two flags every --operations command takes."""
    parser.add_argument("--leave-after", metavar="YYYY-MM-DD", default=None,
                        help="--operations: only deals whose end_date (Leave Date) is on or after this day, or "
                             "that have no end_date and are not in Left Nursery. Default: "
                             f"{ENV_VAR} or {DEFAULT_CUTOFF.isoformat()}")
    parser.add_argument("--all-dates", action="store_true",
                        help="--operations: no date filter (every deal in the Operations pipeline)")
