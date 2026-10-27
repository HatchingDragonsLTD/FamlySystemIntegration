"""CLI dispatcher.

Thin by design and stays that way: it collects the action CLI modules listed in
ACTIONS, wires up their subcommands, and calls the matching handler. All action
logic lives in `actions/<name>/runner.py` (server-importable) and all action CLI
wiring lives in `actions/<name>/cli.py`.

Register a new action by adding one import and one entry to ACTIONS below. Each
listed module must expose: NAME, HELP, add_args(parser), handle(args).
"""

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass

from actions.contact_lookup import cli as contact_lookup
from actions.create_timeline import chain_cancel, chain_resume, chain_status, chain_sweep
from actions.delete_plans import delete_digest, delete_resume
from actions.hubspot_plan_uploaded import cli as hubspot_set_plan_uploaded
from actions.onboard import cli_enqueue as onboard_enqueue
from actions.onboard import cli_phone_audit as onboard_phone_audit
from actions.onboard import cli_status as onboard_status
from actions.onboard import cli_worker as onboard_worker
from actions.onboard_audit import cli as onboard_audit
from actions.onboard_dry_run import cli as onboard_dry_run
from actions.plan_write import cli as plan_write
from actions.probe_bill_payers import cli as probe_bill_payers
from actions.probe_child_create import cli as probe_child_create
from actions.probe_child_create import cli_delete as probe_child_delete
from actions.probe_contact_create import cli as probe_contact_create
from actions.probe_contact_reference import cli as probe_contact_reference
from actions.queue_timeline import (
    queue_cancel,
    queue_list,
    queue_pause,
    queue_resume,
    queue_status,
    queue_worker,
)
from actions.pull_groups import cli as pull_groups
from actions.pull_institution_defaults import cli as pull_institution_defaults
from actions.pull_products import cli as pull_products
from actions.pull_references import cli as pull_references
from actions.pull_roster import cli as pull_roster
from actions.pull_sessions import cli as pull_sessions
from actions.read_child_plans import cli as read_child_plans
from actions.staff_credentials import cli as staff_credentials
from actions.plan_write.runner import PlanCommitRefused
from core.client import GraphQLError, GraphQLHTTPError
from core.config import ConfigError
from core.hubspot import HubSpotError
from core.rest_client import RestHTTPError
from integrations.catalogue import UnknownInstitutionError

# One line per action. main.py does not grow beyond this list.
ACTIONS = [
    staff_credentials,
    read_child_plans,
    plan_write,
    pull_sessions,
    pull_groups,
    pull_products,
    pull_institution_defaults,
    pull_references,
    pull_roster,
    contact_lookup,
    onboard_dry_run,
    onboard_enqueue,
    onboard_worker,
    onboard_status,
    onboard_phone_audit,
    hubspot_set_plan_uploaded,
    onboard_audit,
    probe_contact_create,
    probe_contact_reference,
    probe_bill_payers,
    probe_child_create,
    probe_child_delete,
    chain_status,
    chain_cancel,
    chain_resume,
    chain_sweep,
    queue_worker,
    queue_status,
    queue_list,
    queue_cancel,
    queue_pause,
    queue_resume,
    delete_resume,
    delete_digest,
]


def _print_result(result) -> None:
    def encode(obj):
        if is_dataclass(obj):
            return asdict(obj)
        return str(obj)

    print(json.dumps(result, default=encode, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Query and write the Famly API (GraphQL and REST).",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    for module in ACTIONS:
        sub = subparsers.add_parser(module.NAME, help=module.HELP)
        module.add_args(sub)

    return parser


def _handler_for(name: str):
    for module in ACTIONS:
        if module.NAME == name:
            return module.handle
    raise KeyError(name)  # argparse's required subparser makes this unreachable


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = _handler_for(args.action)

    try:
        result = handler(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except GraphQLHTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except GraphQLError as exc:
        print(f"GraphQL error: {exc}", file=sys.stderr)
        return 1
    except RestHTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except HubSpotError as exc:
        print(f"HubSpot error: {exc}", file=sys.stderr)
        return 1
    except PlanCommitRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 3
    except UnknownInstitutionError as exc:
        print(f"Invalid --institution: {exc}", file=sys.stderr)
        return 4

    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
