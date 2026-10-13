"""Tests for the live check just before runner.commit in approval.py.

Run from the project root:

    python -m unittest tests.test_live_check -v

The check reads the child's plans fresh from Famly at approval time:

  * a plan with no old_plan_id commits only if the child has NO plans;
  * a plan with an old_plan_id commits only if the child's latest-starting plan
    is exactly that one;
  * a check that cannot be made is a refusal.

NOTHING HERE TOUCHES FAMLY: the plans read and `runner.commit` are stubbed, and
every refusal asserts commit was never called.
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.create_timeline import chains
from actions.plan_write import approval, preview_store
from actions.read_child_plans.runner import ChildPlansResult, Plan
from integrations import slack

logging.disable(logging.CRITICAL)

CHILD = "child-under-test"
PREVIEW = "preview-1"
PLAN_BODY = {
    "plan": {
        "id": "",
        "childId": CHILD,
        "from": "2026-09-01",
        "planParts": [{"planPartId": "pp-1", "sessionBookings": []}],
    }
}


class FakeCommitted:
    class plan:
        id = "plan-new"

    warnings: list = []


def plan(plan_id, start):
    return Plan(id=plan_id, child_id=CHILD, from_=start)


class LiveCheckTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(
            "os.environ",
            {
                "PREVIEW_STORE_PATH": str(Path(self._tmp.name) / "store.sqlite3"),
                "PREVIEW_TTL_HOURS": "24",
                "FAMLY_ACCESS_TOKEN": "test-token",
                "COMMIT_ENABLED": "true",
                "COMMIT_ALLOWED_CHILD_IDS": CHILD,
            },
        )
        env.start()
        self.addCleanup(env.stop)

        self.plans: list[Plan] = []
        self.read_error: Exception | None = None
        self.reads: list[tuple] = []

        def fake_run(child_id, version=3, client=None):
            self.reads.append((child_id, version))
            if self.read_error is not None:
                raise self.read_error
            return ChildPlansResult(child_id=child_id, plans=list(self.plans))

        patcher = mock.patch.object(approval.read_child_plans, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.commit = mock.patch.object(
            approval.runner, "commit", return_value=FakeCommitted()
        ).start()
        self.addCleanup(mock.patch.stopall)

        self.notices: list[str] = []
        mock.patch.object(
            slack, "post_notice", lambda text: self.notices.append(text) or True
        ).start()

    def save(self, old_plan_id=None, preview_id=PREVIEW, **kwargs):
        preview_store.save(
            preview_id, PLAN_BODY, CHILD, 3, old_plan_id=old_plan_id, **kwargs
        )

    def approve(self, preview_id=PREVIEW):
        return approval.handle_click(slack.ACTION_APPROVE, preview_id, "drew")

    def status(self, preview_id=PREVIEW):
        return preview_store.get(preview_id).status

    def assert_committed(self, text):
        self.commit.assert_called_once()
        self.assertIn("committed to Famly", text)
        self.assertEqual(self.status(), preview_store.STATUS_COMMITTED)

    def assert_refused(self, text, *fragments):
        self.commit.assert_not_called()
        self.assertIn("Commit refused", text)
        for fragment in fragments:
            self.assertIn(fragment, text)


class NoOldPlanTests(LiveCheckTestCase):
    def test_commits_when_the_child_has_no_plans(self):
        self.save()

        self.assert_committed(self.approve())

    def test_refused_when_the_child_already_has_a_plan(self):
        self.plans = [plan("existing-1", "2025-09-01")]
        self.save()

        text = self.approve()

        self.assert_refused(text, "no old plan", "existing-1", "nothing was written")

    def test_refused_even_when_the_existing_plan_has_ended(self):
        self.plans = [Plan(id="ended", child_id=CHILD, from_="2024-09-01", to="2025-07-31")]
        self.save()

        self.assert_refused(self.approve(), "ended")

    def test_a_refused_ordinary_preview_goes_back_to_pending_so_it_can_be_retried(self):
        self.plans = [plan("existing-1", "2025-09-01")]
        self.save()

        self.approve()
        self.assertEqual(self.status(), preview_store.STATUS_PENDING)

        self.plans = []  # the child's plan was removed in the meantime
        self.assert_committed(self.approve())


class WithOldPlanTests(LiveCheckTestCase):
    def test_commits_when_the_old_plan_is_the_only_plan(self):
        self.plans = [plan("old-1", "2026-01-01")]
        self.save(old_plan_id="old-1")

        self.assert_committed(self.approve())

    def test_commits_when_the_old_plan_is_the_latest_starting_of_several(self):
        self.plans = [plan("a", "2025-01-01"), plan("old-1", "2026-06-01"), plan("b", "2026-01-01")]
        self.save(old_plan_id="old-1")

        self.assert_committed(self.approve())

    def test_refused_when_a_different_plan_starts_later(self):
        self.plans = [plan("old-1", "2026-01-01"), plan("newer", "2026-06-01")]
        self.save(old_plan_id="old-1")

        text = self.approve()

        self.assert_refused(text, "latest-starting plan is `newer`", "`old-1`")

    def test_refused_when_the_old_plan_is_not_the_latest(self):
        self.plans = [plan("old-1", "2025-01-01"), plan("other", "2026-01-01")]
        self.save(old_plan_id="old-1")

        self.assert_refused(self.approve(), "`other`")

    def test_refused_when_two_plans_share_the_latest_start_date(self):
        self.plans = [plan("old-1", "2026-06-01"), plan("twin", "2026-06-01")]
        self.save(old_plan_id="old-1")

        self.assert_refused(self.approve(), "`old-1`", "`twin`")

    def test_refused_when_the_child_has_no_plans_at_all(self):
        self.save(old_plan_id="old-1")

        self.assert_refused(self.approve(), "`old-1`")

    def test_refused_when_the_plans_cannot_be_ordered_by_start(self):
        self.plans = [plan("old-1", "2026-01-01"), Plan(id="undated", child_id=CHILD, from_=None)]
        self.save(old_plan_id="old-1")

        self.assert_refused(self.approve(), "could not be ordered")

    def test_a_refused_preview_goes_back_to_pending(self):
        self.plans = [plan("old-1", "2026-01-01"), plan("newer", "2026-06-01")]
        self.save(old_plan_id="old-1")

        self.approve()

        self.assertEqual(self.status(), preview_store.STATUS_PENDING)


class CheckFailureTests(LiveCheckTestCase):
    def test_a_failed_read_does_not_commit_and_says_so(self):
        self.read_error = RuntimeError("Famly is down")
        self.save()

        text = self.approve()

        self.assert_refused(text, "could not be made", "RuntimeError", "nothing was written")
        self.assertEqual(self.status(), preview_store.STATUS_PENDING)

    def test_a_failed_read_with_an_old_plan_does_not_commit_either(self):
        self.read_error = RuntimeError("Famly is down")
        self.save(old_plan_id="old-1")

        self.assert_refused(self.approve(), "could not be made")


class WhereTheCheckSitsTests(LiveCheckTestCase):
    """The other guards still come first, and none of them is weakened."""

    def test_the_read_uses_the_childs_id_and_the_previews_version(self):
        self.save()
        self.approve()

        self.assertEqual(self.reads, [(CHILD, 3)])

    def test_each_approval_reads_afresh(self):
        self.plans = [plan("existing-1", "2025-09-01")]
        self.save()
        self.approve()
        self.approve()

        self.assertEqual(len(self.reads), 2)

    def test_commit_disabled_stops_before_the_check(self):
        self.save()
        with mock.patch.dict("os.environ", {"COMMIT_ENABLED": "false"}):
            text = self.approve()

        self.assertEqual(text, approval.MSG_COMMIT_DISABLED)
        self.assertEqual(self.reads, [])

    def test_a_child_outside_the_allow_list_stops_before_the_check(self):
        self.save()
        with mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": "someone-else"}):
            text = self.approve()

        self.assertIn("not in the allowed list", text)
        self.assertEqual(self.reads, [])

    def test_an_already_committed_preview_stops_before_the_check(self):
        self.save()
        self.approve()
        self.reads.clear()

        self.assertEqual(self.approve(), approval.MSG_ALREADY_COMMITTED)
        self.assertEqual(self.reads, [])

    def test_passing_the_check_leaves_the_commit_arguments_unchanged(self):
        self.plans = [plan("old-1", "2026-01-01")]
        self.save(old_plan_id="old-1")

        self.approve()

        args, kwargs = self.commit.call_args
        self.assertIs(kwargs["confirm"], True)
        self.assertEqual(kwargs["allowed_child_ids"], {CHILD})
        self.assertEqual(kwargs["old_plan_id"], "old-1")
        self.assertIs(kwargs["replace_old_plan"], False)


class ChainPreviewTests(LiveCheckTestCase):
    def setUp(self):
        super().setUp()
        segments = [
            {"from": "2026-09-01", "to": "2026-10-31", "funded": False, "advanced": False},
            {"from": "2026-11-01", "to": None, "funded": True, "advanced": False},
        ]
        chains.create("chain-1", CHILD, {}, segments)

    def save_chain_preview(self, index, old_plan_id=None):
        self.save(old_plan_id=old_plan_id, chain_id="chain-1", chain_index=index)

    def test_a_failed_check_marks_the_chain_preview_failed_and_stops_the_chain(self):
        self.plans = [plan("old-1", "2026-09-01"), plan("newer", "2026-12-01")]
        self.save_chain_preview(1, old_plan_id="old-1")

        text = self.approve()

        self.assert_refused(text, "`newer`")
        self.assertEqual(self.status(), preview_store.STATUS_FAILED)
        self.assertEqual(chains.get("chain-1").status, chains.STATUS_STOPPED)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Timeline stopped", self.notices[0])
        self.assertIn("latest-starting plan is `newer`", self.notices[0])

    def test_it_is_never_released_back_to_pending(self):
        self.plans = [plan("existing-1", "2025-09-01")]
        self.save_chain_preview(0)

        self.approve()
        text = self.approve()

        self.assertEqual(text, approval.MSG_CHAIN_STOPPED)
        self.commit.assert_not_called()

    def test_a_check_that_cannot_be_made_stops_the_chain_too(self):
        self.read_error = RuntimeError("Famly is down")
        self.save_chain_preview(0)

        text = self.approve()

        self.assert_refused(text, "could not be made")
        self.assertEqual(self.status(), preview_store.STATUS_FAILED)
        self.assertEqual(chains.get("chain-1").status, chains.STATUS_STOPPED)

    def test_a_chain_preview_that_passes_commits_and_advances_the_chain(self):
        self.save_chain_preview(0)
        with mock.patch.object(approval, "_spawn", lambda work: None):
            text = self.approve()

        self.assert_committed(text)
        self.assertEqual(chains.get("chain-1").next_index, 1)


if __name__ == "__main__":
    unittest.main()
