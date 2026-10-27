"""CLI wiring for onboard-dry-run: PHASE 1 of the onboarding pipeline, DRY RUN
ONLY. Makes no writes to Famly or HubSpot; prints JSON with ids, codes and
counts (no names, emails, phone numbers or dates of birth).

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
"""

import argparse

from . import runner

NAME = "onboard-dry-run"
HELP = (
    "DRY RUN: print what onboarding a HubSpot deal into Famly would do "
    "(child and contacts). Reads only; never writes"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", metavar="PATH",
                       help="Dry-run the deal ids in a file (digits only, one per line; blank lines "
                            "and # comments ignored). Needs --limit N or --all")
    group.add_argument("--deal-id", metavar="ID", help="Run one deal and print its plan")
    group.add_argument(
        "--deal-ids",
        metavar="A,B,C",
        help="Run several deals (comma-separated) and print a batch summary at the end",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="--file: only the first N ids")
    parser.add_argument("--all", action="store_true", help="--file: every id in the file")


def handle(args: argparse.Namespace):
    if args.file:
        from actions.onboard import batch

        batch.require_scope(args.limit, args.all)
        ids, _file_hash = batch.read_file(args.file)
        return runner.run(ids if args.all else ids[: args.limit])
    if args.deal_id:
        result = runner.run([args.deal_id])
        # One deal: the plan itself, without the batch wrapper.
        return {
            "mode": result["mode"],
            "writes": result["writes"],
            "allowlistEnforced": result["allowlistEnforced"],
            "associationLabels": result["associationLabels"],
            "statusBlock": result["statusBlocks"].get(result["plans"][0]["dealId"]),
            "plan": result["plans"][0],
        }

    ids = [part for part in args.deal_ids.split(",") if part.strip()]
    if not ids:
        raise SystemExit("--deal-ids needs at least one id")
    return runner.run(ids)
