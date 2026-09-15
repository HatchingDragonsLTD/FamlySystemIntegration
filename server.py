"""HTTP entry point for the HubSpot -> Famly intake.

A deliberately tiny Flask app: one POST route that authenticates the caller
with a shared secret, hands the JSON body to the preview-only intake, and
returns the computed result plus any plan warnings. It never commits -- the
intake is dry-run only by construction (handle_intake raises if asked to write).

Run behind Caddy (which terminates TLS). Served by gunicorn under systemd in
production; `python server.py` runs Flask's dev server for local testing only.

Config comes from the environment (loaded from .env alongside the rest of the
app):
    INTAKE_SHARED_SECRET   required. Callers must send it as the
                           X-Intake-Secret header. Requests without it are 401.
    INTAKE_PLAN_VERSION    optional int, default 3. The plan `version` passed
                           through to preview.
    FAMLY_ACCESS_TOKEN / FAMLY_* are read by the existing core.config layer.
"""

import os
import dataclasses

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from actions.plan_write import hubspot_intake
from core.rest_client import RestHTTPError

load_dotenv()

app = Flask(__name__)

SHARED_SECRET = os.environ.get("INTAKE_SHARED_SECRET", "").strip()
PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))


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


@app.get("/health")
def health():
    """Unauthenticated liveness check (no secrets, no Famly calls)."""
    return jsonify({"status": "ok"}), 200


@app.post("/intake")
def intake():
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "body must be JSON"}), 400

    try:
        result = hubspot_intake.handle_intake(
            payload,
            version=PLAN_VERSION,
            dry_run=True,  # never commit from the webhook path
        )
    except RestHTTPError as exc:
        # Famly rejected the request. A 4xx from Famly is a bad-payload problem
        # (e.g. "please select a billing profile") -- it will NEVER succeed on
        # retry, so we must return a 4xx to HubSpot too. Returning 5xx here makes
        # HubSpot retry the same doomed request forever. Only Famly 5xx (a real
        # transient upstream fault) is worth a retry, so we pass 502 for those.
        if 400 <= exc.status_code < 500:
            return (
                jsonify({"error": "famly rejected the plan", "detail": str(exc)}),
                422,
            )
        return (
            jsonify({"error": "famly upstream error", "detail": str(exc)}),
            502,
        )
    except Exception as exc:  # noqa: BLE001 - unexpected server fault -> 500
        return jsonify({"error": "intake failed", "detail": str(exc)}), 500

    warnings = [
        dataclasses.asdict(w) if dataclasses.is_dataclass(w) else str(w)
        for w in result.warnings
    ]

    body = {
        "ok": result.ok,
        "errors": result.errors,
        "warnings": warnings,
        "previewed": result.result is not None,
    }
    # Validation failures are a client problem (bad payload) -> 422.
    status = 200 if result.ok else 422
    return jsonify(body), status


if __name__ == "__main__":
    # Local testing only. In production gunicorn serves `app` (see systemd unit).
    app.run(host="127.0.0.1", port=8000, debug=False)
