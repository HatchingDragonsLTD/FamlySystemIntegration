"""Adapter: the flat field set a HubSpot native webhook sends -> the nested
plan shape `input_schema.from_dict` expects.

The HubSpot custom code action emits flat scalar fields (HubSpot output fields
cannot hold arrays), and the native webhook forwards them as a flat JSON body:

    childId, from, to, note,
    termTimeOnly,
    institution, has_meals, has_activities  (meals/activities only matter when
                                              funded is true),
    monday ... friday,                   (raw slot per day: "morning" /
                                          "afternoon" / "full day", "" = not
                                          booked -- NOT a pre-resolved UUID)
    funded, fundingMethod, fundingHours, maxFundedMinutes,
    discount_1_name/discount_1_amount/discount_1_flag ... discount_10_name/
                                          discount_10_amount/discount_10_flag
                                          (flag is "percent"/"fixed", optional
                                          -- see build_discounts),
    site_code                            (e.g. "HDCITY" -- metadata only),
    addon_quantity                       (non-funded deals only -- whole days
                                          plus an optional trailing half day,
                                          e.g. 2.5; see build_product_bookings)

This is the NEW contract: HubSpot sends a raw slot string per day plus
institution/funded/meals/activities, and this module resolves each booked
day's Famly session UUID via `integrations.session_catalogue` -- HubSpot no
longer sends a pre-resolved UUID. The old `monday_session`-style fields are no
longer read at all; a payload still sending them looks like a booking with no
institution and no slot, and fails validation accordingly. Both shapes are
deliberately NOT supported side by side, so there is no ambiguity about which
contract is live.

SECOND GENERATION OF THAT SAME CHANGE: HubSpot ALSO no longer sends
`ruleGroupId`, `billingProfileId`, `attendanceScheduleId`, `weeksOfCare`,
`billingId`, `billingTitle` or `billingInvoices`. Those used to arrive as flat,
institution-wide constants -- values that never actually varied per deal, only
per institution (and, for the billing-profile fields, per term-time-only vs
full-year schedule) -- with `billingProfileId` not sent at all, reaching Famly
as a bare `null`. All seven are now resolved here, from
`reference/institution_defaults.json`, via `build_institution_defaults` /
`integrations.institution_defaults.resolve_defaults`, keyed by the deal's
`institution` and a schedule derived from its `termTimeOnly` flag. If HubSpot
still sends any of these seven fields, they are simply ignored -- a no-op, not
an override -- since the resolved values are now the only authoritative
source. `billingProfileId` gets a real, institution-correct value for the
first time as a result, rather than always being null.

THIRD GENERATION: HubSpot ALSO no longer sends `product_1_id`/`product_2_id`.
`addon_quantity` alone now signals "book the two fixed add-on products" on a
non-funded deal; both product ids are resolved from
`reference/institution_defaults.json`'s `addonProducts` (via
`integrations.institution_defaults.resolve_addon_products`), which
`actions/pull_products` populates by an EXACT, case-sensitive title match
against Famly's product list ("Meals & Snacks" / "Educational Activities &
Extras") -- never normalized, so a real naming drift is caught as a hard
failure rather than silently papered over. If HubSpot still sends
`product_1_id`/`product_2_id`, they are ignored -- a no-op, same as the seven
fields above.

This module reshapes the payload into the single-plan-part nested structure.
It does NOT validate -- it only restructures; `input_schema.validate` still
runs after. Two kinds of problems a producer can find here ride separate
channels:

  * discount slots (pairing a name with an amount is only possible here,
    before the slots are flattened away) are ADVISORY: PROBLEMS_KEY, turned
    into warnings by hubspot_intake -- the plan still previews.
  * a session catalogue gap for a booked day, or an institution_defaults
    resolution failure, is a HARD failure: ERRORS_KEY, same channel as a
    malformed product booking -- blocks the preview with zero Famly calls,
    because there is no safe partial plan to fall back to.
"""

import math
from datetime import datetime, timezone
from typing import Any

from integrations import catalogue, institution_defaults, session_catalogue

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

# Up to ten custom discount slots: discount_1_name / discount_1_amount /
# discount_1_flag, etc.
_DISCOUNT_SLOTS = tuple(range(1, 11))

# Fixed fields on every custom discount we build. title, amount, ordering and
# isPercent vary per slot.
_DISCOUNT_DEFAULTS = {
    "fePriceModifierType": "discount",
    "origin": "custom",
    "period": "WEEKLY",
    "showOnInvoice": True,
}

# A slot's `flag` selects which kind of discount it is. Absent/empty defaults
# to "percent" -- the backward-compatible path, since every HubSpot branch
# before this generalisation only ever sent percentage discounts and never a
# flag at all.
DISCOUNT_FLAG_PERCENT = "percent"
DISCOUNT_FLAG_FIXED = "fixed"
VALID_DISCOUNT_FLAGS = (DISCOUNT_FLAG_PERCENT, DISCOUNT_FLAG_FIXED)

# The most days products can be booked on, however large addon_quantity is.
# `full` whole days are capped here, and a trailing half day is only ever
# added BELOW the cap (3.5 books the same as 3) -- see build_product_bookings.
MAX_ADDON_DAYS = 3

# Each flag's valid amount range, both ends exclusive-min/inclusive-max
# (0 < amount <= max). Percent amounts arrive as a FRACTION (5% is 0.05), so
# anything above 1.0 is a fat-fingered percentage rather than a fraction --
# rejecting it stops a typed "5" becoming a 500% discount on a real invoice.
# Fixed amounts arrive in pounds; 5000 is a generous but real ceiling -- a
# weekly custom charge above that is far more likely a mistake than a genuine
# discount/surcharge.
_MIN_PERCENT_AMOUNT = 0.0
_MAX_PERCENT_AMOUNT = 1.0
_MIN_FIXED_AMOUNT = 0.0
_MAX_FIXED_AMOUNT = 5000.0

_DISCOUNT_RANGES = {
    DISCOUNT_FLAG_PERCENT: (_MIN_PERCENT_AMOUNT, _MAX_PERCENT_AMOUNT),
    DISCOUNT_FLAG_FIXED: (_MIN_FIXED_AMOUNT, _MAX_FIXED_AMOUNT),
}

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
    """Build the plan part's custom discounts from the ten flat slots.

    A slot is `discount_N_name` plus `discount_N_amount`, plus an OPTIONAL
    `discount_N_flag` ("percent" or "fixed"). An empty slot (name AND amount
    both empty) is skipped entirely regardless of flag -- the normal "unused
    slot" case. A half-filled, badly-flagged, or out-of-range one is reported
    as a problem and excluded, never guessed at.

    `flag` absent or empty DEFAULTS TO "percent" -- the backward-compatible
    path: every HubSpot branch built before this generalisation never sent a
    flag at all and only ever meant a percentage discount, so that continues
    to work with no warning. A flag that IS present but is not exactly
    "percent"/"fixed" (e.g. a typo) is a malformed slot, not a default.

    Each flag has its own valid amount range (see _DISCOUNT_RANGES):
    percent is a FRACTION, 0 < amount <= 1.0 (5% = 0.05); fixed is POUNDS,
    0 < amount <= 5000.

    An eleventh (or further) `discount_N_*` field is never read -- only slots
    1 through 10 exist.

    Percent and fixed discounts coexist in the list, so anything reading them
    must check `isPercent` rather than assume a percentage. (There used to be
    a further half-day-adjustment discount here; it was replaced by half-day
    add-on PRODUCTS -- see `build_product_bookings`. `half_day_adjustment`/
    `half_day_amount` are no longer read.)

    `ordering` is tied to the SLOT NUMBER (slot 1 -> 0, slot 10 -> 9) and is
    not compacted when a middle slot is empty, so a discount keeps its
    position regardless of what else is filled in.

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
        raw_flag = _s(data.get(f"discount_{slot}_flag"))

        # Both empty: the slot simply is not in use -- regardless of flag.
        if name == "" and raw_amount == "":
            continue

        if name != "" and raw_amount == "":
            problems.append(f"Discount {slot} excluded: name given but amount missing")
            continue

        if name == "" and raw_amount != "":
            problems.append(f"Discount {slot} excluded: amount given but name missing")
            continue

        if raw_flag == "":
            # Backward-compatible default -- not a problem, not a warning.
            flag = DISCOUNT_FLAG_PERCENT
        elif raw_flag in VALID_DISCOUNT_FLAGS:
            flag = raw_flag
        else:
            problems.append(
                f"Discount {slot} excluded: flag {raw_flag!r} is not one of "
                f"{', '.join(VALID_DISCOUNT_FLAGS)}"
            )
            continue

        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            problems.append(
                f"Discount {slot} excluded: amount {raw_amount!r} is not a number"
            )
            continue

        minimum, maximum = _DISCOUNT_RANGES[flag]
        if amount <= minimum or amount > maximum:
            range_description = (
                "0-1 (fraction, so 5% = 0.05)"
                if flag == DISCOUNT_FLAG_PERCENT
                else f"0-{maximum:g} (pounds)"
            )
            problems.append(
                f"Discount {slot} excluded: {flag} amount {amount} is out of "
                f"range {range_description}"
            )
            continue

        discounts.append(
            {
                "title": name,
                "amount": amount,
                "ordering": slot - 1,
                "isPercent": flag == DISCOUNT_FLAG_PERCENT,
                **_DISCOUNT_DEFAULTS,
            }
        )

    return discounts, problems


def build_product_bookings(data: Any, booked_days: list[str]) -> tuple[list, list[str]]:
    """Build add-on product bookings from `addon_quantity` (days, in halves).

    HubSpot no longer sends `product_1_id`/`product_2_id` at all -- see the
    module docstring. `addon_quantity` alone is the only signal: when it is
    set (non-funded deals only; a funded deal sends nothing and gets no
    product bookings), the add-on product ids are resolved from
    `reference/institution_defaults.json` via the deal's `institution`, by
    `integrations.institution_defaults.resolve_addon_products` (each one an
    exact, case-sensitive title match against Famly's product list -- see
    `actions/pull_products/runner.py`).

    With `q = addon_quantity`:

        full = min(floor(q), MAX_ADDON_DAYS)
        half = 1 if (q % 1 == 0.5 and floor(q) < MAX_ADDON_DAYS) else 0

    BOTH full products (meals, activities) are booked, `amount: 1` each, on
    the first `full` booked days; if `half` is 1, BOTH half products
    ("(1/2) Meals & Snacks", "(1/2) Educational Activities & Extras") are
    booked, `amount: 1` each, on the next booked day. Days are taken in week
    order (the order `_DAYS` already produced the session bookings in), so
    for Mon/Wed/Thu/Fri booked, 2.5 is full products Mon+Wed and half
    products Thu.

    So 0.5 -> one half day; 2.5 -> two full + one half; 3 and 3.5 -> three
    full (a half is never added on top of the cap); 4.5 -> three full. Any
    other fraction (2.3) floors: only a trailing .5 means a half day.

    UNLIKE the discount slots, a malformed product setup is a HARD error, not
    a warning. There is no safe partial behaviour -- dropping a day, or
    guessing a product, would silently bill the family for something nobody
    chose. Blocking the preview is the correct outcome. That covers:

      * an addon-product resolution failure (institution or a FULL product
        missing);
      * `full + half` exceeding the number of booked days;
      * a half day being requested when the institution has no resolved half
        products -- and ONLY then: an institution without half products
        books whole days exactly as before.

    Args:
        data: the flat HubSpot payload.
        booked_days: the plan's booked days, already in week order.

    Returns:
        (product_bookings, errors). A non-empty `errors` means the preview must
        not proceed; `product_bookings` is then empty.
    """
    if not isinstance(data, dict):
        return [], []

    raw_quantity = _s(data.get("addon_quantity"))

    # The funded path (or a non-funded deal with no add-ons): nothing sent,
    # nothing booked, nothing wrong.
    if raw_quantity == "":
        return [], []

    institution = _s(data.get("institution"))
    if institution == "":
        return [], ["institution: required, but missing or empty"]

    try:
        addon_products = institution_defaults.resolve_addon_products(institution)
    except institution_defaults.InstitutionDefaultsError as exc:
        return [], [str(exc)]

    try:
        raw_value = float(raw_quantity)
    except (TypeError, ValueError):
        return [], [f"addon_quantity: {raw_quantity!r} is not a number"]

    # float() accepts "nan"/"inf", which have no day count to floor.
    if not math.isfinite(raw_value):
        return [], [f"addon_quantity: {raw_quantity!r} is not a number"]

    if raw_value < 0:
        return [], [f"addon_quantity ({raw_value:g}) cannot be negative"]

    whole_days = math.floor(raw_value)
    full = min(whole_days, MAX_ADDON_DAYS)
    half = 1 if (raw_value % 1 == 0.5 and whole_days < MAX_ADDON_DAYS) else 0

    if full + half > len(booked_days):
        # Report the effective figure, and how it was reached when that is not
        # simply what was typed.
        if half:
            entered = f"{raw_value:g} -> {full} full + 1 half"
        elif raw_value != full:
            capped = f" after capping at {MAX_ADDON_DAYS}" if raw_value > MAX_ADDON_DAYS else ""
            entered = f"{raw_value:g} -> {full}{capped}"
        else:
            entered = f"{raw_value:g}"
        return [], [
            f"addon_quantity ({entered}) exceeds the number of booked days "
            f"({len(booked_days)})"
        ]

    half_meals = addon_products.get("halfMealsProductId")
    half_activities = addon_products.get("halfActivitiesProductId")
    if half and not (half_meals and half_activities):
        titles = institution_defaults.ADDON_PRODUCT_TITLES
        missing = [
            repr(titles[field_name])
            for field_name, value in (
                ("halfMealsProductId", half_meals),
                ("halfActivitiesProductId", half_activities),
            )
            if not value
        ]
        return [], [
            f"addon_quantity ({raw_value:g}) needs a half day of add-ons, but "
            f"institution {institution!r} has no resolved half-day product "
            f"titled {' or '.join(missing)} (check the product exists in Famly "
            f"with exactly that title, then run pull-products)"
        ]

    bookings = []
    for day in booked_days[:full]:
        for product_id in (addon_products["mealsProductId"], addon_products["activitiesProductId"]):
            bookings.append({"productId": product_id, "day": day, "amount": 1})

    if half:
        for product_id in (half_meals, half_activities):
            bookings.append({"productId": product_id, "day": booked_days[full], "amount": 1})

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


def build_institution_defaults(data: Any) -> tuple[dict, list[str]]:
    """Resolve ruleGroupId + this plan part's billing/pricing defaults.

    HubSpot no longer sends ruleGroupId, billingProfileId,
    attendanceScheduleId, weeksOfCare, billingId, billingTitle or
    billingInvoices -- see the module docstring. They are resolved here from
    reference/institution_defaults.json, keyed by the deal's institution and
    its termTimeOnly flag (see institution_defaults.schedule_key). Unlike
    build_session_bookings, this runs regardless of whether any day is
    booked: every plan part needs a billing profile.

    A resolution failure is a HARD failure -- there is no safe partial plan
    without a billing profile, same reasoning as a session catalogue gap.

    Args:
        data: the flat HubSpot payload.

    Returns:
        (defaults, errors). A non-empty `errors` means `defaults` is {}; the
        caller must not build a plan part from it.
    """
    if not isinstance(data, dict):
        return {}, []

    institution = _s(data.get("institution"))
    if institution == "":
        return {}, ["institution: required, but missing or empty"]

    schedule = institution_defaults.schedule_key(_as_bool(data.get("termTimeOnly")))

    try:
        return institution_defaults.resolve_defaults(institution, schedule), []
    except institution_defaults.InstitutionDefaultsError as exc:
        return {}, [str(exc)]


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
    same channel as a malformed product booking. ruleGroupId and the plan
    part's billing/pricing defaults are resolved via
    `build_institution_defaults`, same channel again. Attaches
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

    # ruleGroupId + billingProfileId/attendanceScheduleId/billing.* -- resolved
    # from reference/institution_defaults.json, not read from `data` (see the
    # module docstring). Runs regardless of booked days: every plan part needs
    # a billing profile.
    defaults, defaults_errors = build_institution_defaults(data)

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

    # De-duplicated, preserving order: a missing institution can surface the
    # SAME message from both session-booking resolution and defaults
    # resolution, and there is no reason to show it twice.
    errors = list(dict.fromkeys(session_errors + product_errors + defaults_errors))

    plan_part = {
        "billingProfileId": defaults.get("billingProfileId"),
        "attendanceScheduleId": defaults.get("attendanceScheduleId"),
        "billing": {
            "id": defaults.get("billingId"),
            "title": defaults.get("billingTitle"),
            "weeksOfCare": defaults.get("weeksOfCare"),
            "invoices": defaults.get("billingInvoices"),
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
        "ruleGroupId": defaults.get("ruleGroupId"),
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
