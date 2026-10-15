"""CLI wiring for probe-child-create: a ONE-OFF probe that creates two TEST
children, inspects them, and deletes them. See runner.py for the guards.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
"""

import argparse
import sys

from actions.pull_roster.runner import _public_client
from core.client import GraphQLClient
from core.rest_client import RestClient

from . import runner

NAME = "probe-child-create"
HELP = (
    "ONE-OFF PROBE: create two AAProbe TEST children at HDCW, inspect them, then "
    "delete them (needs --confirm)"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            f"Required. Allows up to {runner.MAX_CHILDREN} test children to be created "
            f"and {runner.MAX_WRITES} write requests in total"
        ),
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help=(
            "Skip the delete step. The created test children stay in Famly: their ids "
            "are printed and MUST be deleted afterwards (see probe-child-delete)"
        ),
    )


def handle(args: argparse.Namespace):
    try:
        result = runner.run_probe(
            args.confirm, RestClient(), _public_client(), GraphQLClient(),
            no_delete=args.no_delete,
        )
    except runner.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {
        "created": result.created,
        "deleted": result.deleted,
        "notDeletedDeleteByHand": result.undeleted,
        "writeRequests": result.writes,
        "birthdayFormatAccepted": result.birthday_format,
    }
