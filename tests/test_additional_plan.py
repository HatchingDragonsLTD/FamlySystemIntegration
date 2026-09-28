"""Tests for the additional_plan action and the oldPlanId/replaceOldPlan
plumbing it relies on (runner.py's query params, and the full
preview-store -> approval commit path).

Run from the project root:

    python -m unittest discover -s tests -v

NOTHING HERE TOUCHES FAMLY: RestClient is stubbed throughout, and the tests
that expect no Famly call assert ZERO calls were made.
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.additional_plan import web as additional_plan_web
from actions.plan_write import approval, preview_store, runner
from actions.read_child_plans.runner import ChildPlansResult, Plan
from integrations import slack

logging.disable(logging.CRITICAL)

CHILD_ID = "child-under-test"

PLAN_BODY = {
    "plan": {
        "id": "",
        "childId": CHILD_ID,
        "from": "2026-09-01",
        "planParts": [{"planPartId": "pp-1", "sessionBookings": []}],
    }
}


class FakeRest:
    """Records every POST's query params, returning a minimal valid plan body."""

    def __init__(self):
        self.calls = []

    def post(self, path, params=None, json_body=None):
        self.calls.append(dict(params or {}))
        return {
            "id": "plan-new-1",
            "childId": CHILD_ID,
            "planParts": [],
            "behaviors": [],
        }


def empty_result():
    return ChildPlansResult(child_id=CHILD_ID, plans=[])


def one_plan_result(plan_id="existing-plan-1"):
    return ChildPlansResult(
        child_id=CHILD_ID, plans=[Plan(id=plan_id, child_id=CHILD_ID)]
    )


def two_plan_result():
    return ChildPlansResult(
        child_id=CHILD_ID,
        plans=[
            Plan(id="plan-a", child_id=CHILD_ID),
            Plan(id="plan-b", child_id=CHILD_ID),
        ],
    )


# --------------------------------------------------------------------------- #
# runner.py: the oldPlanId/replaceOldPlan query params themselves.
# --------------------------------------------------------------------------- #
class RunnerQueryParamTests(unittest.TestCase):
    """_post_plan/preview/commit, with and without old_plan_id."""

    def test_preview_omits_old_plan_params_by_default(self):
        client = FakeRest()
        runner.preview(PLAN_BODY, 3, client=client)

        self.assertNotIn("oldPlanId", client.calls[0])
        self.assertNotIn("replaceOldPlan", client.calls[0])

    def test_preview_includes_old_plan_params_when_given(self):
        client = FakeRest()
        runner.preview(
            PLAN_BODY,
            3,
            client=client,
            old_plan_id="existing-1",
            replace_old_plan=False,
        )

        self.assertEqual(client.calls[0]["oldPlanId"], "existing-1")
        self.assertEqual(client.calls[0]["replaceOldPlan"], "false")

    def test_replace_old_plan_true_is_sent_as_the_string_true(self):
        client = FakeRest()
        runner.preview(
            PLAN_BODY,
            3,
            client=client,
            old_plan_id="existing-1",
            replace_old_plan=True,
        )

        self.assertEqual(client.calls[0]["replaceOldPlan"], "true")

    def test_commit_sends_old_plan_params_at_both_preview_and_write(self):
        # The captured request shows oldPlanId/replaceOldPlan present at
        # PREVIEW time too, not only on the real write.
        client = FakeRest()
        runner.commit(
            PLAN_BODY,
            3,
            confirm=True,
            allowed_child_ids={CHILD_ID},
            client=client,
            old_plan_id="existing-1",
            replace_old_plan=False,
        )

        self.assertEqual(len(client.calls), 2)
        preview_call, write_call = client.calls
        self.assertEqual(preview_call["preview"], "true")
        self.assertEqual(write_call["preview"], "false")
        for call in (preview_call, write_call):
            self.assertEqual(call["oldPlanId"], "existing-1")
            self.assertEqual(call["replaceOldPlan"], "false")

    def test_commit_without_old_plan_id_is_completely_unchanged(self):
        client = FakeRest()
        runner.commit(
            PLAN_BODY, 3, confirm=True, allowed_child_ids={CHILD_ID}, client=client
        )

        for call in client.calls:
            self.assertNotIn("oldPlanId", call)
            self.assertNotIn("replaceOldPlan", call)


# --------------------------------------------------------------------------- #
# additional_plan/web.py: the zero / one / many decision tree.
# --------------------------------------------------------------------------- #
class AdditionalPlanDecisionTests(unittest.TestCase):
    def setUp(self):
        self.notices = []
        notice_patcher = mock.patch.object(
            slack, "post_notice", lambda text: self.notices.append(text) or True
        )
        notice_patcher.start()
        self.addCleanup(notice_patcher.stop)

        self.preview_calls = []

        def fake_run_preview_and_post(payload, **kwargs):
            self.preview_calls.append((payload, kwargs))
            return {"preview_id": "pv-1"}, 200

        preview_patcher = mock.patch.object(
            additional_plan_web.plan_write_web,
            "run_preview_and_post",
            fake_run_preview_and_post,
        )
        preview_patcher.start()
        self.addCleanup(preview_patcher.stop)

    def _run(self, results):
        sleeps = []

        with mock.patch.object(
            additional_plan_web.read_child_plans, "run", side_effect=results
        ) as run:
            data, status = additional_plan_web.handle(
                {"action": "additional_plan", "childId": CHILD_ID},
                sleeper=lambda seconds: sleeps.append(seconds),
            )
        return data, status, run, sleeps

    def test_zero_then_still_zero_after_retry(self):
        data, status, run, sleeps = self._run([empty_result(), empty_result()])

        self.assertEqual(status, 409)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sleeps, [additional_plan_web.RETRY_DELAY_SECONDS])
        self.assertEqual(len(self.notices), 1)
        self.assertIn(CHILD_ID, self.notices[0])
        self.assertIn("no existing plan found after retry", self.notices[0])
        self.assertEqual(self.preview_calls, [])
        self.assertTrue(data["errors"])

    def test_zero_then_one_found_on_retry_proceeds_with_that_plans_id(self):
        data, status, run, sleeps = self._run(
            [empty_result(), one_plan_result("plan-77")]
        )

        self.assertEqual(status, 200)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sleeps, [additional_plan_web.RETRY_DELAY_SECONDS])
        self.assertEqual(len(self.preview_calls), 1)

        _, kwargs = self.preview_calls[0]
        self.assertEqual(kwargs["old_plan_id"], "plan-77")
        self.assertIs(kwargs["replace_old_plan"], False)
        self.assertEqual(self.notices, [])

    def test_exactly_one_plan_proceeds_without_a_retry(self):
        data, status, run, sleeps = self._run([one_plan_result("plan-42")])

        self.assertEqual(status, 200)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(sleeps, [])  # no retry needed at all
        self.assertEqual(len(self.preview_calls), 1)

        payload, kwargs = self.preview_calls[0]
        self.assertEqual(payload["childId"], CHILD_ID)
        self.assertEqual(kwargs["old_plan_id"], "plan-42")
        self.assertIs(kwargs["replace_old_plan"], False)
        self.assertEqual(self.notices, [])

    def test_two_or_more_plans_names_them_all_and_blocks(self):
        data, status, run, sleeps = self._run([two_plan_result()])

        self.assertEqual(status, 409)
        self.assertEqual(sleeps, [])  # not a zero-plans case -- no retry
        self.assertEqual(self.preview_calls, [])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("plan-a", self.notices[0])
        self.assertIn("plan-b", self.notices[0])
        self.assertEqual(data["existingPlanIds"], ["plan-a", "plan-b"])

    def test_missing_child_id_is_a_client_error_with_no_calls(self):
        with mock.patch.object(additional_plan_web.read_child_plans, "run") as run:
            data, status = additional_plan_web.handle(
                {"action": "additional_plan"}
            )

        self.assertEqual(status, 422)
        run.assert_not_called()
        self.assertEqual(self.notices, [])
        self.assertEqual(self.preview_calls, [])


# --------------------------------------------------------------------------- #
# End to end: a stored additional_plan preview commits with the same params.
# --------------------------------------------------------------------------- #
class ApproveThroughCommitTests(unittest.TestCase):
    """The Approve click for a stored additional_plan preview must send
    oldPlanId/replaceOldPlan in the REAL commit request -- not just have them
    accepted as arguments somewhere along the way.
    """

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
                "COMMIT_ALLOWED_CHILD_IDS": CHILD_ID,
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def test_the_commit_request_carries_old_plan_id_and_replace_old_plan(self):
        preview_store.save(
            "pv-additional",
            PLAN_BODY,
            CHILD_ID,
            3,
            old_plan_id="existing-plan-1",
            replace_old_plan=False,
        )

        client = FakeRest()
        with mock.patch.object(runner, "RestClient", lambda *a, **k: client):
            text = approval.handle_click(
                slack.ACTION_APPROVE, "pv-additional", "drew"
            )

        self.assertIn("committed to Famly", text)

        # commit() previews (step 3) before the real write (step 4) -- both
        # must carry the same old-plan params.
        self.assertEqual(len(client.calls), 2)
        preview_call, write_call = client.calls
        self.assertEqual(preview_call["preview"], "true")
        self.assertEqual(write_call["preview"], "false")
        for call in (preview_call, write_call):
            self.assertEqual(call["oldPlanId"], "existing-plan-1")
            self.assertEqual(call["replaceOldPlan"], "false")

    def test_an_ordinary_stored_preview_still_commits_with_no_old_plan_params(self):
        preview_store.save("pv-ordinary", PLAN_BODY, CHILD_ID, 3)

        client = FakeRest()
        with mock.patch.object(runner, "RestClient", lambda *a, **k: client):
            approval.handle_click(slack.ACTION_APPROVE, "pv-ordinary", "drew")

        for call in client.calls:
            self.assertNotIn("oldPlanId", call)
            self.assertNotIn("replaceOldPlan", call)


if __name__ == "__main__":
    unittest.main()
