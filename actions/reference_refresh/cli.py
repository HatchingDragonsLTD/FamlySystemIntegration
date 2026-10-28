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
                             "calls and the duration, plus per institution the unmatched sessions, ambiguous "
                             "slots and half-pair status. Replaces nothing, posts nothing")
    parser.add_argument("--record-unmatched-baseline", action="store_true",
                        help="Record the sessions that are unmatched right now as the known set (review them "
                             "first). Needed once if any are unmatched and no set has been recorded")
    parser.add_argument("--accept-unmatched", action="append", default=[], metavar="SESSION_ID",
                        help="Add a reviewed unmatched session id to the known set (repeatable; the id must be "
                             "unmatched now; logged)")


def handle(args: argparse.Namespace):
    from . import runner

    configure_logging()
    result = runner.run(dry_run=args.dry_run, accept_unmatched=args.accept_unmatched,
                        record_baseline=args.record_unmatched_baseline)
    if result["exit"]:
        print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)
        raise SystemExit(1)
    return result
