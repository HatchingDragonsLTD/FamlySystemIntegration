"""CLI dispatcher.

Thin by design: it parses arguments and calls a runner. All real work lives in
`actions/<name>/runner.py`, so a future server or scheduler can import those
runners directly without going through this file.

Register a new action by adding one entry to ACTIONS below.
"""

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass

from actions.staff_credentials import runner as staff_credentials_runner
from core.client import GraphQLError, GraphQLHTTPError
from core.config import ConfigError

# Sort keys offered for staff-credentials, mapped to the attribute to sort on.
STAFF_CREDENTIALS_SORT_KEYS = {
    "title": "title",
    "date": "qualification_date",
    "expiry": "expiration_date",
    "qualification": "qualification_name",
}


def _add_staff_credentials_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "employee_ids",
        nargs="+",
        metavar="employeeId",
        help="One or more Famly employee IDs",
    )
    parser.add_argument(
        "--format",
        choices=("summary", "full"),
        default="summary",
        help=(
            "summary: title, qualificationDate, note, certificateNumber, "
            "qualification (default). full: every parsed field, including "
            "expirationDate, level and file URLs."
        ),
    )
    parser.add_argument(
        "--sort",
        choices=sorted(STAFF_CREDENTIALS_SORT_KEYS),
        help="Sort the output. Omit to keep the order the API returned.",
    )


def _run_staff_credentials(args: argparse.Namespace):
    assignments = staff_credentials_runner.run(args.employee_ids)

    if args.sort:
        attr = STAFF_CREDENTIALS_SORT_KEYS[args.sort]
        # Missing values sort last rather than blowing up on None comparison.
        assignments.sort(key=lambda a: (getattr(a, attr) is None, getattr(a, attr) or ""))

    if args.format == "summary":
        return staff_credentials_runner.summarise(assignments)
    return assignments


# name -> (help text, argument setup, handler)
ACTIONS = {
    "staff-credentials": (
        "Fetch staff qualification assignments for one or more employees",
        _add_staff_credentials_args,
        _run_staff_credentials,
    ),
}


def _print_result(result) -> None:
    def encode(obj):
        if is_dataclass(obj):
            return asdict(obj)
        return str(obj)

    print(json.dumps(result, default=encode, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Query the Famly GraphQL API.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    for name, (help_text, add_args, _handler) in ACTIONS.items():
        sub = subparsers.add_parser(name, help=help_text)
        add_args(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _help, _add_args, handler = ACTIONS[args.action]

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

    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
