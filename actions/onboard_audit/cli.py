"""onboard-audit: READ-ONLY audit of children already in Famly against their HubSpot deals.
Prints counts and codes only; --out writes a CSV of ids and codes only. See runner.py."""

import argparse
import sys

from . import runner, scope as scope_mod

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
    scope_mod.add_arguments(parser)
    parser.add_argument("--out", metavar="PATH", help="Also write a CSV of ids and codes")
    parser.add_argument("--codes", metavar="CODE1,CODE2",
                        help="--out: only the rows that have ANY of these codes (a base code or a sub-code)")
    parser.add_argument("--include-left", action="store_true",
                        help="Also read and compare the plans of child-only stages (Left Nursery); "
                             "skipped by default")


def handle(args: argparse.Namespace):
    from actions.urn_lookup import runner as urn_runner
    from core.rest_client import RestClient

    from actions.onboard import batch

    if args.codes and not args.out:
        print("--codes restricts the CSV: give --out too", file=sys.stderr)
        raise SystemExit(3)
    if args.stage and not args.operations:
        print("--stage only applies with --operations", file=sys.stderr)
        raise SystemExit(3)
    if (args.leave_after or args.all_dates) and not args.operations:
        print("--leave-after and --all-dates only apply with --operations", file=sys.stderr)
        raise SystemExit(3)
    try:
        scope = scope_mod.resolve(args.leave_after, args.all_dates) if args.operations else None
    except scope_mod.ScopeError as exc:
        print(f"{exc}; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    deal_ids = None
    if args.file:
        deal_ids, _hash = batch.read_file(args.file)
        print(f"{len(deal_ids)} deal id(s) read", file=sys.stderr)

    hubspot = runner.ReadOnlyHubSpot()
    gql = runner.ReadOnlyGraphQL(urn_runner._public_client())
    rest = runner.ReadOnlyRest(RestClient())
    summary, rows = runner.run(hubspot, gql, rest, deal_ids=deal_ids, operations=bool(args.operations),
                               stage_names=args.stage, include_left=bool(args.include_left), scope=scope)
    if args.out:
        written = runner.write_csv(args.out, rows, only_codes=(args.codes or "").split(","))
        summary["csv"] = {"rows": written}
    return summary
