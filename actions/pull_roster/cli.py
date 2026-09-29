"""CLI wiring for the pull-roster maintenance command.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
A SEPARATE command from `pull-references` (sessions/groups/products): this
pull is daily, targets a different Famly API endpoint, and writes a gitignored
SQLite cache rather than a JSON reference file -- see runner.py and store.py's
module docstrings.
"""

import argparse

from . import runner

NAME = "pull-roster"
HELP = (
    "Pull children/contacts/bill-payer ids from Famly's public API and refresh "
    "the local roster_cache.db (daily; see store.py for what it is NOT for)"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pull and report what would change, without writing "
            "roster_cache.db or a backup"
        ),
    )
    parser.add_argument(
        "--institution",
        action="append",
        metavar="CODE",
        help=(
            "Restrict the pull to this site code (from catalogue.json). "
            "Repeatable, e.g. --institution HDCITY --institution HDCW. "
            "Omit to pull every configured institution (default)."
        ),
    )


def handle(args: argparse.Namespace):
    result = runner.pull_all(institutions=args.institution)

    backup = None
    if not args.dry_run:
        backup = runner.write_store(result)

    return runner.summary_payload(result, backup=backup, dry_run=args.dry_run)
