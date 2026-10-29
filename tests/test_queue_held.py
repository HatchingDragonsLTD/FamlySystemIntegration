"""A held queue job is never quiet: the deal is flagged, Slack gets one line, a resend re-flags (Slack at most
once per 6 hours per deal) and `queue-held` lists them. Fakes only; codes and ids only."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from actions.plan_write import status_codes, status_sync
from actions.plan_write import warnings as plan_warnings
from actions.queue_timeline import jobs, worker
from core import hubspot
from tests import test_queue_timeline as queue_tests
from tests.test_plan_status_sync import StatusWriter

ID = "5550001"
NAMES = "Alex Smith pat.smith@example.com 07700 900123 12 Acorn Road"
PAST_LAST_DAY = "Plan continues past the child's last day"


class HeldVisibilityCase(queue_tests.QueueTestCase):
    def setUp(self):
        super().setUp()
        self.writer = StatusWriter()
        for target, name, fake in ((status_sync, "HubSpotDealWriter", lambda: self.writer),
                                   (status_sync, "_spawn", lambda work: work()),
                                   (plan_warnings, "_notifier", lambda warning: None)):  # keep stderr quiet
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def warn_on_funded(self, error="1614173107", title=PAST_LAST_DAY, message="x", severity="INFO"):
        warning = {"title": title, "message": message, "severity": severity, "error": error}

        def hook(node, body):
            if body["plan"].get("publicFundingSettings"):
                node["behaviors"] = [{"id": "ShowPlanWarnings", "payload": {"warnings": [warning]}}]

        self.famly.preview_hook = hook

    def request(self, deal=ID):
        body = {"properties": self.body()}
        if deal:
            body["dealId"] = deal
        return body

    def held_notices(self):
        return [n for n in self.notices if n.startswith("Queue job held")]

    def held_job(self, **warning):
        self.warn_on_funded(**warning)
        data = self.queue(self.request())
        self.run_all()
        return self.job(data)


class HoldVisibilityTests(HeldVisibilityCase):
    def test_a_hold_writes_needs_attention_and_posts_one_slack_line(self):
        job = self.held_job()

        self.assertEqual(job.status, jobs.STATUS_HELD)
        self.assertEqual(self.writer.status_writes, [(ID, "needs_attention")])
        self.assertEqual(self.writer.note_writes, [(ID, "queue_job_held:plan_past_child_last_day")])
        lines = self.held_notices()
        self.assertEqual(len(lines), 1)
        for part in (ID, job.job_id, "queue_job_held:plan_past_child_last_day"):
            self.assertIn(part, lines[0])

    def test_the_hold_decision_is_unchanged(self):
        job = self.held_job()

        self.assertEqual((job.status, job.next_index), (jobs.STATUS_HELD, 1))
        self.assertEqual(len(self.famly.commits), 1)  # plan 1 committed, plan 2 held
        self.assertIn(f"warning: {PAST_LAST_DAY}", job.detail)
        self.assertIn(f"Held for approval — warning: {PAST_LAST_DAY}", self.previews_posted[-1][0])
        self.assertEqual(jobs.consecutive_failures(), 0)

    def test_the_note_and_the_slack_line_hold_codes_only(self):
        job = self.held_job(error="SomethingNew", title=NAMES, message=NAMES)

        note = self.writer.note_writes[-1][1]
        self.assertEqual(note, "queue_job_held:warning_other")
        self.assertRegex(note, hubspot.PLAN_NOTE_PATTERN)
        self.assertNotIn("Smith", note + self.held_notices()[0])
        self.assertEqual(jobs.held_codes(self.job({"job_id": job.job_id})), ["warning_other"])

    def test_a_hold_with_no_deal_id_still_posts_the_line_and_writes_nothing(self):
        self.warn_on_funded()
        self.queue(self.request(deal=None))
        self.run_all()

        self.assertEqual(self.writer.status_writes, [])
        self.assertIn("deal unknown", self.held_notices()[0])


class ReasonCodeTests(unittest.TestCase):
    def test_famly_error_codes_map_to_fixed_codes_with_a_fallback(self):
        self.assertEqual(status_codes.warning_reason_code("1614173107"), "plan_past_child_last_day")
        self.assertEqual(status_codes.warning_reason_code("never seen"), "warning_other")
        self.assertEqual(status_codes.warning_reason_code(None), "warning_other")

    def test_the_other_hold_reasons_have_codes_and_the_note_fits(self):
        self.assertEqual(status_codes.held_code_of("institution X is not in QUEUE_ALLOWED_INSTITUTIONS"),
                         "institution_not_allowed")
        self.assertEqual(status_codes.held_code_of("the estimate for the period from 2026-11-01 is not above 0"),
                         "estimate_not_above_zero")
        self.assertEqual(status_codes.held_code_of("anything else"), "other")
        note = status_codes.queue_job_held(["a_code", "b_code", "c_code", "d_code", "e_code"])
        self.assertEqual(note, "queue_job_held:a_code,b_code,c_code,+2")
        self.assertRegex(note, hubspot.PLAN_NOTE_PATTERN)


class ResendTests(HeldVisibilityCase):
    def test_a_resend_on_a_held_job_re_flags_and_says_why(self):
        job = self.held_job()

        data, status = queue_tests.queue_web.handle(self.request())

        self.assertEqual(status, 200)
        self.assertTrue(data["already_queued"])
        self.assertEqual((data["job_id"], data["status"]), (job.job_id, "held"))
        self.assertEqual(data["reason_code"], "plan_past_child_last_day")
        self.assertEqual(self.writer.status_writes, [(ID, "needs_attention")] * 2)
        self.assertEqual(self.writer.note_writes[-1], (ID, "queue_job_held:plan_past_child_last_day"))

    def test_slack_is_rate_limited_to_one_line_per_deal_per_six_hours(self):
        self.held_job()
        for _ in range(3):
            queue_tests.queue_web.handle(self.request())
        self.assertEqual(len(self.held_notices()), 1)           # the hold's own line covers the window
        self.assertEqual(len(self.writer.status_writes), 4)     # but the deal is flagged every time

        old = datetime.now(timezone.utc) - timedelta(hours=7)
        jobs.state_set(f"held_notice:{ID}", old.isoformat())
        queue_tests.queue_web.handle(self.request())
        queue_tests.queue_web.handle(self.request())
        self.assertEqual(len(self.held_notices()), 2)

    def test_a_resend_on_a_queued_or_done_job_behaves_as_before(self):
        queued = self.queue(self.request())
        data, status = queue_tests.queue_web.handle(self.request())
        self.assertEqual((status, data["already_queued"], data["status"]), (200, True, "queued"))
        self.assertNotIn("reason_code", data)

        self.run_all()
        self.assertEqual(self.job(queued).status, jobs.STATUS_DONE)
        data, status = queue_tests.queue_web.handle(self.request())
        self.assertEqual((status, data["already_queued"], data["status"]), (200, True, "done"))
        self.assertNotIn("reason_code", data)
        self.assertEqual(self.writer.status_writes, [])
        self.assertEqual(self.held_notices(), [])


class QueueHeldCommandTests(HeldVisibilityCase):
    def test_it_lists_held_jobs_as_ids_and_codes_only(self):
        job = self.held_job()

        result = worker.held_list()

        self.assertEqual(result["count"], 1)
        row = result["held"][0]
        self.assertEqual(row["dealId"], ID)
        self.assertEqual((row["childId"], row["jobId"]), (job.child_id, job.job_id))
        self.assertEqual(row["reasonCode"], "plan_past_child_last_day")
        self.assertIsInstance(row["ageHours"], float)
        self.assertEqual(set(row), {"dealId", "childId", "jobId", "reasonCode", "ageHours"})

    def test_a_job_with_no_deal_id_shows_none_and_an_older_hold_still_has_a_code(self):
        self.warn_on_funded()
        data = self.queue(self.request(deal=None))
        self.run_all()
        job = self.job(data)
        # a job held before the codes were recorded: the code comes from its detail
        stored = job.segments[job.next_index]
        stored.pop("hold_codes")
        jobs.record_segment(job.job_id, job.next_index, hold_codes=[])

        row = worker.held_list()["held"][0]

        self.assertIsNone(row["dealId"])
        self.assertEqual(row["reasonCode"], "warning_other")


if __name__ == "__main__":
    unittest.main()
