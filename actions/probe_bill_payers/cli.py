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
    "lag, deletes it (needs --confirm, --site, --test-email and a test child)"
)
DEFAULT_SITES = "HDCW,HDWEST,HDCITY"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("report", choices=("schema", "audit", "write"))
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
                        help="write: the child share value as JSON, in the format the schema report shows")
    parser.add_argument("--confirm", action="store_true", help="write: required")
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
    if args.report == "write":
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
    return runner.audit(_client("public"), {c: sites[c]["institutionId"] for c in wanted})


def _write(args: argparse.Namespace):
    """The write probe. Returns booleans only: ids and the email never leave the
    terminal output the probe itself prints."""
    import json

    from core.rest_client import RestClient

    from . import write_probe

    if not args.confirm:
        print("refusing without --confirm; nothing was written", file=sys.stderr)
        raise SystemExit(3)
    share = None
    if args.share is not None:
        try:
            share = json.loads(args.share)
        except ValueError:
            print("refusing: --share must be valid JSON (for example 100 or 1.0)", file=sys.stderr)
            raise SystemExit(3) from None
    probe = write_probe.Probe(_client("public"), RestClient())
    try:
        result = probe.run(args.site, args.test_email, args.child_id, args.create_test_child,
                           share, args.confirm)
    except write_probe.ProbeRefused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None
    return {
        "billPayerCreated": result.bill_payer_created,
        "billPayerDeleted": result.bill_payer_deleted,
        "testChildCreated": result.test_child_created,
        "testChildDeleted": result.test_child_deleted,
        "stopped": result.stopped,
    }
