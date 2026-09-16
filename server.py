"""HTTP entry point for the HubSpot -> Famly intake.

A deliberately tiny Flask app acting as a ROUTER: one POST route that
authenticates the caller with a shared secret, reads the `action` field from
the JSON body, dispatches to that action's handler via `web_registry`, and
wraps whatever comes back in a standard envelope.

The /intake path never commits: the only registered action is preview-only by
construction. The Slack Approve button IS a write path -- it commits a
previously previewed plan, behind the guards in
actions/plan_write/approval.py (COMMIT_ENABLED, an allow-list of child IDs, a
TTL, and an atomic claim that makes a double-click safe).

This file holds NO per-action logic and must not import any `actions.*` module
directly: the registry owns that. Adding an action means adding one entry to
`web_registry.ACTIONS` (see the note there); nothing here changes.

Request body:

    {"action": "plan_preview", ...action fields...}

Response envelope, applied to every action:

    {"ok": bool, "action": "<name>", "data": {...},
     "warnings": [...], "errors": [...]}

Run behind Caddy (which terminates TLS). Served by gunicorn under systemd in
production; `python server.py` runs Flask's dev server for local testing only.

Config comes from the environment (loaded from .env alongside the rest of the
app):
    INTAKE_SHARED_SECRET   required. Callers must send it as the
                           X-Intake-Secret header. Requests without it are 401.
    INTAKE_PLAN_VERSION    optional int, default 3. Read by the plan_preview
                           action itself, not here.
    SLACK_SIGNING_SECRET   required for /slack/interactivity. Without it that
                           route rejects everything (fails closed).
    SLACK_BOT_TOKEN / SLACK_CHANNEL_ID are read by integrations/slack.py.
    COMMIT_ENABLED / COMMIT_ALLOWED_CHILD_IDS gate the commit path; see
                           core/config.py and actions/plan_write/approval.py.
    FAMLY_ACCESS_TOKEN / FAMLY_* are read by the existing core.config layer.

Routes:
    GET  /health              unauthenticated liveness check
    POST /intake              shared-secret auth, dispatches by `action`
    POST /slack/interactivity Slack-signature auth; Approve commits a plan
"""

import logging
import os

from flask import Flask, request, jsonify
from dotenv import load_dotenv

import web_registry
from core.rest_client import RestHTTPError
from integrations import slack

load_dotenv()

app = Flask(__name__)

logger = logging.getLogger(__name__)

SHARED_SECRET = os.environ.get("INTAKE_SHARED_SECRET", "").strip()


def _authorized(req) -> bool:
    """Constant-ish check of the shared secret header.

    Returns False when no secret is configured, so a misconfigured server
    fails closed rather than accepting everything.
    """
    if not SHARED_SECRET:
        return False
    provided = req.headers.get("X-Intake-Secret", "")
    # Simple equality is fine here; both values are short server-side secrets.
    return provided == SHARED_SECRET


def _envelope(
    action: str | None,
    *,
    ok: bool,
    data: dict | None = None,
    warnings: list | None = None,
    errors: list | None = None,
) -> dict:
    """The one response shape every action returns through."""
    return {
        "ok": ok,
        "action": action,
        "data": data or {},
        "warnings": warnings or [],
        "errors": errors or [],
    }


def _dispatch(action: str, handler, payload: dict) -> tuple[dict, int]:
    """Call an action handler and classify anything it raises.

    Classification lives here so every action inherits it identically:

      * Famly 4xx -> 422. A bad payload (e.g. "please select a billing
        profile") will NEVER succeed on retry, so HubSpot must not retry it.
        Returning 5xx here makes HubSpot retry the same doomed request forever.
      * Famly 5xx -> 502. A real transient upstream fault, worth a retry.
      * anything else -> 500.
    """
    try:
        data, status = handler(payload)
    except RestHTTPError as exc:
        if 400 <= exc.status_code < 500:
            return (
                _envelope(
                    action,
                    ok=False,
                    errors=["famly rejected the plan", str(exc)],
                ),
                422,
            )
        return (
            _envelope(
                action,
                ok=False,
                errors=["famly upstream error", str(exc)],
            ),
            502,
        )
    except Exception as exc:  # noqa: BLE001 - unexpected server fault -> 500
        return (
            _envelope(action, ok=False, errors=["intake failed", str(exc)]),
            500,
        )

    # Handlers return warnings/errors alongside their data; the envelope owns
    # those two, so lift them out of the action payload.
    data = dict(data)
    warnings = data.pop("warnings", [])
    errors = data.pop("errors", [])

    return (
        _envelope(
            action,
            ok=status == 200,
            data=data,
            warnings=warnings,
            errors=errors,
        ),
        status,
    )


@app.get("/health")
def health():
    """Unauthenticated liveness check (no secrets, no Famly calls)."""
    return jsonify({"status": "ok"}), 200


@app.post("/intake")
def intake():
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "body must be a JSON object"}), 400

    action = payload.get("action")
    valid = ", ".join(web_registry.action_names())

    if not action:
        return (
            jsonify(
                _envelope(
                    None,
                    ok=False,
                    errors=[f"missing 'action' field. Valid actions: {valid}"],
                )
            ),
            400,
        )

    handler = web_registry.get_handler(action)
    if handler is None:
        return (
            jsonify(
                _envelope(
                    action,
                    ok=False,
                    errors=[f"unknown action {action!r}. Valid actions: {valid}"],
                )
            ),
            400,
        )

    body, status = _dispatch(action, handler, payload)
    return jsonify(body), status


@app.post("/slack/interactivity")
def slack_interactivity():
    """Receive a Slack button click.

    Separate from /intake and deliberately NOT behind X-Intake-Secret: Slack
    authenticates by signing the request, not by a shared header.

    SLACK APP CONFIGURATION: set the interactivity request URL to
    https://<domain>/slack/interactivity in the Slack app settings.

    Approve COMMITS the previewed plan, subject to the guards in
    actions/plan_write/approval.py. Reject records the decision and writes
    nothing. Both require a valid Slack signature.
    """
    # The signature is computed over the RAW body. Read it before touching
    # request.form -- re-serialising parsed form data would change the bytes and
    # the signature would never match.
    raw_body = request.get_data()

    if not slack.verify_slack_request(request.headers, raw_body):
        logger.warning("Rejected an unverified Slack interactivity request")
        return jsonify({"error": "invalid slack signature"}), 401

    interaction = slack.parse_interaction(raw_body)
    action_id = interaction.get("action_id")
    preview_id = interaction.get("preview_id")
    user = interaction.get("user")

    if action_id not in (slack.ACTION_APPROVE, slack.ACTION_REJECT):
        logger.warning(
            "SLACK INTERACTION ignored: unrecognised action=%s preview_id=%s",
            action_id,
            preview_id,
        )
        return jsonify({"text": "Unrecognised action."}), 200

    logger.info(
        "SLACK APPROVAL: preview_id=%s action=%s user=%s",
        preview_id,
        action_id,
        user,
    )

    # The commit path. Approve writes to Famly; the decision and every guard
    # live in the action (via the registry), so this file keeps no per-action
    # logic. It never raises -- a failure comes back as text for the approver.
    text = web_registry.handle_slack_click(action_id, preview_id, user)

    # The message was posted proactively with chat.postMessage, so an inline
    # `replace_original` response does not reliably update it. POST to the
    # interaction's response_url instead, and ack with an empty 200 -- Slack
    # only needs a prompt acknowledgement here.
    response_url = interaction.get("response_url")

    if response_url:
        slack.update_message(response_url, text)
        return "", 200

    # No response_url (shouldn't happen): fall back to the inline response so
    # the update is at least attempted.
    logger.warning(
        "Slack interaction had no response_url; falling back to the inline "
        "response for preview_id=%s",
        preview_id,
    )
    return jsonify({"replace_original": True, "text": text}), 200


if __name__ == "__main__":
    # Local testing only. In production gunicorn serves `app` (see systemd unit).
    app.run(host="127.0.0.1", port=8000, debug=False)
