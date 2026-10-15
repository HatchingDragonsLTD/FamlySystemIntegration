"""Phase 2 configuration, read fresh from the environment on every call.

    ONBOARD_MODE              off | dry | live   (default off; anything else is off)
        off   the enqueue command refuses and the worker does nothing.
        dry   the worker runs the full plan and the Slack message but makes NO
              Famly or HubSpot writes; the job ends `dry_done`.
        live  writes happen, but ONLY for deals in ONBOARD_ALLOWED_DEAL_IDS.
    ONBOARD_ALLOWED_DEAL_IDS  comma-separated deal ids. Empty means NONE.
    ONBOARD_TEST_DEAL_IDS     comma-separated; a subset of the allowlist. Only a
                              deal in BOTH lists creates its child with
                              isTestChild true. The flag is never set otherwise.
    HUBSPOT_PORTAL_ID         optional; used only to build the deal link in Slack.
    ONBOARD_STORE_PATH        optional path to the jobs SQLite file (default
                              var/onboard_jobs.sqlite3, gitignored).
"""

import logging
import os

logger = logging.getLogger(__name__)

MODE_OFF, MODE_DRY, MODE_LIVE = "off", "dry", "live"


def mode() -> str:
    raw = os.environ.get("ONBOARD_MODE", "").strip().lower()
    if raw == "":
        return MODE_OFF
    if raw in (MODE_OFF, MODE_DRY, MODE_LIVE):
        return raw
    logger.error("ONBOARD_MODE=%r is not off, dry or live; treating it as off", raw)
    return MODE_OFF


def _ids(name: str) -> frozenset[str]:
    return frozenset(
        item.strip()
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    )


def allowed_deal_ids() -> frozenset[str]:
    return _ids("ONBOARD_ALLOWED_DEAL_IDS")


def test_deal_ids() -> frozenset[str]:
    return _ids("ONBOARD_TEST_DEAL_IDS")


def is_allowed(deal_id: str) -> bool:
    return str(deal_id).strip() in allowed_deal_ids()


def is_test_deal(deal_id: str) -> bool:
    """True only for a deal that is in BOTH lists (a test id outside the
    allowlist is not allowed to write at all, so it is not a test deal either)."""
    deal_id = str(deal_id).strip()
    return deal_id in test_deal_ids() and deal_id in allowed_deal_ids()


def portal_id() -> str:
    return os.environ.get("HUBSPOT_PORTAL_ID", "").strip()


def deal_link(deal_id: str) -> str | None:
    portal = portal_id()
    if not portal:
        return None
    return f"https://app.hubspot.com/contacts/{portal}/record/0-3/{deal_id}"
