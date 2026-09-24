"""Slack integration for the plan preview flow.

Two directions:

  * OUTBOUND -- `post_preview` sends a previewed plan to a Slack channel with
    Approve / Reject buttons, and `update_message` replaces that message once a
    button is clicked.
  * INBOUND  -- `verify_slack_request` and `parse_interaction` handle the
    button click Slack posts back.

Messages are posted proactively (chat.postMessage), so they are updated by
POSTing to the interaction's `response_url` -- an inline `replace_original`
response does not reliably update a message Slack did not render itself.

The inbound half is SECURITY-CRITICAL: an Approve click now COMMITS a plan to
Famly, so the signature check is the gate on a real write. It was implemented
and proven before the commit path was attached to it.

SLACK APP CONFIGURATION: the interactivity request URL must be set to

    https://<domain>/slack/interactivity

in the Slack app's "Interactivity & Shortcuts" settings, and the app needs the
`chat:write` scope to post.

Environment:
    SLACK_BOT_TOKEN       xoxb-... bot token, used to post.
    SLACK_CHANNEL_ID      the channel to post previews into.
    SLACK_SIGNING_SECRET  used to verify inbound interactivity requests.

Env is read inside the functions rather than at import, so this module is
importable and unit-testable without a configured environment or a running
server. Nothing here raises on a Slack failure -- an outage must not break the
preview response.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs

import requests

logger = logging.getLogger(__name__)

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
VIEWS_OPEN_URL = "https://slack.com/api/views.open"
POST_TIMEOUT = 10

# action_id values carried by the buttons. The inbound handler switches on these.
ACTION_APPROVE = "plan_approve"
ACTION_REJECT = "plan_reject"
# Opens the adjustment modal. It does NOT act on the plan by itself -- the
# modal's submission does, and the plan it commits goes through the ordinary
# Approve path under a fresh preview_id.
ACTION_ADJUST = "plan_adjust"

# Interaction types Slack posts to the same endpoint.
TYPE_BLOCK_ACTIONS = "block_actions"
TYPE_VIEW_SUBMISSION = "view_submission"

# Modal plumbing. The block id is what an inline validation error is keyed to.
ADJUST_CALLBACK_ID = "plan_adjust_modal"
ADJUST_BLOCK_ID = "adjustment_block"
ADJUST_INPUT_ID = "adjustment_input"

# Hard boundary on a rounding adjustment, in pounds. Enforced on submission,
# and again before the adjusted body is stored -- the modal's own hint is a
# courtesy, not a control.
MIN_ADJUSTMENT = -1.00
MAX_ADJUSTMENT = 1.00

# Slack signs with this version prefix.
SIGNATURE_VERSION = "v0"

# Reject anything older than this (replay protection). Slack's own guidance.
MAX_REQUEST_AGE_SECONDS = 60 * 5


# --------------------------------------------------------------------------- #
# Outbound: post a preview for approval
# --------------------------------------------------------------------------- #
def _money(value: Any) -> str:
    """Format a monetary figure without inventing a currency symbol."""
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)):
        return f"{value:,.2f}"
    return str(value)


# Week order for listing bookings. Anything unrecognised sorts last.
WEEK_ORDER = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)


def _day_sort_key(day: Any) -> tuple:
    """Monday first; unknown or missing days last, alphabetically."""
    name = str(day).strip().upper() if day else ""
    if name in WEEK_ORDER:
        return (0, WEEK_ORDER.index(name), "")
    return (1, 0, name)


def _day_label(day: Any) -> str:
    """`MONDAY` -> `Monday`."""
    if not day:
        return "Unknown day"
    return str(day).strip().title()


def _session_bookings(plan: Any) -> list:
    """The plan's session bookings, listed once each.

    Famly MIRRORS bookings at plan level and plan-part level, so taking both
    would list every session twice. The parts are authoritative; the plan-level
    list is the fallback for a plan with no parts. Matches
    `Plan.session_booking_count`.
    """
    parts = getattr(plan, "plan_parts", None) or []
    if parts:
        bookings = []
        for part in parts:
            bookings.extend(getattr(part, "session_bookings", None) or [])
        return bookings
    return list(getattr(plan, "session_bookings", None) or [])


def _product_bookings(plan: Any) -> list:
    """The plan's product bookings, de-duplicated the same way as sessions.

    These stay raw dicts in the Plan model, so they are read with .get().
    """
    parts = getattr(plan, "plan_parts", None) or []
    if parts:
        bookings = []
        for part in parts:
            bookings.extend(getattr(part, "product_bookings", None) or [])
        return bookings
    return list(getattr(plan, "product_bookings", None) or [])


def _discounts(plan: Any) -> list:
    """The plan's discounts, de-duplicated the same way as sessions.

    Raw dicts in the Plan model, so they are read with .get().
    """
    parts = getattr(plan, "plan_parts", None) or []
    if parts:
        found = []
        for part in parts:
            found.extend(getattr(part, "discounts", None) or [])
        return found
    return list(getattr(plan, "discounts", None) or [])


def _percent(value: Any) -> str:
    """A discount fraction as a percentage: 0.05 -> "5%".

    Trailing zeros are trimmed so 0.05 reads "5%" rather than "5.0%", while
    0.075 still reads "7.5%".
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"

    percent = value * 100
    if float(percent).is_integer():
        return f"{int(percent)}%"
    return f"{percent:g}%"


def _discount_amount(discount: dict) -> str:
    """One discount's amount, in its own units.

    Percentage and fixed-amount discounts sit in the same list, so the entry's
    `isPercent` decides how it reads -- 0.05 is "5%" but 2.94 is "£2.94", and
    showing either in the other's units would misstate a family's bill.

    Anything not explicitly marked as fixed is treated as a percentage, which
    is what every discount was before fixed ones existed.
    """
    amount = discount.get("amount")

    if discount.get("isPercent") is False:
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            return f"£{amount:,.2f}"
        return "unknown"

    return _percent(amount)


def _active_pricing_group(plan: Any) -> str | None:
    """The pricing group whose prices apply to this plan.

    Taken from the plan itself, falling back to the first plan state (the
    current period) when the plan node does not carry it.
    """
    # The Plan model owns this; the fallbacks below cover a duck-typed stand-in.
    group = getattr(plan, "active_pricing_group_id", None)
    if group:
        return group

    group = getattr(plan, "pricing_group_id", None)
    if group:
        return group

    states = getattr(plan, "plan_states", None) or []
    for state in states:
        raw = getattr(state, "raw", None)
        if isinstance(raw, dict) and raw.get("pricingGroupId"):
            return raw["pricingGroupId"]
    return None


def _session_price(booking: Any, pricing_group_id: str | None) -> Any | None:
    """The booking's price for the active pricing group.

    `monthlyPrices` carries one entry per pricing group, so a price is only
    correct when its `pricingGroupId` matches the plan's active group. Returns
    None when it cannot be matched -- an omitted price beats a wrong one, since
    the approver is deciding on money.
    """
    if not pricing_group_id:
        return None

    for price in getattr(booking, "monthly_prices", None) or []:
        if getattr(price, "pricing_group_id", None) == pricing_group_id:
            return getattr(price, "price", None)
    return None


def build_summary(
    plan: Any,
    warnings: list,
    session_titles: dict | None = None,
    product_titles: dict | None = None,
    site: dict | None = None,
) -> str:
    """Format a previewed plan as plain, scannable Slack text for an approver.

    Args:
        plan: the parsed preview plan (read_child_plans.runner.Plan).
        warnings: the serialised warnings from the same result.
        session_titles: sessionId -> title from the child's live catalogue
            (`ChildPlansResult.session_titles`).
        product_titles: productId -> title, likewise.
        site: resolved site metadata ({"label", ...}) for the Site line. Shows
            "unknown" when absent or unresolved -- it is informational, and
            decides nothing about the plan.

    Either map may be None or empty: an unresolved id is shown as the id
    itself. A readable label is a nicety and must never cost the approver the
    message.

    Prices come from the COMPUTED PLAN, not the catalogue, so they are what
    this plan actually charges.

    Returns:
        The message text. Defensive throughout: this runs on the success path,
        so a missing field reads as "unknown" rather than raising.
    """
    if plan is None:
        return "*Plan preview awaiting approval*\n• (no plan returned)"

    session_titles = session_titles or {}
    product_titles = product_titles or {}
    pricing_group_id = _active_pricing_group(plan)

    child_id = getattr(plan, "child_id", None) or "unknown"
    date_from = getattr(plan, "from_", None) or "unknown"
    date_to = getattr(plan, "to", None) or "open-ended"

    site_label = (site or {}).get("label") if isinstance(site, dict) else None

    lines = [
        "*Plan preview awaiting approval*",
        f"• Site: {site_label or 'unknown'}",
        f"• Child: `{child_id}`",
        f"• Dates: {date_from} → {date_to}",
    ]

    # --- Sessions ---------------------------------------------------------- #
    bookings = sorted(
        _session_bookings(plan), key=lambda b: _day_sort_key(getattr(b, "day", None))
    )
    lines.append("")
    if bookings:
        lines.append(f"*Sessions* ({len(bookings)})")
        for booking in bookings:
            session_id = getattr(booking, "session_id", None)
            title = session_titles.get(session_id) or session_id or "unknown session"
            line = f"• {_day_label(getattr(booking, 'day', None))} — {title}"

            price = _session_price(booking, pricing_group_id)
            if price is not None:
                line += f" — {_money(price)}"

            lines.append(line)
    else:
        lines.append("*Sessions*\n• None booked.")

    # --- Products (omitted entirely when there are none) ------------------- #
    products = sorted(
        _product_bookings(plan),
        key=lambda b: _day_sort_key(b.get("day") if isinstance(b, dict) else None),
    )
    if products:
        lines.append("")
        lines.append(f"*Products* ({len(products)})")
        for product in products:
            if not isinstance(product, dict):
                continue
            product_id = product.get("productId")
            title = product_titles.get(product_id) or product_id or "unknown product"
            amount = product.get("amount")
            amount = "?" if amount is None else amount
            line = f"• {_day_label(product.get('day'))} — {amount}× {title}"

            booked_price = product.get("bookedPrice")
            if booked_price is not None:
                line += f" — {_money(booked_price)}"

            lines.append(line)

    # --- Discounts (omitted entirely when there are none) ------------------ #
    discounts = sorted(
        (d for d in _discounts(plan) if isinstance(d, dict)),
        key=lambda d: d.get("ordering") if isinstance(d.get("ordering"), int) else 99,
    )
    if discounts:
        lines.append("")
        lines.append(f"*Discounts* ({len(discounts)})")
        for discount in discounts:
            title = discount.get("title") or "untitled discount"
            lines.append(f"• {title} — {_discount_amount(discount)}")

    # --- Totals ------------------------------------------------------------ #
    lines.append("")
    states = getattr(plan, "plan_states", None) or []
    weekly = states[0].weekly_total if states else None

    lines.append(f"• Weekly total: {_money(weekly)}")
    lines.append(f"• Monthly estimate: {_money(getattr(plan, 'monthly_estimate', None))}")

    if len(states) > 1:
        # Don't enumerate every period -- just flag that the figure above is
        # the current one and the rate moves later.
        lines.append(f"• _(rate changes during plan — {len(states)} periods)_")

    # --- Public funding, only when funded ---------------------------------- #
    funding = getattr(plan, "public_funding", None)
    amount = getattr(funding, "amount", None)
    hours = getattr(funding, "hours", None)
    minutes = getattr(funding, "minutes", None)

    if amount or hours or minutes:
        hours_part = "unknown" if hours is None else f"{hours}h"
        if minutes:
            hours_part += f" {minutes}m"
        lines.append(f"• Public funding: {_money(amount)} ({hours_part})")

    # --- Warnings (unchanged: the approver must see these) ----------------- #
    count = len(warnings) if isinstance(warnings, list) else 0
    lines.append("")
    if count:
        lines.append(f":warning: *{count} warning(s)* — review before approving:")
        for warning in warnings[:5]:
            lines.append(f"• {_warning_line(warning)}")
        if count > 5:
            lines.append(f"• …and {count - 5} more")
    else:
        lines.append("• No warnings returned.")

    return "\n".join(lines)


def _warning_line(warning: Any) -> str:
    """One warning as a short line. Handles the nested classified shape."""
    if not isinstance(warning, dict):
        return str(warning)

    inner = warning.get("warning")
    inner = inner if isinstance(inner, dict) else warning

    title = inner.get("title") or "untitled warning"
    severity = inner.get("severity")

    parts = [str(title)]
    if severity:
        parts.append(f"({severity})")

    # `key` is a property on ClassifiedWarning, so dataclasses.asdict drops it;
    # `known` is the field that survives, and None there means the registry did
    # not recognise this warning. That is the escalation case, so say so.
    if "known" in warning:
        known = warning.get("known")
        if known is None:
            parts.append("[UNTRACKED]")
        elif isinstance(known, dict) and known.get("key"):
            parts.append(f"[{known['key']}]")
    elif warning.get("key"):
        parts.append(f"[{warning['key']}]")

    return " ".join(parts)


def _approval_blocks(summary_text: str, preview_id: str) -> list:
    """The message body: the summary plus Approve / Reject buttons.

    Each button carries `preview_id` as its value, so the click can be tied
    back to the preview that produced it.
    """
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary_text},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"preview_id: `{preview_id}`"}
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "value": preview_id,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Adjust & Approve",
                        "emoji": True,
                    },
                    "action_id": ACTION_ADJUST,
                    "value": preview_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "style": "danger",
                    "action_id": ACTION_REJECT,
                    "value": preview_id,
                },
            ],
        },
    ]


def post_preview(summary_text: str, preview_id: str) -> bool:
    """Post a preview to Slack with Approve / Reject buttons.

    Args:
        summary_text: the message body, e.g. from `build_summary`.
        preview_id: ties the buttons back to this preview.

    Returns:
        True when Slack accepted the message, False otherwise.

    Never raises. A Slack outage, a missing token or an API error is logged and
    reported as False so the caller's own response is unaffected.
    """
    import os

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL_ID", "").strip()

    if not token or not channel:
        logger.warning(
            "Slack not configured (SLACK_BOT_TOKEN / SLACK_CHANNEL_ID missing); "
            "skipping post for preview_id=%s",
            preview_id,
        )
        return False

    try:
        response = requests.post(
            POST_MESSAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "channel": channel,
                # `text` is the notification/fallback; blocks carry the layout.
                "text": summary_text,
                "blocks": _approval_blocks(summary_text, preview_id),
            },
            timeout=POST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - Slack must never break the caller
        logger.warning(
            "Slack post failed for preview_id=%s: %s", preview_id, exc
        )
        return False

    # Slack returns HTTP 200 with {"ok": false, "error": ...} on failure, so the
    # status code alone proves nothing.
    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code != 200 or not body.get("ok"):
        logger.warning(
            "Slack rejected the message for preview_id=%s: HTTP %s %s",
            preview_id,
            response.status_code,
            body.get("error") or response.text[:200],
        )
        return False

    logger.info("Posted plan preview to Slack: preview_id=%s", preview_id)
    return True


def adjustment_modal(
    preview_id: str, estimate=None, response_url: str | None = None
) -> dict:
    """The Adjust & Approve modal: the current estimate, and one number field.

    `private_metadata` carries everything the submission needs, because a
    submission payload has no button and NO response_url of its own. Slack
    gives the message's response_url only on the click, so it is stashed here
    to let the submission update that original message.

    Encoded as JSON; a bare preview_id is still accepted on the way back in, so
    a modal opened by an older build keeps working.
    """
    metadata = {"preview_id": preview_id}
    if response_url:
        metadata["response_url"] = response_url

    blocks = []
    if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
        # Shown so the approver can see what they are adjusting without
        # scrolling back to the original message.
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Current monthly estimate: *£{estimate:,.2f}*",
                    }
                ],
            }
        )

    return {
        "type": "modal",
        "callback_id": ADJUST_CALLBACK_ID,
        "private_metadata": json.dumps(metadata),
        "title": {"type": "plain_text", "text": "Adjust plan"},
        "submit": {"type": "plain_text", "text": "Re-price"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks + [
            {
                "type": "input",
                "block_id": ADJUST_BLOCK_ID,
                "label": {
                    "type": "plain_text",
                    "text": (
                        f"Adjustment (£, between {MIN_ADJUSTMENT:.2f} and "
                        f"{MAX_ADJUSTMENT:.2f})"
                    ),
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": ADJUST_INPUT_ID,
                    "placeholder": {"type": "plain_text", "text": "e.g. 0.50"},
                },
                "hint": {
                    "type": "plain_text",
                    "text": "A small rounding adjustment. Re-prices before committing.",
                },
            }
        ],
    }


def open_modal(trigger_id: str, view: dict) -> bool:
    """Open a modal via views.open.

    Returns True when Slack accepted it. Never raises -- a failed modal leaves
    the original message and its buttons untouched, so the approver can retry
    or use plain Approve.

    `trigger_id` is short-lived (a few seconds), so this must be called
    promptly after the click.
    """
    import os

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token or not trigger_id:
        logger.warning("Cannot open the Slack modal: missing token or trigger_id")
        return False

    try:
        response = requests.post(
            VIEWS_OPEN_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"trigger_id": trigger_id, "view": view},
            timeout=POST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - Slack must never break the caller
        logger.warning("views.open failed: %s", exc)
        return False

    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}

    if response.status_code != 200 or not body.get("ok"):
        logger.warning(
            "Slack rejected views.open: HTTP %s %s",
            response.status_code,
            body.get("error") or response.text[:200],
        )
        return False

    return True


def modal_error(message: str) -> dict:
    """A `response_action: errors` body, shown inline under the input.

    This must be the HTTP response to the view_submission request itself --
    Slack will not display it any other way.
    """
    return {
        "response_action": "errors",
        "errors": {ADJUST_BLOCK_ID: message},
    }


def post_adjusted(summary_text: str, preview_id: str) -> bool:
    """Post the re-priced plan with a single Confirm Commit button.

    The button carries ACTION_APPROVE, so the click lands in exactly the same
    handler, guards and idempotency as an ordinary approval -- the only
    difference is the preview_id it names, which holds the adjusted body.
    """
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_text}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"preview_id: `{preview_id}`"}],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Confirm Commit",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "value": preview_id,
                }
            ],
        },
    ]

    if not _post_message(summary_text, blocks, label=f"adjusted plan {preview_id}"):
        return False

    logger.info("Posted adjusted plan to Slack: preview_id=%s", preview_id)
    return True


def post_notice(text: str) -> bool:
    """Post a plain message to the channel -- no buttons, nothing to click.

    Used to tell the approver that background work failed. Without it a failed
    re-price would leave them waiting on a Confirm Commit message that is never
    coming.

    Never raises, same as every other poster here.
    """
    return _post_message(text, None, label="notice")


def _post_message(text: str, blocks, label: str) -> bool:
    """Post to the configured channel. Returns True only when Slack accepted it.

    Shared by the posters so they fail identically: log, return False, never
    raise at the caller.
    """
    import os

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL_ID", "").strip()

    if not token or not channel:
        logger.warning("Slack not configured; dropping %s", label)
        return False

    payload = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        response = requests.post(
            POST_MESSAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=POST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - Slack must never break the caller
        logger.warning("Slack post failed (%s): %s", label, exc)
        return False

    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}

    if response.status_code != 200 or not body.get("ok"):
        logger.warning(
            "Slack rejected a post (%s): HTTP %s %s",
            label,
            response.status_code,
            body.get("error") or response.text[:200],
        )
        return False

    return True


# --------------------------------------------------------------------------- #
# Outbound: update a message in response to a click
# --------------------------------------------------------------------------- #
def update_message(response_url: str, text: str) -> bool:
    """Replace the message a button was clicked on, via its `response_url`.

    An inline `replace_original` response only reliably updates messages Slack
    itself rendered from a slash command. Previews are posted proactively with
    chat.postMessage, so the message is updated by POSTing back to the
    interaction's short-lived `response_url` instead.

    Args:
        response_url: from the interaction payload (see `parse_interaction`).
        text: the replacement message text.

    Returns:
        True when Slack accepted the update, False otherwise.

    Never raises -- same failure isolation as `post_preview`. A failed update
    leaves the original message in place; it does not affect the caller.
    """
    if not response_url:
        logger.warning("No response_url given; cannot update the Slack message")
        return False

    try:
        response = requests.post(
            response_url,
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"replace_original": True, "text": text},
            timeout=POST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - Slack must never break the caller
        logger.warning("Slack message update failed: %s", exc)
        return False

    # A response_url POST answers with a plain "ok" body rather than the JSON
    # envelope chat.postMessage returns, so the status code is what counts --
    # but check for an {"ok": false} body too, in case one is sent.
    if response.status_code != 200:
        logger.warning(
            "Slack rejected the message update: HTTP %s %s",
            response.status_code,
            response.text[:200],
        )
        return False

    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - this function promises never to raise
        body = None

    if isinstance(body, dict) and body.get("ok") is False:
        logger.warning(
            "Slack rejected the message update: %s",
            body.get("error") or response.text[:200],
        )
        return False

    logger.info("Updated the Slack message via response_url")
    return True


# --------------------------------------------------------------------------- #
# Inbound: verify and parse a button click
# --------------------------------------------------------------------------- #
def verify_slack_request(headers: Any, raw_body: Any) -> bool:
    """Verify an inbound request really came from Slack.

    Slack signs `v0:<timestamp>:<raw body>` with the signing secret using
    HMAC-SHA256 and sends the hex digest as `X-Slack-Signature`.

    THE BODY MUST BE THE RAW BYTES AS RECEIVED. Re-serialising parsed form data
    changes the byte sequence and the signature will never match.

    Args:
        headers: a mapping supporting `.get` (Flask's request.headers works).
        raw_body: the unmodified request body, bytes or str.

    Returns:
        True only when the signature matches AND the timestamp is recent.
        False on anything else -- missing secret, missing headers, malformed
        timestamp, replay, or mismatch. Never raises.
    """
    import os

    secret = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        # Fail closed: an unconfigured server rejects everything rather than
        # accepting unverified requests.
        logger.error("SLACK_SIGNING_SECRET is not set; rejecting Slack request")
        return False

    try:
        timestamp = headers.get("X-Slack-Request-Timestamp", "")
        signature = headers.get("X-Slack-Signature", "")
    except Exception:  # noqa: BLE001 - odd header mapping
        return False

    if not timestamp or not signature:
        logger.warning("Slack request missing signature headers")
        return False

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        logger.warning("Slack request has a non-numeric timestamp")
        return False

    # Replay protection: reject anything older (or further in the future) than
    # the allowed window.
    if abs(time.time() - sent_at) > MAX_REQUEST_AGE_SECONDS:
        logger.warning("Slack request timestamp outside the allowed window")
        return False

    if isinstance(raw_body, str):
        body_bytes = raw_body.encode("utf-8")
    elif isinstance(raw_body, (bytes, bytearray)):
        body_bytes = bytes(raw_body)
    else:
        logger.warning("Slack request body was not bytes or str")
        return False

    basestring = b"%s:%s:%s" % (
        SIGNATURE_VERSION.encode("utf-8"),
        str(sent_at).encode("utf-8"),
        body_bytes,
    )
    expected = (
        f"{SIGNATURE_VERSION}="
        + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    )

    # Constant-time comparison: a timing side channel here would leak the digest.
    if not hmac.compare_digest(expected, signature):
        logger.warning("Slack request signature did not match")
        return False

    return True


def parse_interaction(raw_body: Any) -> dict:
    """Parse Slack's interactivity payload into the bits we act on.

    Slack posts this URL-encoded, with the JSON in a `payload` form field.

    Handles both interaction types Slack posts here: a button click
    (`block_actions`) and a modal submission (`view_submission`). A submission
    has no button, so its preview_id comes from the view's `private_metadata`.

    Returns:
        A dict with `type`, `action_id`, `preview_id`, `user`, `response_url`,
        `trigger_id`, `adjustment` and the full `payload`. Missing pieces come
        back as None -- defensive throughout, since this parses untrusted input.
    """
    empty = {
        "type": None,
        "action_id": None,
        "preview_id": None,
        "user": None,
        "response_url": None,
        "trigger_id": None,
        "adjustment": None,
        "payload": None,
    }

    if isinstance(raw_body, (bytes, bytearray)):
        text = raw_body.decode("utf-8", errors="replace")
    elif isinstance(raw_body, str):
        text = raw_body
    else:
        return empty

    try:
        fields = parse_qs(text)
        raw_payload = (fields.get("payload") or [None])[0]
        if not raw_payload:
            return empty
        payload = json.loads(raw_payload)
    except Exception:  # noqa: BLE001 - untrusted input
        logger.warning("Could not parse Slack interactivity payload")
        return empty

    if not isinstance(payload, dict):
        return empty

    user = payload.get("user")
    user_name = None
    if isinstance(user, dict):
        user_name = user.get("username") or user.get("name") or user.get("id")

    interaction_type = payload.get("type")

    if interaction_type == TYPE_VIEW_SUBMISSION:
        # A modal submission carries no button, so the preview_id comes from
        # private_metadata and the entered value from the view's state.
        view = payload.get("view")
        view = view if isinstance(view, dict) else {}

        metadata = _private_metadata(view.get("private_metadata"))

        return {
            **empty,
            "type": TYPE_VIEW_SUBMISSION,
            "action_id": view.get("callback_id"),
            "preview_id": metadata.get("preview_id"),
            "user": user_name,
            # The ORIGINAL message's response_url, stashed when the modal was
            # opened -- a submission payload does not carry one.
            "response_url": metadata.get("response_url"),
            "adjustment": _view_value(view, ADJUST_BLOCK_ID, ADJUST_INPUT_ID),
            "payload": payload,
        }

    actions = payload.get("actions")
    action = actions[0] if isinstance(actions, list) and actions else {}
    action = action if isinstance(action, dict) else {}

    return {
        **empty,
        "type": interaction_type or TYPE_BLOCK_ACTIONS,
        "action_id": action.get("action_id"),
        # The button carries the preview_id as its value.
        "preview_id": action.get("value"),
        "user": user_name,
        "response_url": payload.get("response_url"),
        # Short-lived; needed to open a modal in response to this click.
        "trigger_id": payload.get("trigger_id"),
        "payload": payload,
    }


def _private_metadata(raw) -> dict:
    """Decode a modal's private_metadata.

    JSON since the response_url had to travel with the preview_id. A bare
    preview_id string is still accepted, so a modal opened by an older build
    submits successfully instead of failing to find its preview.
    """
    if not isinstance(raw, str) or not raw.strip():
        return {}

    try:
        decoded = json.loads(raw)
    except ValueError:
        return {"preview_id": raw}

    if isinstance(decoded, dict):
        return decoded
    return {"preview_id": raw}


def _view_value(view: dict, block_id: str, action_id: str):
    """Read one input's value out of a modal's state, defensively."""
    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    block = values.get(block_id) if isinstance(values, dict) else None
    element = block.get(action_id) if isinstance(block, dict) else None
    return element.get("value") if isinstance(element, dict) else None
