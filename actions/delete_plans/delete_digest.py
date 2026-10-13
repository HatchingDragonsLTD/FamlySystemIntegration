"""CLI wiring for delete-digest -- part of the TEMPORARY delete_plans action.

Posts the delete_plans Slack digest: every `delete_log` row not yet reported, one
section per category, with any allowlist child that sent no request listed as
"Not received". Names are fetched here, at digest time (3-second lookup, falling
back to the bare id); they are never stored. Logic lives in digest.py.

    python main.py delete-digest
    python main.py delete-digest --if-quiet 10     # for cron, every 5 minutes

`--if-quiet N` posts only if there are unreported rows and the newest is at least
N minutes old, so a run still arriving is not cut in half.
"""

import argparse
import dataclasses

from . import digest

NAME = "delete-digest"
HELP = "Post the delete_plans Slack digest of unreported rows (TEMPORARY action)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--if-quiet",
        type=float,
        default=None,
        metavar="MINUTES",
        help="Post only if there are unreported rows and the newest is at least this old",
    )


def handle(args: argparse.Namespace):
    return dataclasses.asdict(digest.post_digest(names=True, if_quiet_minutes=args.if_quiet))
