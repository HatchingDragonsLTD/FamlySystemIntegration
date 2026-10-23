"""Batch onboarding from a file of deal ids: parsing, scope, validation, eligibility and the
file-route gate. Ids, counts and codes only."""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from actions.onboard import approval, batch, cli_enqueue, config, store
from actions.onboard_dry_run import cli as dry_cli
from core.hubspot import HubSpotError
from integrations import slack
from tests.test_onboard_dry_run import FakeHubSpot, make_deal
from tests.test_onboard_phase2 import DEAL, Phase2Case

HASH = "f" * 64


class Hub(FakeHubSpot):
    def __init__(self, ids, **stages):
        super().__init__()
        self.stages = {"s-pending": ("Operations", "Deposit Pending"),
                       "s-lost": ("Operations", "Closed Lost"),
                       "s-sales": ("Sales", "Quote Sent")}
        self.deals = {i: make_deal(dealstage=stages.get(i, "s-pending")) for i in ids}


class ParsingTests(unittest.TestCase):
    def test_comments_blanks_and_duplicates(self):
        self.assertEqual(batch.parse_ids("# top\n12\n\n34 # note\n12\n"), (["12", "34"], None))

    def test_a_bad_line_names_its_number_and_never_its_content(self):
        self.assertEqual(batch.parse_ids("12\n\nabc-secret\n34\n"), ([], 3))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "ids.txt")
            path.write_text("12\nsecret@example.org\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit):
                batch.read_file(str(path))
            self.assertIn("line 2", err.getvalue())
            self.assertNotIn("secret", err.getvalue())

    def test_the_file_hash_is_of_the_bytes_and_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "ids.txt")
            path.write_text("12\n34\n", encoding="utf-8")
            first = batch.read_file(str(path))
            self.assertEqual(first, batch.read_file(str(path)))
            self.assertEqual(len(first[1]), 64)


class ScopeTests(Phase2Case):
    def args(self, path, **over):
        base = dict(deal_id=None, file=path, limit=None, all=False, skip_invalid=False, include_review=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_a_file_run_needs_exactly_one_of_limit_or_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "ids.txt")
            path.write_text("12\n", encoding="utf-8")
            for over in ({}, {"limit": 1, "all": True}, {"limit": 0}):
                with self.subTest(over=over), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli_enqueue.handle(self.args(str(path), **over))
        self.assertIsNone(store.latest_for_deal("12"))

    def test_the_dry_run_file_has_the_same_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "ids.txt")
            path.write_text("12\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                dry_cli.handle(argparse.Namespace(file=str(path), deal_id=None, deal_ids=None,
                                                  limit=None, all=False))

    def test_mode_off_refuses_as_today(self):
        self.mode("off")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli_enqueue.handle(self.args("anything", all=True))


class EnqueueFileTests(Phase2Case):
    IDS = ["101", "102", "103", "104", "105", "106", "107"]

    def run_file(self, ids, hub=None, mode="live", **over):
        kwargs = dict(limit=None, skip_invalid=False, include_review=False)
        kwargs.update(over)
        return batch.enqueue_file(hub or Hub(ids), ids, HASH, mode, **kwargs)

    def seed(self, deal, status, code=None, mode="live"):
        job = store.enqueue(deal, mode).job
        store.set_status(job.id, status, last_error_code=code)

    def test_each_eligibility_case(self):
        self.mode("live", allowed="")
        self.seed("102", store.DRY_DONE)
        self.seed("103", store.DONE)
        self.seed("104", store.QUEUED)
        self.seed("105", store.NEEDS_REVIEW, "readback_failed")
        self.seed("106", store.FAILED, "plan_read_failed")
        self.seed("107", store.HELD, "site_unmapped")
        out = self.run_file(self.IDS)
        self.assertEqual(out["queuedIds"], ["101", "102"])              # no job, and dry_done
        self.assertEqual(out["skipped"], {"already_done": 1, "already_queued_or_running": 1,
                                          "needs_a_person": 3})
        self.assertEqual(out["needsAPerson"], {"readback_failed": 1, "plan_read_failed": 1,
                                               "site_unmapped": 1})
        self.assertEqual((out["idsRead"], out["valid"], out["queued"]), (7, 7, 2))
        self.assertEqual(store.latest_for_deal("101").source, "file")
        self.assertEqual(store.latest_for_deal("101").file_hash, HASH)

    def test_include_review_queues_them_as_fresh_jobs(self):
        self.mode("live", allowed="")
        self.seed("105", store.NEEDS_REVIEW, "readback_failed")
        out = self.run_file(["105"], include_review=True)
        self.assertEqual(out["queuedIds"], ["105"])
        self.assertEqual(store.latest_for_deal("105").status, store.QUEUED)

    def test_a_dry_done_deal_is_not_dry_run_again(self):
        self.mode("dry", allowed="")
        self.seed("102", store.DRY_DONE, mode="dry")
        out = self.run_file(["102", "103"], mode="dry")
        self.assertEqual(out["queuedIds"], ["103"])
        self.assertEqual(out["skipped"], {"already_dry_run": 1})

    def test_limit_continues_across_runs(self):
        self.mode("live", allowed="")
        first = self.run_file(self.IDS, limit=3)
        self.assertEqual(first["queuedIds"], ["101", "102", "103"])
        second = self.run_file(self.IDS, limit=3)
        self.assertEqual(second["queuedIds"], ["104", "105", "106"])
        self.assertEqual(second["skipped"], {"already_queued_or_running": 3})

    def test_invalid_ids_block_everything_unless_skip_invalid(self):
        self.mode("live", allowed="")
        hub = Hub(["101", "102", "103"], **{"102": "s-lost", "103": "s-sales"})
        ids = ["101", "102", "103", "999"]
        blocked = self.run_file(ids, hub=hub)
        self.assertEqual(blocked["invalid"], {"102": "closed_lost", "103": "not_operations_pipeline",
                                              "999": "deal_not_found"})
        self.assertIn("blocked", blocked)
        self.assertEqual(blocked["queued"], 0)
        self.assertIsNone(store.latest_for_deal("101"))
        self.assertEqual(blocked["byStage"], {"Operations / Closed Lost": 1, "Operations / Deposit Pending": 1,
                                              "Sales / Quote Sent": 1})
        self.assertIn("NOT applied", blocked["note"])
        allowed = self.run_file(ids, hub=hub, skip_invalid=True)
        self.assertEqual(allowed["queuedIds"], ["101"])

    def test_a_hubspot_read_failure_is_a_code_not_a_crash(self):
        class Down(Hub):
            def get_deal(self, deal_id):
                raise HubSpotError("GET failed", 500)
        self.mode("live", allowed="")
        out = self.run_file(["101"], hub=Down(["101"]))
        self.assertEqual(out["invalid"], {"101": "hubspot_read_failed"})

    def test_test_ids_are_never_queued_by_file_in_live_mode(self):
        self.mode("live", allowed="", test="102")
        out = self.run_file(["101", "102"])
        self.assertEqual(out["queuedIds"], ["101"])
        self.assertEqual(out["skipped"], {"test_deal_not_allowed_by_file": 1})


class FileGateTests(Phase2Case):
    """A file job is allowed to write live without ONBOARD_ALLOWED_DEAL_IDS; nothing else is."""

    def file_job(self, deal=DEAL, mode="live"):
        return store.enqueue(deal, mode, source="file", file_hash=HASH).job

    def test_a_file_id_is_accepted_and_still_needs_its_own_approval(self):
        self.mode("live", allowed="999")                    # the env allowlist does not have it
        self.file_job()
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)       # Gate A, no auto-approve
        self.assertEqual(len(self.notify.gates), 1)
        self.assertEqual(self.famly.child_creates, [])

    def test_an_id_that_was_not_in_a_file_is_not_accepted(self):
        self.mode("live", allowed="999")
        self.enqueue()                                       # a plain CLI job for the same deal
        self.assertEqual(self.run_worker()["status"], store.HELD)
        self.assertEqual(self.job().last_error_code, "deal_not_allowlisted")
        self.assertEqual(self.notify.gates, [])

    def test_a_test_id_is_not_accepted_through_the_file_route(self):
        self.mode("live", allowed="999", test=DEAL)
        self.file_job()
        self.assertEqual(self.run_worker()["status"], store.HELD)
        self.assertEqual(self.job().last_error_code, "deal_not_allowlisted")

    def test_a_dry_mode_file_job_cannot_be_replayed_live(self):
        self.mode("dry", allowed="999")
        self.file_job(mode="dry")
        self.mode("live", allowed="999")                    # the operator flips the mode
        self.assertEqual(self.run_worker()["status"], store.HELD)
        self.assertEqual(self.famly.child_creates, [])

    def test_a_file_job_row_cannot_be_made_without_a_hash(self):
        with self.assertRaises(ValueError):
            store.enqueue(DEAL, "live", source="file")
        with self.assertRaises(ValueError):
            store.enqueue(DEAL, "live", source="cli", file_hash=HASH)

    def test_retry_on_a_file_enqueued_hold_works_and_keeps_the_file_route(self):
        self.mode("live", allowed="999")
        holds = []
        self.notify.hold_notice = lambda text, job_id, nonce: holds.append((job_id, nonce)) or True
        self.hub.deals[DEAL]["location_of_interest"] = "Shoreditch"          # a hold: site_unmapped
        self.file_job()
        self.assertEqual(self.run_worker()["status"], store.HELD)
        held = self.job()
        value = approval.button_value(held.id, "", holds[-1][1])
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_RETRY, value, "U1"),
                         approval.MSG_RETRY_QUEUED)
        fresh = store.latest_for_deal(DEAL)
        self.assertEqual((fresh.source, fresh.file_hash, fresh.status), ("file", HASH, store.QUEUED))
        self.hub.deals[DEAL]["location_of_interest"] = "Westminster"          # fixed in HubSpot
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)

    def test_retry_of_a_dry_file_hold_is_refused_live(self):
        self.mode("dry", allowed="999")
        holds = []
        self.notify.hold_notice = lambda text, job_id, nonce: holds.append((job_id, nonce)) or True
        self.hub.deals[DEAL]["location_of_interest"] = "Shoreditch"
        self.file_job(mode="dry")
        self.assertEqual(self.run_worker()["status"], store.HELD)
        held = self.job()
        self.mode("live", allowed="999")
        value = approval.button_value(held.id, "", holds[-1][1])
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_RETRY, value, "U1"),
                         approval.MSG_RETRY_NOT_ALLOWED)


if __name__ == "__main__":
    unittest.main()
