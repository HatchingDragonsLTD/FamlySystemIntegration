"""CLI wiring for chain-cancel. Logic lives in maintenance.py."""

import argparse

from . import maintenance

NAME = "chain-cancel"
HELP = "Close a child's active create_timeline chain record and post what exists in Famly (deletes nothing)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("child_id", metavar="childId", help="The Famly child id")


def handle(args: argparse.Namespace):
    return maintenance.cancel(args.child_id)
