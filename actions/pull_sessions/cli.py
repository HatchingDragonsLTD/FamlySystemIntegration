"""CLI wiring for the pull-sessions maintenance command.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Keeps argparse out of runner.py so the runner stays importable without a CLI
(and, eventually, from a scheduled job).
"""

import argparse

from . import runner

NAME = "pull-sessions"
HELP = "Pull live session UUIDs from Famly and refresh reference/session_catalogue.json"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pull and report what would change, without writing "
            "session_catalogue.json or a backup"
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
        backup = runner.write_catalogue(result)

    return runner.summary_payload(result, backup=backup, dry_run=args.dry_run)
