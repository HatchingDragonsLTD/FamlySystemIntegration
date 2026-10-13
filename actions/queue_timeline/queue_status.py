"""CLI wiring for queue-status. Logic lives in worker.py."""

import argparse

from . import worker

NAME = "queue-status"
HELP = "Show the queue: mode, pause flag, counters and job counts"


def add_args(parser: argparse.ArgumentParser) -> None:
    pass


def handle(args: argparse.Namespace):
    return worker.status()
