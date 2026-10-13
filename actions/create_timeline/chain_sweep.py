"""CLI wiring for chain-sweep, for cron. Logic lives in maintenance.py."""

import argparse

from . import maintenance

NAME = "chain-sweep"
HELP = (
    "Expire create_timeline chains whose pending preview expired, and flag "
    "chains stuck after a recorded commit (run from cron)"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    pass


def handle(args: argparse.Namespace):
    return maintenance.sweep()
