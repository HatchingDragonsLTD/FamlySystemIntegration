"""CLI wiring for queue-list. Logic lives in worker.py."""

import argparse

from . import worker

NAME = "queue-list"
HELP = "List queued jobs, optionally of one status (queue-list --status held)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--status",
        default=None,
        help=(
            "queued, running, done, dry_done, held, failed or cancelled. "
            "Omit for every job."
        ),
    )


def handle(args: argparse.Namespace):
    return worker.list_jobs(args.status)
