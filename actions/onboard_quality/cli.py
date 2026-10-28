"""onboard-quality-check: READ-ONLY twice-weekly quality check of onboarded children (see runner.py). Prints
counts only; posts to Slack. Exit 1 when the audit fails or a due Slack post fails (state is then not saved)."""

import argparse
import logging
import sys

from core.logging_setup import configure_logging

NAME = "onboard-quality-check"
HELP = ("READ-ONLY: audit the in-scope deals, diff with the last run and post new issues to Slack "
        "(--baseline: record only; --digest: always post counts by tier). Never writes")


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baseline", action="store_true",
                        help="Record this run and post a one-line summary (counts, no ids). "
                             "The first ever run behaves like this")
    parser.add_argument("--digest", action="store_true",
                        help="Always post counts by tier (the Monday digest), plus any new issues")


def handle(args: argparse.Namespace):
    from actions.onboard_audit import runner as audit
    from actions.urn_lookup import runner as urn_runner
    from core.rest_client import RestClient

    from . import runner

    configure_logging()
    hubspot = audit.ReadOnlyHubSpot()
    gql = audit.ReadOnlyGraphQL(urn_runner._public_client())
    rest = audit.ReadOnlyRest(RestClient())
    try:
        result = runner.run(hubspot, gql, rest, baseline=args.baseline, digest=args.digest)
    except Exception as exc:  # noqa: BLE001 - the failure notice was posted; the exit code says it failed
        logging.getLogger(__name__).error("onboard-quality-check failed (%s)", type(exc).__name__)
        print(f"quality check failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    if result.get("postFailed"):
        logging.getLogger(__name__).error("onboard-quality-check: Slack post failed, state not saved; "
                                          "the same issues will be new on the next run")
        print("quality check: Slack post failed, state not saved", file=sys.stderr)
        raise SystemExit(1)
    return result
