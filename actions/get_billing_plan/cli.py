"""CLI wiring for the child-plans action.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Keeps argparse out of runner.py so the runner stays server-importable.
"""

import argparse

from . import runner

NAME = "child-plans"
HELP = "Fetch the full plan structure for a child"

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
        "--format",
        choices=("summary", "full"),
        default="summary",
        help=(
            "summary: one flat row per plan (dates, billing scheme, estimate, "
            "session days, part count). full: every parsed field, including "
            "session bookings, prices, funding and plan parts."
        ),
    )
    parser.add_argument(
        "--sort",
        choices=sorted(SORT_KEYS),
        help="Sort the plans. Omit to keep the order the API returned.",
    )


def handle(args: argparse.Namespace):
    # Sort while these are still Plan objects. Never sort after summarise() --
    # that returns plain dicts and getattr() would fail.
    plans = runner.run(args.child_id)

    if args.sort:
        attr = SORT_KEYS[args.sort]
        # Missing values sort last rather than blowing up on None comparison.
        plans.sort(key=lambda p: (getattr(p, attr) is None, getattr(p, attr) or ""))

    if args.format == "summary":
        return runner.summarise(plans)
    return plans
