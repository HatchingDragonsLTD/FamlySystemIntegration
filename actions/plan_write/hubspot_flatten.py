"""Adapter: the flat field set a HubSpot native webhook sends -> the nested
plan shape `input_schema.from_dict` expects.

The HubSpot custom code action emits flat scalar fields (HubSpot output fields
cannot hold arrays), and the native webhook forwards them as a flat JSON body:

    childId, from, to, ruleGroupId, note,
    attendanceScheduleId, weeksOfCare, billingId, billingTitle, billingInvoices,
    termTimeOnly,
    monday_session ... friday_session,   (sessionId per day, "" = not booked)
    funded, fundingMethod, fundingHours, maxFundedMinutes

This module reshapes that into the single-plan-part nested structure. It does
NOT validate -- it only restructures; `input_schema.validate` still runs after.
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

    if funded:
        nested["publicFundingSettings"] = {
            "method": _s(data.get("fundingMethod")) or None,
            "hours": data.get("fundingHours"),
            "minutes": None,
            "fundingTypeIds": [],
            "maxFundedMinutes": data.get("maxFundedMinutes"),
        }

    return nested
