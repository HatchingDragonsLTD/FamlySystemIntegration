"""ONBOARD_ENFORCE_ALLOWLIST: default enforced; only an exact "off" turns the gate off."""

import contextlib
import io
import os
import unittest
from unittest import mock

import server
from actions.onboard import cli_status, config, store, web
from actions.onboard_dry_run import runner as dry_runner
from tests.test_onboard_phase2 import DEAL, Phase2Case


class AllowlistSwitchTests(Phase2Case):
    def setUp(self):
        super().setUp()
        os.environ.pop("ONBOARD_ENFORCE_ALLOWLIST", None)

    def off(self):
        os.environ["ONBOARD_ENFORCE_ALLOWLIST"] = "off"

    def test_default_and_invalid_values_stay_enforced(self):
        self.assertTrue(config.allowlist_enforced())
        for value in ("", "  ", "no", "false", "0", "disabled", "offf"):
            os.environ["ONBOARD_ENFORCE_ALLOWLIST"] = value
            self.assertTrue(config.allowlist_enforced(), value)
        self.mode("live", allowed="999")
        os.environ["ONBOARD_ENFORCE_ALLOWLIST"] = "nope"
        self.enqueue()
        self.assertEqual(self.run_worker()["code"], "deal_not_allowlisted")

    def test_off_lets_an_unlisted_deal_through_to_gate_a(self):
        self.mode("live", allowed="999")
        self.off()
        self.assertTrue(config.allowlist_enforced() is False)
        self.enqueue()
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), 1)         # Gate A still asks
        self.assertFalse(self.famly.child_creates)          # and nothing is written yet

    def test_off_lets_the_webhook_enqueue_an_unlisted_deal_but_the_stage_check_still_holds(self):
        client = server.app.test_client()
        self.mode("live", allowed="")
        self.off()
        with mock.patch.object(server, "SHARED_SECRET", "s"), mock.patch.object(web, "_notify"):
            response = client.post("/intake", json={"action": "onboard_enqueue", "dealId": DEAL},
                                   headers={"X-Intake-Secret": "s"})
        self.assertEqual(response.get_json()["data"]["outcome"], "queued")
        self.hub.stages["s-sales"] = ("Sales", "Contract Signed")
        self.hub.deals[DEAL]["dealstage"] = "s-sales"
        self.assertEqual(self.run_worker()["code"], "stage_not_eligible")

    def test_the_test_list_still_marks_test_children_with_the_allowlist_off(self):
        self.mode("live", allowed="", test=DEAL)
        self.off()
        self.assertTrue(config.is_test_deal(DEAL))
        self.run_through()
        self.assertIs(self.famly.child_creates[0]["isTestChild"], True)
        self.mode("live", allowed="", test="")
        self.assertFalse(config.is_test_deal(DEAL))

    def test_status_and_dry_run_report_the_state_and_startup_logs_it(self):
        self.assertIs(cli_status.handle(mock.Mock(deal_id=DEAL))["allowlistEnforced"], True)
        self.off()
        self.assertIs(cli_status.handle(mock.Mock(deal_id=DEAL))["allowlistEnforced"], False)
        self.assertIs(dry_runner.allowlist_enforced(), False)
        config.log_allowlist_state()
        self.assertIn("onboard allowlist enforced=False", self.logs)


if __name__ == "__main__":
    unittest.main()
