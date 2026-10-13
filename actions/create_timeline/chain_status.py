"""CLI wiring for chain-status. Logic lives in maintenance.py."""

import argparse

from . import maintenance

NAME = "chain-status"
HELP = "List active create_timeline chains (optionally for one child)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "child_id",
        nargs="?",
        metavar="childId",
        help="Only this child's chain. Omit to list every active chain.",
    )


def handle(args: argparse.Namespace):
    return maintenance.status(args.child_id)
