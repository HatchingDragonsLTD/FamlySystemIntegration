"""CLI wiring for chain-resume. Logic lives in maintenance.py."""

import argparse

from . import maintenance

NAME = "chain-resume"
HELP = "Post the next plan of a stalled create_timeline chain, after checking the last plan in Famly"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("child_id", metavar="childId", help="The Famly child id")


def handle(args: argparse.Namespace):
    return maintenance.resume(args.child_id)
