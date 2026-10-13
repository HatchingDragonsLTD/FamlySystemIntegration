"""CLI wiring for queue-pause. Logic lives in worker.py."""

import argparse

from . import worker

NAME = "queue-pause"
HELP = "Pause the queue: no commit is made while it is paused"


def add_args(parser: argparse.ArgumentParser) -> None:
    pass


def handle(args: argparse.Namespace):
    return worker.pause()
