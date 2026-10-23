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
    parser.add_argument("--deal-id", metavar="ID", help="The HubSpot deal id")
    parser.add_argument("--file", metavar="PATH",
                        help="A file of deal ids (digits only, one per line; blank lines and # "
                             "comments ignored). Needs --limit N or --all")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="--file: queue the first N eligible ids in file order")
    parser.add_argument("--all", action="store_true", help="--file: queue every eligible id")
    parser.add_argument("--skip-invalid", action="store_true",
                        help="--file: queue the valid ids even if some failed validation")
    parser.add_argument("--include-review", action="store_true",
                        help="--file: also queue deals whose latest job is needs_review, failed or held")


def handle(args: argparse.Namespace):
    mode = config.mode()
    if mode == config.MODE_OFF:
        print("ONBOARD_MODE is off; nothing was queued. Set ONBOARD_MODE=dry or live.",
              file=sys.stderr)
        raise SystemExit(3)
    if args.file:
        return _file(args, mode)
    if not args.deal_id:
        print("give --deal-id, or --file with --limit N or --all; nothing was queued", file=sys.stderr)
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


def _file(args: argparse.Namespace, mode: str):
    """Queue the ids of a file (see batch.py). Prints ids, counts and codes only."""
    import json

    from core.hubspot import HubSpotClient

    from . import batch

    if args.deal_id:
        print("use --deal-id or --file, not both; nothing was queued", file=sys.stderr)
        raise SystemExit(3)
    batch.require_scope(args.limit, args.all)
    ids, file_hash = batch.read_file(args.file)
    summary = batch.enqueue_file(
        HubSpotClient(), ids, file_hash, mode, limit=args.limit,
        skip_invalid=args.skip_invalid, include_review=args.include_review)
    if "blocked" in summary:
        print(json.dumps(summary, indent=2))
        raise SystemExit(3)
    return summary
