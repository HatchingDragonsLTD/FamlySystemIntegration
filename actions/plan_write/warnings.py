"""Plan validation warnings.

Kept deliberately separate from the runner because warnings are the only signal
that a plan write was given bad input: the endpoint returns HTTP 200 whether or
not the plan makes sense. Nothing here does I/O or touches argparse.

Warnings arrive inside the response's `behaviors[]` array, as the entry whose
`id` is "ShowPlanWarnings":

    {"id": "ShowPlanWarnings",
     "payload": {"warnings": [{title, message, severity, error, metadata}]}}

Two kinds of warning matter differently:

* KNOWN     -- seen before and understood; classified and handed back.
* UNTRACKED -- never seen before. Escalated through `notify()` so it cannot
               pass silently. Today that logs loudly to stderr; a future
               integration replaces the notifier without callers changing.
"""

import sys
from dataclasses import dataclass, field
from typing import Any, Callable

# The behaviors[] entry that carries validation warnings.
WARNINGS_BEHAVIOR_ID = "ShowPlanWarnings"

NOTIFY_PREFIX = "UNHANDLED PLAN WARNING"


@dataclass
class PlanWarning:
    """One warning from the ShowPlanWarnings payload."""

    title: str | None = None
    message: str | None = None
    severity: str | None = None
    error: str | None = None
    metadata: Any | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class KnownWarning:
    """A warning we have seen before and know how to talk about.

    Matched on the `error` code when we have one, and/or on a case-insensitive
    substring of the title. Both are optional so an entry can be keyed on
    whichever the server actually populates.
    """

    key: str
    description: str
    error_codes: tuple[str, ...] = ()
    title_contains: tuple[str, ...] = ()

    def matches(self, warning: PlanWarning) -> bool:
        if warning.error and warning.error in self.error_codes:
            return True
        title = (warning.title or "").lower()
        return any(fragment in title for fragment in self.title_contains)


# --------------------------------------------------------------------------- #
# Registry of warnings we understand.
#
# Seeded with the funding mismatch case. NOTE: `error_codes` here is a guess
# until a real response is captured -- the title match is what will fire in the
# meantime. When you see the real payload, put the exact `error` string in and
# tighten `title_contains`. Anything not matched is escalated, so an incomplete
# entry fails loudly rather than silently.
# --------------------------------------------------------------------------- #
KNOWN_WARNINGS: tuple[KnownWarning, ...] = (
    KnownWarning(
        key="funding_mismatch",
        description=(
            "Claimed public funding does not line up with the sessions booked "
            "on the plan (hours/minutes or funded bookings disagree)."
        ),
        error_codes=("FundingMismatch",),
        title_contains=("funding",),
    ),
)


@dataclass
class ClassifiedWarning:
    """A warning plus what we know about it."""

    warning: PlanWarning
    known: KnownWarning | None = None

    @property
    def is_known(self) -> bool:
        return self.known is not None

    @property
    def key(self) -> str:
        return self.known.key if self.known else "UNTRACKED"


def _parse_warning(node: Any) -> PlanWarning:
    """Parse one warning defensively -- any field may be missing."""
    if not isinstance(node, dict):
        return PlanWarning(raw={"value": node})
    return PlanWarning(
        title=node.get("title"),
        message=node.get("message"),
        severity=node.get("severity"),
        error=node.get("error"),
        metadata=node.get("metadata"),
        raw=node,
    )


def extract_warnings(body: Any) -> list[PlanWarning]:
    """Pull the ShowPlanWarnings warnings out of a parsed plan response.

    Returns an empty list when the behaviour is absent or malformed -- never
    raises, so a warning-shaped surprise cannot break a write.
    """
    if not isinstance(body, dict):
        return []

    # The plan may sit at the top level or under a "plan" wrapper.
    candidates = [body]
    plan = body.get("plan")
    if isinstance(plan, dict):
        candidates.append(plan)

    warnings: list[PlanWarning] = []
    for node in candidates:
        behaviors = node.get("behaviors")
        if not isinstance(behaviors, list):
            continue
        for behavior in behaviors:
            if not isinstance(behavior, dict):
                continue
            if behavior.get("id") != WARNINGS_BEHAVIOR_ID:
                continue
            payload = behavior.get("payload")
            if not isinstance(payload, dict):
                continue
            items = payload.get("warnings")
            if not isinstance(items, list):
                continue
            warnings.extend(_parse_warning(item) for item in items)

    return warnings


def notify(warning: PlanWarning) -> None:
    """SEAM: escalate a warning we do not recognise.

    Today: log loudly to stderr. Tomorrow: a Slack/HubSpot/email integration
    living in `integrations/`. Replace the delivery by calling
    `set_notifier(fn)` -- callers of `classify_all` never change.
    """
    print(
        f"{NOTIFY_PREFIX}: "
        f"error={warning.error!r} severity={warning.severity!r} "
        f"title={warning.title!r} message={warning.message!r} "
        f"metadata={warning.metadata!r}",
        file=sys.stderr,
    )


# The active notifier. Swap with set_notifier(); callers keep using classify_all.
_notifier: Callable[[PlanWarning], None] = notify


def set_notifier(fn: Callable[[PlanWarning], None]) -> None:
    """Replace the notifier (e.g. with an integrations/ delivery function)."""
    global _notifier
    _notifier = fn


def classify(warning: PlanWarning) -> ClassifiedWarning:
    """Match one warning against the registry."""
    for known in KNOWN_WARNINGS:
        if known.matches(warning):
            return ClassifiedWarning(warning=warning, known=known)
    return ClassifiedWarning(warning=warning, known=None)


def classify_all(
    warnings: list[PlanWarning],
    notifier: Callable[[PlanWarning], None] | None = None,
) -> list[ClassifiedWarning]:
    """Classify every warning, escalating the ones we do not recognise.

    Args:
        warnings: parsed warnings from `extract_warnings`.
        notifier: override the escalation target (tests, or an integration).

    Returns:
        One ClassifiedWarning per input, in order.
    """
    deliver = notifier or _notifier

    classified = []
    for warning in warnings:
        result = classify(warning)
        if not result.is_known:
            deliver(warning)
        classified.append(result)
    return classified


def format_warnings(classified: list[ClassifiedWarning]) -> str:
    """A human-readable block for printing -- prominent, not buried."""
    if not classified:
        return "No plan warnings returned."

    lines = [f"PLAN WARNINGS ({len(classified)}):"]
    for item in classified:
        w = item.warning
        marker = item.key if item.is_known else "UNTRACKED -- escalated"
        lines.append(f"  [{marker}] {w.severity or 'unknown severity'}: {w.title}")
        if w.message:
            lines.append(f"      {w.message}")
        if w.error:
            lines.append(f"      error={w.error}")
        if w.metadata:
            lines.append(f"      metadata={w.metadata}")
    return "\n".join(lines)
