"""Builds the request body for the plans endpoint.

Constructs the sparse `{"plan": {...}}` shape the real client sends. The server
backfills everything it computes -- prices, session versions, booking ids,
funding -- so nothing here tries to reproduce the enriched response.

No argparse, no I/O: importable anywhere.

Values are passed through as given. Where a field is nullable, `None` is sent
as `null` rather than being guessed at or dropped.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

# Sentinel used for the `id` of a plan that does not exist yet. The captured
# create request sends an empty string, not null.
NEW_PLAN_ID = ""


@dataclass
class BillingInput:
    """The `billing` object on a plan part."""

    id: str | None = None
    title: str | None = None
    weeks_of_care: Any | None = None
    invoices: Any | None = None

    def to_body(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "weeksOfCare": self.weeks_of_care,
            "invoices": self.invoices,
        }


@dataclass
class SessionBookingInput:
    """One entry in a plan part's `sessionBookings`.

    `fundable` is optional: the server sets it when omitted, but it is sent
    verbatim when supplied so a caller can override.
    """

    session_id: str | None = None
    day: str | None = None
    start: Any | None = None
    end: Any | None = None
    fundable: Any | None = None

    def to_body(self) -> dict:
        body = {
            "sessionId": self.session_id,
            "day": self.day,
            "start": self.start,
            "end": self.end,
        }
        # Only send `fundable` when the caller had an opinion about it.
        if self.fundable is not None:
            body["fundable"] = self.fundable
        return body


@dataclass
class ProductBookingInput:
    """One entry in a plan part's `productBookings`."""

    product_id: str | None = None
    amount: Any | None = None
    day: str | None = None
    booked_price: Any | None = None

    def to_body(self) -> dict:
        return {
            "productId": self.product_id,
            "amount": self.amount,
            "day": self.day,
            "bookedPrice": self.booked_price,
        }


@dataclass
class PublicFundingSettingsInput:
    """The plan-level `publicFundingSettings` object."""

    method: str | None = None
    hours: Any | None = None
    minutes: Any | None = None
    funding_type_ids: list = field(default_factory=list)
    max_funded_minutes: Any | None = None

    def to_body(self) -> dict:
        return {
            "method": self.method,
            "hours": self.hours,
            "minutes": self.minutes,
            "fundingTypeIds": list(self.funding_type_ids),
            "maxFundedMinutes": self.max_funded_minutes,
        }


@dataclass
class PlanPartInput:
    """One entry in the plan's `planParts`.

    `plan_part_id` is generated when not supplied -- the client sends a UUID it
    made itself for new parts.
    """

    attendance_schedule_id: str | None = None
    billing_profile_id: str | None = None
    ordering: Any | None = None
    term_schedule_id: str | None = None
    term_time_only: Any | None = None
    billing: BillingInput | None = None
    plan_part_id: str | None = None
    session_bookings: list[SessionBookingInput] = field(default_factory=list)
    product_bookings: list[ProductBookingInput] = field(default_factory=list)
    charge_rule_exemptions: list = field(default_factory=list)
    discounts: list = field(default_factory=list)
    total_adjustments: list = field(default_factory=list)

    def to_body(self) -> dict:
        return {
            "planPartId": self.plan_part_id or new_plan_part_id(),
            "ordering": self.ordering,
            "attendanceScheduleId": self.attendance_schedule_id,
            "termScheduleId": self.term_schedule_id,
            "billingProfileId": self.billing_profile_id,
            "termTimeOnly": self.term_time_only,
            "billing": self.billing.to_body() if self.billing else None,
            "sessionBookings": [b.to_body() for b in self.session_bookings],
            "productBookings": [b.to_body() for b in self.product_bookings],
            "chargeRuleExemptions": list(self.charge_rule_exemptions),
            "discounts": list(self.discounts),
            "totalAdjustments": list(self.total_adjustments),
        }


def new_plan_part_id() -> str:
    """A fresh plan part UUID, as the web client generates client-side."""
    return str(uuid.uuid4())


def build_plan_body(
    child_id: str,
    from_date: str,
    *,
    plan_id: str = NEW_PLAN_ID,
    to: str | None = None,
    pricing_group_id: str | None = None,
    rule_group_id: str | None = None,
    note: str | None = None,
    plan_parts: list[PlanPartInput] | None = None,
    public_funding_settings: PublicFundingSettingsInput | None = None,
) -> dict:
    """Build the `{"plan": {...}}` request body.

    Args:
        child_id: the child the plan belongs to.
        from_date: plan start date (sent as `from`).
        plan_id: existing plan id, or "" (the default) to create a new plan.
        to: plan end date, or None for open-ended.
        pricing_group_id: nullable.
        rule_group_id: nullable.
        note: free-text note.
        plan_parts: the plan's parts; each gets a generated UUID if it has none.
        public_funding_settings: omitted from the body entirely when None,
            since there is no safe default to invent for it.

    Returns:
        The request body dict, ready to POST.
    """
    plan: dict[str, Any] = {
        "id": plan_id,
        "childId": child_id,
        "pricingGroupId": pricing_group_id,
        "ruleGroupId": rule_group_id,
        "note": note,
        "from": from_date,
        "to": to,
        "planParts": [part.to_body() for part in (plan_parts or [])],
    }

    if public_funding_settings is not None:
        plan["publicFundingSettings"] = public_funding_settings.to_body()

    return {"plan": plan}
