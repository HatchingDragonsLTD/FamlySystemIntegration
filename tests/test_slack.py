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

from actions.read_child_plans.runner import parse_plan, parse_response
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


PRICING_GROUP = "pg-active"
OTHER_PRICING_GROUP = "pg-old"


def _sample_plan_and_reference():
    """A plan with mirrored bookings, products, funding and a weekly total."""
    def booking(session_id, day, prices):
        return {
            "sessionId": session_id,
            "day": day,
            "monthlyPrices": [
                {"pricingGroupId": group, "price": price} for group, price in prices
            ],
        }

    bookings = [
        booking("s-full", "WEDNESDAY", [(PRICING_GROUP, 203.13)]),
        booking("s-full", "MONDAY", [(PRICING_GROUP, 203.13), (OTHER_PRICING_GROUP, 180.0)]),
        booking("s-am", "FRIDAY", [(PRICING_GROUP, 101.5)]),
        booking("s-full", "TUESDAY", [(PRICING_GROUP, 203.13)]),
    ]
    plan_node = {
        "id": "plan-1",
        "childId": "child-1",
        "from": "2026-09-01",
        "to": None,
        "pricingGroupId": PRICING_GROUP,
        "monthlyEstimate": 812.5,
        "publicFunding": {"amount": 210.0, "hours": 15, "minutes": 30},
        "planParts": [
            {
                "planPartId": "pp-1",
                "sessionBookings": bookings,
                "productBookings": [
                    {"productId": "p-lunch", "day": "MONDAY", "amount": 1, "bookedPrice": 3.5},
                    {"productId": "p-nap", "day": "WEDNESDAY", "amount": 3},
                ],
            }
        ],
        # Famly mirrors these at plan level; they must not be listed twice.
        "sessionBookings": list(bookings),
        "productBookings": [{"productId": "p-lunch", "day": "MONDAY", "amount": 1}],
        "planStates": [
            {
                "from": "2026-09-01",
                "to": None,
                "planPartStates": [{"planPartId": "pp-1", "weeklyTotal": 187.5}],
            }
        ],
    }
    reference_body = {
        "plans": [],
        "sessions": [
            {"id": "s-full", "title": "Full Day"},
            {"id": "s-am", "title": "Morning Session"},
        ],
        "products": [
            {"id": "p-lunch", "title": "Hot Lunch"},
            {"id": "p-nap", "title": "Nappies"},
        ],
    }
    return parse_plan(plan_node), parse_response(reference_body)


class BuildSummaryTests(unittest.TestCase):
    """The summary is what an approver reads before clicking Approve."""

    def setUp(self):
        self.plan, self.reference = _sample_plan_and_reference()
        self.text = slack.build_summary(
            self.plan,
            [],
            session_titles=self.reference.session_titles,
            product_titles=self.reference.product_titles,
        )

    def test_lists_each_session_once_in_week_order(self):
        session_lines = [
            line for line in self.text.splitlines() if line.startswith("• ") and "—" in line
        ]
        days = [line.split(" — ")[0].removeprefix("• ") for line in session_lines]

        # Four bookings, mirrored at both levels -- must appear four times, not eight.
        self.assertEqual(days[:4], ["Monday", "Tuesday", "Wednesday", "Friday"])
        self.assertIn("*Sessions* (4)", self.text)

    def test_uses_session_titles_from_the_reference(self):
        self.assertIn("• Monday — Full Day — 203.13", self.text)
        self.assertIn("• Friday — Morning Session — 101.50", self.text)

    def test_falls_back_to_session_id_when_the_title_is_unknown(self):
        plan = parse_plan(
            {
                "planParts": [
                    {"planPartId": "pp", "sessionBookings": [{"sessionId": "s-x", "day": "MONDAY"}]}
                ]
            }
        )
        text = slack.build_summary(plan, [], session_titles=self.reference.session_titles)
        self.assertIn("• Monday — s-x", text)

    def test_lists_products_with_amount_and_title(self):
        self.assertIn("*Products* (2)", self.text)
        self.assertIn("• Monday — 1× Hot Lunch — 3.50", self.text)
        self.assertIn("• Wednesday — 3× Nappies", self.text)  # no bookedPrice -> no price

    def test_products_section_is_omitted_when_there_are_none(self):
        plan = parse_plan(
            {
                "planParts": [
                    {"planPartId": "pp", "sessionBookings": [{"sessionId": "s-full", "day": "MONDAY"}]}
                ]
            }
        )
        text = slack.build_summary(plan, [], session_titles=self.reference.session_titles)
        self.assertNotIn("*Products*", text)

    def test_shows_weekly_and_monthly_totals(self):
        self.assertIn("• Weekly total: 187.50", self.text)
        self.assertIn("• Monthly estimate: 812.50", self.text)

    def test_notes_a_mid_plan_rate_change_without_enumerating_periods(self):
        plan = parse_plan(
            {
                "planStates": [
                    {"planPartStates": [{"weeklyTotal": 187.5}]},
                    {"planPartStates": [{"weeklyTotal": 200.0}]},
                ]
            }
        )
        text = slack.build_summary(plan, [])
        self.assertIn("rate changes during plan", text)
        # The first (current) state's figure is the one shown.
        self.assertIn("• Weekly total: 187.50", text)
        self.assertNotIn("200.00", text)

    def test_public_funding_shown_only_when_funded(self):
        self.assertIn("• Public funding: 210.00 (15h 30m)", self.text)

        unfunded = parse_plan({"publicFunding": None, "planParts": []})
        self.assertNotIn("Public funding", slack.build_summary(unfunded, []))

    def test_warnings_are_surfaced(self):
        warnings = [
            {"warning": {"title": "Funding mismatch", "severity": "warning"}, "known": {"key": "funding_mismatch"}},
            {"warning": {"title": "Unseen problem", "severity": "error"}, "known": None},
        ]
        text = slack.build_summary(self.plan, warnings, session_titles=self.reference.session_titles)
        self.assertIn("*2 warning(s)*", text)
        self.assertIn("[funding_mismatch]", text)
        self.assertIn("[UNTRACKED]", text)

    def test_price_comes_from_the_active_pricing_group_only(self):
        # MONDAY has both the active group's 203.13 and another group's 180.00.
        self.assertIn("• Monday — Full Day — 203.13", self.text)
        self.assertNotIn("180.00", self.text)

    def test_price_is_omitted_when_the_active_group_has_none(self):
        plan = parse_plan(
            {
                "pricingGroupId": PRICING_GROUP,
                "planParts": [
                    {
                        "planPartId": "pp",
                        "sessionBookings": [
                            {
                                "sessionId": "s-full",
                                "day": "MONDAY",
                                # Only a price for a DIFFERENT pricing group.
                                "monthlyPrices": [
                                    {"pricingGroupId": OTHER_PRICING_GROUP, "price": 180.0}
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        text = slack.build_summary(
            plan, [], session_titles=self.reference.session_titles
        )
        # Better no price than the wrong one.
        self.assertIn("• Monday — Full Day", text)
        self.assertNotIn("180.00", text)

    def test_falls_back_to_ids_without_a_catalogue(self):
        text = slack.build_summary(self.plan, [])
        self.assertIn("• Monday — s-full — 203.13", text)
        self.assertIn("• Monday — 1× p-lunch — 3.50", text)
        self.assertNotIn("Full Day", text)

    def test_missing_reference_and_plan_never_raise(self):
        self.assertIn("Monday — s-full", slack.build_summary(self.plan, []))
        self.assertIn("no plan returned", slack.build_summary(None, []))


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
