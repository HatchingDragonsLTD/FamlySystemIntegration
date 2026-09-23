"""Tests for the Adjust & Approve flow.

Run from the project root:

    python -m unittest discover -s tests -v

NOTHING HERE TOUCHES FAMLY. `runner.preview` and `runner.commit` are stubbed;
the tests that expect a rejection assert ZERO calls were made.

The -1.00..1.00 range is a hard boundary. It is checked on the only path that
can produce an adjusted body, so these tests pin that no route gets around it.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.plan_write import approval, preview_store
from integrations import slack

logging.disable(logging.CRITICAL)

CHILD = "child-under-test"
PREVIEW_ID = "preview-original"
PRICING_GROUP = "pg-active"

PLAN_BODY = {
    "plan": {
        "id": "",
        "childId": CHILD,
        "from": "2026-09-01",
        "planParts": [{"planPartId": "pp-1", "sessionBookings": []}],
    }
}


class FakePlan:
    id = "plan-created-1"
    monthly_estimate = 812.50


class FakeResult:
    plan = FakePlan()
    warnings: list = []


class AdjustTestCase(unittest.TestCase):
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
                "SLACK_BOT_TOKEN": "xoxb-test",
                "SLACK_CHANNEL_ID": "C123",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        self.previews = []
        self.posted = []
        self.opened = []

        def fake_preview(body, version, client=None):
            self.previews.append((body, version))
            return FakeResult()

        for target, replacement in (
            ("preview", fake_preview),
            ("commit", mock.DEFAULT),
        ):
            if replacement is not mock.DEFAULT:
                patcher = mock.patch.object(approval.runner, target, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)

        post = mock.patch.object(
            slack, "post_adjusted", lambda text, pid: self.posted.append((text, pid)) or True
        )
        post.start()
        self.addCleanup(post.stop)

        self.notices = []
        notice = mock.patch.object(
            slack, "post_notice", lambda text: self.notices.append(text) or True
        )
        notice.start()
        self.addCleanup(notice.stop)

        opener = mock.patch.object(
            slack, "open_modal", lambda trigger, view: self.opened.append((trigger, view)) or True
        )
        opener.start()
        self.addCleanup(opener.stop)

    def save_pending(self, preview_id=PREVIEW_ID, pricing_group=PRICING_GROUP):
        preview_store.save(
            preview_id, PLAN_BODY, CHILD, 3, pricing_group_id=pricing_group
        )

    def submit(self, value, preview_id=PREVIEW_ID):
        """Submit, running the background work inline.

        The real handler hands the re-price to a thread; running it inline here
        keeps these assertions deterministic. The threading itself is covered
        by AsyncAckTests below.
        """
        return approval.handle_adjust_submission(
            preview_id, value, "drew", spawn=lambda work: work()
        )

    def new_preview_id(self):
        """The preview_id the Confirm Commit button was posted with."""
        return self.posted[-1][1]


class AdjustClickTests(AdjustTestCase):
    def test_the_click_opens_a_modal_and_changes_nothing(self):
        self.save_pending()

        text = approval.handle_click(
            slack.ACTION_ADJUST, PREVIEW_ID, "drew", "trigger-1"
        )

        self.assertIsNone(text)  # the message is left as it was
        self.assertEqual(len(self.opened), 1)
        trigger, view = self.opened[0]
        self.assertEqual(trigger, "trigger-1")
        self.assertEqual(view["private_metadata"], PREVIEW_ID)
        # Nothing previewed, nothing stored, nothing changed.
        self.assertEqual(self.previews, [])
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
        )

    def test_an_expired_preview_cannot_be_adjusted(self):
        from datetime import datetime, timedelta, timezone

        self.save_pending()
        stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        with preview_store._open() as connection:
            connection.execute(
                "UPDATE previews SET created_at = ? WHERE preview_id = ?",
                (stale, PREVIEW_ID),
            )

        text = approval.handle_click(
            slack.ACTION_ADJUST, PREVIEW_ID, "drew", "trigger-1"
        )

        self.assertEqual(text, approval.MSG_NOT_FOUND)
        self.assertEqual(self.opened, [])


class AdjustRangeTests(AdjustTestCase):
    """The hard boundary. Nothing may pass -1.00..1.00."""

    def test_a_valid_adjustment_is_accepted(self):
        self.save_pending()
        self.assertIsNone(self.submit("0.50"))

    def test_the_boundaries_themselves_are_inclusive(self):
        for index, value in enumerate(("1.00", "-1.00")):
            preview_id = f"pv-bound-{index}"
            self.save_pending(preview_id)
            self.assertIsNone(self.submit(value, preview_id), value)

    def test_out_of_range_is_rejected_inline_and_does_nothing(self):
        self.save_pending()

        response = self.submit("1.50")

        self.assertEqual(response["response_action"], "errors")
        self.assertIn(slack.ADJUST_BLOCK_ID, response["errors"])
        self.assertIn("between -1.00 and 1.00", response["errors"][slack.ADJUST_BLOCK_ID])

        # No Famly call, nothing stored, nothing superseded.
        self.assertEqual(self.previews, [])
        self.assertEqual(self.posted, [])
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
        )

    def test_just_outside_the_boundary_is_rejected(self):
        for value in ("1.01", "-1.01", "5", "-2.5"):
            self.save_pending()
            response = self.submit(value)
            self.assertEqual(response["response_action"], "errors", value)
        self.assertEqual(self.previews, [])

    def test_a_non_numeric_entry_is_rejected_inline(self):
        self.save_pending()

        response = self.submit("a lot")

        self.assertEqual(response["response_action"], "errors")
        self.assertEqual(self.previews, [])

    def test_an_empty_entry_is_rejected_inline(self):
        self.save_pending()

        self.assertEqual(self.submit("")["response_action"], "errors")
        self.assertEqual(self.submit(None)["response_action"], "errors")
        self.assertEqual(self.previews, [])


class AdjustSubmissionTests(AdjustTestCase):
    def test_the_adjustment_is_applied_and_re_previewed(self):
        self.save_pending()

        self.assertIsNone(self.submit("0.50"))

        self.assertEqual(len(self.previews), 1)
        body, version = self.previews[0]
        self.assertEqual(version, 3)

        adjustments = body["plan"]["planParts"][0]["totalAdjustments"]
        self.assertEqual(
            adjustments, [{"pricingGroupId": PRICING_GROUP, "adjustment": 0.5}]
        )

    def test_the_original_stored_body_is_left_untouched(self):
        self.save_pending()
        self.submit("0.50")

        original = preview_store.get(PREVIEW_ID)
        self.assertEqual(original.plan_body, PLAN_BODY)
        self.assertNotIn(
            "totalAdjustments", original.plan_body["plan"]["planParts"][0]
        )

    def test_a_new_preview_is_stored_with_the_adjusted_body(self):
        self.save_pending()
        self.submit("0.50")

        stored = preview_store.get(self.new_preview_id())

        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, preview_store.STATUS_PENDING)
        self.assertEqual(stored.child_id, CHILD)
        self.assertEqual(stored.version, 3)
        self.assertEqual(stored.pricing_group_id, PRICING_GROUP)
        self.assertEqual(
            stored.plan_body["plan"]["planParts"][0]["totalAdjustments"],
            [{"pricingGroupId": PRICING_GROUP, "adjustment": 0.5}],
        )

    def test_the_original_is_marked_superseded(self):
        self.save_pending()
        self.submit("0.50")

        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_SUPERSEDED
        )

    def test_a_superseded_preview_can_no_longer_be_approved(self):
        self.save_pending()
        self.submit("0.50")

        with mock.patch.object(approval.runner, "commit") as commit:
            text = approval.handle_click(slack.ACTION_APPROVE, PREVIEW_ID, "drew")

        commit.assert_not_called()
        self.assertEqual(text, approval.MSG_SUPERSEDED)

    def test_the_posted_message_shows_the_adjustment_and_new_estimate(self):
        self.save_pending()
        self.submit("0.50")

        text, _ = self.posted[-1]
        self.assertIn("+0.50", text)
        self.assertIn("812.50", text)

    def test_nothing_is_committed_by_a_submission(self):
        self.save_pending()

        with mock.patch.object(approval.runner, "commit") as commit:
            self.submit("0.50")

        commit.assert_not_called()

    def test_a_preview_without_a_pricing_group_is_refused(self):
        # Guessing the group would misprice the adjustment.
        self.save_pending(pricing_group=None)

        response = self.submit("0.50")

        self.assertEqual(response["response_action"], "errors")
        self.assertIn("pricing group", response["errors"][slack.ADJUST_BLOCK_ID])
        self.assertEqual(self.previews, [])

    def test_a_famly_failure_is_reported_in_the_channel(self):
        # The submission is already acked by the time Famly is called, so the
        # failure cannot be an inline modal error -- it has to reach the
        # approver as a message.
        from core.rest_client import RestHTTPError

        self.save_pending()

        with mock.patch.object(
            approval.runner, "preview", side_effect=RestHTTPError("boom", 500, "x")
        ):
            response = self.submit("0.50")

        self.assertIsNone(response)  # validation passed, so the modal closed
        self.assertEqual(self.posted, [])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Could not re-price", self.notices[0])
        # Still approvable: the adjustment simply did not happen.
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
        )


class AsyncAckTests(AdjustTestCase):
    """Slack allows ~3s to acknowledge. The re-price must not hold that up."""

    def test_the_ack_returns_before_the_re_preview_finishes(self):
        import threading
        import time

        self.save_pending()

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_preview(body, version, client=None):
            started.set()
            release.wait(timeout=5)
            finished.set()
            return FakeResult()

        with mock.patch.object(approval.runner, "preview", blocking_preview):
            began = time.monotonic()
            # The real threading path -- no injected spawn.
            response = approval.handle_adjust_submission(
                PREVIEW_ID, "0.50", "drew"
            )
            elapsed = time.monotonic() - began

            self.assertIsNone(response)
            # Well inside Slack's ~3 second budget, while the work is blocked.
            self.assertLess(elapsed, 1.0)
            self.assertTrue(started.wait(timeout=5))
            self.assertFalse(finished.is_set())

            # Nothing has happened yet: the original is still pending.
            self.assertEqual(
                preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
            )

            release.set()
            self.assertTrue(finished.wait(timeout=5))

        # And once it completes, the work really did happen.
        for _ in range(50):
            if self.posted:
                break
            time.sleep(0.02)

        self.assertEqual(len(self.posted), 1)
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_SUPERSEDED
        )

    def test_an_invalid_submission_is_still_answered_inline_not_backgrounded(self):
        self.save_pending()

        spawned = []
        response = approval.handle_adjust_submission(
            PREVIEW_ID, "1.50", "drew", spawn=lambda work: spawned.append(work)
        )

        # Validation runs before the ack, so this is an inline error and no
        # background work is scheduled at all.
        self.assertEqual(response["response_action"], "errors")
        self.assertEqual(spawned, [])

    def test_a_background_failure_never_escapes_the_thread(self):
        self.save_pending()

        with mock.patch.object(
            approval.runner, "preview", side_effect=RuntimeError("unexpected")
        ):
            # Runs inline: an escaping exception would fail this test outright.
            response = self.submit("0.50")

        self.assertIsNone(response)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Could not re-price", self.notices[0])

    def test_a_failed_post_still_tells_the_approver(self):
        self.save_pending()

        with mock.patch.object(slack, "post_adjusted", lambda text, pid: False):
            self.submit("0.50")

        # The plan was re-priced and stored, so the id must be recoverable.
        self.assertEqual(len(self.notices), 1)
        self.assertIn("could not post", self.notices[0])


class ConfirmCommitTests(AdjustTestCase):
    """Confirm Commit is an ordinary Approve, guards and all."""

    def test_it_commits_the_adjusted_body_through_the_normal_path(self):
        self.save_pending()
        self.submit("0.50")
        new_id = self.new_preview_id()

        with mock.patch.object(
            approval.runner, "commit", return_value=FakeResult()
        ) as commit:
            text = approval.handle_click(slack.ACTION_APPROVE, new_id, "drew")

        commit.assert_called_once()
        args, kwargs = commit.call_args

        # The ADJUSTED body, with the guards unchanged.
        self.assertEqual(
            args[0]["plan"]["planParts"][0]["totalAdjustments"],
            [{"pricingGroupId": PRICING_GROUP, "adjustment": 0.5}],
        )
        self.assertIs(kwargs["confirm"], True)
        self.assertEqual(kwargs["allowed_child_ids"], {CHILD})
        self.assertIn("committed to Famly", text)

    def test_the_test_child_guard_still_applies(self):
        preview_store.save(
            "pv-other",
            {"plan": {"childId": "someone-else", "planParts": [{}]}},
            "someone-else",
            3,
            pricing_group_id=PRICING_GROUP,
        )
        self.submit("0.50", "pv-other")

        with mock.patch.object(approval.runner, "commit") as commit:
            text = approval.handle_click(
                slack.ACTION_APPROVE, self.new_preview_id(), "drew"
            )

        commit.assert_not_called()
        self.assertIn("Commit refused", text)

    def test_commit_disabled_still_blocks(self):
        self.save_pending()
        self.submit("0.50")

        with mock.patch.dict("os.environ", {"COMMIT_ENABLED": "false"}):
            with mock.patch.object(approval.runner, "commit") as commit:
                text = approval.handle_click(
                    slack.ACTION_APPROVE, self.new_preview_id(), "drew"
                )

        commit.assert_not_called()
        self.assertEqual(text, approval.MSG_COMMIT_DISABLED)

    def test_double_clicking_confirm_commits_once(self):
        self.save_pending()
        self.submit("0.50")
        new_id = self.new_preview_id()

        with mock.patch.object(
            approval.runner, "commit", return_value=FakeResult()
        ) as commit:
            first = approval.handle_click(slack.ACTION_APPROVE, new_id, "drew")
            second = approval.handle_click(slack.ACTION_APPROVE, new_id, "drew")

        self.assertEqual(commit.call_count, 1)
        self.assertIn("committed to Famly", first)
        self.assertEqual(second, approval.MSG_ALREADY_COMMITTED)


class UnadjustedFlowTests(AdjustTestCase):
    """The plain Approve/Reject behaviour must be exactly as it was."""

    def test_approve_without_any_adjustment_is_unchanged(self):
        self.save_pending()

        with mock.patch.object(
            approval.runner, "commit", return_value=FakeResult()
        ) as commit:
            text = approval.handle_click(slack.ACTION_APPROVE, PREVIEW_ID, "drew")

        commit.assert_called_once()
        args, _ = commit.call_args
        # The original body, with no totalAdjustments added.
        self.assertNotIn("totalAdjustments", args[0]["plan"]["planParts"][0])
        self.assertIn("committed to Famly", text)

    def test_reject_without_any_adjustment_is_unchanged(self):
        self.save_pending()

        with mock.patch.object(approval.runner, "commit") as commit:
            text = approval.handle_click(slack.ACTION_REJECT, PREVIEW_ID, "drew")

        commit.assert_not_called()
        self.assertEqual(text, approval.MSG_REJECTED)
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_REJECTED
        )


if __name__ == "__main__":
    unittest.main()
