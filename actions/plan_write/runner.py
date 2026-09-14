"""Plan write action: preview and commit against the REST plans endpoint.

CLI-agnostic and server-importable -- no argparse, no printing of results.
Call `preview()` to have the server compute a plan without persisting it, and
`commit()` to actually write one.

Preview and commit are the SAME request; only the `preview` query param differs.
That makes an accidental write easy, so `commit()` is guarded (see below).

The enriched response is parsed with the Plan model from the read-child-plans
action rather than a parallel copy, so the plan shape lives in one place.

IMPORTANT: the endpoint returns HTTP 200 even when the plan is invalid. The
warnings in `behaviors[]` are the only signal, so every call here extracts them.
"""

from dataclasses import dataclass, field
from typing import Any

from actions.read_child_plans.runner import Plan, parse_plan
from actions.plan_write import warnings as plan_warnings
from core.rest_client import RestClient

# Path under the configured REST base URL (which already ends in /api).
PLANS_PATH = "/v2/plans"


class PlanCommitRefused(RuntimeError):
    """A commit was blocked by a guard before any write was attempted."""


@dataclass
class ParsedPlanResult:
    """What came back from a preview or a commit."""

    plan: Plan | None = None
    warnings: list[plan_warnings.ClassifiedWarning] = field(default_factory=list)
    committed: bool = False
    raw: Any = None

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    @property
    def untracked_warnings(self) -> list[plan_warnings.ClassifiedWarning]:
        """Warnings the registry did not recognise -- these were escalated."""
        return [w for w in self.warnings if not w.is_known]


def _plan_node(body: Any) -> Any:
    """Locate the plan object in a response.

    The response may be the plan itself or wrapped in a `plan` key; both are
    handled rather than guessed at.
    """
    if isinstance(body, dict) and isinstance(body.get("plan"), dict):
        return body["plan"]
    return body


def _parse_result(body: Any, *, committed: bool) -> ParsedPlanResult:
    """Parse a response into a plan plus its classified warnings."""
    node = _plan_node(body)

    return ParsedPlanResult(
        plan=parse_plan(node) if isinstance(node, dict) else None,
        # Warnings are read from the whole body: extract_warnings looks at the
        # top level and inside a `plan` wrapper.
        warnings=plan_warnings.classify_all(plan_warnings.extract_warnings(body)),
        committed=committed,
        raw=body,
    )


def _post_plan(
    plan_body: dict,
    version: int,
    *,
    preview_only: bool,
    client: RestClient | None = None,
) -> Any:
    """POST the plan body with the preview flag set as asked."""
    client = client or RestClient()

    return client.post(
        PLANS_PATH,
        params={
            # The API takes these as strings in the captured requests.
            "preview": "true" if preview_only else "false",
            "version": str(version),
        },
        json_body=plan_body,
    )


def child_id_of(plan_body: Any) -> str | None:
    """Read `plan.childId` out of a request body, defensively."""
    if not isinstance(plan_body, dict):
        return None
    plan = plan_body.get("plan")
    if not isinstance(plan, dict):
        return None
    child_id = plan.get("childId")
    return child_id if isinstance(child_id, str) else None


def preview(
    plan_body: dict,
    version: int,
    client: RestClient | None = None,
) -> ParsedPlanResult:
    """Have the server compute the plan without persisting it.

    Never writes: `preview=true`.

    Args:
        plan_body: the `{"plan": {...}}` body, e.g. from builder.build_plan_body.
        version: the plan version the request targets.
        client: optional client, mainly for tests or reuse across calls.

    Returns:
        The computed plan and its classified warnings.
    """
    body = _post_plan(plan_body, version, preview_only=True, client=client)
    return _parse_result(body, committed=False)


def commit(
    plan_body: dict,
    version: int,
    *,
    confirm: bool,
    allowed_child_ids: set[str],
    client: RestClient | None = None,
) -> ParsedPlanResult:
    """Persist a plan. GUARDED -- this is the only function here that writes.

    Order of operations, and none of it is skippable:

      1. Refuse unless `confirm` is True.
      2. Refuse unless the body's childId is in `allowed_child_ids`. This is the
         test-child guard: it keeps an experimental write off a real child.
      3. Run `preview()` first and surface its warnings.
      4. Only then POST with `preview=false`.

    The preview in step 3 is not skipped even when it returns warnings: warnings
    are advisory, and the caller decided to commit. They are surfaced (and any
    untracked one escalated through warnings.notify) and returned on the result
    so a caller can still act on them.

    Args:
        plan_body: the `{"plan": {...}}` body to write.
        version: the plan version the request targets.
        confirm: must be True. Exists so a write is never one typo away.
        allowed_child_ids: the child IDs this commit is permitted to touch.
        client: optional client, mainly for tests or reuse across calls.

    Returns:
        The committed plan and its classified warnings.

    Raises:
        PlanCommitRefused: if a guard blocks the write. Nothing is sent.
    """
    if confirm is not True:
        raise PlanCommitRefused(
            "Commit refused: confirm=True is required to write a plan. "
            "Run a preview first if you did not mean to commit."
        )

    child_id = child_id_of(plan_body)
    if child_id is None:
        raise PlanCommitRefused(
            "Commit refused: the plan body has no childId, so the test-child "
            "guard cannot be checked."
        )

    if not allowed_child_ids:
        raise PlanCommitRefused(
            f"Commit refused: no allowed child IDs were given, so writing to "
            f"child {child_id!r} is not permitted."
        )

    if child_id not in allowed_child_ids:
        raise PlanCommitRefused(
            f"Commit refused: child {child_id!r} is not in the allowed set "
            f"{sorted(allowed_child_ids)!r}. The first real commit must target "
            f"a disposable test child."
        )

    # Step 3: preview before writing. Warnings are extracted and any untracked
    # one is escalated by classify_all inside _parse_result.
    preview_result = preview(plan_body, version, client=client)

    # Step 4: the actual write.
    body = _post_plan(plan_body, version, preview_only=False, client=client)
    result = _parse_result(body, committed=True)

    # Carry forward any preview warning the commit response did not repeat, so
    # nothing seen before the write is lost.
    if preview_result.warnings and not result.warnings:
        result.warnings = preview_result.warnings

    return result
