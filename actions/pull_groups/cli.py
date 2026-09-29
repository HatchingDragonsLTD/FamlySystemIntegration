"""CLI wiring for the pull-groups maintenance command.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Mirrors actions/pull_sessions/cli.py.
"""

import argparse

from . import runner

NAME = "pull-groups"
HELP = "Pull live group/room ids from Famly and refresh reference/groups_catalogue.json"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pull and report what would change, without writing "
            "groups_catalogue.json or a backup"
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
