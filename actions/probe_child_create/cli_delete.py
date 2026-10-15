"""CLI wiring for probe-child-delete: delete named TEST children (at most 2).

Companion to probe-child-create (use it after `--no-delete`). Each id is re-read
via the internal summary and must show isTestChild true; one delete request per
id, never `siteIds`. See runner.py.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
"""

import argparse
import sys

from actions.pull_roster.runner import _public_client
from core.rest_client import RestClient

from . import runner

NAME = "probe-child-delete"
HELP = (
    "ONE-OFF: delete up to 2 named TEST children, each only if its summary shows "
    "isTestChild true (needs --confirm)"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--child-ids", required=True, metavar="A,B",
        help=f"Comma-separated child ids, at most {runner.MAX_CHILDREN}",
    )
    parser.add_argument("--confirm", action="store_true", help="Required")


def handle(args: argparse.Namespace):
    try:
        ids = runner.parse_child_ids(args.child_ids)
        result = runner.run_delete(ids, args.confirm, RestClient(), _public_client())
    except runner.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {
        "deleted": result.deleted,
        "notDeletedDeleteByHand": result.undeleted,
        "writeRequests": result.writes,
    }
