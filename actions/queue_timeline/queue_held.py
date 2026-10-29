"""CLI wiring for queue-held. Logic lives in worker.py."""

import argparse

from . import worker

NAME = "queue-held"
HELP = "List held queue jobs: deal id, child id, job id, reason code, age (read-only; ids and codes only)"


def add_args(parser: argparse.ArgumentParser) -> None:
    """No arguments."""


def handle(args: argparse.Namespace):
    return worker.held_list()
