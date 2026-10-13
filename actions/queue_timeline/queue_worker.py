"""CLI wiring for queue-worker: the process the queue's systemd unit runs."""

import argparse
import logging

from . import worker

NAME = "queue-worker"
HELP = "Run the queue worker: build queued timelines unattended (see README)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--once",
        action="store_true",
        help="Recover any running job, run at most one job, and exit (for testing)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=worker.POLL_SECONDS,
        help=f"Seconds to wait between polls when the queue is empty (default {worker.POLL_SECONDS})",
    )


def handle(args: argparse.Namespace):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.once:
        recovered = worker.recover_running()
        return {"recovered": recovered, "ran": worker.run_once(), "mode": worker.mode()}
    worker.run_forever(args.poll)
    return {"stopped": True}
