"""CLI wiring for the plan action.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Keeps argparse out of runner.py/builder.py/warnings.py/input_schema.py so they
stay server-importable.

Preview is the default mode. Committing takes two extra, deliberate flags:
--confirm and --test-child. The CSV mode previews only and can never commit.
"""

import argparse
import json
import sys
from pathlib import Path

from . import csv_loader, input_schema, runner, warnings as plan_warnings

NAME = "plan"
HELP = "Preview or commit a child plan (REST write)"


def add_args(parser: argparse.ArgumentParser) -> None:
    # Stashed so handle() can report a bad flag combination as a usage error
    # (usage text + exit 2) rather than raising a traceback at the user.
    parser.set_defaults(_parser=parser)

    parser.add_argument(
        "--mode",
        choices=("preview", "commit", "preview-csv"),
        default="preview",
        help=(
            "preview (default): the server computes the plan without saving. "
            "commit: actually writes, and requires --confirm and --test-child. "
            "preview-csv: load plans from a CSV and preview each one; never "
            "commits."
        ),
    )
    parser.add_argument(
        "--from-json",
        metavar="FILE",
        help=(
            "Required for --mode preview/commit. Path to a JSON file holding "
            'the request body: either the full {"plan": {...}} wrapper or the '
            "bare plan object."
        ),
    )
    parser.add_argument(
        "--file",
        metavar="FILE",
        help="Required for --mode preview-csv. Path to the CSV of booking rows.",
    )
    parser.add_argument(
        "--version",
        required=True,
        type=int,
        help="Plan version the request targets (e.g. 3)",
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


def _print_block(lines: str) -> None:
    """Print a banner-wrapped block to stderr, above the JSON result."""
    banner = "=" * 72
    print(banner, file=sys.stderr)
    print(lines, file=sys.stderr)
    print(banner, file=sys.stderr)


def _print_warnings(result: runner.ParsedPlanResult, label: str = "") -> None:
    """Warnings go to stderr so they cannot be missed.

    The endpoint returns HTTP 200 for an invalid plan, so this block is the only
    place bad input shows up.
    """
    heading = f"{label}\n" if label else ""
    body = heading + plan_warnings.format_warnings(result.warnings)

    if result.untracked_warnings:
        body += (
            f"\n{len(result.untracked_warnings)} warning(s) were not recognised "
            f"and have been escalated. Review before trusting this plan."
        )

    _print_block(body)


def _print_validation_errors(child_id: str | None, errors: list[str]) -> None:
    lines = [f"VALIDATION FAILED for child {child_id!r} -- not sent to Famly:"]
    lines.extend(f"  - {error}" for error in errors)
    _print_block("\n".join(lines))


def _handle_csv(args: argparse.Namespace) -> list[dict]:
    """Validate and preview every plan in a CSV. Never commits."""
    plans = csv_loader.load_csv(args.file)

    if not plans:
        _print_block(f"No plans found in {args.file} -- nothing to preview.")
        return []

    results = []
    for plan_input in plans:
        errors = input_schema.validate(plan_input)

        if errors:
            _print_validation_errors(plan_input.child_id, errors)
            results.append(
                {
                    "childId": plan_input.child_id,
                    "valid": False,
                    "errors": errors,
                    "previewed": False,
                }
            )
            continue

        result = runner.preview(input_schema.to_plan_body(plan_input), args.version)
        _print_warnings(result, label=f"child {plan_input.child_id}")

        results.append(
            {
                "childId": plan_input.child_id,
                "valid": True,
                "errors": [],
                "previewed": True,
                "monthlyEstimate": result.plan.monthly_estimate if result.plan else None,
                "warnings": [
                    {
                        "key": w.key,
                        "title": w.warning.title,
                        "message": w.warning.message,
                        "severity": w.warning.severity,
                        "error": w.warning.error,
                    }
                    for w in result.warnings
                ],
            }
        )

    return results


def _usage_error(args: argparse.Namespace, message: str) -> None:
    """Report a bad flag combination the way argparse reports any other."""
    parser = getattr(args, "_parser", None)
    if parser is not None:
        parser.error(message)
    raise ValueError(message)


def handle(args: argparse.Namespace):
    if args.mode == "preview-csv":
        if not args.file:
            _usage_error(args, "--mode preview-csv requires --file")
        return _handle_csv(args)

    if not args.from_json:
        _usage_error(args, f"--mode {args.mode} requires --from-json")

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
