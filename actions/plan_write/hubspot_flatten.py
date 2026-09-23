"""Adapter: the flat field set a HubSpot native webhook sends -> the nested
plan shape `input_schema.from_dict` expects.

The HubSpot custom code action emits flat scalar fields (HubSpot output fields
cannot hold arrays), and the native webhook forwards them as a flat JSON body:

    childId, from, to, ruleGroupId, note,
    attendanceScheduleId, weeksOfCare, billingId, billingTitle, billingInvoices,
    termTimeOnly,
    monday_session ... friday_session,   (sessionId per day, "" = not booked)
    funded, fundingMethod, fundingHours, maxFundedMinutes,
    discount_1_name/discount_1_amount ... discount_3_name/discount_3_amount

This module reshapes that into the single-plan-part nested structure. It does
NOT validate -- it only restructures; `input_schema.validate` still runs after.
The one exception is the discount slots: pairing a name with an amount is only
possible here, before the slots are flattened away, so problems found there are
carried out under PROBLEMS_KEY. They are ADVISORY -- hubspot_intake turns them
into warnings, so a malformed slot is excluded and flagged while the rest of
the plan previews normally.
billingProfileId is intentionally absent (the server resolves it separately).
"""

from datetime import datetime, timezone
from typing import Any

# HubSpot field prefix -> Famly day enum. Order fixed for stable output.
_DAYS = [
    ("monday_session", "MONDAY"),
    ("tuesday_session", "TUESDAY"),
    ("wednesday_session", "WEDNESDAY"),
    ("thursday_session", "THURSDAY"),
    ("friday_session", "FRIDAY"),
]

# Values HubSpot may send for a boolean field.
_TRUE_VALUES = {"true", "yes", "1"}

# Up to three custom discount slots: discount_1_name / discount_1_amount, etc.
_DISCOUNT_SLOTS = (1, 2, 3)

# Fixed fields on every custom discount we build. Only title, amount and
# ordering vary per slot.
_DISCOUNT_DEFAULTS = {
    "fePriceModifierType": "discount",
    "isPercent": True,
    "origin": "custom",
    "period": "WEEKLY",
    "showOnInvoice": True,
}

# Amounts arrive as a FRACTION (5% is 0.05), so anything above 1.0 is a
# fat-fingered percentage rather than a fraction. Rejecting it here stops a
# typed "5" becoming a 500% discount on a real invoice.
_MIN_DISCOUNT_AMOUNT = 0.0
_MAX_DISCOUNT_AMOUNT = 1.0

# Key the problems are carried under, for input_schema to pick up. Stripped
# before the plan body is built -- it never reaches Famly.
PROBLEMS_KEY = "_problems"


def _s(value: Any) -> str:
    """Normalize a field to a trimmed string ('' for None)."""
    if value is None:
        return ""
    return str(value).strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _s(value).lower() in _TRUE_VALUES


def _to_iso_date(value: Any) -> str | None:
    """Convert a HubSpot date field to an ISO date string (YYYY-MM-DD).

    HubSpot date/datetime properties arrive as epoch milliseconds (a number or
    numeric string). Returns None for empty/missing (e.g. an open-ended plan's
    `to`). An already-ISO string passes through (trimmed to the date). An
    unparseable value passes through unchanged so validate() can flag it.

    Assumes date-only properties (midnight UTC). If these are DateTime
    properties in a non-UTC zone, a value near midnight could land one day off;
    convert in the property's timezone instead if that ever happens.
    """
    s = _s(value)
    if s == "":
        return None
    # Already an ISO-style string? Keep the date portion.
    if "-" in s and not s.lstrip("-").isdigit():
        return s[:10]
    # Otherwise treat as epoch milliseconds.
    try:
        ms = int(float(s))
    except (ValueError, TypeError):
        return s  # unrecognised; let validate() report it rather than crash
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def build_discounts(data: Any) -> tuple[list, list[str]]:
    """Build the plan part's custom discounts from the three flat slots.

    A slot is `discount_N_name` plus `discount_N_amount`. An empty slot is
    skipped entirely; a half-filled or out-of-range one is reported as a
    problem and excluded, never guessed at.

    `ordering` is tied to the SLOT NUMBER (slot 1 -> 0, slot 3 -> 2) and is not
    compacted when a middle slot is empty, so a discount keeps its position
    regardless of what else is filled in.

    Args:
        data: the flat HubSpot payload.

    Returns:
        (discounts, problems). `problems` are human-readable strings for the
        approver; a slot that produced one contributes no discount. They are
        ADVISORY -- the preview still runs and succeeds without that discount
        (see hubspot_intake, which turns them into warnings).
    """
    if not isinstance(data, dict):
        return [], []

    discounts: list = []
    problems: list[str] = []

    for slot in _DISCOUNT_SLOTS:
        name = _s(data.get(f"discount_{slot}_name"))
        raw_amount = _s(data.get(f"discount_{slot}_amount"))

        # Both empty: the slot simply is not in use.
        if name == "" and raw_amount == "":
            continue

        if name != "" and raw_amount == "":
            problems.append(f"Discount {slot} excluded: name given but amount missing")
            continue

        if name == "" and raw_amount != "":
            problems.append(f"Discount {slot} excluded: amount given but name missing")
            continue

        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            problems.append(
                f"Discount {slot} excluded: amount {raw_amount!r} is not a number"
            )
            continue

        # The amount is a fraction already (5% = 0.05), so it is never > 1.0.
        if amount <= _MIN_DISCOUNT_AMOUNT or amount > _MAX_DISCOUNT_AMOUNT:
            problems.append(
                f"Discount {slot} excluded: amount {amount} is out of range 0-1 "
                f"(fraction, so 5% = 0.05)"
            )
            continue

        discounts.append(
            {
                "title": name,
                "amount": amount,
                "ordering": slot - 1,
                **_DISCOUNT_DEFAULTS,
            }
        )

    return discounts, problems


def is_flat_payload(data: Any) -> bool:
    """True when the payload looks like the flat HubSpot shape.

    Used only as a guard by callers that want to be defensive; the server is
    configured to always send flat, so the caller may skip this.
    """
    if not isinstance(data, dict):
        return False
    if "planParts" in data or "plan_parts" in data:
        return False
    return any(day_key in data for day_key, _ in _DAYS)


def flatten_to_nested(data: Any) -> dict:
    """Reshape the flat HubSpot payload into the nested plan dict.

    Skips any day whose *_session value is empty (not booked). Attaches
    publicFundingSettings only when `funded` is truthy. Passes identity fields
    through unchanged. Never raises; missing fields simply come through empty so
    validate() can report them.
    """
    if not isinstance(data, dict):
        return {}

    # Session bookings: one per booked day. fundable is the deal-wide `funded`
    # flag applied to every booking (a child is entirely funded or entirely
    # non-funded; the funded/non-funded session UUIDs are chosen upstream).
    funded = _as_bool(data.get("funded"))
    session_bookings = []
    for day_key, day_enum in _DAYS:
        session_id = _s(data.get(day_key))
        if session_id == "":
            continue  # not booked that day
        session_bookings.append(
            {"sessionId": session_id, "day": day_enum, "fundable": funded}
        )

    # Up to three custom discounts. A half-filled or out-of-range slot yields a
    # problem instead of a discount, and the problems ride along so validate()
    # reports them at PREVIEW time rather than after a commit.
    discounts, discount_problems = build_discounts(data)

    plan_part = {
        "billingProfileId": _s(data.get("billingProfileId")) or None,
        "attendanceScheduleId": _s(data.get("attendanceScheduleId")) or None,
        "billing": {
            "id": _s(data.get("billingId")) or None,
            "title": _s(data.get("billingTitle")) or None,
            "weeksOfCare": data.get("weeksOfCare"),
            "invoices": data.get("billingInvoices"),
        },
        "sessionBookings": session_bookings,
        "productBookings": [],
        "discounts": discounts,
        "termScheduleId": None,
        "termTimeOnly": _as_bool(data.get("termTimeOnly")),
    }

    nested = {
        "childId": _s(data.get("childId")) or None,
        "from": _to_iso_date(data.get("from")),
        "to": _to_iso_date(data.get("to")),
        "ruleGroupId": _s(data.get("ruleGroupId")) or None,
        "note": _s(data.get("note")),
        "planParts": [plan_part],
    }

    if discount_problems:
        nested[PROBLEMS_KEY] = discount_problems

    if funded:
        nested["publicFundingSettings"] = {
            "method": _s(data.get("fundingMethod")) or None,
            "hours": data.get("fundingHours"),
            "minutes": None,
            "fundingTypeIds": [],
            "maxFundedMinutes": data.get("maxFundedMinutes"),
        }

    return nested
