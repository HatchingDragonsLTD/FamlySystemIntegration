"""CLI wiring for probe-contact-create: a ONE-OFF probe that creates a few test
contacts on ONE verified test child. See runner.py for the guards.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
"""

import argparse
import sys

from actions.pull_roster.runner import _public_client
from core.rest_client import RestClient

from . import runner

NAME = "probe-contact-create"
HELP = (
    "ONE-OFF PROBE: create AAProbe test contacts on one verified TEST child via "
    "contacts.create (needs --confirm; refuses any other child)"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--child-id", required=True, metavar="ID", help="The TEST child's id")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=f"Required. Allows up to {runner.MAX_CONTACTS} test contacts to be created",
    )


def handle(args: argparse.Namespace):
    try:
        result = runner.run_probe(args.child_id, args.confirm, RestClient(), _public_client())
    except runner.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {
        "createdContactIds": [c["id"] for c in result.created],
        "acceptedFormats": result.accepted,
        "rejectedCount": len(result.rejected),
        "createRequests": result.attempts,
    }
