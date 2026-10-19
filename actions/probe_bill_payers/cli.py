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
    "READ-ONLY: `schema` prints what the live schema says about bill payers; "
    "`audit` counts bill payers per site, scan time and duplicate emails (counts only)"
)
DEFAULT_SITES = "HDCW,HDWEST,HDCITY"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("report", choices=("schema", "audit"))
    parser.add_argument("--env", metavar="PATH", help="Explicit .env path to load first")
    parser.add_argument("--sites", default=DEFAULT_SITES,
                        help=f"Site codes (default {DEFAULT_SITES})")
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
