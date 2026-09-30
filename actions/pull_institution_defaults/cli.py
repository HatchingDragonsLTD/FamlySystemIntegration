"""CLI wiring for the pull-institution-defaults maintenance command.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Mirrors actions/pull_groups/cli.py.
"""

import argparse

from . import runner

NAME = "pull-institution-defaults"
HELP = (
    "Pull live pricing rule group + billing-profile defaults from Famly and "
    "refresh reference/institution_defaults.json"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pull and report what would change, without writing "
            "institution_defaults.json or a backup"
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
