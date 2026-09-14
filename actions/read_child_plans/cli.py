"""CLI wiring for the read-child-plans action.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Keeps argparse out of runner.py so the runner stays server-importable -- the
write path calls `runner.run()` directly to decide create-vs-edit.
"""

import argparse

from . import runner

NAME = "read-child-plans"
HELP = "Read a child's existing plans (REST)"

SORT_KEYS = {
    "from": "from_",
    "to": "to",
    "estimate": "monthly_estimate",
    "version": "version",
}


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "child_id",
        metavar="childId",
        help="The Famly child ID whose plans to fetch",
    )
    parser.add_argument(
        "--version",
        type=int,
        default=runner.DEFAULT_VERSION,
        help=f"Plans API version (default: {runner.DEFAULT_VERSION})",
    )
    parser.add_argument(
        "--format",
        choices=("summary", "full"),
        default="summary",
        help=(
            "summary: one flat row per plan (planId, version, dates, estimate, "
            "planPartIds, session count) -- what a create-vs-edit decision "
            "needs. full: every parsed field, plus the session/product "
            "reference lists."
        ),
    )
    parser.add_argument(
        "--sort",
        choices=sorted(SORT_KEYS),
        help="Sort the plans. Omit to keep the order the API returned.",
    )


def handle(args: argparse.Namespace):
    result = runner.run(args.child_id, version=args.version)

    if args.sort:
        attr = SORT_KEYS[args.sort]
        # Missing values sort last rather than blowing up on None comparison.
        result.plans.sort(
            key=lambda p: (getattr(p, attr) is None, getattr(p, attr) or "")
        )

    # Sort while these are still Plan objects. Never sort after summarise() --
    # that returns plain dicts and getattr() would fail.
    if args.format == "summary":
        return summary_payload(result)
    return result


def summary_payload(result: runner.ChildPlansResult) -> dict:
    """Summary rows plus the context needed to act on them."""
    return {
        "childId": result.child_id,
        "hasPlans": result.has_plans,
        "planCount": len(result.plans),
        # None when there are zero plans or more than one -- an ambiguous case
        # the caller has to resolve deliberately.
        "currentPlanId": result.current_plan.id if result.current_plan else None,
        "plans": runner.summarise(result),
    }
