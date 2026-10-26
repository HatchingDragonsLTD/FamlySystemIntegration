"""probe-contact-reference: ONE-OFF WRITE PROBE on a verified TEST child (same narrow exception as
probe-contact-create). Needs --confirm. Prints booleans, counts, seconds and ids only.
DELETE this package and its main.py line once the answer is in docs/status.md."""

import argparse
import sys

from actions.pull_roster.runner import _public_client
from core.rest_client import RestClient

from . import runner

NAME = "probe-contact-reference"
HELP = ("ONE-OFF WRITE PROBE: store a HUBSPOT reference on a TEST contact, look it up, and see what "
        "an update does to existing records and references (test child only; needs --confirm)")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--child-id", required=True, metavar="ID", help="The TEST child's id")
    parser.add_argument("--confirm", action="store_true",
                        help=f"Required. Creates up to {runner.MAX_CONTACTS} test contacts "
                             f"({runner.MAX_WRITES} write requests at most)")


def handle(args: argparse.Namespace):
    try:
        result = runner.run_probe(args.child_id, args.confirm, RestClient(), _public_client())
    except runner.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {"rows": result.rows, "writes": result.writes, "createdContactIds": result.created_ids,
            "stopped": result.stopped}
