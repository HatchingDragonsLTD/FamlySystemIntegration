"""Tests for the onboarding HTTP trigger: the `onboard_enqueue` action on /intake.

Run from the project root:

    python -m unittest tests.test_onboard_webhook -q

Fakes only. Requests go through Flask's test client against the real router, so
the existing secret check and dispatch are what is exercised. The webhook never
calls Famly or HubSpot; the worker tests use the phase-2 fakes.
"""

import contextlib
import hmac
import io
import json
import sqlite3
import unittest
from unittest import mock

import server
from actions.onboard import cli_enqueue, store, web
from tests.test_onboard_phase2 import DEAL, PII, Phase2Case
from tests import fast_sqlite  # noqa: F401  (SQLite without fsync: see the module)

SECRET = "s3cret-value-for-tests"


class WebhookCase(Phase2Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(server, "SHARED_SECRET", SECRET)
        patcher.start()
        self.addCleanup(patcher.stop)
        notices = mock.patch.object(web, "_notify")
        self.slack = notices.start()
        self.addCleanup(notices.stop)
        self.client = server.app.test_client()

    def post(self, body, secret=SECRET, headers=None):
        send = dict(headers or {})
        if secret is not None:
            send["X-Intake-Secret"] = secret
        return self.client.post("/intake", json=body, headers=send)

    def trigger(self, deal_id=DEAL, **extra):
        return self.post({"action": "onboard_enqueue", "dealId": deal_id, **extra})

    def data(self, response):
        return response.get_json()["data"]


class AuthTests(WebhookCase):
    def test_a_bad_or_missing_secret_is_401_with_no_detail_and_no_job(self):
        for secret in ("wrong", "", SECRET + "x", None):
            with self.subTest(secret=secret):
                response = self.post({"action": "onboard_enqueue", "dealId": DEAL}, secret=secret)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json(), {"error": "unauthorized"})
        self.assertIsNone(store.latest_for_deal(DEAL))

    def test_a_server_with_no_secret_configured_rejects_everything(self):
        with mock.patch.object(server, "SHARED_SECRET", ""):
            self.assertEqual(self.trigger().status_code, 401)
            self.assertEqual(self.post({"action": "onboard_enqueue", "dealId": DEAL},
                                       secret="").status_code, 401)

    def test_the_secret_is_compared_in_constant_time(self):
        with mock.patch.object(server.hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.trigger()
            self.trigger(secret="nope")
        self.assertEqual(compare.call_count, 2)

    def test_the_existing_route_and_envelope_are_used_not_a_new_one(self):
        body = self.trigger().get_json()
        self.assertEqual(set(body), {"ok", "action", "data", "warnings", "errors"})
        self.assertEqual((body["ok"], body["action"]), (True, "onboard_enqueue"))
        self.assertEqual(self.client.post("/onboard", json={}).status_code, 404)  # no new route


class ValidationTests(WebhookCase):
    def test_a_deal_id_that_is_not_digits_only_is_400_and_never_echoed(self):
        for bad in ("12a", "", " 123", "123 ", "1.5", 12.5, True, False, None, [1], {"a": 1},
                    "-5", -5, "٣٤", "1" * 21, "1e5", "0x10", "12;DROP"):
            with self.subTest(dealId=bad):
                response = self.trigger(deal_id=bad)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("12a", response.get_data(as_text=True))
        missing = self.post({"action": "onboard_enqueue"})
        self.assertEqual(missing.status_code, 400)
        self.assertIsNone(store.latest_for_deal(DEAL))
        self.assertEqual(self.slack.call_count, 0)

    def test_a_number_and_a_string_id_are_both_accepted(self):
        number = self.trigger(deal_id=1001)
        self.assertEqual((number.status_code, self.data(number)["outcome"]), (200, "queued"))
        string = self.trigger(deal_id="1001")
        self.assertEqual((string.status_code, self.data(string)["outcome"]), (200, "already_queued"))
        self.assertEqual(self.data(number)["jobId"], self.data(string)["jobId"])
        self.assertEqual(store.latest_for_deal("1001").deal_id, "1001")

    def test_the_request_makes_no_famly_or_hubspot_call(self):
        self.trigger()
        self.assertEqual(self.hub.calls, [])
        self.assertEqual(self.famly.calls, [])
        self.assertEqual(self.rest.gets, [])
        self.assertEqual(self.hub.writes, [])


class RefusalTests(WebhookCase):
    def test_mode_off_is_2xx_with_a_code_and_no_job(self):
        self.mode("off")
        response = self.trigger()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response)["outcome"], "refused_mode_off")
        self.assertIsNone(self.data(response)["jobId"])
        self.assertIsNone(store.latest_for_deal(DEAL))
        self.assertEqual(self.slack.call_count, 0)

    def test_a_deal_outside_the_live_allowlist_is_2xx_with_no_job_and_a_slack_notice(self):
        self.mode("live", allowed="999")
        response = self.trigger()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response)["outcome"], "refused_not_allowlisted")
        self.assertIsNone(store.latest_for_deal(DEAL))
        self.slack.assert_called_once()
        notice = self.slack.call_args.args[0]
        self.assertIn(DEAL, notice)                       # the deal id, and only that
        self.assertIn("deal_not_allowlisted", notice)
        for value in PII:
            self.assertNotIn(value, notice.casefold())

    def test_dry_mode_queues_any_deal_because_it_writes_nothing(self):
        self.mode("dry", allowed="")
        response = self.trigger()
        self.assertEqual(self.data(response)["outcome"], "queued")
        self.assertEqual(store.latest_for_deal(DEAL).mode, "dry")

    def test_a_duplicate_request_returns_the_existing_job(self):
        first = self.data(self.trigger())
        again = self.data(self.trigger())
        self.assertEqual((first["outcome"], again["outcome"]), ("queued", "already_queued"))
        self.assertEqual(first["jobId"], again["jobId"])
        self.assertEqual(sum(store.status_counts_for_deal(DEAL).values()), 1)
        store.set_status(first["jobId"], store.DONE)
        done = self.trigger()
        self.assertEqual(done.status_code, 200)
        self.assertEqual(self.data(done)["outcome"], "already_done")
        self.assertEqual(sum(store.status_counts_for_deal(DEAL).values()), 1)

    def test_a_genuine_internal_error_is_5xx_and_never_a_refusal(self):
        with mock.patch.object(store, "enqueue",
                               side_effect=sqlite3.OperationalError("disk I/O error Zara Quillfeather")):
            response = self.trigger()
        self.assertEqual(response.status_code, 500)
        text = response.get_data(as_text=True)
        self.assertNotIn("Zara", text)
        self.assertNotIn("disk I/O", text)             # the text is not returned
        self.assertTrue(any("OperationalError" in line for line in self.logs))   # the type is logged
        self.assertFalse(any("Zara" in line for line in self.logs))
        # Deliberate refusals are NOT 5xx.
        self.mode("off")
        self.assertEqual(self.trigger().status_code, 200)


class SourceAndStageTests(WebhookCase):
    def ineligible(self):
        self.hub.stages["s-sales"] = ("Sales", "Contract Signed")
        self.hub.deals[DEAL]["dealstage"] = "s-sales"

    def test_a_job_from_the_webhook_is_marked_with_its_source(self):
        self.trigger()
        self.assertEqual(store.latest_for_deal(DEAL).source, "webhook")
        store._reset_for_tests()
        cli_enqueue.handle(mock.Mock(deal_id=DEAL, file=None))
        self.assertEqual(store.latest_for_deal(DEAL).source, "cli")
        with self.assertRaises(ValueError):
            store.enqueue("5", "live", source="carrier-pigeon")

    def test_a_webhook_job_at_the_wrong_stage_holds_before_any_other_read_or_write(self):
        self.ineligible()
        self.trigger()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.HELD, "stage_not_eligible"))
        self.assertEqual(self.famly.calls, [])                  # not even a Famly read
        self.assertEqual(self.hub.writes, [])                   # no HubSpot write-back either
        self.assertEqual(self.notify.gates, [])
        self.assertFalse(self.famly.child_creates or self.famly.contact_creates)
        self.assertTrue(any("stage_not_eligible" in n for n in self.notify.notices))
        # Its first read was the stage check: deal and pipelines, nothing else yet.
        self.assertEqual(self.hub.calls[:1], ["get_deal"])

    def test_the_wrong_pipeline_is_not_eligible_even_at_a_stage_with_the_same_name(self):
        self.hub.stages["s-sales"] = ("Sales", "Deposit Pending")
        self.hub.deals[DEAL]["dealstage"] = "s-sales"
        self.trigger()
        self.assertEqual(self.run_worker()["code"], "stage_not_eligible")

    def test_a_webhook_job_at_operations_deposit_pending_goes_on_to_approval(self):
        self.trigger()                                          # fixture stage is Operations / Deposit Pending
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), 1)

    def test_stage_matching_ignores_case_and_stray_whitespace(self):
        self.hub.stages["s-pending"] = (" OPERATIONS ", "  Deposit   Pending ")
        self.trigger()
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)

    def test_a_cli_job_has_no_stage_restriction(self):
        self.ineligible()
        cli_enqueue.handle(mock.Mock(deal_id=DEAL, file=None))
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)    # not held
        self.assertNotEqual(result["code"], "stage_not_eligible")

    def test_a_webhook_job_in_dry_mode_is_held_too_and_writes_nothing(self):
        self.ineligible()
        self.mode("dry")
        self.trigger()
        self.assertEqual(self.run_worker()["code"], "stage_not_eligible")
        self.assertEqual(self.hub.writes, [])

    def test_a_deal_that_cannot_be_found_holds_with_no_writes(self):
        self.mode("live", allowed=f"{DEAL},4242")
        self.trigger(deal_id="4242")
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.HELD, "deal_not_found"))
        self.assertEqual(self.hub.writes, [])


class NoPersonalDataTests(WebhookCase):
    def test_nothing_personal_or_secret_is_logged_or_returned(self):
        body = {"action": "onboard_enqueue", "dealId": DEAL,
                "name": "Zara Quillfeather", "email": "ada.l@example.org",
                "phone": "07700 900123", "dob": "2024-01-15", "address": "1 Mill Lane"}
        responses = [
            self.post(body),                                    # accepted
            self.post(body),                                    # duplicate
            self.post(body, secret="wrong-secret-attempt"),     # rejected
            self.post({**body, "dealId": "not-a-number"}),      # bad request
        ]
        self.mode("off")
        responses.append(self.post(body))                       # refused
        self.mode("live", allowed="999")
        responses.append(self.post(body))                       # refused, notice sent
        text = "\n".join(r.get_data(as_text=True) for r in responses).casefold()
        logged = "\n".join(self.logs).casefold()
        notices = "\n".join(call.args[0] for call in self.slack.call_args_list).casefold()
        for value in PII:
            for where, blob in (("response", text), ("log", logged), ("notice", notices)):
                self.assertNotIn(value, blob, f"{value} in {where}")
        for secret in (SECRET.casefold(), "wrong-secret-attempt", "x-intake-secret"):
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret, text)
        self.assertTrue(self.logs)          # and logging was on, so the checks mean something


if __name__ == "__main__":
    unittest.main()
