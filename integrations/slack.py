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

The inbound half is SECURITY-CRITICAL and fully verified even though nothing
acts on it yet: the signature check is proven before a commit path is ever
wired to it.

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
POST_TIMEOUT = 10

# action_id values carried by the buttons. The inbound handler switches on these.
ACTION_APPROVE = "plan_approve"
ACTION_REJECT = "plan_reject"

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


def build_summary(plan_summary: dict, warnings: list) -> str:
    """Format the key figures as plain, scannable Slack text.

    Args:
        plan_summary: the flat summary dict from the plan_preview action.
        warnings: the serialised warnings from the same result.

    Returns:
        Message text. Defensive: a missing field reads as "unknown" rather
        than raising, since this runs on the success path and must not break it.
    """
    summary = plan_summary if isinstance(plan_summary, dict) else {}

    child_id = summary.get("childId") or "unknown"
    date_from = summary.get("from") or "unknown"
    date_to = summary.get("to") or "open-ended"
    estimate = _money(summary.get("monthlyEstimate"))
    sessions = summary.get("sessionCount")
    sessions = "unknown" if sessions is None else str(sessions)

    funding_amount = summary.get("publicFundingAmount")
    funding_hours = summary.get("publicFundingHours")
    funding_minutes = summary.get("publicFundingMinutes")

    lines = [
        "*Plan preview awaiting approval*",
        f"• Child: `{child_id}`",
        f"• Dates: {date_from} → {date_to}",
        f"• Monthly estimate: {estimate}",
        f"• Sessions booked: {sessions}",
    ]

    if funding_amount is not None or funding_hours is not None:
        hours_part = "unknown" if funding_hours is None else f"{funding_hours}h"
        if funding_minutes:
            hours_part += f" {funding_minutes}m"
        lines.append(
            f"• Public funding: {_money(funding_amount)} ({hours_part})"
        )

    count = len(warnings) if isinstance(warnings, list) else 0
    if count:
        lines.append(f"• :warning: *{count} warning(s)* — review before approving:")
        for warning in warnings[:5]:
            lines.append(f"    – {_warning_line(warning)}")
        if count > 5:
            lines.append(f"    – …and {count - 5} more")
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
    except ValueError:
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

    Returns:
        A dict with `action_id`, `preview_id`, `user`, `response_url` and the
        full `payload`. Missing pieces come back as None -- defensive
        throughout, since this parses untrusted input.
    """
    empty = {
        "action_id": None,
        "preview_id": None,
        "user": None,
        "response_url": None,
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

    actions = payload.get("actions")
    action = actions[0] if isinstance(actions, list) and actions else {}
    action = action if isinstance(action, dict) else {}

    user = payload.get("user")
    user_name = None
    if isinstance(user, dict):
        user_name = user.get("username") or user.get("name") or user.get("id")

    return {
        "action_id": action.get("action_id"),
        # The button carries the preview_id as its value.
        "preview_id": action.get("value"),
        "user": user_name,
        "response_url": payload.get("response_url"),
        "payload": payload,
    }
