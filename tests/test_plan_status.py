"""Tests for the plan_status action.

Run from the project root:

    python -m unittest discover -s tests -v

`read_child_plans.runner.RestClient` is swapped for a fake so no test reaches
Famly. `slack.post_notice` is swapped for a recorder so a Slack post can be
asserted on (or asserted NOT to have happened) without a real token.
"""

import logging
import unittest
from unittest import mock

from actions.plan_status import web as plan_status_web
from actions.read_child_plans import runner as read_child_plans
from core.rest_client import RestHTTPError
from integrations import slack

logging.disable(logging.CRITICAL)

CHILD_ID = "child-under-test"
EXISTING_PLAN_ID = "plan-existing-1"
EXISTING_VERSION = 2


class FakeRest:
    """Stands in for RestClient.get -- returns a canned plans response."""

    def __init__(self, body):
        self._body = body
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self._body


def no_plans_body():
    return {"plans": [], "sessions": [], "products": []}


def one_plan_body():
    return {
        "plans": [
            {
                "id": EXISTING_PLAN_ID,
                "version": EXISTING_VERSION,
                "childId": CHILD_ID,
                "planParts": [],
                "behaviors": [],
            }
        ],
        "sessions": [],
        "products": [],
    }


class PlanStatusTests(unittest.TestCase):
    def setUp(self):
        self.notices = []

        def fake_post_notice(text):
            self.notices.append(text)
            return True

        notice_patcher = mock.patch.object(slack, "post_notice", fake_post_notice)
        notice_patcher.start()
        self.addCleanup(notice_patcher.stop)

    def _patch_rest(self, body):
        fake = FakeRest(body)
        patcher = mock.patch.object(
            read_child_plans, "RestClient", lambda *a, **k: fake
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_no_existing_plan_reports_new_plan_with_no_slack_call(self):
        self._patch_rest(no_plans_body())

        data, status = plan_status_web.handle(
            {"action": "plan_status", "childId": CHILD_ID}
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["decision"], plan_status_web.DECISION_NEW_PLAN)
        self.assertIsNone(data["existing_plan_id"])
        self.assertIsNone(data["existing_version"])
        self.assertEqual(self.notices, [])

    def test_existing_plan_with_change_plan_intent_reports_change_plan(self):
        self._patch_rest(one_plan_body())

        data, status = plan_status_web.handle(
            {
                "action": "plan_status",
                "childId": CHILD_ID,
                "intended_action": "change_plan",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["decision"], plan_status_web.DECISION_CHANGE_PLAN)
        self.assertEqual(data["existing_plan_id"], EXISTING_PLAN_ID)
        self.assertEqual(data["existing_version"], EXISTING_VERSION)
        # No Famly write of any kind -- this decision is reported only.
        self.assertEqual(self.notices, [])

    def test_existing_plan_with_preview_intent_reports_error_and_posts_slack(self):
        self._patch_rest(one_plan_body())

        data, status = plan_status_web.handle(
            {
                "action": "plan_status",
                "childId": CHILD_ID,
                "intended_action": "plan_preview",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["decision"], plan_status_web.DECISION_ERROR_PLAN_EXISTS)
        self.assertEqual(data["existing_plan_id"], EXISTING_PLAN_ID)
        self.assertEqual(len(self.notices), 1)
        self.assertIn(CHILD_ID, self.notices[0])
        self.assertIn(EXISTING_PLAN_ID, self.notices[0])

    def test_existing_plan_with_no_intended_action_reports_error_and_posts_slack(self):
        self._patch_rest(one_plan_body())

        data, status = plan_status_web.handle(
            {"action": "plan_status", "childId": CHILD_ID}
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["decision"], plan_status_web.DECISION_ERROR_PLAN_EXISTS)
        self.assertEqual(data["existing_plan_id"], EXISTING_PLAN_ID)
        self.assertEqual(len(self.notices), 1)

    def test_famly_read_failure_is_not_swallowed_as_no_plan(self):
        class FailingRest:
            def get(self, path, params=None):
                raise RestHTTPError("boom", status_code=502, body="upstream down")

        patcher = mock.patch.object(
            read_child_plans, "RestClient", lambda *a, **k: FailingRest()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.assertRaises(RestHTTPError):
            plan_status_web.handle({"action": "plan_status", "childId": CHILD_ID})

        # A read failure must never be mistaken for "no plan" and post nothing.
        self.assertEqual(self.notices, [])


if __name__ == "__main__":
    unittest.main()
