"""CLI wiring for the pull-products maintenance command.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Mirrors actions/pull_sessions/cli.py. Writes TWO files -- see runner.py's
module docstring: the flat products_catalogue.json display map, and (for
institutions that resolve cleanly) institution_defaults.json's addonProducts.
"""

import argparse

from . import runner

NAME = "pull-products"
HELP = (
    "Pull live product ids from Famly, refresh reference/products_catalogue.json, "
    "and resolve each institution's two fixed add-on products into "
    "reference/institution_defaults.json"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Pull and report what would change, without writing "
            "products_catalogue.json, institution_defaults.json, or a backup"
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
        runner.write_extras(result)

    return runner.summary_payload(result, backup=backup, dry_run=args.dry_run)
