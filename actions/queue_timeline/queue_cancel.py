"""CLI wiring for queue-cancel. Logic lives in worker.py."""

import argparse

from . import worker

NAME = "queue-cancel"
HELP = "Cancel a queued job (never deletes anything in Famly)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_id", metavar="job", help="The job id")


def handle(args: argparse.Namespace):
    return worker.cancel(args.job_id)
