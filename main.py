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

from actions.plan_write import cli as plan_write
from actions.read_child_plans import cli as read_child_plans
from actions.staff_credentials import cli as staff_credentials
from actions.plan_write.runner import PlanCommitRefused
from core.client import GraphQLError, GraphQLHTTPError
from core.config import ConfigError
from core.rest_client import RestHTTPError

# One line per action. main.py does not grow beyond this list.
ACTIONS = [
    staff_credentials,
    read_child_plans,
    plan_write,
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
    except PlanCommitRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 3

    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
