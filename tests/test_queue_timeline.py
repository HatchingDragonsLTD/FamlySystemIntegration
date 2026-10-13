"""Tests for the persistent queue: queue_timeline, the worker and its commands.

Run from the project root:

    python -m unittest tests.test_queue_timeline -v

Reuses the create_timeline harness (a stateful fake Famly, recorded Slack posts,
fixture catalogues) and the two-period pricing fake from the auto-round tests.
NOTHING here touches Famly or Slack. Commits go through the REAL approval path
(`approval._approve`), so its guards are exercised, not stubbed.
"""

import json
import logging
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:  # `python -m unittest tests.test_queue_timeline`
    from tests import test_auto_round as auto
    from tests import test_create_timeline as harness
except ImportError:  # `python -m unittest discover -s tests`
    import test_auto_round as auto
    import test_create_timeline as harness

from actions.create_timeline import chains
from actions.plan_write import approval, preview_store
from actions.queue_timeline import jobs, worker
from actions.queue_timeline import web as queue_web
from core.rest_client import RestHTTPError
from integrations import child_lookup, slack

logging.disable(logging.CRITICAL)

CHILD = harness.CHILD
WARNING = [
    {
        "id": "ShowPlanWarnings",
        "payload": {
            "warnings": [{"title": "Something odd", "message": "x", "severity": "warning", "error": "ODD"}]
        },
    }
]


class QueueFamly(auto.PricingFamly):
    """The pricing fake, plus the ways a real Famly misbehaves."""

    def __init__(self):
        super().__init__()
        self.preview_hook = None  # callable(node, body)
        self.preview_failures: list[int] = []  # HTTP statuses to raise, in order
        self.get_failures: list[int] = []
        self.write_then_fail: int | None = None  # nth commit attempt: write, then error
        self.commit_attempts = 0
        self.lag_reads = 0  # the next N reads leave the newest plan out

    def get(self, path, params=None):
        if self.get_failures:
            raise RestHTTPError("read failed", self.get_failures.pop(0), "")
        body = super().get(path, params)
        wanted = (params or {}).get("childId")
        body["plans"] = [p for p in body["plans"] if not wanted or p["childId"] == wanted]
        if self.lag_reads and body["plans"]:
            self.lag_reads -= 1
            body["plans"] = body["plans"][:-1]
        return body

    def post(self, path, params=None, json_body=None):
        if (params or {}).get("preview") == "true":
            if self.preview_failures:
                raise RestHTTPError("preview failed", self.preview_failures.pop(0), "")
            node = super().post(path, params, json_body)
            if self.preview_hook:
                self.preview_hook(node, json_body)
            return node

        self.commit_attempts += 1
        if self.write_then_fail == self.commit_attempts:
            super().post(path, params, json_body)
            raise RestHTTPError("timed out after the write", 504, "")
        return super().post(path, params, json_body)


class QueueTestCase(harness.ChainTestCase):
    def setUp(self):
        super().setUp()
        self.famly = QueueFamly()

        env = mock.patch.dict(
            "os.environ",
            {
                "QUEUE_MODE": "auto",
                "QUEUE_ALLOWED_INSTITUTIONS": harness.INSTITUTION,
                "QUEUE_MAX_COMMITS_PER_DAY": "20",
                "QUEUE_MAX_MONTHLY": "5000",
                "AUTO_ROUND_DEFAULT": "",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        self.worker_sleeps: list[float] = []
        patcher = mock.patch.object(worker, "_sleep", self.worker_sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.clock = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
        patcher = mock.patch.object(worker, "_now", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- requests ---------------------------------------------------------- #
    def body(self, funded=True, **overrides):
        if funded:
            return harness.with_funding(site_code="HDCITY", **overrides)
        return harness.payload(site_code="HDCITY", **overrides)

    def queue(self, body=None):
        data, status = queue_web.handle(body if body is not None else self.body())
        self.assertEqual(status, 200, data)
        return data

    def job(self, data):
        return jobs.get(data["job_id"])

    def run_all(self, limit=50):
        for _ in range(limit):
            if not worker.run_once():
                return
        self.fail("the worker never went idle")

    def starts(self, call):
        return self.sent(call)["from"]

    def commit_params(self):
        return [c["params"].get("oldPlanId") for c in self.famly.commits]


# --------------------------------------------------------------------------- #
# queue_timeline: validate, store, acknowledge
# --------------------------------------------------------------------------- #
class QueueRequestTests(QueueTestCase):
    def test_it_returns_the_job_at_once_without_calling_famly_or_slack(self):
        data = self.queue()

        self.assertEqual(data["status"], jobs.STATUS_QUEUED)
        self.assertFalse(data["already_queued"])
        self.assertEqual(len(data["segments"]), 3)
        self.assertEqual((self.famly.posts, self.famly.gets), ([], 0))
        self.assertEqual((self.previews_posted, self.notices), ([], []))

    def test_a_duplicate_request_returns_the_existing_job(self):
        first = self.queue()
        second = self.queue()

        self.assertEqual(second["job_id"], first["job_id"])
        self.assertTrue(second["already_queued"])
        self.assertFalse(first["already_queued"])
        self.assertEqual(len(jobs.list_jobs()), 1)

    def test_the_key_ignores_names_and_other_unlisted_fields(self):
        first = self.queue(self.body(child_name="Alex Smith"))
        second = self.queue(self.body(child_name="Someone Else", dealname="x"))

        self.assertEqual(second["job_id"], first["job_id"])

    def test_a_different_payload_is_a_different_job_once_the_first_is_out_of_the_way(self):
        first = self.queue()
        worker.cancel(first["job_id"])
        second = self.queue(self.body(addon_quantity="1"))

        self.assertNotEqual(second["job_id"], first["job_id"])
        self.assertFalse(second["already_queued"])

    def test_a_different_start_date_is_a_different_job_once_the_first_is_out_of_the_way(self):
        first = self.queue()
        worker.cancel(first["job_id"])
        second = self.queue(self.body(start_date="2026-09-02"))

        self.assertNotEqual(second["job_id"], first["job_id"])

    def test_the_key_is_child_start_and_a_payload_hash(self):
        data = self.queue()
        job = self.job(data)

        self.assertEqual(
            job.job_key, jobs.job_key(CHILD, "2026-09-01", job.payload)
        )
        self.assertNotEqual(
            jobs.job_key(CHILD, "2026-09-01", job.payload),
            jobs.job_key("other-child", "2026-09-01", job.payload),
        )

    def test_an_identical_request_finds_the_job_whatever_became_of_it(self):
        # The idempotency key is UNIQUE outright: a cancelled, failed or finished
        # job is still THE job for that request. To ask again, change the request.
        for status in (jobs.STATUS_DONE, jobs.STATUS_CANCELLED, jobs.STATUS_FAILED):
            with self.subTest(status=status):
                jobs.state_set("x", None)
                first = self.queue()
                jobs.set_status(first["job_id"], status, "ended")

                again = self.queue()

                self.assertEqual(again["job_id"], first["job_id"])
                self.assertTrue(again["already_queued"])
                with preview_store.open_store() as connection:
                    connection.execute("DELETE FROM jobs")

    def test_the_stored_payload_is_allow_listed_with_no_names(self):
        data = self.queue(self.body(child_name="Alex Smith", parent_name="Pat Smith"))
        stored = self.job(data).payload

        self.assertTrue(set(stored) <= harness.engine.ALLOWED_FIELDS)
        with preview_store.open_store() as connection:
            dumped = json.dumps([tuple(r) for r in connection.execute("SELECT * FROM jobs")], default=str)
        self.assertNotIn("Smith", dumped)

    def test_auto_round_is_stored_for_the_worker(self):
        data = self.queue(self.body(auto_round="true"))

        self.assertEqual(self.job(data).payload["auto_round"], "true")

    def test_every_segment_is_validated_before_anything_is_stored(self):
        rules = json.loads(json.dumps(harness.FUNDING_RULES))
        rules["institutions"][harness.INSTITUTION]["fundedWeeklyHours"] = None
        self.funding_file.write_text(json.dumps(rules), encoding="utf-8")

        data, status = queue_web.handle(self.body())

        self.assertEqual(status, 422)
        self.assertTrue(any("funding_rules" in e for e in data["errors"]))
        self.assertEqual(jobs.list_jobs(), [])
        self.assertEqual((self.famly.posts, self.famly.gets), ([], 0))

    def test_bad_input_is_refused(self):
        for body in (
            self.body(start_date=""),
            self.body(leave_date="2026-08-01"),
            self.body(funded=False, has_funding="true", funding_start="2026-11-01", funding_end=""),
        ):
            with self.subTest(body=body):
                data, status = queue_web.handle(body)
                self.assertEqual(status, 422)
                self.assertTrue(data["errors"])
        self.assertEqual(jobs.list_jobs(), [])

    def test_the_queue_being_off_refuses_everything(self):
        with mock.patch.dict("os.environ", {"QUEUE_MODE": "off"}):
            data, status = queue_web.handle(self.body())

        self.assertEqual(status, 503)
        self.assertEqual(jobs.list_jobs(), [])

    def test_it_is_registered_with_the_router(self):
        import web_registry

        self.assertIs(web_registry.get_handler("queue_timeline"), queue_web.handle)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
class ModeTests(QueueTestCase):
    def test_the_default_mode_is_dry_run(self):
        with mock.patch.dict("os.environ", {"QUEUE_MODE": ""}):
            self.assertEqual(worker.mode(), worker.MODE_DRY_RUN)

    def test_an_unknown_mode_is_off(self):
        with mock.patch.dict("os.environ", {"QUEUE_MODE": "yolo"}):
            self.assertEqual(worker.mode(), worker.MODE_OFF)

    def test_off_does_nothing(self):
        data = self.queue()
        with mock.patch.dict("os.environ", {"QUEUE_MODE": "off"}):
            self.assertFalse(worker.run_once())

        self.assertEqual(self.job(data).status, jobs.STATUS_QUEUED)
        self.assertEqual(self.famly.posts, [])


class DryRunTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict("os.environ", {"QUEUE_MODE": "dry_run"})
        env.start()
        self.addCleanup(env.stop)

    def test_it_commits_nothing(self):
        data = self.queue()
        self.run_all()

        self.assertEqual(self.famly.commits, [])
        self.assertEqual(self.famly.commit_attempts, 0)
        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)
        self.assertEqual(self.job(data).next_index, 0)

    def test_each_segment_is_previewed_on_its_own_with_no_old_plan(self):
        self.queue()
        self.run_all()

        self.assertEqual([c["params"].get("oldPlanId") for c in self.famly.previews], [None] * 3)
        self.assertEqual(
            [self.starts(c) for c in self.famly.previews],
            ["2026-09-01", "2026-11-01", "2027-01-01"],
        )

    def test_the_digest_says_it_was_a_dry_run_and_reports_each_plan(self):
        self.queue()
        self.run_all()

        digest = next(n for n in self.notices if "Queue digest" in n)
        self.assertIn("DRY RUN: nothing was committed", digest)
        self.assertIn("Plan 1 of 3", digest)
        self.assertIn("Plan 2 of 3 (funded", digest)
        self.assertIn("clean", digest)

    def test_the_checks_are_applied_and_reported_per_plan(self):
        def warn_funded(node, body):
            if body["plan"].get("publicFundingSettings"):
                node["behaviors"] = WARNING

        self.famly.preview_hook = warn_funded
        data = self.queue()
        self.run_all()

        segments = self.job(data).segments
        self.assertEqual([s["dry_run"]["clean"] for s in segments], [True, False, True])
        self.assertIn("warning: Something odd", segments[1]["dry_run"]["reasons"])
        self.assertIn("would be HELD", next(n for n in self.notices if "Queue digest" in n))

    def test_rounding_is_applied_and_shown(self):
        data = self.queue(self.body(auto_round="true"))
        self.run_all()

        self.assertEqual(self.job(data).segments[0]["dry_run"]["rounded"], ["269.03 -> 270.00", "1503.30 -> 1503.00"])

    def test_nothing_it_previewed_can_be_approved_afterwards(self):
        self.queue()
        self.run_all()

        with preview_store.open_store() as connection:
            statuses = {r["status"] for r in connection.execute("SELECT status FROM previews")}
        self.assertEqual(statuses, {preview_store.STATUS_SUPERSEDED})
        self.assertEqual(self.previews_posted, [])  # and none was ever posted

    def test_a_child_that_already_has_plans_fails_before_any_preview(self):
        self.famly.plans.append({"id": "old", "childId": CHILD, "from": "2025-09-01", "to": None})
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_FAILED)
        self.assertEqual(self.famly.posts, [])
        self.assertIn("already has", self.notices[-1])


# --------------------------------------------------------------------------- #
# Auto: a clean chain
# --------------------------------------------------------------------------- #
class CleanChainTests(QueueTestCase):
    def test_a_clean_chain_is_committed_in_order(self):
        data = self.queue()
        self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_DONE)
        self.assertEqual(job.next_index, 3)
        self.assertEqual([self.starts(c) for c in self.famly.commits], ["2026-09-01", "2026-11-01", "2027-01-01"])
        self.assertEqual(self.commit_params(), [None, "plan-1", "plan-2"])
        self.assertEqual([s["plan_id"] for s in job.segments], ["plan-1", "plan-2", "plan-3"])

    def test_only_the_last_plan_has_an_end_date_and_replace_is_never_set(self):
        self.queue()
        self.run_all()

        self.assertEqual([self.sent(c)["to"] for c in self.famly.commits], [None, None, "2027-07-31"])
        for call in self.famly.commits[1:]:
            self.assertEqual(call["params"]["replaceOldPlan"], "false")

    def test_famly_ends_up_with_plans_that_meet(self):
        self.queue()
        self.run_all()

        self.assertEqual(
            [(p["from"], p["to"]) for p in self.famly.plans],
            [("2026-09-01", "2026-11-01"), ("2026-11-01", "2027-01-01"), ("2027-01-01", "2027-07-31")],
        )

    def test_the_funded_plan_uses_the_servers_funding_rules(self):
        self.queue()
        self.run_all()

        self.assertEqual(self.sent(self.famly.commits[1])["publicFundingSettings"]["hours"], 24.5)

    def test_a_rounded_chain_commits_the_adjustments(self):
        self.queue(self.body(auto_round="true"))
        self.run_all()

        for call in self.famly.commits:
            self.assertEqual(len(call["body"]["plan"]["planParts"][0]["totalAdjustments"]), 2)

    def test_it_runs_one_job_at_a_time_in_order(self):
        other = "00000000-0000-0000-0000-0000000000c2"
        first = self.queue()
        second = self.queue(self.body(childId=other))
        with mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": f"{CHILD},{other}"}):
            self.assertTrue(worker.run_once())
            self.assertEqual(self.job(first).status, jobs.STATUS_DONE)
            self.assertEqual(self.job(second).status, jobs.STATUS_QUEUED)
            self.assertTrue(worker.run_once())
            self.assertEqual(self.job(second).status, jobs.STATUS_DONE)

    def test_a_digest_is_posted_when_the_queue_drains(self):
        self.queue()
        worker.run_once()

        digest = next(n for n in self.notices if "Queue digest" in n)
        self.assertIn("1 job(s) finished", digest)
        self.assertIn("plan `plan-3`", digest)
        self.assertNotIn("DRY RUN", digest)

    def test_a_job_is_digested_once(self):
        self.queue()
        self.run_all()
        self.run_all()

        self.assertEqual(len([n for n in self.notices if "Queue digest" in n]), 1)

    def test_digests_wait_thirty_minutes_while_the_queue_is_busy(self):
        other = "00000000-0000-0000-0000-0000000000c2"
        self.queue()
        self.queue(self.body(childId=other))
        with mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": f"{CHILD},{other}"}):
            worker.run_once()  # first job done, second still queued: not drained
            self.assertEqual([n for n in self.notices if "Queue digest" in n], [])

            self.clock += timedelta(minutes=31)
            worker.post_digest(self.clock)

        self.assertEqual(len([n for n in self.notices if "Queue digest" in n]), 1)

    def test_the_consecutive_failure_count_resets_on_success(self):
        jobs.state_set("consecutive_failures", "2")
        self.queue()
        self.run_all()

        self.assertEqual(jobs.consecutive_failures(), 0)


class StateCheckTests(QueueTestCase):
    def test_a_child_with_a_plan_is_never_touched(self):
        self.famly.plans.append({"id": "old", "childId": CHILD, "from": "2025-09-01", "to": None})
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_FAILED)
        self.assertEqual(self.famly.posts, [])
        self.assertIn("`", self.notices[-1])
        self.assertIn("Queue job failed", self.notices[-1])

    def test_the_failure_names_the_child_with_the_name_helper(self):
        with mock.patch.object(child_lookup, "lookup_child_name", lambda *a, **k: "Alex Smith"):
            self.famly.plans.append({"id": "old", "childId": CHILD, "from": "2025-09-01", "to": None})
            self.queue()
            self.run_all()

        self.assertIn(f"Alex Smith (`{CHILD}`)", self.notices[-1])

    def test_an_unexpected_plan_appearing_mid_job_fails_the_job(self):
        data = self.queue()
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            if (params or {}).get("preview") == "false" and self.famly.commit_attempts == 1:
                self.famly.plans.append(
                    {"id": "surprise", "childId": CHILD, "from": "2030-01-01", "to": None}
                )
            return node

        with mock.patch.object(self.famly, "post", post):
            self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_FAILED)
        self.assertEqual(len(self.famly.commits), 1)
        self.assertIn("surprise", job.detail)
        self.assertIn("open-ended", self.notices[-1])

    def test_a_bad_join_fails_the_job(self):
        self.famly.trim = False
        data = self.queue()
        self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_FAILED)
        self.assertIn("Famly's end date is exclusive", job.detail)
        self.assertEqual(len(self.famly.commits), 2)  # stopped after the second

    def test_rounding_that_did_not_save_warns_but_does_not_fail(self):
        self.famly.drop_adjustments_on_save = True
        data = self.queue(self.body(auto_round="true"))
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)
        self.assertTrue(any("does not add up" in n for n in self.notices))


# --------------------------------------------------------------------------- #
# Held plans
# --------------------------------------------------------------------------- #
class HeldTests(QueueTestCase):
    def warn_on_funded(self):
        def hook(node, body):
            if body["plan"].get("publicFundingSettings"):
                node["behaviors"] = WARNING

        self.famly.preview_hook = hook

    def test_a_warning_holds_the_job_and_nothing_is_committed_for_that_plan(self):
        self.warn_on_funded()
        data = self.queue()
        self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_HELD)
        self.assertEqual(job.next_index, 1)  # plan 1 was clean and committed
        self.assertEqual(len(self.famly.commits), 1)
        self.assertIn("warning: Something odd", job.detail)

    def test_the_normal_approval_message_is_posted_at_once_with_the_reason(self):
        self.warn_on_funded()
        self.queue()
        self.run_all()

        text, preview_id = self.previews_posted[-1]
        self.assertIn("*Plan 2 of 3 (funded, 2026-11-01 to 2026-12-31)*", text)
        self.assertIn("Held for approval — warning: Something odd", text)
        stored = preview_store.get(preview_id)
        self.assertEqual(stored.status, preview_store.STATUS_PENDING)
        self.assertEqual(stored.old_plan_id, "plan-1")

    def test_the_held_message_names_the_child(self):
        self.warn_on_funded()
        with mock.patch.object(child_lookup, "lookup_child_name", lambda *a, **k: "Alex Smith"):
            self.queue()
            self.run_all()

        self.assertIn(f"Alex Smith (`{CHILD}`)", self.previews_posted[-1][0])

    def test_approving_it_returns_the_job_to_the_queue_and_the_worker_carries_on(self):
        self.warn_on_funded()
        data = self.queue()
        self.run_all()
        held_preview = self.job(data).preview_id

        text = approval.handle_click(slack.ACTION_APPROVE, held_preview, "drew")

        self.assertIn("committed to Famly", text)
        job = self.job(data)
        self.assertEqual((job.status, job.next_index), (jobs.STATUS_QUEUED, 2))
        self.assertEqual(job.segments[1]["plan_id"], "plan-2")

        self.famly.preview_hook = None
        self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_DONE)
        self.assertEqual(self.commit_params(), [None, "plan-1", "plan-2"])

    def test_the_approval_still_goes_through_every_guard(self):
        self.warn_on_funded()
        data = self.queue()
        self.run_all()

        with mock.patch.dict("os.environ", {"COMMIT_ENABLED": "false"}):
            text = approval.handle_click(slack.ACTION_APPROVE, self.job(data).preview_id, "drew")

        self.assertEqual(text, approval.MSG_COMMIT_DISABLED)
        self.assertEqual(self.job(data).status, jobs.STATUS_HELD)

    def test_rejecting_it_cancels_the_job(self):
        self.warn_on_funded()
        data = self.queue()
        self.run_all()

        approval.handle_click(slack.ACTION_REJECT, self.job(data).preview_id, "drew")

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_CANCELLED)
        self.assertIn("rejected", job.detail)
        self.assertEqual(len(self.famly.commits), 1)  # plan 1 stays; nothing deleted

    def test_an_expired_held_plan_cancels_the_job_with_a_notice(self):
        self.warn_on_funded()
        data = self.queue()
        self.run_all()
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with preview_store.open_store() as connection:
            connection.execute("UPDATE previews SET created_at = ?", (old,))

        self.assertEqual(worker.expire_held(), [data["job_id"]])

        self.assertEqual(self.job(data).status, jobs.STATUS_CANCELLED)
        self.assertIn("expired", self.notices[-1])

    def test_a_held_job_does_not_count_as_a_failure(self):
        self.warn_on_funded()
        self.queue()
        self.run_all()

        self.assertEqual(jobs.consecutive_failures(), 0)

    def test_the_next_job_runs_while_one_is_held(self):
        other = "00000000-0000-0000-0000-0000000000c2"
        self.warn_on_funded()
        first = self.queue()
        second = self.queue(self.body(childId=other, funded=False))
        with mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": f"{CHILD},{other}"}):
            self.run_all()

        self.assertEqual(self.job(first).status, jobs.STATUS_HELD)
        self.assertEqual(self.job(second).status, jobs.STATUS_DONE)


class CleanCheckTests(QueueTestCase):
    def held_for(self, fragment):
        job = jobs.list_jobs()[0]
        self.assertEqual(job.status, jobs.STATUS_HELD, job.detail)
        self.assertIn(fragment, job.detail)
        self.assertEqual(len(self.famly.commits), 0)

    def test_a_flattener_warning_holds_it(self):
        self.queue(harness.payload())  # no site_code: the flattener warns
        self.run_all()

        self.held_for("warning:")

    def test_a_famly_warning_holds_it(self):
        self.famly.preview_hook = lambda node, body: node.update(behaviors=WARNING)
        self.queue()
        self.run_all()

        self.held_for("Something odd")

    def test_the_funding_limit_note_is_informational(self):
        note = [
            {
                "id": "ShowPlanWarnings",
                "payload": {
                    "warnings": [
                        {"title": "Funding limit reached", "message": "fyi", "severity": "info", "error": "X"}
                    ]
                },
            }
        ]
        self.famly.preview_hook = lambda node, body: node.update(behaviors=note)
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)

    def test_rounding_that_could_not_be_applied_holds_it(self):
        self.famly.estimates = [("pg-1", 269.03), ("pg-1", 275.10)]  # a shared group
        self.queue(self.body(auto_round="true"))
        self.run_all()

        self.held_for("rounding was not applied")

    def test_failed_rounding_verification_holds_it(self):
        self.famly.on_adjusted_preview = lambda node: node["planStates"][0].update(monthlyEstimate=1)
        self.queue(self.body(auto_round="true"))
        self.run_all()

        self.held_for("rounding was not applied")

    def test_rounding_not_requested_is_fine(self):
        self.famly.estimates = [("pg-1", 269.03), ("pg-1", 275.10)]
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)

    def test_an_estimate_of_zero_or_less_holds_it(self):
        for estimate in (0, -5):
            with self.subTest(estimate=estimate):
                jobs.set_status(jobs.list_jobs()[0].job_id, jobs.STATUS_CANCELLED) if jobs.list_jobs() else None
                self.famly.estimates = [("pg-1", estimate), ("pg-2", 300.0)]
                self.queue(self.body(addon_quantity=str(abs(estimate) + 1)))
                self.run_all()
                job = jobs.list_jobs()[-1]
                self.assertEqual(job.status, jobs.STATUS_HELD)
                self.assertIn("not above 0", job.detail)
                jobs.set_status(job.job_id, jobs.STATUS_CANCELLED)

    def test_an_estimate_at_or_above_the_ceiling_holds_it(self):
        self.famly.estimates = [("pg-1", 5000.00), ("pg-2", 300.0)]
        self.queue()
        self.run_all()

        self.held_for("not below QUEUE_MAX_MONTHLY")

    def test_just_below_the_ceiling_is_fine(self):
        self.famly.estimates = [("pg-1", 4999.99), ("pg-2", 300.0)]
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)

    def test_the_ceiling_is_configurable(self):
        with mock.patch.dict("os.environ", {"QUEUE_MAX_MONTHLY": "300"}):
            self.queue()
            self.run_all()

        self.held_for("QUEUE_MAX_MONTHLY (300)")

    def test_a_missing_estimate_holds_it(self):
        self.famly.estimates = [("pg-1", None), ("pg-2", 300.0)]
        self.queue()
        self.run_all()

        self.held_for("not above 0")

    def test_a_plan_that_will_not_validate_at_preview_time_fails_the_job(self):
        data = self.queue()
        self.funding_file.write_text("{}", encoding="utf-8")  # rules vanish after queueing
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_FAILED)
        self.assertEqual(self.famly.commits[:1], self.famly.commits)


# --------------------------------------------------------------------------- #
# Safety rails
# --------------------------------------------------------------------------- #
class InstitutionLimitTests(QueueTestCase):
    def test_an_institution_not_on_the_list_is_held(self):
        with mock.patch.dict("os.environ", {"QUEUE_ALLOWED_INSTITUTIONS": "HDCW"}):
            data = self.queue()
            self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_HELD)
        self.assertIn("not in QUEUE_ALLOWED_INSTITUTIONS", job.detail)
        self.assertEqual(self.famly.commits, [])
        self.assertEqual(len(self.previews_posted), 1)  # the human is asked

    def test_an_empty_list_allows_nothing(self):
        with mock.patch.dict("os.environ", {"QUEUE_ALLOWED_INSTITUTIONS": ""}):
            data = self.queue()
            self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_HELD)
        self.assertEqual(self.famly.commits, [])

    def test_matching_is_case_insensitive_and_trimmed(self):
        with mock.patch.dict("os.environ", {"QUEUE_ALLOWED_INSTITUTIONS": " hdcw , hdcity "}):
            data = self.queue()
            self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)

    def test_the_list_is_read_afresh_for_every_plan(self):
        data = self.queue()
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            if (params or {}).get("preview") == "false":
                os_env = mock.patch.dict("os.environ", {"QUEUE_ALLOWED_INSTITUTIONS": "HDCW"})
                os_env.start()
                self.addCleanup(os_env.stop)
            return node

        with mock.patch.object(self.famly, "post", post):
            self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_HELD)
        self.assertEqual(len(self.famly.commits), 1)  # plan 1 only


class DailyCapTests(QueueTestCase):
    def test_the_cap_stops_commits_and_the_batch_waits_for_the_next_utc_day(self):
        with mock.patch.dict("os.environ", {"QUEUE_MAX_COMMITS_PER_DAY": "2"}):
            data = self.queue()
            self.run_all()

            job = self.job(data)
            self.assertEqual(job.status, jobs.STATUS_QUEUED)
            self.assertEqual((job.next_index, len(self.famly.commits)), (2, 2))
            self.assertIn("daily commit limit", job.detail)
            self.assertEqual(len([n for n in self.notices if "daily limit" in n]), 1)

            self.clock += timedelta(days=1)  # the next UTC day
            self.run_all()

        job = self.job(data)
        self.assertEqual((job.status, job.next_index), (jobs.STATUS_DONE, 3))
        self.assertEqual(len(self.famly.commits), 3)

    def test_the_cap_is_checked_before_every_commit(self):
        with mock.patch.dict("os.environ", {"QUEUE_MAX_COMMITS_PER_DAY": "1"}):
            self.queue()
            self.run_all()

        self.assertEqual(len(self.famly.commits), 1)
        self.assertEqual(self.famly.commit_attempts, 1)

    def test_the_notice_is_posted_once_a_day_however_often_it_polls(self):
        with mock.patch.dict("os.environ", {"QUEUE_MAX_COMMITS_PER_DAY": "1"}):
            self.queue()
            for _ in range(5):
                worker.run_once()

        self.assertEqual(len([n for n in self.notices if "daily limit" in n]), 1)

    def test_other_jobs_wait_once_the_cap_is_reached(self):
        other = "00000000-0000-0000-0000-0000000000c2"
        with mock.patch.dict(
            "os.environ",
            {"QUEUE_MAX_COMMITS_PER_DAY": "3", "COMMIT_ALLOWED_CHILD_IDS": f"{CHILD},{other}"},
        ):
            first = self.queue()
            second = self.queue(self.body(childId=other))
            self.run_all()

        self.assertEqual(self.job(first).status, jobs.STATUS_DONE)
        self.assertEqual(self.job(second).status, jobs.STATUS_QUEUED)
        self.assertEqual(len(self.famly.commits), 3)

    def test_the_count_is_per_utc_day(self):
        jobs.state_increment(f"commits:{jobs.utc_day(self.clock)}")
        self.assertEqual(jobs.commits_today(self.clock), 1)
        self.assertEqual(jobs.commits_today(self.clock + timedelta(days=1)), 0)

    def test_a_dry_run_is_not_limited_by_the_cap(self):
        with mock.patch.dict("os.environ", {"QUEUE_MODE": "dry_run", "QUEUE_MAX_COMMITS_PER_DAY": "0"}):
            data = self.queue()
            self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)


class CircuitBreakerTests(QueueTestCase):
    def failing_job(self, n):
        child = f"00000000-0000-0000-0000-00000000f{n:03d}"
        self.famly.plans.append({"id": f"old-{n}", "childId": child, "from": "2025-09-01", "to": None})
        return self.queue(self.body(childId=child, funded=False))

    def run_with_children(self, count):
        children = ",".join(f"00000000-0000-0000-0000-00000000f{n:03d}" for n in range(count))
        return mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": children + f",{CHILD}"})

    def test_three_failed_jobs_in_a_row_pause_the_queue(self):
        for n in range(4):
            self.failing_job(n)
        self.run_all()

        self.assertTrue(jobs.is_paused())
        self.assertEqual(jobs.consecutive_failures(), 3)
        self.assertEqual(len(jobs.list_jobs(jobs.STATUS_FAILED)), 3)
        self.assertEqual(len(jobs.list_jobs(jobs.STATUS_QUEUED)), 1)  # the fourth waits
        self.assertTrue(any("Queue paused" in n for n in self.notices))

    def test_two_failures_do_not_pause(self):
        for n in range(2):
            self.failing_job(n)
        self.run_all()

        self.assertFalse(jobs.is_paused())

    def test_a_success_in_between_resets_the_count(self):
        self.failing_job(0)
        self.failing_job(1)
        self.run_all()
        self.assertEqual(jobs.consecutive_failures(), 2)

        self.queue(self.body(funded=False))
        self.run_all()
        self.assertEqual(jobs.consecutive_failures(), 0)

        self.failing_job(2)
        self.failing_job(3)
        self.run_all()
        self.assertFalse(jobs.is_paused())

    def test_a_paused_queue_claims_nothing(self):
        jobs.set_paused(True)
        data = self.queue()

        self.assertFalse(worker.run_once())
        self.assertEqual(self.job(data).status, jobs.STATUS_QUEUED)
        self.assertEqual(self.famly.posts, [])

    def test_resume_clears_the_pause_and_the_count_and_the_queue_runs(self):
        for n in range(3):
            self.failing_job(n)
        self.run_all()
        waiting = self.queue(self.body(funded=False))
        self.assertFalse(worker.run_once())

        self.assertEqual(worker.resume(), {"paused": False})

        self.assertEqual((jobs.is_paused(), jobs.consecutive_failures()), (False, 0))
        self.run_all()
        self.assertEqual(self.job(waiting).status, jobs.STATUS_DONE)

    def test_manual_pause(self):
        self.assertEqual(worker.pause(), {"paused": True})
        self.assertTrue(jobs.is_paused())
        self.queue()
        self.assertFalse(worker.run_once())


class PauseMidJobTests(QueueTestCase):
    def test_the_pause_flag_is_checked_before_every_commit_not_just_at_the_start(self):
        data = self.queue()

        def pause_during_plan_2(node, body):
            if self.starts({"body": body}) == "2026-11-01":
                jobs.set_paused(True)

        self.famly.preview_hook = pause_during_plan_2
        self.run_all()

        job = self.job(data)
        self.assertEqual(len(self.famly.commits), 1)  # plan 1 only
        self.assertEqual(self.famly.commit_attempts, 1)
        self.assertEqual((job.status, job.next_index), (jobs.STATUS_QUEUED, 1))
        self.assertIn("paused", job.detail)

        preview_status = {
            r["status"] for r in self._previews_for(2)
        }
        self.assertEqual(preview_status, {preview_store.STATUS_SUPERSEDED})

    def _previews_for(self, index):
        with preview_store.open_store() as connection:
            return list(connection.execute("SELECT status FROM previews WHERE job_index = ?", (index - 1,)))

    def test_after_resume_the_job_carries_on_from_where_it_stopped(self):
        data = self.queue()
        self.famly.preview_hook = (
            lambda node, body: jobs.set_paused(True) if self.starts({"body": body}) == "2026-11-01" else None
        )
        self.run_all()
        self.famly.preview_hook = None

        worker.resume()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)
        self.assertEqual(self.commit_params(), [None, "plan-1", "plan-2"])
        self.assertEqual(len(self.famly.commits), 3)


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #
class AmbiguousCommitTests(QueueTestCase):
    def test_a_commit_that_errored_after_writing_is_read_back_as_a_success(self):
        self.famly.write_then_fail = 2  # plan 2 is written, then Famly errors
        data = self.queue()
        self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_DONE)
        self.assertEqual([s["plan_id"] for s in job.segments], ["plan-1", "plan-2", "plan-3"])
        self.assertEqual(self.famly.commit_attempts, 3)  # never retried

    def test_the_written_plans_preview_can_never_be_approved_into_a_second_plan(self):
        self.famly.write_then_fail = 1
        data = self.queue()
        self.run_all()

        with preview_store.open_store() as connection:
            first = connection.execute(
                "SELECT preview_id FROM previews WHERE job_index = 0 ORDER BY created_at LIMIT 1"
            ).fetchone()["preview_id"]
        self.assertEqual(preview_store.get(first).status, preview_store.STATUS_COMMITTED)
        self.assertEqual(
            approval.handle_click(slack.ACTION_APPROVE, first, "drew"), approval.MSG_ALREADY_COMMITTED
        )

    def test_a_commit_that_failed_and_wrote_nothing_fails_the_job_without_a_retry(self):
        self.famly.fail_commit_number = 2
        data = self.queue()
        self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_FAILED)
        self.assertEqual(job.next_index, 1)
        self.assertEqual(self.famly.commit_attempts, 2)  # one attempt at plan 2, no more
        self.assertEqual(len(self.famly.plans), 1)
        self.assertIn("not there", job.detail)
        self.assertIn("Queue job failed", self.notices[-1])
        self.assertIn("plan `plan-1`", self.notices[-1])
        self.assertIn("open-ended", self.notices[-1])
        self.assertEqual(jobs.consecutive_failures(), 1)

    def test_it_reads_the_childs_plans_to_find_out(self):
        self.famly.fail_commit_number = 1
        self.queue()
        gets_before = self.famly.gets
        self.run_all()

        self.assertGreater(self.famly.gets, gets_before + 1)  # the read-back happened

    def test_a_written_plan_that_shows_up_late_is_still_found(self):
        data = self.queue()
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            if (params or {}).get("preview") == "false" and self.famly.commit_attempts == 1:
                self.famly.lag_reads = 2  # the next two reads omit the new plan
            return node

        with mock.patch.object(self.famly, "post", post):
            self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)
        self.assertEqual(len(self.famly.commits), 3)

    def test_a_commit_the_guards_refuse_is_a_failure_not_a_write(self):
        data = self.queue()
        with mock.patch.dict("os.environ", {"COMMIT_ENABLED": "false"}):
            self.run_all()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_FAILED)
        self.assertEqual(self.famly.commits, [])
        self.assertIn("Commit is disabled", job.detail)

    def test_the_child_allow_list_still_applies(self):
        data = self.queue()
        with mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": "someone-else"}):
            self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_FAILED)
        self.assertEqual(self.famly.commits, [])


class RetryTests(QueueTestCase):
    def test_a_preview_is_retried_with_backoff(self):
        self.famly.preview_failures = [503, 502]
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)
        self.assertEqual(self.worker_sleeps[:2], [2, 4])

    def test_a_429_backs_off_much_longer(self):
        self.famly.preview_failures = [429]
        self.queue()
        self.run_all()

        self.assertEqual(self.worker_sleeps[0], worker.RATE_LIMIT_BACKOFF_SECONDS)

    def test_three_failures_in_a_row_give_up(self):
        self.famly.preview_failures = [503, 503, 503]
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_FAILED)
        self.assertEqual(self.famly.commits, [])

    def test_a_plain_4xx_is_not_retried(self):
        self.famly.preview_failures = [422]
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_FAILED)
        self.assertEqual(self.worker_sleeps, [])

    def test_a_read_is_retried_too(self):
        self.famly.get_failures = [503]
        data = self.queue()
        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)
        self.assertEqual(self.worker_sleeps[0], 2)

    def test_the_retry_helper_directly(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RestHTTPError("x", 500, "")
            return "ok"

        self.assertEqual(worker.retrying(flaky, "test"), "ok")
        self.assertEqual(len(calls), 3)

    def test_a_commit_is_never_wrapped_in_a_retry(self):
        self.famly.fail_commit_number = 1
        self.queue()
        self.run_all()

        self.assertEqual(self.famly.commit_attempts, 1)


class RestartTests(QueueTestCase):
    def crash_mid_job(self, data):
        """A worker that died with the job `running`."""
        claimed = jobs.claim_next()
        self.assertEqual(claimed.job_id, data["job_id"])
        return claimed

    def test_a_running_job_is_resumed_by_read_back_never_by_replaying(self):
        data = self.queue()
        self.crash_mid_job(data)
        # Plan 1 reached Famly before the crash; the job never recorded it.
        self.famly.plans.append({"id": "p-existing", "childId": CHILD, "from": "2026-09-01", "to": None})
        posts_before = len(self.famly.posts)

        recovered = worker.recover_running()

        self.assertEqual(recovered, [data["job_id"]])
        job = self.job(data)
        self.assertEqual((job.status, job.next_index), (jobs.STATUS_QUEUED, 1))
        self.assertEqual(job.segments[0]["plan_id"], "p-existing")
        self.assertEqual(len(self.famly.posts), posts_before)  # nothing replayed

    def test_the_resumed_job_carries_on_from_the_plan_that_exists(self):
        data = self.queue()
        self.crash_mid_job(data)
        self.famly.plans.append({"id": "p-existing", "childId": CHILD, "from": "2026-09-01", "to": None})
        worker.recover_running()

        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)
        self.assertEqual(self.commit_params(), ["p-existing", "plan-1"])  # plans 2 and 3
        self.assertEqual(len(self.famly.commits), 2)  # plan 1 was never committed again

    def test_a_running_job_with_nothing_in_famly_goes_back_to_the_start(self):
        data = self.queue()
        self.crash_mid_job(data)

        worker.recover_running()

        job = self.job(data)
        self.assertEqual((job.status, job.next_index), (jobs.STATUS_QUEUED, 0))
        self.assertEqual(self.famly.commits, [])

    def test_plans_that_do_not_match_the_job_fail_it(self):
        data = self.queue()
        self.crash_mid_job(data)
        self.famly.plans.append({"id": "stranger", "childId": CHILD, "from": "2024-03-01", "to": None})

        worker.recover_running()

        job = self.job(data)
        self.assertEqual(job.status, jobs.STATUS_FAILED)
        self.assertIn("do not match", job.detail)

    def test_every_running_job_is_recovered(self):
        other = "00000000-0000-0000-0000-0000000000c2"
        first = self.queue()
        second = self.queue(self.body(childId=other))
        jobs.claim_next()
        jobs.claim_next()
        self.famly.get = lambda path, params=None: {"plans": []}

        self.assertEqual(sorted(worker.recover_running()), sorted([first["job_id"], second["job_id"]]))

    def test_the_once_command_recovers_then_runs(self):
        import argparse

        from actions.queue_timeline import queue_worker

        data = self.queue()
        self.crash_mid_job(data)

        result = queue_worker.handle(argparse.Namespace(once=True, poll=1))

        self.assertEqual(result["recovered"], [data["job_id"]])
        self.assertTrue(result["ran"])
        self.assertEqual(self.job(data).status, jobs.STATUS_DONE)

    def test_a_job_resumed_after_a_held_approval_is_verified_before_it_goes_on(self):
        data = self.queue()
        job = jobs.claim_next()
        jobs.mark_committed(job.job_id, 0, "ghost")  # recorded, but not in Famly
        jobs.set_status(job.job_id, jobs.STATUS_QUEUED)

        self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_FAILED)
        self.assertEqual(self.famly.posts, [])


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
class CommandTests(QueueTestCase):
    def test_status_reports_the_mode_flags_and_counts(self):
        self.queue()
        jobs.state_increment(f"commits:{jobs.utc_day(self.clock)}")

        result = worker.status()

        self.assertEqual(result["mode"], "auto")
        self.assertFalse(result["paused"])
        self.assertEqual(result["commitsToday"], 1)
        self.assertEqual(result["jobs"]["queued"], 1)
        self.assertEqual(result["allowedInstitutions"], [harness.INSTITUTION])

    def test_list_filters_by_status(self):
        self.queue()
        self.queue(self.body(childId="00000000-0000-0000-0000-0000000000c2"))
        jobs.set_status(jobs.list_jobs()[0].job_id, jobs.STATUS_HELD, "why")

        self.assertEqual(worker.list_jobs("held")["count"], 1)
        self.assertEqual(worker.list_jobs("queued")["count"], 1)
        self.assertEqual(worker.list_jobs()["count"], 2)
        self.assertIn("error", worker.list_jobs("bogus"))

    def test_cancel_a_queued_job(self):
        data = self.queue()

        result = worker.cancel(data["job_id"])

        self.assertTrue(result["cancelled"])
        self.assertEqual(self.job(data).status, jobs.STATUS_CANCELLED)
        self.assertFalse(worker.run_once())

    def test_cancel_never_touches_famly(self):
        data = self.queue()
        self.run_all()  # done
        plans = [dict(p) for p in self.famly.plans]
        posts = len(self.famly.posts)

        result = worker.cancel(data["job_id"])

        self.assertFalse(result["cancelled"])  # already done
        self.assertEqual(self.famly.plans, plans)
        self.assertEqual(len(self.famly.posts), posts)

    def test_cancelling_a_held_job_rejects_its_preview_and_leaves_the_plans(self):
        self.famly.preview_hook = lambda node, body: node.update(behaviors=WARNING) if body["plan"].get("publicFundingSettings") else None
        data = self.queue()
        self.run_all()
        held_preview = self.job(data).preview_id
        plans = [dict(p) for p in self.famly.plans]

        result = worker.cancel(data["job_id"])

        self.assertTrue(result["cancelled"])
        self.assertEqual(preview_store.get(held_preview).status, preview_store.STATUS_REJECTED)
        self.assertEqual(self.famly.plans, plans)
        self.assertEqual(
            approval.handle_click(slack.ACTION_APPROVE, held_preview, "drew"), approval.MSG_ALREADY_REJECTED
        )

    def test_a_cancelled_running_job_stops_at_the_next_plan(self):
        data = self.queue()
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            if (params or {}).get("preview") == "false" and self.famly.commit_attempts == 1:
                worker.cancel(data["job_id"])
            return node

        with mock.patch.object(self.famly, "post", post):
            self.run_all()

        self.assertEqual(self.job(data).status, jobs.STATUS_CANCELLED)
        self.assertEqual(len(self.famly.commits), 1)

    def test_an_unknown_job(self):
        self.assertFalse(worker.cancel("nope")["cancelled"])

    def test_the_commands_are_registered_with_main(self):
        import main

        parser = main.build_parser()
        self.assertEqual(parser.parse_args(["queue-list", "--status", "held"]).status, "held")
        self.assertEqual(parser.parse_args(["queue-cancel", "j1"]).job_id, "j1")
        for command in ("queue-worker", "queue-status", "queue-pause", "queue-resume"):
            self.assertEqual(parser.parse_args([command]).action, command)


class ApprovalHooksTests(QueueTestCase):
    def test_a_plan_that_belongs_to_no_job_is_untouched_by_the_hooks(self):
        plain = {"plan": {"childId": CHILD, "from": "2026-09-01", "planParts": []}}
        preview_store.save("plain", plain, CHILD, 3)

        self.assertEqual(approval.handle_click(slack.ACTION_REJECT, "plain", "drew"), approval.MSG_REJECTED)
        self.assertEqual(jobs.list_jobs(), [])

    def test_the_hooks_do_nothing_for_a_job_that_is_not_held(self):
        data = self.queue()
        job = self.job(data)

        self.assertFalse(jobs.release_after_approval(job.job_id, 0, "plan-x"))
        self.assertFalse(jobs.cancel_after_rejection(job.job_id, 0, "drew"))
        self.assertEqual(self.job(data).status, jobs.STATUS_QUEUED)


# --------------------------------------------------------------------------- #
# Concurrency-safe enqueueing
# --------------------------------------------------------------------------- #
def child_id(n: int) -> str:
    return f"00000000-0000-0000-0000-00000000e{n:03d}"


class ConcurrentEnqueueTests(QueueTestCase):
    def race(self, bodies):
        """Send every body at once, from as many threads, through the real handler."""
        barrier = threading.Barrier(len(bodies))
        results = [None] * len(bodies)
        errors = []

        def send(index, body):
            try:
                barrier.wait(timeout=10)
                results[index] = queue_web.handle(body)
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=send, args=(i, b)) for i, b in enumerate(bodies)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        return results

    def test_two_threads_enqueuing_the_identical_payload_create_one_job(self):
        results = self.race([self.body(), self.body()])

        self.assertEqual([status for _, status in results], [200, 200])
        ids = {data["job_id"] for data, _ in results}
        self.assertEqual(len(ids), 1)
        self.assertEqual(sorted(data["already_queued"] for data, _ in results), [False, True])
        self.assertEqual(len(jobs.list_jobs()), 1)
        self.assertEqual(self.notices, [])

    def test_many_threads_with_the_identical_payload_still_make_one_job(self):
        results = self.race([self.body() for _ in range(8)])

        self.assertEqual({data["job_id"] for data, _ in results}, {jobs.list_jobs()[0].job_id})
        self.assertEqual(len(jobs.list_jobs()), 1)
        self.assertEqual(sum(not data["already_queued"] for data, _ in results), 1)

    def test_two_different_payloads_for_one_child_give_one_active_job_and_a_409(self):
        results = self.race([self.body(), self.body(addon_quantity="1")])

        statuses = sorted(status for _, status in results)
        self.assertEqual(statuses, [200, 409])
        self.assertEqual(len(jobs.list_jobs()), 1)
        winner = jobs.list_jobs()[0]
        loser_data = next(data for data, status in results if status == 409)
        self.assertEqual(loser_data["job_id"], winner.job_id)  # the EXISTING job's id
        self.assertFalse(loser_data["already_queued"])

    def test_the_conflict_posts_a_notice_with_the_cancel_command(self):
        first = self.queue()

        data, status = queue_web.handle(self.body(addon_quantity="1"))

        self.assertEqual(status, 409)
        self.assertEqual(data["job_id"], first["job_id"])
        self.assertEqual(len(self.notices), 1)
        notice = self.notices[0]
        self.assertIn("already has an active job", notice)
        self.assertIn(first["job_id"], notice)
        self.assertIn(f"queue-cancel {first['job_id']}", notice)
        self.assertEqual(data["errors"], [notice])

    def test_a_held_or_running_job_also_blocks(self):
        for status in (jobs.STATUS_RUNNING, jobs.STATUS_HELD):
            with self.subTest(status=status):
                first = self.queue()
                jobs.set_status(first["job_id"], status, "x")

                _, code = queue_web.handle(self.body(addon_quantity="1"))

                self.assertEqual(code, 409)
                jobs.set_status(first["job_id"], jobs.STATUS_CANCELLED)
                with preview_store.open_store() as connection:
                    connection.execute("DELETE FROM jobs")

    def test_a_finished_job_does_not_block_a_different_request_for_the_child(self):
        for status in (jobs.STATUS_DONE, jobs.STATUS_FAILED, jobs.STATUS_CANCELLED):
            with self.subTest(status=status):
                first = self.queue()
                jobs.set_status(first["job_id"], status, "ended")

                data, code = queue_web.handle(self.body(addon_quantity="1"))

                self.assertEqual(code, 200)
                self.assertNotEqual(data["job_id"], first["job_id"])
                with preview_store.open_store() as connection:
                    connection.execute("DELETE FROM jobs")

    def test_the_identical_request_wins_over_the_active_job_rule(self):
        first = self.queue()

        data, status = queue_web.handle(self.body())

        self.assertEqual((status, data["already_queued"]), (200, True))
        self.assertEqual(data["job_id"], first["job_id"])
        self.assertEqual(self.notices, [])

    def test_the_database_itself_enforces_both_rules(self):
        segments = [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}]

        first = jobs.create(CHILD, "2026-09-01", {"a": 1}, segments)
        same = jobs.create(CHILD, "2026-09-01", {"a": 1}, segments)
        other = jobs.create(CHILD, "2026-09-01", {"a": 2}, segments)
        elsewhere = jobs.create("another", "2026-09-01", {"a": 2}, segments)

        self.assertEqual(first.outcome, jobs.CREATED)
        self.assertEqual((same.outcome, same.job.job_id), (jobs.ALREADY_QUEUED, first.job.job_id))
        self.assertEqual((other.outcome, other.job.job_id), (jobs.ACTIVE_CONFLICT, first.job.job_id))
        self.assertEqual(elsewhere.outcome, jobs.CREATED)

        with preview_store.open_store() as connection:
            with self.assertRaises(sqlite3.IntegrityError):  # a raw second INSERT is refused too
                connection.execute(
                    "INSERT INTO jobs (job_id, job_key, child_id, payload, segments, next_index, "
                    "status, created_at, updated_at) VALUES ('x', ?, 'c', '{}', '[]', 0, 'done', 't', 't')",
                    (first.job.job_key,),
                )

    def test_the_indexes_exist_and_the_old_one_is_gone(self):
        jobs.state_get("x")
        with preview_store.open_store() as connection:
            names = {r["name"] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("jobs_job_key_unique", names)
        self.assertIn("jobs_one_active_per_child", names)
        self.assertNotIn("jobs_one_live_per_key", names)

    def test_an_older_store_is_migrated_to_the_new_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, child_id TEXT NOT NULL, "
                "payload TEXT NOT NULL, segments TEXT NOT NULL, next_index INTEGER NOT NULL, "
                "status TEXT NOT NULL, detail TEXT, preview_id TEXT, digest_sent INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX jobs_one_live_per_key ON jobs (job_key) "
                "WHERE status IN ('queued', 'running', 'held', 'done')"
            )
            connection.commit()
            connection.close()

            with mock.patch.dict("os.environ", {"PREVIEW_STORE_PATH": str(path)}):
                segments = [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}]
                self.assertEqual(jobs.create("c", "2026-09-01", {}, segments).outcome, jobs.CREATED)
                with preview_store.open_store() as store:
                    names = {r["name"] for r in store.execute("SELECT name FROM sqlite_master WHERE type='index'")}

        self.assertNotIn("jobs_one_live_per_key", names)
        self.assertIn("jobs_job_key_unique", names)

    def test_ten_different_children_enqueue_concurrently_and_are_processed_in_order(self):
        children = [child_id(n) for n in range(10)]
        bodies = [self.body(funded=False, childId=c) for c in children]
        with mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": ",".join(children)}):
            results = self.race(bodies)
            self.assertEqual([status for _, status in results], [200] * 10)
            self.assertEqual(len({data["job_id"] for data, _ in results}), 10)

            order = [j.child_id for j in jobs.list_jobs()]  # oldest first
            self.assertEqual(sorted(order), sorted(children))

            self.run_all()

        self.assertEqual(
            [c["body"]["plan"]["childId"] for c in self.famly.commits], order
        )
        self.assertEqual({j.status for j in jobs.list_jobs()}, {jobs.STATUS_DONE})

    def test_the_worker_takes_jobs_oldest_first(self):
        first = self.queue(self.body(funded=False, childId=child_id(1)))
        second = self.queue(self.body(funded=False, childId=child_id(2)))
        third = self.queue(self.body(funded=False, childId=child_id(3)))

        claimed = [jobs.claim_next().job_id for _ in range(3)]

        self.assertEqual(claimed, [first["job_id"], second["job_id"], third["job_id"]])
        self.assertIsNone(jobs.claim_next())

    def test_created_at_is_stamped_in_the_insert_so_it_follows_insertion_order(self):
        segments = [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}]
        made = [jobs.create(f"child-{n}", "2026-09-01", {"n": n}, segments).job for n in range(5)]

        self.assertEqual([j.job_id for j in jobs.list_jobs()], [j.job_id for j in made])
        stamps = [j.created_at for j in made]
        self.assertEqual(stamps, sorted(stamps))

    def test_the_handler_makes_no_network_calls(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("the enqueue path made a network call")

        self.queue()  # a first job, so every outcome below is exercised
        with mock.patch("socket.socket.connect", forbidden), mock.patch(
            "requests.sessions.Session.request", forbidden
        ), mock.patch("requests.post", forbidden), mock.patch("requests.get", forbidden):
            created = queue_web.handle(self.body(childId=child_id(7)))
            again = queue_web.handle(self.body(childId=child_id(7)))
            conflict = queue_web.handle(self.body(childId=child_id(7), addon_quantity="1"))
            refused = queue_web.handle(self.body(start_date=""))

        self.assertEqual([c[1] for c in (created, again, conflict, refused)], [200, 200, 409, 422])
        self.assertEqual((self.famly.posts, self.famly.gets), ([], 0))

    def test_the_handler_does_not_look_up_the_childs_name(self):
        calls = []
        with mock.patch.object(child_lookup, "lookup_child_name", lambda *a, **k: calls.append(a)):
            first = self.queue()
            queue_web.handle(self.body(addon_quantity="1"))  # the 409, which posts a notice

        self.assertEqual(calls, [])

    def test_a_store_that_is_busy_waits_instead_of_failing(self):
        # A held write lock makes the enqueue wait (the store's busy timeout)
        # rather than raise at once.
        segments = [{"from": "2026-09-01", "to": None, "funded": False, "advanced": False}]
        holder = sqlite3.connect(preview_store.store_path(), timeout=10, check_same_thread=False)
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("BEGIN IMMEDIATE")
        released = threading.Timer(0.5, lambda: (holder.rollback(), holder.close()))
        released.start()
        try:
            started = time.monotonic()
            result = jobs.create(CHILD, "2026-09-01", {"busy": 1}, segments)
            waited = time.monotonic() - started
        finally:
            released.join()

        self.assertEqual(result.outcome, jobs.CREATED)
        self.assertGreaterEqual(waited, 0.3)

    def test_the_store_runs_in_wal_mode_with_a_busy_timeout(self):
        with preview_store.open_store() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertGreaterEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 10000)


if __name__ == "__main__":
    unittest.main()
