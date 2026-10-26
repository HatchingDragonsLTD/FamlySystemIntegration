"""onboard-audit: READ-ONLY audit of children already in Famly against their HubSpot deals.
Prints counts and codes only; --out writes a CSV of ids and codes only. See runner.py."""

import argparse
import sys

from . import runner

NAME = "onboard-audit"
HELP = ("READ-ONLY: for deals whose child is already in Famly, report (as codes) what differs from "
        "HubSpot and whether the child has plans. Never writes; no names or values are printed")


def add_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", metavar="PATH",
                       help="A file of deal ids (digits only, one per line; blank lines and # comments ignored)")
    group.add_argument("--operations", action="store_true",
                       help="Read every deal in the Operations pipeline")
    parser.add_argument("--stage", action="append", metavar="NAME",
                        help="--operations: only deals in this stage label (repeatable)")
    parser.add_argument("--out", metavar="PATH", help="Also write a CSV of ids and codes")


def handle(args: argparse.Namespace):
    from actions.urn_lookup import runner as urn_runner
    from core.rest_client import RestClient

    from actions.onboard import batch

    if args.stage and not args.operations:
        print("--stage only applies with --operations", file=sys.stderr)
        raise SystemExit(3)
    deal_ids = None
    if args.file:
        deal_ids, _hash = batch.read_file(args.file)
        print(f"{len(deal_ids)} deal id(s) read", file=sys.stderr)

    hubspot = runner.ReadOnlyHubSpot()
    gql = runner.ReadOnlyGraphQL(urn_runner._public_client())
    rest = runner.ReadOnlyRest(RestClient())
    summary, rows = runner.run(hubspot, gql, rest, deal_ids=deal_ids, operations=bool(args.operations),
                               stage_names=args.stage)
    if args.out:
        runner.write_csv(args.out, rows)
        summary["csv"] = {"rows": len(rows)}
    return summary
