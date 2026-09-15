"""HTTP entry point for the HubSpot -> Famly intake.

A deliberately tiny Flask app acting as a ROUTER: one POST route that
authenticates the caller with a shared secret, reads the `action` field from
the JSON body, dispatches to that action's handler via `web_registry`, and
wraps whatever comes back in a standard envelope.

It never commits -- the only registered action is preview-only by construction.

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
    FAMLY_ACCESS_TOKEN / FAMLY_* are read by the existing core.config layer.
"""

import os

from flask import Flask, request, jsonify
from dotenv import load_dotenv

import web_registry
from core.rest_client import RestHTTPError

load_dotenv()

app = Flask(__name__)

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


if __name__ == "__main__":
    # Local testing only. In production gunicorn serves `app` (see systemd unit).
    app.run(host="127.0.0.1", port=8000, debug=False)
