"""onboard-enqueue --reopen-done: a deal whose latest job is done can be queued again (live mode, CLI and
file only) so an existing child can be repaired. Fake data only; nothing live."""

import argparse
import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from actions.onboard import batch, cli_enqueue, cli_status, store, web
from tests.test_onboard_batch import HASH, Hub
from tests.test_onboard_phase2 import DEAL, Phase2Case


class ReopenCase(Phase2Case):
    def seed(self, deal, status, code=None, mode="live"):
        job = store.enqueue(deal, mode).job
        store.set_status(job.id, status, last_error_code=code)
        return job

    def run_file(self, ids, **over):
        kwargs = dict(limit=None, skip_invalid=False, include_review=False)
        kwargs.update(over)
        return batch.enqueue_file(Hub(ids), ids, HASH, "live", **kwargs)

    def args(self, **over):
        base = dict(deal_id=DEAL, file=None, limit=None, all=False, skip_invalid=False, include_review=False,
                    new_urn=False, reopen_done=True)
        base.update(over)
        return argparse.Namespace(**base)


class FileTests(ReopenCase):
    def test_a_done_deal_is_skipped_without_the_flag_and_queued_with_it(self):
        self.mode("live", allowed="")
        self.seed("101", store.DONE)
        out = self.run_file(["101"])
        self.assertEqual((out["queued"], out["reopened"], out["skipped"]), (0, 0, {"already_done": 1}))
        out = self.run_file(["101"], reopen_done=True)
        self.assertEqual((out["queued"], out["reopened"], out["queuedIds"]), (1, 1, ["101"]))
        job = store.latest_for_deal("101")
        self.assertEqual((job.status, job.source, job.file_hash, job.reopen), (store.QUEUED, "file", HASH, True))

    def test_only_a_done_latest_job_is_reopened_and_the_summary_counts_it(self):
        self.mode("live", allowed="")
        self.seed("101", store.DONE)
        out = self.run_file(["101", "102"], reopen_done=True)             # 102 has no job: a normal queue
        self.assertEqual((out["queued"], out["reopened"]), (2, 1))
        self.assertFalse(store.latest_for_deal("102").reopen)

    def test_an_active_deal_is_not_queued_twice(self):
        self.mode("live", allowed="")
        active = self.seed("101", store.QUEUED)
        out = self.run_file(["101"], reopen_done=True)
        self.assertEqual((out["queued"], out["reopened"], out["skipped"]), (0, 0, {"already_queued_or_running": 1}))
        self.assertEqual(store.latest_for_deal("101").id, active.id)
        # even asked directly, the unique active-job index returns the active job
        again = store.enqueue("101", "live", reopen=True)
        self.assertEqual((again.outcome, again.job.id), (store.ALREADY_QUEUED, active.id))

    def test_needs_review_failed_and_held_are_not_affected_by_the_flag(self):
        self.mode("live", allowed="")
        self.seed("105", store.NEEDS_REVIEW, "readback_failed")
        self.seed("106", store.FAILED, "plan_read_failed")
        self.seed("107", store.HELD, "site_unmapped")
        out = self.run_file(["105", "106", "107"], reopen_done=True)
        self.assertEqual((out["queued"], out["reopened"], out["skipped"]), (0, 0, {"needs_a_person": 3}))
        out = self.run_file(["105"], reopen_done=True, include_review=True)  # still needs --include-review
        self.assertEqual((out["queued"], out["reopened"]), (1, 0))

    def test_the_gate_still_accepts_a_reopened_file_job_live_only(self):
        self.mode("live", allowed="")
        self.seed("101", store.DONE)
        self.run_file(["101"], reopen_done=True)
        from actions.onboard import config
        self.assertTrue(config.allowed_for_job(store.latest_for_deal("101"), "live"))


class CliTests(ReopenCase):
    def test_deal_id_reopens_a_done_deal_in_live_mode(self):
        self.mode("live")
        self.seed(DEAL, store.DONE)
        out = cli_enqueue.handle(self.args())
        self.assertEqual((out["outcome"], out["reopened"], out["status"]), (store.CREATED, 1, store.QUEUED))
        # without the flag the deal still has a done job, so it is "already done": no second job either way
        self.assertEqual(cli_enqueue.handle(self.args(reopen_done=False))["outcome"], store.ALREADY_DONE)
        self.assertEqual(cli_enqueue.handle(self.args())["outcome"], store.ALREADY_QUEUED)      # the active job

    def test_dry_and_off_refuse_with_a_clear_message_and_queue_nothing(self):
        self.seed(DEAL, store.DONE)
        for mode in ("dry", "off"):
            self.mode(mode)
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                cli_enqueue.handle(self.args())
            self.assertEqual(ctx.exception.code, 3)
        self.mode("dry")
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            cli_enqueue.handle(self.args())
        self.assertIn("--reopen-done needs ONBOARD_MODE=live", stderr.getvalue())
        self.assertEqual(store.latest_for_deal(DEAL).status, store.DONE)

    def test_the_store_itself_refuses_a_dry_run_or_a_webhook_reopen(self):
        self.seed(DEAL, store.DONE)
        with self.assertRaises(ValueError):
            store.enqueue(DEAL, "dry", reopen=True)
        with self.assertRaises(ValueError):
            store.enqueue(DEAL, "live", source="webhook", reopen=True)


class WebhookCannotReopen(ReopenCase):
    def test_the_intake_path_never_passes_reopen_whatever_the_payload_says(self):
        self.mode("live")
        self.seed(DEAL, store.DONE)
        with mock.patch.object(web.store, "enqueue", wraps=store.enqueue) as spy:
            data, status = web.handle({"action": "onboard_enqueue", "dealId": DEAL, "reopen": True,
                                       "reopen_done": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["outcome"] if "data" in data else data["outcome"], "already_done")
        for call in spy.call_args_list:
            self.assertNotIn("reopen", call.kwargs)
        self.assertEqual(store.latest_for_deal(DEAL).status, store.DONE)

    def test_no_other_caller_passes_reopen(self):
        from pathlib import Path
        callers = []
        for path in Path("actions").rglob("*.py"):
            if "reopen=" in path.read_text(encoding="utf-8"):
                callers.append(path.as_posix())
        self.assertEqual(sorted(callers), ["actions/onboard/batch.py", "actions/onboard/cli_enqueue.py",
                                           "actions/onboard/store.py"])


class JobTests(ReopenCase):
    def test_status_shows_a_reopened_job(self):
        self.mode("live")
        self.seed(DEAL, store.DONE)
        cli_enqueue.handle(self.args())
        latest = cli_status.handle(argparse.Namespace(deal_id=DEAL))["latest"]
        self.assertTrue(latest["reopened"])
        store._reset_for_tests()
        self.enqueue()
        self.assertFalse(cli_status.handle(argparse.Namespace(deal_id=DEAL))["latest"]["reopened"])

    def test_a_reopened_job_runs_the_normal_live_plan_and_a_zero_write_plan_completes_unasked(self):
        self.run_through()                                              # the first job creates everything
        self.assertEqual(self.job().status, store.DONE)
        gates = len(self.notify.gates)
        cli_enqueue.handle(self.args())
        result = self.run_worker()
        self.assertEqual(result["status"], store.DONE)                  # nothing to write: no approval
        self.assertEqual(len(self.notify.gates), gates)
        self.assertTrue(self.job().reopen)

    def test_a_reopened_job_with_something_missing_asks_gate_a_as_usual(self):
        self.run_through()
        child_id = next(iter(self.famly.contacts))
        self.famly.relations[self.famly.contacts[child_id][0][0]] = False      # a RELATION is missing
        gates = len(self.notify.gates)
        cli_enqueue.handle(self.args())
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), gates + 1)
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(len(self.famly.relation_updates), 1)


if __name__ == "__main__":
    unittest.main()
