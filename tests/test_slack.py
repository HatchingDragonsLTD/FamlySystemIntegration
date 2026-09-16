"""Tests for the Slack integration.

Run from the project root:

    python -m unittest discover -s tests -v

Stdlib unittest only -- no extra dependency, no running server, no real Slack.
`requests.post` is swapped for a recorder so the exact outbound JSON can be
asserted on.
"""

import hashlib
import hmac
import json
import logging
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from integrations import slack

# The integration logs loudly on failure by design; keep the test output clean.
logging.disable(logging.CRITICAL)

SIGNING_SECRET = "test-signing-secret"
RESPONSE_URL = "https://hooks.slack.com/actions/T000/123/abc"


class FakeResponse:
    def __init__(self, status_code=200, text="ok", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class UpdateMessageTests(unittest.TestCase):
    """update_message must POST the exact shape Slack expects."""

    def test_posts_replace_original_json_to_response_url(self):
        recorded = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            recorded.update(url=url, headers=headers, json=json, timeout=timeout)
            # A response_url POST answers with a plain "ok" body, not JSON.
            return FakeResponse(200, "ok")

        with mock.patch.object(slack.requests, "post", fake_post):
            result = slack.update_message(RESPONSE_URL, "✅ Approved")

        self.assertTrue(result)
        self.assertEqual(recorded["url"], RESPONSE_URL)
        self.assertEqual(
            recorded["json"],
            {"replace_original": True, "text": "✅ Approved"},
        )
        self.assertIn("application/json", recorded["headers"]["Content-Type"])
        self.assertEqual(recorded["timeout"], slack.POST_TIMEOUT)

    def test_missing_response_url_returns_false_without_posting(self):
        def fail(*args, **kwargs):
            raise AssertionError("must not post without a response_url")

        with mock.patch.object(slack.requests, "post", fail):
            self.assertFalse(slack.update_message("", "text"))
            self.assertFalse(slack.update_message(None, "text"))

    def test_non_200_is_reported_as_failure(self):
        with mock.patch.object(
            slack.requests, "post", lambda *a, **k: FakeResponse(404, "not_found")
        ):
            self.assertFalse(slack.update_message(RESPONSE_URL, "text"))

    def test_ok_false_body_is_reported_as_failure(self):
        response = FakeResponse(200, "", {"ok": False, "error": "expired_url"})
        with mock.patch.object(slack.requests, "post", lambda *a, **k: response):
            self.assertFalse(slack.update_message(RESPONSE_URL, "text"))

    def test_network_error_never_raises(self):
        def boom(*args, **kwargs):
            raise ConnectionError("slack unreachable")

        with mock.patch.object(slack.requests, "post", boom):
            self.assertFalse(slack.update_message(RESPONSE_URL, "text"))


class VerifySlackRequestTests(unittest.TestCase):
    """The signature check gates an endpoint a commit path will later use."""

    def setUp(self):
        self.body = urlencode({"payload": json.dumps({"actions": []})})
        patcher = mock.patch.dict(
            "os.environ", {"SLACK_SIGNING_SECRET": SIGNING_SECRET}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _headers(self, body, timestamp=None, secret=SIGNING_SECRET):
        timestamp = timestamp or str(int(time.time()))
        base = f"v0:{timestamp}:{body}".encode()
        digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return {
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": f"v0={digest}",
        }

    def test_accepts_a_valid_signature(self):
        self.assertTrue(
            slack.verify_slack_request(self._headers(self.body), self.body)
        )

    def test_rejects_a_tampered_body(self):
        self.assertFalse(
            slack.verify_slack_request(self._headers(self.body), self.body + "&x=1")
        )

    def test_rejects_a_replayed_request(self):
        old = str(int(time.time()) - slack.MAX_REQUEST_AGE_SECONDS - 60)
        self.assertFalse(
            slack.verify_slack_request(
                self._headers(self.body, timestamp=old), self.body
            )
        )

    def test_fails_closed_without_a_signing_secret(self):
        with mock.patch.dict("os.environ", {"SLACK_SIGNING_SECRET": ""}):
            self.assertFalse(
                slack.verify_slack_request(self._headers(self.body), self.body)
            )


class ParseInteractionTests(unittest.TestCase):
    def test_extracts_action_preview_id_and_response_url(self):
        payload = {
            "user": {"username": "drew"},
            "response_url": RESPONSE_URL,
            "actions": [{"action_id": slack.ACTION_APPROVE, "value": "prev-123"}],
        }
        body = urlencode({"payload": json.dumps(payload)})

        parsed = slack.parse_interaction(body)

        self.assertEqual(parsed["action_id"], slack.ACTION_APPROVE)
        self.assertEqual(parsed["preview_id"], "prev-123")
        self.assertEqual(parsed["response_url"], RESPONSE_URL)
        self.assertEqual(parsed["user"], "drew")

    def test_malformed_input_yields_empty_fields(self):
        for bad in (b"", "not-a-form", urlencode({"payload": "{broken"}), None):
            parsed = slack.parse_interaction(bad)
            self.assertIsNone(parsed["action_id"])
            self.assertIsNone(parsed["preview_id"])


if __name__ == "__main__":
    unittest.main()
