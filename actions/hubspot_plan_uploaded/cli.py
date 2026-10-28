"""CLI wiring for hubspot-set-plan-uploaded: a one-off that sets `famly_plan_uploaded` on a list of
deals. Dry run unless --confirm. Counts, ids and codes only."""

import argparse
import json
import sys

from core.hubspot import HubSpotDealWriter

from actions.onboard_audit import scope as scope_mod

from . import runner

NAME = "hubspot-set-plan-uploaded"
HELP = ("Set famly_plan_uploaded on the deals in a file (max 60): to --value, or with --from-famly to "
        "what Famly shows (a plan = true). Dry run (reads only) unless --confirm")


def add_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", metavar="PATH",
                        help="Deal ids (digits, one per line) or the onboard-audit CSV (dealId column)")
    source.add_argument("--operations", action="store_true",
                        help="--from-famly only: every deal in the Operations pipeline (at most 60 writes per run)")
    scope_mod.add_arguments(parser)
    parser.add_argument("--value", choices=runner.VALUES, default=None,
                        help="The value to set: true or false (not with --from-famly)")
    parser.add_argument("--from-famly", action="store_true",
                        help="Reconcile: set the flag to what Famly shows for the deal's famly_childid "
                             "(a plan = true, none = false). A plan that cannot be read changes nothing")
    parser.add_argument("--confirm", action="store_true",
                        help="Write the deals whose value differs (otherwise a dry run)")


def _reconcile(args: argparse.Namespace):
    from actions.onboard_audit.runner import ReadOnlyRest
    from actions.read_child_plans import runner as plans_runner
    from core.rest_client import RestClient

    writer = HubSpotDealWriter()
    rest = ReadOnlyRest(RestClient())
    scope_report: dict = {}
    if args.operations:
        states = runner.operations_states(writer, scope_mod.resolve(args.leave_after, args.all_dates), scope_report)
    else:
        with open(args.file, encoding="utf-8-sig") as handle:
            ids = runner.parse_deal_ids(handle.read())
        states = runner.file_states(writer, ids)
    result = runner.reconcile(writer, lambda child_id: bool(plans_runner.run(child_id, client=rest).plans),
                              states, args.confirm)
    result["famlyRequests"] = rest.calls
    result.update(scope_report)
    return result


def handle(args: argparse.Namespace):
    if (args.leave_after or args.all_dates) and not (args.operations and args.from_famly):
        print("--leave-after and --all-dates only apply with --operations --from-famly; nothing was done",
              file=sys.stderr)
        raise SystemExit(3)
    if args.from_famly:
        if args.value is not None:
            print("--value is not used with --from-famly; nothing was done", file=sys.stderr)
            raise SystemExit(3)
        try:
            result = _reconcile(args)
        except OSError:
            print("cannot read the file; nothing was done", file=sys.stderr)
            raise SystemExit(3) from None
        except runner.FileRefused as exc:
            print(f"refused: {exc.code}; nothing was done", file=sys.stderr)
            raise SystemExit(3) from None
        except scope_mod.ScopeError as exc:
            print(f"{exc}; nothing was done", file=sys.stderr)
            raise SystemExit(3) from None
        results = result.get("results", {})
        if result["mode"] == "stopped":
            print(json.dumps(result, indent=2))
            raise SystemExit(3)
        if results.get(runner.REJECTED) or results.get(runner.READ_FAILED):
            print(json.dumps(result, indent=2))
            raise SystemExit(1)
        return result
    if args.operations or args.value is None:
        print("--operations and a missing --value need --from-famly / --value; nothing was done",
              file=sys.stderr)
        raise SystemExit(3)
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
