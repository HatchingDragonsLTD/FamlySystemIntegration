"""CLI wiring for onboard-status: counts and codes for one deal. No personal data."""

import argparse

from . import store

NAME = "onboard-status"
HELP = "Show the onboarding jobs for a deal: counts and codes only"


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deal-id", required=True, metavar="ID", help="The HubSpot deal id")


def handle(args: argparse.Namespace):
    deal_id = str(args.deal_id).strip()
    latest = store.latest_for_deal(deal_id)
    return {
        "dealId": deal_id,
        "jobsByStatus": store.status_counts_for_deal(deal_id),
        "latest": None if latest is None else {
            "jobId": latest.id,
            "status": latest.status,
            "mode": latest.mode,
            "lastErrorCode": latest.last_error_code,
            "childId": latest.child_id,
            "attempts": latest.attempts,
            "outcomes": latest.outcomes,
            "planHash": (latest.plan_hash or "")[:16] or None,
        },
    }
