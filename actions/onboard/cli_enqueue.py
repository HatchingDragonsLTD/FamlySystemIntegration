"""CLI wiring for onboard-enqueue: queue a deal for the phase-2 onboarding job.

`python main.py onboard-enqueue --deal-id <id>` records a job and returns; the
worker (`onboard-worker`) does the work later. Refuses when ONBOARD_MODE is off.
No webhook triggers this yet. Prints codes and ids only.
"""

import argparse
import sys

from . import config, store

NAME = "onboard-enqueue"
HELP = "Queue a HubSpot deal for onboarding into Famly (child and contacts); refuses when ONBOARD_MODE=off"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deal-id", required=True, metavar="ID", help="The HubSpot deal id")


def handle(args: argparse.Namespace):
    mode = config.mode()
    if mode == config.MODE_OFF:
        print("ONBOARD_MODE is off; nothing was queued. Set ONBOARD_MODE=dry or live.",
              file=sys.stderr)
        raise SystemExit(3)
    deal_id = str(args.deal_id).strip()
    if not deal_id.isdigit():
        print("--deal-id must be a HubSpot deal id (digits only); nothing was queued.",
              file=sys.stderr)
        raise SystemExit(3)
    result = store.enqueue(deal_id, mode)
    return {
        "outcome": result.outcome,
        "jobId": result.job.id,
        "status": result.job.status,
        "mode": mode,
    }
