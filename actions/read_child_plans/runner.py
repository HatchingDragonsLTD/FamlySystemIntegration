"""Read a child's plans from the REST plans endpoint.

    GET {rest_base}/v2/plans/?version=<int>&childId=<id>&flexiblePackages=true

Importable directly by a future server or scheduler: call `run(child_id)` and
you get parsed objects back -- no CLI involvement. The write path uses this to
answer "does a plan already exist, and what are its id / version / planPartId?"
before choosing whether to create, edit, or add a second plan.

The response is deeply nested. The hierarchy modelled here:

    ChildPlansResult
      plans          -> list[Plan]
      sessions       -> list[SessionRef]   (sessionId -> title lookup)
      products       -> list[ProductRef]
      discount_presets, behaviors
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

Every field is parsed with .get() and Optional defaults, so a missing or renamed
field yields None rather than a crash. The untouched node is kept in `raw` on
the top-level Plan (and on the result) so anything not modelled is reachable.
"""

from dataclasses import dataclass, field
from typing import Any

from core.rest_client import RestClient

# Path under the configured REST base URL (which already ends in /api).
PLANS_PATH = "v2/plans/"

# The endpoint is versioned by query param, not by path.
DEFAULT_VERSION = 3


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
class PlanPartState:
    """One plan part's figures for a given plan state."""

    plan_part_id: str | None = None
    weekly_total: Any | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class PlanState:
    """A period of the plan. More than one means the rate changes mid-plan."""

    from_: str | None = None
    to: str | None = None
    plan_part_states: list[PlanPartState] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def weekly_total(self) -> float | None:
        """The plan's weekly total for this state: its parts added together.

        None when no part reports a numeric total, so a caller can tell
        "nothing reported" apart from a genuine zero.
        """
        totals = [
            p.weekly_total
            for p in self.plan_part_states
            if isinstance(p.weekly_total, (int, float))
            and not isinstance(p.weekly_total, bool)
        ]
        return float(sum(totals)) if totals else None


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

    @property
    def plan_part_ids(self) -> list[str]:
        """The plan's part IDs -- an edit needs these to target a part."""
        return [p.plan_part_id for p in self.plan_parts if p.plan_part_id]

    @property
    def session_booking_count(self) -> int:
        """How many sessions the plan books.

        Famly MIRRORS the same bookings at both levels: `plan.sessionBookings`
        and each `planPart.sessionBookings` describe the same sessions, so
        adding them together counts every session twice.

        The plan parts are the authoritative source, since that is where a
        booking actually lives. The plan-level list is the fallback for a plan
        that has no parts at all.
        """
        part_total = sum(len(p.session_bookings) for p in self.plan_parts)
        return part_total if self.plan_parts else len(self.session_bookings)


# --------------------------------------------------------------------------- #
# Lightweight reference types -- the lookup tables the response ships alongside
# the plans. Only the fields useful for naming things are pulled out; the whole
# node is kept in `raw`.
# --------------------------------------------------------------------------- #
@dataclass
class SessionRef:
    id: str | None = None
    title: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class ProductRef:
    id: str | None = None
    title: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class ChildPlansResult:
    child_id: str | None = None
    plans: list[Plan] = field(default_factory=list)
    sessions: list[SessionRef] = field(default_factory=list)
    products: list[ProductRef] = field(default_factory=list)
    discount_presets: dict = field(default_factory=dict)
    behaviors: list[str] = field(default_factory=list)
    raw: Any = None

    @property
    def has_plans(self) -> bool:
        """True when the child has at least one plan."""
        return bool(self.plans)

    @property
    def current_plan(self) -> Plan | None:
        """The plan, when there is exactly one.

        None when the child has no plans OR more than one -- in the ambiguous
        case the caller must choose deliberately rather than be handed a guess.
        """
        return self.plans[0] if len(self.plans) == 1 else None

    @property
    def session_titles(self) -> dict[str, str | None]:
        """sessionId -> title, for naming bookings in output."""
        return {s.id: s.title for s in self.sessions if s.id}


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


def _parse_plan_part_states(node: Any) -> list[PlanPartState]:
    if not isinstance(node, list):
        return []
    return [
        PlanPartState(
            plan_part_id=p.get("planPartId"),
            weekly_total=p.get("weeklyTotal"),
            raw=p,
        )
        for p in node
        if isinstance(p, dict)
    ]


def _parse_plan_states(node: Any) -> list[PlanState]:
    if not isinstance(node, list):
        return []
    return [
        PlanState(
            from_=s.get("from"),
            to=s.get("to"),
            plan_part_states=_parse_plan_part_states(s.get("planPartStates")),
            raw=s,
        )
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


def parse_plan(node: Any) -> Plan:
    """Parse a single plan node into a Plan.

    Public because the plan-write action parses the same plan shape out of its
    own responses -- the model lives here and only here.
    """
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
        plan_parts=[_parse_plan_part(p) for p in parts]
        if isinstance(parts, list)
        else [],
        raw=node,
    )


def _parse_sessions(node: Any) -> list[SessionRef]:
    """Session reference list. `title` is what names a booking in output."""
    if not isinstance(node, list):
        return []
    return [
        SessionRef(id=s.get("id"), title=s.get("title"), raw=s)
        for s in node
        if isinstance(s, dict)
    ]


def _parse_products(node: Any) -> list[ProductRef]:
    if not isinstance(node, list):
        return []
    return [
        ProductRef(id=p.get("id"), title=p.get("title"), raw=p)
        for p in node
        if isinstance(p, dict)
    ]


def parse_response(body: Any, child_id: str | None = None) -> ChildPlansResult:
    """Map the raw REST response into a ChildPlansResult."""
    if not isinstance(body, dict):
        return ChildPlansResult(child_id=child_id, raw=body)

    plans_node = body.get("plans")
    discount_presets = body.get("discountPresets")

    return ChildPlansResult(
        child_id=child_id,
        plans=[parse_plan(p) for p in plans_node] if isinstance(plans_node, list) else [],
        sessions=_parse_sessions(body.get("sessions")),
        products=_parse_products(body.get("products")),
        discount_presets=discount_presets if isinstance(discount_presets, dict) else {},
        behaviors=_parse_behaviors(body.get("behaviors")),
        raw=body,
    )


def summarise(result: ChildPlansResult) -> list[dict]:
    """A flat row per plan -- what a create-vs-edit decision needs.

    `sessionCount` counts the plan-part bookings (the two levels mirror each
    other, so they must not be added together).
    """
    rows = []
    for p in result.plans:
        rows.append(
            {
                "planId": p.id,
                "version": p.version,
                "childId": p.child_id,
                "from": p.from_,
                "to": p.to,
                "openEnded": p.is_open_ended,
                "billingScheme": p.billing_scheme,
                "monthlyEstimate": p.monthly_estimate,
                "planPartIds": p.plan_part_ids,
                "sessionCount": p.session_booking_count,
            }
        )
    return rows


def run(
    child_id: str,
    version: int = DEFAULT_VERSION,
    client: RestClient | None = None,
) -> ChildPlansResult:
    """Fetch and parse the plans for one child.

    Args:
        child_id: the Famly child ID.
        version: the plans API version (query param, not a path segment).
        client: optional client, mainly for tests or reuse across calls.

    Returns:
        A ChildPlansResult. `has_plans` answers whether the child has any, and
        `current_plan` gives the single plan when there is exactly one.
    """
    client = client or RestClient()

    body = client.get(
        PLANS_PATH,
        params={
            "version": version,
            "childId": child_id,
            # Sent as a string: this is a query param, not a JSON body.
            "flexiblePackages": "true",
        },
    )

    return parse_response(body, child_id=child_id)
