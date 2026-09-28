"""Tests for Famly-call failure logging in actions/plan_write/runner.py.

Run from the project root:

    python -m unittest discover -s tests -v

BEFORE THIS: only a SUCCESSFUL preview was ever logged (plan_write.web's
`PLAN PREVIEW ... plan_full=...` line) -- a failed preview or commit left no
trace of what was actually sent to Famly or what it said back. `_post_plan` is
the single place both `preview()` and `commit()` reach Famly through (for the
plain plan_preview path AND additional_plan alike, and for both steps of a
commit), so it is the one place that needs to log a failure.
"""

import logging
import unittest

from actions.plan_write import runner
from core.rest_client import RestHTTPError

CHILD_ID = "child-under-test"

PLAN_BODY = {
    "plan": {
        "id": "",
        "childId": CHILD_ID,
        "from": "2026-09-01",
        "planParts": [{"planPartId": "pp-1", "sessionBookings": []}],
    }
}


class FailingRest:
    """Always raises RestHTTPError, recording every call it received."""

    def __init__(self, status_code=422, body="famly says no"):
        self.status_code = status_code
        self.body = body
        self.calls = []

    def post(self, path, params=None, json_body=None):
        self.calls.append((path, dict(params or {}), json_body))
        raise RestHTTPError(
            f"POST {path} failed with HTTP {self.status_code}: {self.body}",
            status_code=self.status_code,
            body=self.body,
        )


class FailureLoggingTestCase(unittest.TestCase):
    """Other test modules disable logging globally at import time
    (`logging.disable` is process-wide and never reset), which would silently
    stop `assertLogs` from seeing anything. Re-enable it for the duration of
    these tests only, restoring the ambient disable afterward so it does not
    affect any other test running in the same process.
    """

    def setUp(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)


class PreviewFailureLoggingTests(FailureLoggingTestCase):
    def test_a_failed_preview_logs_the_request_body_and_error_detail(self):
        client = FailingRest(status_code=422, body='{"error":"bad billing profile"}')

        with self.assertLogs(runner.logger, level="ERROR") as captured:
            with self.assertRaises(RestHTTPError):
                runner.preview(PLAN_BODY, 3, client=client)

        self.assertEqual(len(captured.output), 1)
        log_line = captured.output[0]

        # The exact request that was sent to Famly.
        self.assertIn(CHILD_ID, log_line)
        self.assertIn("pp-1", log_line)
        # The error Famly gave back.
        self.assertIn("422", log_line)
        self.assertIn("bad billing profile", log_line)

    def test_the_error_still_propagates_to_the_caller(self):
        # Logging must never swallow the failure.
        client = FailingRest()

        with self.assertRaises(RestHTTPError):
            with self.assertLogs(runner.logger, level="ERROR"):
                runner.preview(PLAN_BODY, 3, client=client)

    def test_query_params_including_old_plan_id_are_logged(self):
        # additional_plan's params must be visible in the failure log too.
        client = FailingRest()

        with self.assertLogs(runner.logger, level="ERROR") as captured:
            with self.assertRaises(RestHTTPError):
                runner.preview(
                    PLAN_BODY,
                    3,
                    client=client,
                    old_plan_id="existing-plan-1",
                    replace_old_plan=False,
                )

        log_line = captured.output[0]
        self.assertIn("oldPlanId", log_line)
        self.assertIn("existing-plan-1", log_line)
        self.assertIn("replaceOldPlan", log_line)
        self.assertIn("preview", log_line)


class CommitFailureLoggingTests(FailureLoggingTestCase):
    def test_a_failed_commit_write_logs_even_though_its_own_preview_succeeded(self):
        # commit() previews successfully (step 3), then fails on the real
        # write (step 4) -- pins that the SECOND _post_plan call is covered
        # too, not just the first.
        class PreviewOkThenWriteFails:
            def __init__(self):
                self.calls = []

            def post(self, path, params=None, json_body=None):
                params = dict(params or {})
                self.calls.append(params)
                if params.get("preview") == "true":
                    return {
                        "id": "",
                        "childId": CHILD_ID,
                        "planParts": [],
                        "behaviors": [],
                    }
                raise RestHTTPError("write failed", status_code=500, body="down")

        client = PreviewOkThenWriteFails()

        with self.assertLogs(runner.logger, level="ERROR") as captured:
            with self.assertRaises(RestHTTPError):
                runner.commit(
                    PLAN_BODY,
                    3,
                    confirm=True,
                    allowed_child_ids={CHILD_ID},
                    client=client,
                )

        # Only the failing (write) call logged -- the preview step succeeded
        # and must not itself produce a failure log line.
        self.assertEqual(len(captured.output), 1)
        log_line = captured.output[0]
        self.assertIn("500", log_line)
        self.assertIn("down", log_line)
        self.assertIn(CHILD_ID, log_line)

    def test_a_failed_commit_preview_step_logs_and_the_write_never_happens(self):
        client = FailingRest(status_code=422, body="rejected before any write")

        with self.assertLogs(runner.logger, level="ERROR") as captured:
            with self.assertRaises(RestHTTPError):
                runner.commit(
                    PLAN_BODY,
                    3,
                    confirm=True,
                    allowed_child_ids={CHILD_ID},
                    client=client,
                )

        self.assertEqual(len(captured.output), 1)
        self.assertIn("rejected before any write", captured.output[0])
        # commit()'s own preview call is the only one that ran.
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]["preview"], "true")


if __name__ == "__main__":
    unittest.main()
