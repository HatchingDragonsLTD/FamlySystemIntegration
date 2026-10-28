"""CLI wiring for reference-refresh: refresh the five generated reference files safely (see runner.py).
Prints counts and codes only. Exit 1 on any refusal or error."""

import argparse
import json
import sys

from core.logging_setup import configure_logging

NAME = "reference-refresh"
HELP = ("Refresh session/groups/products/institution-defaults reference files into REFERENCE_DIR: pull to a "
        "staging copy, validate, back up, replace atomically, prune backups, post a Slack summary "
        "(--dry-run: report only)")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true",
                        help="Pull to staging and print the validation, the diff counts, the number of Famly "
                             "calls and the duration. Replaces nothing, posts nothing")


def handle(args: argparse.Namespace):
    from . import runner

    configure_logging()
    result = runner.run(dry_run=args.dry_run)
    if result["exit"]:
        print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)
        raise SystemExit(1)
    return result
