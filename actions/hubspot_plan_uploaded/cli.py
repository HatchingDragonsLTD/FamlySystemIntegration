"""CLI wiring for hubspot-set-plan-uploaded: a one-off that sets `famly_plan_uploaded` on a list of
deals. Dry run unless --confirm. Counts, ids and codes only."""

import argparse
import json
import sys

from core.hubspot import HubSpotDealWriter

from . import runner

NAME = "hubspot-set-plan-uploaded"
HELP = ("One-off: set famly_plan_uploaded to true or false on the deals in a file (max 60). "
        "Dry run (reads only) unless --confirm")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", required=True, metavar="PATH",
                        help="Deal ids (digits, one per line) or the onboard-audit CSV (dealId column)")
    parser.add_argument("--value", required=True, choices=runner.VALUES,
                        help="The value to set: true or false")
    parser.add_argument("--confirm", action="store_true",
                        help="Write the deals whose value differs (otherwise a dry run)")


def handle(args: argparse.Namespace):
    try:
        with open(args.file, encoding="utf-8-sig") as handle:
            ids = runner.parse_deal_ids(handle.read())
        result = runner.run(HubSpotDealWriter(), ids, args.value, args.confirm)
    except OSError:
        print("cannot read the file; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    except runner.FileRefused as exc:
        print(f"refused: {exc.code}; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    if result["mode"] == "stopped":
        print(json.dumps(result, indent=2))
        raise SystemExit(3)
    if result.get("results", {}).get(runner.REJECTED) or result.get("results", {}).get(runner.READ_FAILED):
        print(json.dumps(result, indent=2))
        raise SystemExit(1)
    return result
