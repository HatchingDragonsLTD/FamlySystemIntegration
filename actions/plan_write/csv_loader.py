"""Loads the normalized plan input from a CSV file.

One row = one booking line. Rows sharing a childId are grouped into a single
PlanInput with a single plan part; plan-level and part-level values are taken
from the first row for that child.

A row carries EITHER a sessionId or a productId, never both -- that is what
decides whether it becomes a session booking or a product booking.

UUIDs only: every id column must already hold a UUID. Label-to-UUID resolution
happens upstream, in whatever produced the CSV.

Stdlib `csv` only. No argparse.

Sample (see samples/plans.sample.csv for the committed copy):

    childId,from,ruleGroupId,note,billingProfileId,attendanceScheduleId,termScheduleId,termTimeOnly,billingId,billingTitle,weeksOfCare,invoices,day,sessionId,productId,fundable,amount,fundingMethod,fundingHours,fundingMinutes,fundingTypeIds,maxFundedMinutes
    3f1a...,2026-09-01,,Autumn plan,7c2b...,9d4e...,,false,1a2b...,Monthly,51,ADVANCE,MONDAY,5e6f...,,true,,STRETCHED,15,0,8a9b...,
    3f1a...,2026-09-01,,,7c2b...,9d4e...,,false,1a2b...,Monthly,51,ADVANCE,TUESDAY,5e6f...,,true,,,,,,
    3f1a...,2026-09-01,,,7c2b...,9d4e...,,false,1a2b...,Monthly,51,ADVANCE,WEDNESDAY,,4c5d...,,2,,,,,

The first row for a child supplies the plan-level fields; later rows only need
their booking columns filled in. Blank cells become None (or are skipped), so
repeating the plan-level values on every row is optional.
"""

import csv
from pathlib import Path
from typing import Any

from .input_schema import (
    BillingInput,
    PlanInput,
    PlanPartInput,
    ProductBookingInput,
    SessionBookingInput,
    PublicFundingSettingsInput,
)

# Values accepted for boolean columns, lower-cased.
TRUE_VALUES = ("true", "yes", "y", "1")
FALSE_VALUES = ("false", "no", "n", "0")

# Separator for the multi-value fundingTypeIds column.
LIST_SEPARATOR = ";"


def _cell(row: dict, *names: str) -> str | None:
    """Read the first populated column of `names`, stripped; None when blank."""
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
    return None


def _as_bool(value: str | None) -> Any:
    """Parse a boolean cell.

    Returns None when blank and the raw string when unrecognised, so that
    `validate` reports a bad value instead of this silently choosing one.
    """
    if value is None:
        return None
    lowered = value.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    return value


def _as_int(value: str | None) -> Any:
    """Parse an integer cell, passing the raw string through when it is not one."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _as_list(value: str | None) -> list:
    if value is None:
        return []
    return [item.strip() for item in value.split(LIST_SEPARATOR) if item.strip()]


def _funding_from_row(row: dict) -> PublicFundingSettingsInput | None:
    """Build funding settings when a row carries any funding column."""
    method = _cell(row, "fundingMethod", "funding_method")
    hours = _cell(row, "fundingHours", "funding_hours")
    minutes = _cell(row, "fundingMinutes", "funding_minutes")
    type_ids = _cell(row, "fundingTypeIds", "funding_type_ids")
    max_funded = _cell(row, "maxFundedMinutes", "max_funded_minutes")

    if not any((method, hours, minutes, type_ids, max_funded)):
        return None

    return PublicFundingSettingsInput(
        method=method,
        hours=_as_int(hours),
        minutes=_as_int(minutes),
        funding_type_ids=_as_list(type_ids),
        max_funded_minutes=_as_int(max_funded),
    )


def _plan_from_row(child_id: str, row: dict) -> PlanInput:
    """Start a plan (and its single part) from the first row for a child."""
    part = PlanPartInput(
        billing_profile_id=_cell(row, "billingProfileId", "billing_profile_id"),
        attendance_schedule_id=_cell(
            row, "attendanceScheduleId", "attendance_schedule_id"
        ),
        billing=BillingInput(
            id=_cell(row, "billingId", "billing_id"),
            title=_cell(row, "billingTitle", "billing_title"),
            weeks_of_care=_as_int(_cell(row, "weeksOfCare", "weeks_of_care")),
            invoices=_cell(row, "invoices"),
        ),
        term_schedule_id=_cell(row, "termScheduleId", "term_schedule_id"),
        # Default to False when the column is blank, matching the contract.
        term_time_only=_as_bool(_cell(row, "termTimeOnly", "term_time_only")) or False,
    )

    return PlanInput(
        child_id=child_id,
        from_date=_cell(row, "from", "from_date"),
        rule_group_id=_cell(row, "ruleGroupId", "rule_group_id"),
        note=_cell(row, "note") or "",
        plan_parts=[part],
        public_funding_settings=_funding_from_row(row),
    )


def _add_booking(plan: PlanInput, row: dict) -> None:
    """Append this row's booking to the plan's single part.

    A row with neither a sessionId nor a productId contributes no booking; the
    resulting empty part is reported by `validate`, not silently accepted.
    """
    if not plan.plan_parts:
        return
    part = plan.plan_parts[0]

    day = _cell(row, "day")
    session_id = _cell(row, "sessionId", "session_id")
    product_id = _cell(row, "productId", "product_id")

    if session_id:
        part.session_bookings.append(
            SessionBookingInput(
                session_id=session_id,
                day=day,
                fundable=_as_bool(_cell(row, "fundable")),
            )
        )
    elif product_id:
        part.product_bookings.append(
            ProductBookingInput(
                product_id=product_id,
                day=day,
                amount=_as_int(_cell(row, "amount")),
            )
        )


def load_rows(rows: list[dict]) -> list[PlanInput]:
    """Group already-read CSV rows into PlanInput objects, one per child.

    Kept separate from file reading so callers (and tests) can supply rows from
    anywhere. Never raises on odd data: unparseable values pass through for
    `validate` to report.
    """
    plans: dict[str, PlanInput] = {}
    order: list[str] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        child_id = _cell(row, "childId", "child_id")
        if not child_id:
            # No child to attach this row to; validate() can say nothing useful
            # about a row that belongs to no plan, so skip it.
            continue

        if child_id not in plans:
            plans[child_id] = _plan_from_row(child_id, row)
            order.append(child_id)
        elif plans[child_id].public_funding_settings is None:
            # Funding may appear on a later row rather than the first.
            plans[child_id].public_funding_settings = _funding_from_row(row)

        _add_booking(plans[child_id], row)

    return [plans[child_id] for child_id in order]


def load_csv(path: str | Path) -> list[PlanInput]:
    """Read a CSV file and return one PlanInput per child.

    Args:
        path: the CSV file. Must have a header row.

    Returns:
        PlanInput objects in the order children first appear in the file.
    """
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return load_rows(list(csv.DictReader(handle)))
