"""CLI wiring for the staff-credentials action.

Keeps everything argparse-related out of `runner.py` so the runner stays
importable by a server or scheduler without pulling in CLI concerns. `main.py`
discovers this module through the standard interface below:

    NAME       -- subcommand name
    HELP       -- one-line help text
    add_args(parser)  -- register this action's arguments
    handle(args)      -- do the work, return a JSON-serialisable result

Add a new action by giving it a `cli.py` with these four names and listing it
in `main.py`.
"""

import argparse

from . import runner

NAME = "staff-credentials"
HELP = "Fetch staff qualification assignments for one or more employees"

# Sort keys offered on the CLI, mapped to the Assignment attribute to sort on.
SORT_KEYS = {
    "title": "title",
    "date": "qualification_date",
    "expiry": "expiration_date",
    "qualification": "qualification_name",
}


def add_args(parser: argparse.ArgumentParser) -> None:
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
        choices=sorted(SORT_KEYS),
        help="Sort the output. Omit to keep the order the API returned.",
    )


def handle(args: argparse.Namespace):
    # Sort while these are still Assignment objects. Never sort after
    # summarise() -- that returns plain dicts and getattr() would fail.
    assignments = runner.run(args.employee_ids)

    if args.sort:
        attr = SORT_KEYS[args.sort]
        # Missing values sort last rather than blowing up on None comparison.
        assignments.sort(
            key=lambda a: (getattr(a, attr) is None, getattr(a, attr) or "")
        )

    if args.format == "summary":
        return runner.summarise(assignments)
    return assignments
