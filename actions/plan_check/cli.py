"""CLI wiring for plan-check: READ-ONLY, partial pre-check of the plan inputs of the deals in a file (see runner.py).
Prints counts and codes only."""

import argparse
import sys

NAME = "plan-check"
HELP = ("READ-ONLY: for the deals in a file (max 60), read each from HubSpot (GET only) and report as sub-codes "
        "which plan_preview validation rules its plan inputs would fail. Partial: says what it cannot see")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", metavar="PATH", required=True,
                        help="Deal ids (digits, one per line; # comments ignored) or the onboard-audit CSV "
                             "(dealId column); at most 60")


def handle(args: argparse.Namespace):
    from actions.hubspot_plan_uploaded import runner as ids_runner
    from actions.onboard_audit.runner import ReadOnlyHubSpot

    from . import runner

    try:
        with open(args.file, encoding="utf-8-sig") as handle:
            ids = ids_runner.parse_deal_ids(handle.read())
    except OSError:
        print("cannot read the file; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    except ids_runner.FileRefused as exc:
        print(f"refused: {exc.code}; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    return runner.run(ReadOnlyHubSpot(), ids)
