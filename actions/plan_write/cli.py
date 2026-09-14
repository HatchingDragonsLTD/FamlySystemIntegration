"""CLI wiring for the plan action.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Keeps argparse out of runner.py/builder.py/warnings.py so they stay
server-importable.

Preview is the default mode. Committing takes two extra, deliberate flags:
--confirm and --test-child.
"""

import argparse
import json
import sys
from pathlib import Path

from . import runner, warnings as plan_warnings

NAME = "plan"
HELP = "Preview or commit a child plan (REST write)"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--from-json",
        required=True,
        metavar="FILE",
        help=(
            "Path to a JSON file holding the request body. Either the full "
            '{"plan": {...}} wrapper or the bare plan object.'
        ),
    )
    parser.add_argument(
        "--version",
        required=True,
        type=int,
        help="Plan version the request targets (e.g. 3)",
    )
    parser.add_argument(
        "--mode",
        choices=("preview", "commit"),
        default="preview",
        help=(
            "preview (default): the server computes the plan without saving. "
            "commit: actually writes, and requires --confirm and --test-child."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for --mode commit. Without it a commit is refused.",
    )
    parser.add_argument(
        "--test-child",
        action="append",
        default=[],
        metavar="childId",
        help=(
            "Child ID the commit is allowed to touch. Repeatable. The plan's "
            "childId must be in this set or the commit is refused."
        ),
    )


def _load_body(path: str) -> dict:
    """Read the request body, accepting the wrapper or the bare plan."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    if isinstance(raw, dict) and isinstance(raw.get("plan"), dict):
        return raw
    if isinstance(raw, dict):
        # A bare plan object: wrap it as the endpoint expects.
        return {"plan": raw}

    raise ValueError(
        f"{path} must contain a JSON object, either the full "
        f'{{"plan": {{...}}}} wrapper or the bare plan.'
    )


def _print_warnings(result: runner.ParsedPlanResult) -> None:
    """Warnings go to stderr, above the result, so they cannot be missed.

    The endpoint returns HTTP 200 for an invalid plan, so this block is the
    only place a bad input shows up.
    """
    banner = "=" * 72
    print(banner, file=sys.stderr)
    print(plan_warnings.format_warnings(result.warnings), file=sys.stderr)
    if result.untracked_warnings:
        print(
            f"{len(result.untracked_warnings)} warning(s) were not recognised "
            f"and have been escalated. Review before trusting this plan.",
            file=sys.stderr,
        )
    print(banner, file=sys.stderr)


def handle(args: argparse.Namespace):
    plan_body = _load_body(args.from_json)

    if args.mode == "preview":
        result = runner.preview(plan_body, args.version)
    else:
        # The guards live in runner.commit so a server calling it directly gets
        # exactly the same protection; these flags only feed them.
        result = runner.commit(
            plan_body,
            args.version,
            confirm=args.confirm,
            allowed_child_ids=set(args.test_child),
        )

    _print_warnings(result)
    return result
