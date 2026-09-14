"""Child plans action.

Fetches the full plan structure for a child and maps it into typed objects.
Owns its GraphQL query file (query.graphql) and its own response types.
Importable directly by a future server or scheduler: call `run(child_id)` and
you get parsed objects back -- no CLI involvement.

The response is deeply nested. The hierarchy modelled here:

    Plan
      billing (Billing)
      publicFunding (PublicFunding)
      publicFundingSettings (PublicFundingSettings)
      behaviors      -> list[str] (capability flags, e.g. CanAddPlanParts)
      planStates     -> list[PlanState]
      sessionBookings-> list[SessionBooking]   (plan level)
      planParts      -> list[PlanPart]
        PlanPart
          billing (Billing)
          sessionBookings -> list[SessionBooking]  (part level)
          SessionBooking
            monthlyPrices -> list[MonthlyPrice]

Every field is parsed with .get() and Optional defaults, so a missing or
renamed field yields None rather than a crash. The untouched node is kept in
`raw` on the top-level Plan so anything not modelled is still reachable.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.client import GraphQLClient

QUERY_PATH = Path(__file__).with_name("query.graphql")

# Must match the operationName in query.graphql. Update after capturing.
OPERATION_NAME = "GetChildPlans"


# --------------------------------------------------------------------------- #
# Leaf / nested types
# --------------------------------------------------------------------------- #
@dataclass
class Billing:
    id: str | None = None
    title: str | None = None
    weeks_of_care: Any | None = None
    invoices: Any | None = None


@dataclass
class MonthlyPrice:
    pricing_group_id: str | None = None
    price: Any | None = None


@dataclass
class SessionBooking:
    booking_id: str | None = None
    session_id: str | None = None
    session_version: str | None = None
    day: str | None = None
    start: Any | None = None
    end: Any | None = None
    fundable: Any | None = None
    monthly_prices: list[MonthlyPrice] = field(default_factory=list)


@dataclass
class PlanState:
    from_: str | None = None
    to: str | None = None


@dataclass
class PublicFunding:
    amount: Any | None = None
    claimable: Any | None = None
    applied: Any | None = None
    hours: Any | None = None
    minutes: Any | None = None
    seconds: Any | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class PublicFundingSettings:
    method: str | None = None
    hours: Any | None = None
    minutes: Any | None = None
    grant_ids: list = field(default_factory=list)
    funding_type_ids: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class PlanPart:
    plan_part_id: str | None = None
    ordering: Any | None = None
    attendance_schedule_id: str | None = None
    term_schedule_id: str | None = None
    billing_profile_id: str | None = None
    billing: Billing | None = None
    discounts: list = field(default_factory=list)
    product_bookings: list = field(default_factory=list)
    charge_rule_exemptions: list = field(default_factory=list)
    total_adjustments: list = field(default_factory=list)
    session_bookings: list[SessionBooking] = field(default_factory=list)


@dataclass
class Plan:
    id: str | None = None
    version: Any | None = None
    child_id: str | None = None
    from_: str | None = None
    to: str | None = None
    billing_scheme: str | None = None
    billing: Billing | None = None
    monthly_estimate: Any | None = None
    note: str | None = None
    age_group_id: str | None = None
    pricing_group_id: str | None = None
    rule_group_id: str | None = None
    term_schedule_id: str | None = None
    term_time_only: Any | None = None
    discounts: list = field(default_factory=list)
    product_bookings: list = field(default_factory=list)
    package_bookings: list = field(default_factory=list)
    funding_entitlements: Any | None = None
    public_funding: PublicFunding | None = None
    public_funding_settings: PublicFundingSettings | None = None
    behaviors: list[str] = field(default_factory=list)
    plan_states: list[PlanState] = field(default_factory=list)
    session_bookings: list[SessionBooking] = field(default_factory=list)
    plan_parts: list[PlanPart] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def is_open_ended(self) -> bool:
        """True when the plan has no end date."""
        return self.to is None


# --------------------------------------------------------------------------- #
# Parsers -- each guards its input type and uses .get() throughout
# --------------------------------------------------------------------------- #
def _parse_billing(node: Any) -> Billing | None:
    if not isinstance(node, dict):
        return None
    return Billing(
        id=node.get("id"),
        title=node.get("title"),
        weeks_of_care=node.get("weeksOfCare"),
        invoices=node.get("invoices"),
    )


def _parse_monthly_prices(node: Any) -> list[MonthlyPrice]:
    if not isinstance(node, list):
        return []
    return [
        MonthlyPrice(pricing_group_id=p.get("pricingGroupId"), price=p.get("price"))
        for p in node
        if isinstance(p, dict)
    ]


def _parse_session_bookings(node: Any) -> list[SessionBooking]:
    if not isinstance(node, list):
        return []
    out = []
    for s in node:
        if not isinstance(s, dict):
            continue
        out.append(
            SessionBooking(
                booking_id=s.get("bookingId"),
                session_id=s.get("sessionId"),
                session_version=s.get("sessionVersion"),
                day=s.get("day"),
                start=s.get("start"),
                end=s.get("end"),
                fundable=s.get("fundable"),
                monthly_prices=_parse_monthly_prices(s.get("monthlyPrices")),
            )
        )
    return out


def _parse_behaviors(node: Any) -> list[str]:
    """Behaviors are capability flags [{id, payload}]; keep the ids."""
    if not isinstance(node, list):
        return []
    return [b.get("id") for b in node if isinstance(b, dict) and b.get("id")]


def _parse_plan_states(node: Any) -> list[PlanState]:
    if not isinstance(node, list):
        return []
    return [
        PlanState(from_=s.get("from"), to=s.get("to"))
        for s in node
        if isinstance(s, dict)
    ]


def _parse_public_funding(node: Any) -> PublicFunding | None:
    if not isinstance(node, dict):
        return None
    return PublicFunding(
        amount=node.get("amount"),
        claimable=node.get("claimable"),
        applied=node.get("applied"),
        hours=node.get("hours"),
        minutes=node.get("minutes"),
        seconds=node.get("seconds"),
        raw=node,
    )


def _parse_public_funding_settings(node: Any) -> PublicFundingSettings | None:
    if not isinstance(node, dict):
        return None
    return PublicFundingSettings(
        method=node.get("method"),
        hours=node.get("hours"),
        minutes=node.get("minutes"),
        grant_ids=node.get("grantIds") or [],
        funding_type_ids=node.get("fundingTypeIds") or [],
        raw=node,
    )


def _parse_plan_part(node: Any) -> PlanPart:
    if not isinstance(node, dict):
        return PlanPart()
    return PlanPart(
        plan_part_id=node.get("planPartId"),
        ordering=node.get("ordering"),
        attendance_schedule_id=node.get("attendanceScheduleId"),
        term_schedule_id=node.get("termScheduleId"),
        billing_profile_id=node.get("billingProfileId"),
        billing=_parse_billing(node.get("billing")),
        discounts=node.get("discounts") or [],
        product_bookings=node.get("productBookings") or [],
        charge_rule_exemptions=node.get("chargeRuleExemptions") or [],
        total_adjustments=node.get("totalAdjustments") or [],
        session_bookings=_parse_session_bookings(node.get("sessionBookings")),
    )


def _parse_plan(node: Any) -> Plan:
    if not isinstance(node, dict):
        return Plan(raw={"value": node})
    parts = node.get("planParts")
    return Plan(
        id=node.get("id"),
        version=node.get("version"),
        child_id=node.get("childId"),
        from_=node.get("from"),
        to=node.get("to"),
        billing_scheme=node.get("billingScheme"),
        billing=_parse_billing(node.get("billing")),
        monthly_estimate=node.get("monthlyEstimate"),
        note=node.get("note"),
        age_group_id=node.get("ageGroupId"),
        pricing_group_id=node.get("pricingGroupId"),
        rule_group_id=node.get("ruleGroupId"),
        term_schedule_id=node.get("termScheduleId"),
        term_time_only=node.get("termTimeOnly"),
        discounts=node.get("discounts") or [],
        product_bookings=node.get("productBookings") or [],
        package_bookings=node.get("packageBookings") or [],
        funding_entitlements=node.get("fundingEntitlements"),
        public_funding=_parse_public_funding(node.get("publicFunding")),
        public_funding_settings=_parse_public_funding_settings(
            node.get("publicFundingSettings")
        ),
        behaviors=_parse_behaviors(node.get("behaviors")),
        plan_states=_parse_plan_states(node.get("planStates")),
        session_bookings=_parse_session_bookings(node.get("sessionBookings")),
        plan_parts=[_parse_plan_part(p) for p in parts] if isinstance(parts, list) else [],
        raw=node,
    )


# Public alias: the REST plan-write action reuses this parser so the enriched
# plan shape is modelled in exactly one place.
def parse_plan(node: Any) -> Plan:
    """Parse a single plan node into a Plan."""
    return _parse_plan(node)


def parse_response(body: dict) -> list[Plan]:
    """Map a raw GraphQL response body into Plan objects.

    The exact path to `plans` depends on the captured query. This assumes the
    plans array sits somewhere under `data`; adjust _extract_plans once the real
    query is in place.
    """
    if not isinstance(body, dict):
        return []
    plans_node = _extract_plans(body.get("data") or {})
    if not isinstance(plans_node, list):
        return []
    return [_parse_plan(p) for p in plans_node]


def _extract_plans(data: dict) -> Any:
    """Locate the plans array within `data`.

    The captured response is `{ "plans": [...], "sessions": [...], ... }`, but
    it may be nested under a wrapper field (e.g. data.<something>.plans) once you
    see the real query. Handle both: direct `data.plans`, else search one level
    down for a dict containing a `plans` list.
    """
    if isinstance(data.get("plans"), list):
        return data["plans"]
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("plans"), list):
            return value["plans"]
    return None


def summarise(plans: list[Plan]) -> list[dict]:
    """A flat, human-scannable row per plan -- the operationally useful bits."""
    rows = []
    for p in plans:
        rows.append(
            {
                "planId": p.id,
                "childId": p.child_id,
                "from": p.from_,
                "to": p.to,
                "openEnded": p.is_open_ended,
                "billingScheme": p.billing_scheme,
                "monthlyEstimate": p.monthly_estimate,
                "sessionDays": [b.day for b in p.session_bookings],
                "planPartCount": len(p.plan_parts),
            }
        )
    return rows


def run(
    child_id: str,
    client: GraphQLClient | None = None,
) -> list[Plan]:
    """Fetch and parse the full plan structure for one child.

    Args:
        child_id: the Famly child ID.
        client: optional client, mainly for tests or reuse across calls.

    Returns:
        Parsed Plan objects, in the order the API returned them.

    NOTE: if the captured query needs more than childId (e.g. an
    institutionSetId), add it to `variables` below and to the CLI in cli.py.
    """
    client = client or GraphQLClient()

    body = client.execute(
        query_path=QUERY_PATH,
        variables={"childId": child_id},
        operation_name=OPERATION_NAME,
    )

    return parse_response(body)
