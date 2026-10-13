"""CLI wiring for queue-resume. Logic lives in worker.py."""

import argparse

from . import worker

NAME = "queue-resume"
HELP = "Resume a paused queue and clear its consecutive-failure count"


def add_args(parser: argparse.ArgumentParser) -> None:
    pass


def handle(args: argparse.Namespace):
    return worker.resume()
