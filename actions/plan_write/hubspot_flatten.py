"""Adapter: the flat field set a HubSpot native webhook sends -> the nested
plan shape `input_schema.from_dict` expects.

The HubSpot custom code action emits flat scalar fields (HubSpot output fields
cannot hold arrays), and the native webhook forwards them as a flat JSON body:

    childId, from, to, ruleGroupId, note,
    attendanceScheduleId, weeksOfCare, billingId, billingTitle, billingInvoices,
    termTimeOnly,
    institution, has_meals, has_activities  (meals/activities only matter when
                                              funded is true),
    monday ... friday,                   (raw slot per day: "morning" /
                                          "afternoon" / "full day", "" = not
                                          booked -- NOT a pre-resolved UUID)
    funded, fundingMethod, fundingHours, maxFundedMinutes,
    discount_1_name/discount_1_amount ... discount_3_name/discount_3_amount,
    half_day_adjustment ("yes"/"no") + half_day_amount (2.94 or 3.53),
    site_code                            (e.g. "HDCITY" -- metadata only),
    product_1_id, product_2_id, addon_quantity   (non-funded deals only)

This is the NEW contract: HubSpot sends a raw slot string per day plus
institution/funded/meals/activities, and this module resolves each booked
day's Famly session UUID via `integrations.session_catalogue` -- HubSpot no
longer sends a pre-resolved UUID. The old `monday_session`-style fields are no
longer read at all; a payload still sending them looks like a booking with no
institution and no slot, and fails validation accordingly. Both shapes are
deliberately NOT supported side by side, so there is no ambiguity about which
contract is live.

This module reshapes the payload into the single-plan-part nested structure.
It does NOT validate -- it only restructures; `input_schema.validate` still
runs after. Two kinds of problems a producer can find here ride separate
channels:

  * discount slots (pairing a name with an amount is only possible here,
    before the slots are flattened away) are ADVISORY: PROBLEMS_KEY, turned
    into warnings by hubspot_intake -- the plan still previews.
  * a session catalogue gap for a booked day is a HARD failure: ERRORS_KEY,
    same channel as a malformed product booking -- blocks the preview with
    zero Famly calls, because there is no safe partial plan to fall back to.

billingProfileId is intentionally absent (the server resolves it separately).
"""

import math
from datetime import datetime, timezone
from typing import Any

from integrations import catalogue, session_catalogue

# HubSpot field -> Famly day enum. Order fixed for stable output. Each field
# carries a raw slot string ("morning"/"afternoon"/"full day"), resolved to a
# session UUID via session_catalogue -- see build_session_bookings.
_DAYS = [
    ("monday", "MONDAY"),
    ("tuesday", "TUESDAY"),
    ("wednesday", "WEDNESDAY"),
    ("thursday", "THURSDAY"),
    ("friday", "FRIDAY"),
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

# The most days products can be booked on, however large addon_quantity is.
# A fractional quantity rounds UP (2.5 -> 3, since a part-day of add-ons is
# still a day the family receives them) and is then capped here.
MAX_ADDON_DAYS = 3

# The half-day adjustment is a FIXED amount, not a percentage, and HubSpot
# precomputes it -- the server does not work out the child's age. Only these
# two values are legitimate; anything else means the upstream calculation went
# wrong, so the discount is excluded rather than applied on trust.
HALF_DAY_TITLE = "Half Day Adjustment"
HALF_DAY_ORDERING = 3
VALID_HALF_DAY_AMOUNTS = (2.94, 3.53)

# Amounts arrive as a FRACTION (5% is 0.05), so anything above 1.0 is a
# fat-fingered percentage rather than a fraction. Rejecting it here stops a
# typed "5" becoming a 500% discount on a real invoice.
_MIN_DISCOUNT_AMOUNT = 0.0
_MAX_DISCOUNT_AMOUNT = 1.0

# Key the problems are carried under, for input_schema to pick up. Stripped
# before the plan body is built -- it never reaches Famly.
PROBLEMS_KEY = "_problems"

# Hard validation failures the producer found -- routed to input_schema.validate
# alongside a missing childId, so they BLOCK the preview. Distinct from
# PROBLEMS_KEY, which is advisory and only warns.
ERRORS_KEY = "_errors"

# Sibling metadata: site context, deliberately OUTSIDE the plan dict so it can
# never be posted to Famly's plan endpoint. to_plan_body does not read it.
METADATA_KEY = "_metadata"


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

    A fourth, FIXED-amount discount is appended when `half_day_adjustment` is
    yes: see `_half_day_discount`. It carries `isPercent: False`, so the two
    kinds coexist in the list and anything reading them must check that flag
    rather than assume a percentage.

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

    # The fixed-amount half-day adjustment sits after the three slots, at its
    # own fixed ordering, whether or not any of them were filled.
    half_day, half_day_problem = _half_day_discount(data)
    if half_day is not None:
        discounts.append(half_day)
    if half_day_problem is not None:
        problems.append(half_day_problem)

    return discounts, problems


def _half_day_discount(data: dict) -> tuple[dict | None, str | None]:
    """The fixed-amount half-day adjustment, when the deal has one.

    Returns (discount, problem); at most one is ever set. Like the percentage
    slots, a bad value is EXCLUDED and warned about rather than blocking the
    preview -- an omitted discount is visible to the approver, whereas a wrong
    one would quietly alter a family's bill.

    `isPercent` is False here: this is pounds, not a fraction, so it must not
    be read or displayed as a percentage.
    """
    if not _as_bool(data.get("half_day_adjustment")):
        return None, None

    raw_amount = _s(data.get("half_day_amount"))
    expected = " or ".join(f"{amount:.2f}" for amount in VALID_HALF_DAY_AMOUNTS)

    if raw_amount == "":
        return None, (
            f"Half day adjustment excluded: half_day_adjustment is yes but "
            f"half_day_amount is missing (expected {expected})"
        )

    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        return None, (
            f"Half day adjustment excluded: amount {raw_amount!r} is not a "
            f"number (expected {expected})"
        )

    # Compared to the penny: these are exact figures HubSpot computed, and a
    # near miss means the upstream calculation is wrong, not that it rounded.
    if round(amount, 2) not in VALID_HALF_DAY_AMOUNTS:
        # Echo what was sent, not a reformatted version of it: "3.00" reported
        # back as "3" makes it harder to spot what was actually entered.
        return None, (
            f"Half day adjustment excluded: amount {raw_amount} is not a "
            f"recognised value (expected {expected})"
        )

    return (
        {
            "title": HALF_DAY_TITLE,
            "amount": amount,
            "ordering": HALF_DAY_ORDERING,
            "fePriceModifierType": "discount",
            # NOT a percentage -- a fixed number of pounds.
            "isPercent": False,
            "origin": "custom",
            "period": "WEEKLY",
            "showOnInvoice": True,
        },
        None,
    )


def build_product_bookings(data: Any, booked_days: list[str]) -> tuple[list, list[str]]:
    """Build product bookings for the first `addon_quantity` booked days.

    Non-funded deals send two product UUIDs and a quantity; a funded deal sends
    none of them, and gets no product bookings. Both products are booked on
    each selected day, one unit each.

    Days are taken in week order (the order `_DAYS` already produced the
    session bookings in), so "the first 3 of Mon/Wed/Thu/Fri" is Mon, Wed, Thu.

    `addon_quantity` may be fractional. It is rounded UP and then capped at
    MAX_ADDON_DAYS: 2.5 -> 3, and 4.5 -> 5 -> 3. The capped figure is what the
    day count is checked against, so 4.5 against two booked days is still an
    error (3 > 2), while 4.5 against five booked days books the first three.

    UNLIKE the discount slots, a malformed product setup is a HARD error, not a
    warning. There is no safe partial behaviour: capping the quantity, or
    guessing which of the two products to drop, would silently bill the family
    for something nobody chose. Blocking the preview is the correct outcome.

    Args:
        data: the flat HubSpot payload.
        booked_days: the plan's booked days, already in week order.

    Returns:
        (product_bookings, errors). A non-empty `errors` means the preview must
        not proceed; `product_bookings` is then empty.
    """
    if not isinstance(data, dict):
        return [], []

    product_1 = _s(data.get("product_1_id"))
    product_2 = _s(data.get("product_2_id"))
    raw_quantity = _s(data.get("addon_quantity"))

    supplied = [bool(product_1), bool(product_2), bool(raw_quantity)]

    # The funded path: nothing sent, nothing booked, nothing wrong.
    if not any(supplied):
        return [], []

    # Half-specified. Naming what is missing beats a generic complaint.
    if not all(supplied):
        missing = []
        if not product_1:
            missing.append("product_1_id")
        if not product_2:
            missing.append("product_2_id")
        if not raw_quantity:
            missing.append("addon_quantity")
        return [], [
            f"product booking is half-specified: missing {', '.join(missing)}. "
            f"Send product_1_id, product_2_id and addon_quantity together, or "
            f"none of them."
        ]

    try:
        raw_value = float(raw_quantity)
    except (TypeError, ValueError):
        return [], [f"addon_quantity: {raw_quantity!r} is not a number"]

    if raw_value < 0:
        return [], [f"addon_quantity ({raw_value:g}) cannot be negative"]

    # Round up, then cap. A half day of add-ons still means the family gets
    # them that day, so 2.5 covers three days; and no plan books products on
    # more than MAX_ADDON_DAYS days whatever was entered.
    quantity = min(math.ceil(raw_value), MAX_ADDON_DAYS)

    if quantity > len(booked_days):
        # Report the effective figure, and how it was reached when that is not
        # simply what was typed.
        if quantity != raw_value:
            entered = (
                f"addon_quantity ({raw_value:g} -> {quantity} after rounding up"
                f"{' and capping at ' + str(MAX_ADDON_DAYS) if math.ceil(raw_value) > MAX_ADDON_DAYS else ''})"
            )
        else:
            entered = f"addon_quantity ({quantity})"
        return [], [
            f"{entered} exceeds the number of booked days ({len(booked_days)})"
        ]

    bookings = []
    for day in booked_days[:quantity]:
        for product_id in (product_1, product_2):
            bookings.append({"productId": product_id, "day": day, "amount": 1})

    return bookings, []


def build_session_bookings(data: Any) -> tuple[list, list[str]]:
    """Build session bookings for the booked days, resolved via the catalogue.

    Each day field (`monday`.."friday") carries a raw slot string ("morning",
    "afternoon", "full day") or is empty (not booked). The Famly session UUID
    for a booked day is resolved from `session_catalogue`, using the deal's
    institution, funded flag, and -- for a funded deal -- its meals/activities
    flags. HubSpot no longer sends a pre-resolved UUID.

    A missing `institution` with at least one booked day is reported the same
    way: there is no session to resolve without it, and naming it explicitly
    beats a producer poking around inside a generic catalogue error.

    A catalogue gap for ANY booked day is a HARD failure for the whole plan --
    unlike the discount slots, there is no safe partial behaviour: skipping the
    bad day and keeping the rest would produce a plan nobody asked for. See
    `build_product_bookings` for the same reasoning on the product side.

    Args:
        data: the flat HubSpot payload.

    Returns:
        (session_bookings, errors). A non-empty `errors` means the preview
        must not proceed; `session_bookings` is then empty.
    """
    if not isinstance(data, dict):
        return [], []

    funded = _as_bool(data.get("funded"))
    institution = _s(data.get("institution"))
    has_meals = _as_bool(data.get("has_meals"))
    has_activities = _as_bool(data.get("has_activities"))

    booked = [(day_key, day_enum, _s(data.get(day_key))) for day_key, day_enum in _DAYS]
    booked = [(day_key, day_enum, slot) for day_key, day_enum, slot in booked if slot]

    if not booked:
        return [], []

    if institution == "":
        return [], ["institution: required, but missing or empty"]

    bookings = []
    errors = []
    for _day_key, day_enum, slot in booked:
        try:
            session_id = session_catalogue.resolve_session(
                institution, funded, slot, has_meals, has_activities
            )
        except session_catalogue.SessionCatalogueError as exc:
            errors.append(str(exc))
            continue
        bookings.append({"sessionId": session_id, "day": day_enum, "fundable": funded})

    if errors:
        return [], errors

    return bookings, []


def build_site_metadata(data: Any) -> dict:
    """Resolve `site_code` into sibling metadata for the plan.

    METADATA ONLY. The result is carried beside the plan, never inside it:
    `institution_id` is not sent to Famly, and nothing here influences which
    session, billing profile or attendance schedule the plan uses -- those stay
    entirely determined by the UUIDs HubSpot already sends.

    Never blocks a preview. A missing or unrecognised code yields a `warning`
    for the approver and leaves `institution_id` as None.

    Returns:
        {"site_code", "label", "institution_id", "warning"}; `warning` is None
        when the code resolved.
    """
    raw_code = _s(data.get("site_code")) if isinstance(data, dict) else ""

    if raw_code == "":
        return {
            "site_code": None,
            "label": None,
            "institution_id": None,
            "warning": "No site_code supplied - proceeding without site context",
        }

    site = catalogue.resolve_site(raw_code)
    if site is None:
        return {
            "site_code": raw_code,
            "label": None,
            "institution_id": None,
            "warning": (
                f"Unrecognised site_code: {raw_code} - proceeding without site "
                f"context"
            ),
        }

    return {
        "site_code": site["site_code"],
        "label": site["label"],
        "institution_id": site["institution_id"],
        "warning": None,
    }


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

    Skips any day whose slot value is empty (not booked). A booked day's
    session UUID is resolved via the session catalogue (see
    `build_session_bookings`); a gap there rides ERRORS_KEY as a hard failure,
    same channel as a malformed product booking. Attaches
    publicFundingSettings only when `funded` is truthy. Passes identity fields
    through unchanged. Never raises; missing fields simply come through empty so
    validate() can report them.
    """
    if not isinstance(data, dict):
        return {}

    # Session bookings: one per booked day, resolved via the session catalogue
    # (see build_session_bookings). fundable is the deal-wide `funded` flag
    # applied to every booking -- a child is entirely funded or entirely
    # non-funded.
    funded = _as_bool(data.get("funded"))
    session_bookings, session_errors = build_session_bookings(data)

    # Up to three custom discounts. A half-filled or out-of-range slot yields a
    # problem instead of a discount, and the problems ride along so validate()
    # reports them at PREVIEW time rather than after a commit.
    discounts, discount_problems = build_discounts(data)

    # Product bookings land on the first `addon_quantity` booked days, in week
    # order. A malformed setup is a hard error and blocks the preview. Booked
    # days are taken from the raw slot fields, not from `session_bookings`, so
    # a session catalogue gap does not also masquerade as "nothing booked" here.
    booked_days = [day_enum for day_key, day_enum in _DAYS if _s(data.get(day_key))]
    product_bookings, product_errors = build_product_bookings(data, booked_days)
    errors = session_errors + product_errors

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
        "productBookings": product_bookings,
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

    if errors:
        nested[ERRORS_KEY] = errors

    # Site context rides alongside the plan, never inside it.
    nested[METADATA_KEY] = build_site_metadata(data)

    if funded:
        nested["publicFundingSettings"] = {
            "method": _s(data.get("fundingMethod")) or None,
            "hours": data.get("fundingHours"),
            "minutes": None,
            "fundingTypeIds": [],
            "maxFundedMinutes": data.get("maxFundedMinutes"),
        }

    return nested
