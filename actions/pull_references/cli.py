"""CLI wiring for the pull-references maintenance command.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Runs actions/pull_sessions + actions/pull_groups + actions/pull_products
together -- see runner.py's module docstring. Each is still individually
runnable via its own `pull-sessions` / `pull-groups` / `pull-products` command.
"""

import argparse

from . import runner

NAME = "pull-references"
HELP = "Run the sessions + groups + products reference-data pulls together (weekly cron)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pull and report what every source would change, without writing "
            "any catalogue file or backup"
        ),
    )
    parser.add_argument(
        "--institution",
        action="append",
        metavar="CODE",
        help=(
            "Restrict every pull to this site code (from catalogue.json). "
            "Repeatable, e.g. --institution HDCITY --institution HDCW. "
            "Omit to pull every configured institution (default)."
        ),
    )


def handle(args: argparse.Namespace):
    outcome = runner.pull_all_references(
        institutions=args.institution, dry_run=args.dry_run
    )
    return runner.summary_payload(outcome, dry_run=args.dry_run)
