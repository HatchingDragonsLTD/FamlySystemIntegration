"""The normalized plan input contract.

One shape, two producers: a CSV file (`csv_loader.py`) and a HubSpot custom-code
action (`hubspot_intake.py`) both emit exactly this. Anything that can produce a
`PlanInput` can drive a plan write.

UUIDs ONLY, with one exception. Every label-to-UUID resolution (billing profile
names, session names, product names, funding types) happens upstream, in
whatever built the payload. Nothing here looks a label up, and nothing here
should ever learn how to -- a label arriving at this layer is a bug in the
producer, and `validate` rejects it as a non-UUID.

The exception is `billing.id`, which is a billing-scheme enum string such as
"ANNUALIZED_V2" and never a UUID. See KNOWN_BILLING_SCHEME_IDS.

NOTE on naming: `builder.py` has its own `PlanPartInput`, `SessionBookingInput`,
`ProductBookingInput`, `PublicFundingSettingsInput` and `BillingInput` with
different fields -- those model the REQUEST BODY, these model the NORMALIZED
INPUT. They are deliberately kept apart; `to_plan_body` is the one place that
converts between them, and it refers to the builder's types module-qualified so
the two are never confused.

No argparse, no I/O: importable anywhere.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import builder

# Days as the API names them.
VALID_DAYS = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)

# Billing scheme identifiers seen on `billing.id`. Unlike every other *_id in
# this contract, billing.id is an enum string, not a UUID.
#
# THIS LIST IS ALMOST CERTAINLY INCOMPLETE -- it holds only the schemes seen so
# far. An id that is not listed is flagged rather than rejected: a scheme we
# have not met yet is legitimate, but so is a typo, and being told about both
# beats silently accepting either. Add real schemes here as they turn up.
KNOWN_BILLING_SCHEME_IDS = {
    "ANNUALIZED_V2",
}

# Canonical 8-4-4-4-12 hex form, matched case-insensitively.
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class BillingInput:
    """Billing settings for a plan part."""

    id: str | None = None
    title: str | None = None
    weeks_of_care: Any | None = None
    invoices: Any | None = None


@dataclass
class SessionBookingInput:
    """A session booked on a given day.

    No start/end here: the normalized contract identifies the session by UUID
    and leaves its times to the server.
    """

    session_id: str | None = None
    day: str | None = None
    fundable: Any | None = None


@dataclass
class ProductBookingInput:
    """A product booked on a given day."""

    product_id: str | None = None
    day: str | None = None
    amount: Any | None = None


@dataclass
class PublicFundingSettingsInput:
    method: str | None = None
    hours: Any | None = None
    minutes: Any | None = None
    funding_type_ids: list = field(default_factory=list)
    max_funded_minutes: Any | None = None


@dataclass
class PlanPartInput:
    billing_profile_id: str | None = None
    attendance_schedule_id: str | None = None
    billing: BillingInput | None = None
    session_bookings: list[SessionBookingInput] = field(default_factory=list)
    product_bookings: list[ProductBookingInput] = field(default_factory=list)
    term_schedule_id: str | None = None
    term_time_only: bool = False


@dataclass
class PlanInput:
    child_id: str | None = None
    from_date: str | None = None
    rule_group_id: str | None = None
    note: str = ""
    plan_parts: list[PlanPartInput] = field(default_factory=list)
    public_funding_settings: PublicFundingSettingsInput | None = None


# --------------------------------------------------------------------------- #
# Parsing -- defensive throughout. A malformed payload yields a PlanInput with
# missing fields for `validate` to report on; parsing itself never raises.
# --------------------------------------------------------------------------- #
def _first(node: dict, *keys: str) -> Any:
    """Return the first key present, so camelCase and snake_case both work."""
    for key in keys:
        if key in node:
            return node.get(key)
    return None


def _parse_billing(node: Any) -> BillingInput | None:
    if not isinstance(node, dict):
        return None
    return BillingInput(
        id=node.get("id"),
        title=node.get("title"),
        weeks_of_care=_first(node, "weeksOfCare", "weeks_of_care"),
        invoices=node.get("invoices"),
    )


def _parse_session_bookings(node: Any) -> list[SessionBookingInput]:
    if not isinstance(node, list):
        return []
    return [
        SessionBookingInput(
            session_id=_first(s, "sessionId", "session_id"),
            day=s.get("day"),
            fundable=s.get("fundable"),
        )
        for s in node
        if isinstance(s, dict)
    ]


def _parse_product_bookings(node: Any) -> list[ProductBookingInput]:
    if not isinstance(node, list):
        return []
    return [
        ProductBookingInput(
            product_id=_first(p, "productId", "product_id"),
            day=p.get("day"),
            amount=p.get("amount"),
        )
        for p in node
        if isinstance(p, dict)
    ]


def _parse_public_funding_settings(node: Any) -> PublicFundingSettingsInput | None:
    if not isinstance(node, dict):
        return None
    funding_type_ids = _first(node, "fundingTypeIds", "funding_type_ids")
    return PublicFundingSettingsInput(
        method=node.get("method"),
        hours=node.get("hours"),
        minutes=node.get("minutes"),
        funding_type_ids=list(funding_type_ids)
        if isinstance(funding_type_ids, list)
        else [],
        max_funded_minutes=_first(node, "maxFundedMinutes", "max_funded_minutes"),
    )


def _parse_plan_part(node: Any) -> PlanPartInput:
    if not isinstance(node, dict):
        return PlanPartInput()

    term_time_only = _first(node, "termTimeOnly", "term_time_only")

    return PlanPartInput(
        billing_profile_id=_first(node, "billingProfileId", "billing_profile_id"),
        attendance_schedule_id=_first(
            node, "attendanceScheduleId", "attendance_schedule_id"
        ),
        billing=_parse_billing(node.get("billing")),
        session_bookings=_parse_session_bookings(
            _first(node, "sessionBookings", "session_bookings")
        ),
        product_bookings=_parse_product_bookings(
            _first(node, "productBookings", "product_bookings")
        ),
        term_schedule_id=_first(node, "termScheduleId", "term_schedule_id"),
        # Kept as-is when it is already a bool so validate() can report a
        # non-boolean rather than silently coercing one.
        term_time_only=term_time_only if term_time_only is not None else False,
    )


def from_dict(data: Any) -> PlanInput:
    """Parse the normalized payload into a PlanInput.

    Accepts camelCase or snake_case keys, since a CSV loader and a HubSpot
    custom-code action may not agree on style. Never raises: anything missing or
    malformed comes back as None/empty for `validate` to report.
    """
    if not isinstance(data, dict):
        return PlanInput()

    parts = _first(data, "planParts", "plan_parts")
    funding = _first(data, "publicFundingSettings", "public_funding_settings")
    note = data.get("note")

    return PlanInput(
        child_id=_first(data, "childId", "child_id"),
        from_date=_first(data, "from", "from_date"),
        rule_group_id=_first(data, "ruleGroupId", "rule_group_id"),
        note=note if isinstance(note, str) else "",
        plan_parts=[_parse_plan_part(p) for p in parts]
        if isinstance(parts, list)
        else [],
        public_funding_settings=_parse_public_funding_settings(funding),
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def is_uuid(value: Any) -> bool:
    """True when `value` is a non-empty string in canonical UUID form."""
    return isinstance(value, str) and bool(UUID_PATTERN.match(value.strip()))


def _check_uuid(value: Any, label: str, errors: list[str]) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(f"{label}: required, but missing or empty")
    elif not is_uuid(value):
        errors.append(
            f"{label}: {value!r} is not a UUID (labels must be resolved upstream)"
        )


def _check_optional_uuid(value: Any, label: str, errors: list[str]) -> None:
    """Nullable field: absent is fine, present-but-not-a-UUID is not."""
    if value is not None and not is_uuid(value):
        errors.append(f"{label}: {value!r} is not a UUID (use null to omit it)")


def _check_billing_scheme_id(value: Any, label: str, errors: list[str]) -> None:
    """Check `billing.id`, which is a billing-scheme enum, NOT a UUID.

    Two tiers:
      * hard failure -- missing, empty, or not a string at all;
      * soft flag    -- a non-empty string that is not in
        KNOWN_BILLING_SCHEME_IDS. That list is incomplete by nature, so an
        unrecognised value is surfaced rather than rejected: it may be a scheme
        we have not catalogued, or it may be a typo, and both are worth seeing.
    """
    if value is None or not isinstance(value, str) or not value.strip():
        errors.append(f"{label}: required, but missing or empty")
        return

    if value.strip() not in KNOWN_BILLING_SCHEME_IDS:
        errors.append(
            f"{label}: NOTE -- {value!r} is not a known billing scheme "
            f"({', '.join(sorted(KNOWN_BILLING_SCHEME_IDS))}). It may be valid "
            f"and simply uncatalogued, or it may be a typo. Check it before "
            f"committing."
        )


def _check_day(day: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(day, str) or day.strip().upper() not in VALID_DAYS:
        errors.append(f"{prefix}.day: {day!r} is not one of {', '.join(VALID_DAYS)}")


def _check_amount(amount: Any, prefix: str, errors: list[str]) -> None:
    # bool is an int subclass, so True would otherwise pass as 1.
    if isinstance(amount, bool) or not isinstance(amount, int):
        errors.append(f"{prefix}.amount: {amount!r} is not an integer")
    elif amount <= 0:
        errors.append(f"{prefix}.amount: {amount!r} must be greater than zero")


def validate(plan_input: PlanInput) -> list[str]:
    """Check a PlanInput for structural problems.

    Returns human-readable problems; an empty list means valid.

    THIS CANNOT TELL YOU THE UUIDS ARE CORRECT. It checks only that values are
    present and shaped like UUIDs -- not that they exist in Famly, belong to
    this child, or refer to what the producer intended. A well-formed UUID for
    the wrong session passes here and still produces a wrong plan. Running a
    preview against Famly is the only way to find that out, and the warnings it
    returns are the only signal.
    """
    errors: list[str] = []

    _check_uuid(plan_input.child_id, "childId", errors)

    if not plan_input.from_date:
        errors.append("from: required, but missing or empty")
    else:
        try:
            date.fromisoformat(str(plan_input.from_date))
        except (ValueError, TypeError):
            errors.append(
                f"from: {plan_input.from_date!r} is not an ISO date (YYYY-MM-DD)"
            )

    _check_optional_uuid(plan_input.rule_group_id, "ruleGroupId", errors)

    if not plan_input.plan_parts:
        errors.append("planParts: at least one plan part is required")

    for index, part in enumerate(plan_input.plan_parts):
        prefix = f"planParts[{index}]"

        _check_uuid(part.billing_profile_id, f"{prefix}.billingProfileId", errors)
        _check_uuid(
            part.attendance_schedule_id, f"{prefix}.attendanceScheduleId", errors
        )
        _check_optional_uuid(part.term_schedule_id, f"{prefix}.termScheduleId", errors)

        if not isinstance(part.term_time_only, bool):
            errors.append(
                f"{prefix}.termTimeOnly: {part.term_time_only!r} is not a boolean"
            )

        if part.billing is None:
            errors.append(f"{prefix}.billing: required, but missing")
        else:
            # NOT a UUID: billing.id carries a billing-scheme enum string.
            _check_billing_scheme_id(part.billing.id, f"{prefix}.billing.id", errors)

        if not part.session_bookings and not part.product_bookings:
            errors.append(f"{prefix}: has neither session nor product bookings")

        for b_index, booking in enumerate(part.session_bookings):
            b_prefix = f"{prefix}.sessionBookings[{b_index}]"
            _check_uuid(booking.session_id, f"{b_prefix}.sessionId", errors)
            _check_day(booking.day, b_prefix, errors)
            if not isinstance(booking.fundable, bool):
                errors.append(
                    f"{b_prefix}.fundable: {booking.fundable!r} is not a boolean"
                )

        for p_index, booking in enumerate(part.product_bookings):
            p_prefix = f"{prefix}.productBookings[{p_index}]"
            _check_uuid(booking.product_id, f"{p_prefix}.productId", errors)
            _check_day(booking.day, p_prefix, errors)
            _check_amount(booking.amount, p_prefix, errors)

    funding = plan_input.public_funding_settings
    if funding is not None:
        for f_index, type_id in enumerate(funding.funding_type_ids):
            if not is_uuid(type_id):
                errors.append(
                    f"publicFundingSettings.fundingTypeIds[{f_index}]: "
                    f"{type_id!r} is not a UUID"
                )

    return errors


# --------------------------------------------------------------------------- #
# Conversion to the request body
# --------------------------------------------------------------------------- #
def to_plan_body(plan_input: PlanInput) -> dict:
    """Convert a PlanInput into the `{"plan": {...}}` request body.

    All body construction is delegated to `builder.build_plan_body` -- this only
    maps normalized fields onto the builder's inputs, so the body shape stays
    defined in exactly one place.

    NOTE: the normalized contract carries no session start/end and no product
    bookedPrice, so those reach the builder as None. If Famly rejects a null
    start/end, it shows up on the first preview; the fix then belongs in the
    contract and its producers, not in a guess here.
    """
    parts = []
    for part in plan_input.plan_parts:
        parts.append(
            builder.PlanPartInput(
                attendance_schedule_id=part.attendance_schedule_id,
                billing_profile_id=part.billing_profile_id,
                # Ordering is positional: parts go out in the order given.
                ordering=len(parts),
                term_schedule_id=part.term_schedule_id,
                term_time_only=part.term_time_only,
                billing=builder.BillingInput(
                    id=part.billing.id,
                    title=part.billing.title,
                    weeks_of_care=part.billing.weeks_of_care,
                    invoices=part.billing.invoices,
                )
                if part.billing
                else None,
                session_bookings=[
                    builder.SessionBookingInput(
                        session_id=b.session_id,
                        day=b.day,
                        fundable=b.fundable,
                    )
                    for b in part.session_bookings
                ],
                product_bookings=[
                    builder.ProductBookingInput(
                        product_id=b.product_id,
                        day=b.day,
                        amount=b.amount,
                    )
                    for b in part.product_bookings
                ],
            )
        )

    funding = plan_input.public_funding_settings

    return builder.build_plan_body(
        child_id=plan_input.child_id,
        from_date=plan_input.from_date,
        rule_group_id=plan_input.rule_group_id,
        note=plan_input.note,
        plan_parts=parts,
        public_funding_settings=builder.PublicFundingSettingsInput(
            method=funding.method,
            hours=funding.hours,
            minutes=funding.minutes,
            funding_type_ids=funding.funding_type_ids,
            max_funded_minutes=funding.max_funded_minutes,
        )
        if funding
        else None,
    )
