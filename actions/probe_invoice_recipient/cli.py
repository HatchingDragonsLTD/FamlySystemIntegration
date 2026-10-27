"""probe-invoice-recipient: ONE-OFF WRITE PROBE on a verified TEST child (same narrow exception as
probe-contact-reference). Needs --confirm. Prints booleans, counts and ids only.
DELETE this package and its main.py line once the answer is in docs/status.md."""

import argparse
import sys

from actions.pull_roster.runner import _public_client
from core.rest_client import RestClient

from . import runner

NAME = "probe-invoice-recipient"
HELP = ("ONE-OFF WRITE PROBE: link a TEST contact to a TEST bill payer as an invoice recipient (twice), "
        "then set a HUBSPOT reference on the contact (test child only; needs --confirm)")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--child-id", required=True, metavar="ID", help="The TEST child's id")
    parser.add_argument("--contact-id", metavar="ID", default=None,
                        help="Reuse this AAProbe test contact (it must be on the test child) instead of "
                             "creating one: step 1 is skipped and is not a write")
    parser.add_argument("--confirm", action="store_true",
                        help=f"Required. Creates 1 test contact and 1 test bill payer "
                             f"({runner.MAX_WRITES} write requests at most)")


def handle(args: argparse.Namespace):
    try:
        result = runner.run_probe(args.child_id, args.confirm, RestClient(), _public_client(),
                                  contact_id=args.contact_id)
    except runner.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {"rows": result.rows, "writes": result.writes, "contactId": result.contact_id,
            "billPayerId": result.payer_id, "stopped": result.stopped}
