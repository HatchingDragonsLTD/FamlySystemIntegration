"""CLI wiring for onboard-worker: process queued onboarding jobs.

The same shape as `queue-worker` (actions/queue_timeline): `--once` recovers any
job caught mid-write, runs at most one job and exits; without it the worker
polls (what a systemd unit would run on the server). Prints codes and ids only.
"""

import argparse
import logging

from . import worker

NAME = "onboard-worker"
HELP = "Run the onboarding worker: build queued deals' child and contacts in Famly (see README)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--once", action="store_true",
        help="Recover any in-flight job, run at most one job, and exit",
    )
    parser.add_argument(
        "--poll", type=float, default=worker.POLL_SECONDS,
        help=f"Seconds between polls when the queue is empty (default {worker.POLL_SECONDS})",
    )


def handle(args: argparse.Namespace):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.once:
        recovered = worker.recover()
        return {"recovered": recovered, **worker.run_once()}
    worker.run_forever(args.poll)
    return {"stopped": True}
