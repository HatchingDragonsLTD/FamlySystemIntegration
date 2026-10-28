"""CLI wiring for hubspot-set-plan-status: backfill famly_plan_status = plan_created from Famly. Dry run unless
--confirm. Counts, ids and codes only."""

import argparse
import json
import sys

from actions.onboard_audit import scope as scope_mod
from core.hubspot import HubSpotDealWriter

from . import runner

NAME = "hubspot-set-plan-status"
HELP = ("Backfill famly_plan_status = plan_created where Famly shows the child has a plan and the deal's status is "
        "empty (never overwrites, never writes needs_attention, never clears a note; max 60 writes per run). "
        "Dry run (reads only) unless --confirm")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from-famly", action="store_true", required=True,
                        help="Required: the status is set from what Famly shows for the deal's famly_childid")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", metavar="PATH",
                        help="Deal ids (digits, one per line) or the onboard-audit CSV (dealId column); max 60")
    source.add_argument("--operations", action="store_true",
                        help="Every Operations deal in scope (Leave Date cutoff; at most 60 writes per run)")
    scope_mod.add_arguments(parser)
    parser.add_argument("--confirm", action="store_true",
                        help="Write plan_created where it applies (otherwise a dry run)")


def handle(args: argparse.Namespace):
    from actions.onboard_audit.runner import ReadOnlyRest
    from actions.read_child_plans import runner as plans_runner
    from core.rest_client import RestClient

    if (args.leave_after or args.all_dates) and not args.operations:
        print("--leave-after and --all-dates only apply with --operations; nothing was done", file=sys.stderr)
        raise SystemExit(3)
    try:
        writer = HubSpotDealWriter()
        rest = ReadOnlyRest(RestClient())
        report: dict = {}
        if args.operations:
            states = runner.operations_states(writer, scope_mod.resolve(args.leave_after, args.all_dates), report)
        else:
            with open(args.file, encoding="utf-8-sig") as handle:
                ids = runner.parse_deal_ids(handle.read())
            states = runner.file_states(writer, ids)
        result = runner.backfill(writer, lambda child_id: bool(plans_runner.run(child_id, client=rest).plans),
                                 states, args.confirm)
    except OSError:
        print("cannot read the file; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    except runner.FileRefused as exc:
        print(f"refused: {exc.code}; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    except scope_mod.ScopeError as exc:
        print(f"{exc}; nothing was done", file=sys.stderr)
        raise SystemExit(3) from None
    result["famlyRequests"] = rest.calls
    result.update(report)
    if result["mode"] == "stopped":
        print(json.dumps(result, indent=2))
        raise SystemExit(3)
    if result.get("results", {}).get(runner.REJECTED) or result.get("results", {}).get(runner.READ_FAILED):
        print(json.dumps(result, indent=2))
        raise SystemExit(1)
    return result
