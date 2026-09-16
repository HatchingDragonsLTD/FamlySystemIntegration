"""Tests for the preview store and the Slack approval commit path.

Run from the project root:

    python -m unittest discover -s tests -v

NOTHING HERE TOUCHES FAMLY. `runner.commit` is mocked in every test; a test
that reached the real API could create a real plan for a real child, so the
mock is the safety boundary, not a convenience.

Each test points PREVIEW_STORE_PATH at a temporary SQLite file, so the real
store is never read or written.
"""

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from actions.plan_write import approval, preview_store

logging.disable(logging.CRITICAL)

CHILD_ID = "child-under-test"
OTHER_CHILD = "some-other-child"
PREVIEW_ID = "preview-abc"

PLAN_BODY = {
    "plan": {
        "id": "",
        "childId": CHILD_ID,
        "from": "2026-09-01",
        "to": None,
        "planParts": [{"planPartId": "pp-1", "sessionBookings": []}],
    }
}


class FakePlan:
    id = "plan-created-1"


class FakeResult:
    plan = FakePlan()
    warnings: list = []


class StoreTestCase(unittest.TestCase):
    """Base: an isolated store, and commit-enabled config by default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        store_file = Path(self._tmp.name) / "store.sqlite3"
        env = mock.patch.dict(
            "os.environ",
            {
                "PREVIEW_STORE_PATH": str(store_file),
                "PREVIEW_TTL_HOURS": "24",
                "FAMLY_ACCESS_TOKEN": "test-token",
                "COMMIT_ENABLED": "true",
                "COMMIT_ALLOWED_CHILD_IDS": CHILD_ID,
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def save_pending(self, preview_id=PREVIEW_ID, child_id=CHILD_ID):
        preview_store.save(preview_id, PLAN_BODY, child_id, 3)

    def approve(self, preview_id=PREVIEW_ID):
        return approval.handle_click("plan_approve", preview_id, "drew")

    def reject(self, preview_id=PREVIEW_ID):
        return approval.handle_click("plan_reject", preview_id, "drew")


class PreviewStoreTests(StoreTestCase):
    def test_save_then_get_returns_the_exact_plan_body(self):
        self.save_pending()
        stored = preview_store.get(PREVIEW_ID)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.plan_body, PLAN_BODY)
        self.assertEqual(stored.child_id, CHILD_ID)
        self.assertEqual(stored.version, 3)
        self.assertEqual(stored.status, preview_store.STATUS_PENDING)

    def test_get_returns_none_for_an_unknown_id(self):
        self.assertIsNone(preview_store.get("never-existed"))
        self.assertIsNone(preview_store.get(""))

    def test_mark_updates_status(self):
        self.save_pending()
        self.assertTrue(preview_store.mark(PREVIEW_ID, preview_store.STATUS_REJECTED))
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_REJECTED
        )
        self.assertFalse(preview_store.mark("never-existed", "committed"))

    def test_survives_a_restart(self):
        # A fresh connection, as a restarted process would open.
        self.save_pending()
        self.assertEqual(preview_store.get(PREVIEW_ID).plan_body, PLAN_BODY)

    def test_entries_past_the_ttl_are_expired(self):
        self.save_pending()

        stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        with preview_store._open() as connection:
            connection.execute(
                "UPDATE previews SET created_at = ? WHERE preview_id = ?",
                (stale, PREVIEW_ID),
            )

        stored = preview_store.get(PREVIEW_ID)
        self.assertTrue(stored.is_expired)
        # The expiry is persisted, not just computed.
        self.assertEqual(stored.status, preview_store.STATUS_EXPIRED)


class RejectTests(StoreTestCase):
    def test_reject_marks_and_never_commits(self):
        self.save_pending()

        with mock.patch.object(approval.runner, "commit") as commit:
            text = self.reject()

        commit.assert_not_called()
        self.assertEqual(text, approval.MSG_REJECTED)
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_REJECTED
        )


class ApproveTests(StoreTestCase):
    def test_approve_commits_the_stored_body(self):
        self.save_pending()

        with mock.patch.object(
            approval.runner, "commit", return_value=FakeResult()
        ) as commit:
            text = self.approve()

        commit.assert_called_once()
        args, kwargs = commit.call_args

        # EXACTLY what was previewed, not a rebuild.
        self.assertEqual(args[0], PLAN_BODY)
        self.assertEqual(args[1], 3)
        # The guards are passed through, never weakened.
        self.assertIs(kwargs["confirm"], True)
        self.assertEqual(kwargs["allowed_child_ids"], {CHILD_ID})

        self.assertIn("committed to Famly", text)
        self.assertIn("plan-created-1", text)
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_COMMITTED
        )

    def test_double_click_does_not_commit_twice(self):
        self.save_pending()

        with mock.patch.object(
            approval.runner, "commit", return_value=FakeResult()
        ) as commit:
            first = self.approve()
            second = self.approve()

        # The second click must not reach Famly.
        self.assertEqual(commit.call_count, 1)
        self.assertIn("committed to Famly", first)
        self.assertEqual(second, approval.MSG_ALREADY_COMMITTED)

    def test_commit_disabled_short_circuits(self):
        self.save_pending()

        with mock.patch.dict("os.environ", {"COMMIT_ENABLED": "false"}):
            with mock.patch.object(approval.runner, "commit") as commit:
                text = self.approve()

        commit.assert_not_called()
        self.assertEqual(text, approval.MSG_COMMIT_DISABLED)
        # Still pending: disabling is not a decision about the plan.
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
        )

    def test_child_not_in_allow_list_is_refused(self):
        self.save_pending(child_id=OTHER_CHILD)

        with mock.patch.object(approval.runner, "commit") as commit:
            text = self.approve()

        commit.assert_not_called()
        self.assertIn("Commit refused", text)
        self.assertIn(OTHER_CHILD, text)

    def test_empty_allow_list_refuses(self):
        self.save_pending()

        with mock.patch.dict("os.environ", {"COMMIT_ALLOWED_CHILD_IDS": ""}):
            with mock.patch.object(approval.runner, "commit") as commit:
                text = self.approve()

        commit.assert_not_called()
        self.assertIn("Commit refused", text)

    def test_expired_preview_is_refused(self):
        self.save_pending()
        stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        with preview_store._open() as connection:
            connection.execute(
                "UPDATE previews SET created_at = ? WHERE preview_id = ?",
                (stale, PREVIEW_ID),
            )

        with mock.patch.object(approval.runner, "commit") as commit:
            text = self.approve()

        commit.assert_not_called()
        self.assertEqual(text, approval.MSG_NOT_FOUND)

    def test_unknown_preview_is_refused(self):
        with mock.patch.object(approval.runner, "commit") as commit:
            text = self.approve("no-such-preview")

        commit.assert_not_called()
        self.assertEqual(text, approval.MSG_NOT_FOUND)

    def test_runner_refusal_is_surfaced_not_raised(self):
        self.save_pending()

        with mock.patch.object(
            approval.runner,
            "commit",
            side_effect=approval.runner.PlanCommitRefused("child not allowed"),
        ):
            text = self.approve()

        self.assertIn("Commit refused", text)
        # Released, so a corrected retry is possible.
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
        )

    def test_famly_error_is_surfaced_and_nothing_is_marked_committed(self):
        from core.rest_client import RestHTTPError

        self.save_pending()

        with mock.patch.object(
            approval.runner,
            "commit",
            side_effect=RestHTTPError("boom", 500, "upstream"),
        ):
            text = self.approve()

        self.assertIn("Commit failed", text)
        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
        )

    def test_a_4xx_and_a_5xx_read_differently(self):
        from core.rest_client import RestHTTPError

        self.save_pending()
        with mock.patch.object(
            approval.runner,
            "commit",
            side_effect=RestHTTPError("bad plan", 400, "nope"),
        ):
            client_error = self.approve()

        preview_store.save("second", PLAN_BODY, CHILD_ID, 3)
        with mock.patch.object(
            approval.runner,
            "commit",
            side_effect=RestHTTPError("down", 503, "nope"),
        ):
            upstream_error = self.approve("second")

        self.assertIn("rejected the plan", client_error)
        self.assertIn("upstream error", upstream_error)
        self.assertIn("trying again", upstream_error)


class ClaimTests(StoreTestCase):
    def test_only_one_claim_can_win(self):
        self.save_pending()

        first = preview_store.claim_for_commit(PREVIEW_ID)
        second = preview_store.claim_for_commit(PREVIEW_ID)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_release_returns_it_to_pending(self):
        self.save_pending()
        preview_store.claim_for_commit(PREVIEW_ID)
        preview_store.release_claim(PREVIEW_ID)

        self.assertEqual(
            preview_store.get(PREVIEW_ID).status, preview_store.STATUS_PENDING
        )


if __name__ == "__main__":
    unittest.main()
