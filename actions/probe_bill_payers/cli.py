"""CLI wiring for probe-bill-payers: READ-ONLY schema introspection and a bill payer
audit. Writes nothing to Famly or HubSpot. See runner.py.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
"""

import argparse
import sys

from dotenv import load_dotenv

from integrations import catalogue

from . import runner

NAME = "probe-bill-payers"
HELP = (
    "`schema` and `audit` are READ-ONLY (bill payer types; counts per site). "
    "`write` is a ONE-OFF WRITE PROBE: creates one test bill payer, measures read-back "
    "lag, removes its shares, deletes it; `attach` adds a second test child to a probe "
    "bill payer (the sibling path). Both need --confirm, --site, --test-email and test children"
)
DEFAULT_SITES = "HDCW,HDWEST,HDCITY"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("report", choices=("schema", "audit", "write", "attach"))
    parser.add_argument("--env", metavar="PATH", help="Explicit .env path to load first")
    parser.add_argument("--sites", default=DEFAULT_SITES,
                        help=f"Site codes (default {DEFAULT_SITES})")
    parser.add_argument("--site", metavar="CODE", help="write: the site code, e.g. HDCW")
    parser.add_argument("--test-email", metavar="EMAIL",
                        help="write: a test email you control (never stored or logged)")
    parser.add_argument("--child-id", metavar="ID", help="write: an existing TEST child")
    parser.add_argument("--create-test-child", action="store_true",
                        help="write: create one test child (isTestChild) and delete it at the end")
    parser.add_argument("--share", metavar="JSON",
                        help="write: the share; `1.0` is wrapped to the share input's own field "
             '({"multiplier": 1.0}); a full JSON object is also accepted')
    parser.add_argument("--existing-bill-payer-id", metavar="ID",
                        help="attach: use an existing PROBE bill payer (externalId probe-..., the "
                             "test email) instead of creating one; it is not deleted")
    parser.add_argument("--second-share", metavar="JSON",
                        help="attach: the second child's share (default: the same as --share)")
    parser.add_argument("--probe-delete-after-child-delete", action="store_true",
                        help="write: EXPERIMENT. Delete the test child first, then try "
                             "billPayers.delete once and report whether the share was released")
    parser.add_argument("--confirm", action="store_true", help="write/attach: required")
    parser.add_argument(
        "--types", default="", metavar="A,B",
        help="Schema report only: also print these types in full "
             f"(use `default` for {','.join(runner.DEFAULT_TYPES)})",
    )
    parser.add_argument(
        "--api", choices=("public", "internal"), default="public",
        help="Which Famly GraphQL API the schema report reads (the audit is public-only)",
    )


def _client(api: str):
    from actions.pull_roster.runner import _public_client
    from core.client import GraphQLClient

    return _public_client() if api == "public" else GraphQLClient()


def handle(args: argparse.Namespace):
    if args.env:
        load_dotenv(args.env, override=False)
    if args.report in ("write", "attach"):
        return _write(args)
    if args.report == "schema":
        names = runner.DEFAULT_TYPES if args.types == "default" else tuple(
            n.strip() for n in args.types.split(",") if n.strip())
        for line in runner.run_schema(_client(args.api), names):
            print(line)
        return {"api": args.api, "report": "schema"}

    sites = catalogue.load_sites()
    wanted = [c.strip().upper() for c in args.sites.split(",") if c.strip()]
    missing = [c for c in wanted if not (sites.get(c) or {}).get("institutionId")]
    if missing:
        print(f"unknown or unconfigured site code(s): {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(3)
    try:
        return runner.audit(_client("public"), {c: sites[c]["institutionId"] for c in wanted})
    except runner.AuditRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None


def _json_arg(value, name: str):
    import json
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        print(f"refusing: {name} must be valid JSON (for example 1.0)", file=sys.stderr)
        raise SystemExit(3) from None


def _write(args: argparse.Namespace):
    """The write probes. Returns booleans only: ids and the email never leave the
    terminal output the probe itself prints."""
    from core.rest_client import RestClient

    from . import write_probe

    if not args.confirm:
        print("refusing without --confirm; nothing was written", file=sys.stderr)
        raise SystemExit(3)
    share = _json_arg(args.share, "--share")
    second_share = _json_arg(getattr(args, "second_share", None), "--second-share")
    probe = write_probe.Probe(_client("public"), RestClient())
    try:
        if args.report == "attach":
            result = probe.run_attach(args.site, args.test_email, args.create_test_child,
                                      getattr(args, "existing_bill_payer_id", None), share,
                                      second_share, args.confirm)
        else:
            result = probe.run(args.site, args.test_email, args.child_id, args.create_test_child,
                               share, args.confirm,
                               probe_delete_after_child_delete=getattr(
                                   args, "probe_delete_after_child_delete", False))
    except write_probe.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {
        "billPayerCreated": result.bill_payer_created,
        "childrenAdded": result.children_added,
        "sharesDeleted": result.shares_deleted,
        "billPayerDeleted": result.bill_payer_deleted,
        "testChildCreated": result.test_child_created,
        "testChildDeleted": result.test_child_deleted,
        "stopped": result.stopped,
    }
