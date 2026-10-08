"""Tests for the child's name in the Slack preview and the adjusted-plan message.

Run from the project root:

    python -m unittest discover -s tests -v

NOTHING HERE TOUCHES FAMLY OR SLACK. The lookup talks to a fake client (or a
faked `requests.post`), and every Slack call is stubbed.

The rules under test, in order of how much they matter:

  1. A name can NEVER block, break or noticeably delay a preview: any failure
     or timeout shows the bare id exactly as before.
  2. The name is NEVER stored -- not in the preview store, not in any file, not
     in the response, not in any log record.
  3. One child per call, name only, from the public API with a short timeout.
  4. It reads "Alex Smith (`<id>`)": name first, then the id in code formatting,
     in both the preview and the adjusted-plan message.
"""

import contextlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from actions.plan_write import approval, hubspot_intake, preview_store, web
from actions.read_child_plans.runner import parse_plan
from core.client import GraphQLError, GraphQLHTTPError
from core.config import ConfigError
from integrations import child_lookup, slack

logging.disable(logging.CRITICAL)

CHILD = "724d7d15-0000-4000-8000-000000000001"
OTHER_CHILD = "99999999-0000-4000-8000-000000000009"
NAME = "Alex Smith"
# Distinctive on purpose: searched for byte-for-byte in files, logs and responses.
SECRET_NAME = "Zzyzx Quuxley-Wibblethorpe"


def response_for(*rows):
    """A public-API response body carrying `rows` of (id, fullName)."""
    return {
        "data": {
            "children": {
                "listByChildIds": {
                    "result": [{"id": i, "name": {"fullName": n}} for i, n in rows]
                }
            }
        }
    }


class FakeClient:
    """Stands in for GraphQLClient: one canned outcome, calls recorded."""

    def __init__(self, outcome=None, raises=None):
        self.outcome = outcome
        self.raises = raises
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append(
            {"query_path": query_path, "variables": variables, "operation": operation_name}
        )
        if self.raises is not None:
            raise self.raises
        return self.outcome


@contextlib.contextmanager
def captured_logs():
    """Every log record emitted inside the block, rendered as a log line would be.

    Other test modules switch logging off globally, so it is switched back on
    for the duration and restored after.
    """
    lines = []
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")

    class Handler(logging.Handler):
        def emit(self, record):
            lines.append(formatter.format(record))  # includes any traceback text

    handler = Handler(level=logging.DEBUG)
    root = logging.getLogger()
    prior_level, prior_disable = root.level, logging.root.manager.disable
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        yield lines
    finally:
        logging.disable(prior_disable)
        root.setLevel(prior_level)
        root.removeHandler(handler)


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #
class LookupSuccessTests(unittest.TestCase):
    def test_a_matching_child_returns_the_full_name(self):
        client = FakeClient(response_for((CHILD, NAME)))

        self.assertEqual(child_lookup.lookup_child_name(CHILD, client=client), NAME)

    def test_the_name_is_trimmed(self):
        client = FakeClient(response_for((CHILD, "  Alex Smith \n")))

        self.assertEqual(child_lookup.lookup_child_name(CHILD, client=client), "Alex Smith")

    def test_the_id_match_ignores_case_and_padding(self):
        client = FakeClient(response_for((CHILD.upper(), NAME)))

        self.assertEqual(child_lookup.lookup_child_name(f"  {CHILD}  ", client=client), NAME)

    def test_exactly_one_child_is_asked_for_per_call(self):
        client = FakeClient(response_for((CHILD, NAME)))

        child_lookup.lookup_child_name(CHILD, client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["variables"], {"childIds": [CHILD]})
        self.assertEqual(client.calls[0]["operation"], "ChildName")

    def test_the_query_asks_for_the_name_and_nothing_else_about_the_child(self):
        text = child_lookup.QUERY_PATH.read_text(encoding="utf-8")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))

        self.assertIn("listByChildIds", body)
        self.assertIn("fullName", body)
        # Nothing else the Child type offers.
        for field in (
            "birthday", "gender", "contacts", "externalId", "currentGroup",
            "diagnosedConditions", "records", "sensitiveRecords", "profileImage",
            "firstName", "lastName",
        ):
            self.assertNotIn(field, body, field)

    def test_nothing_is_cached_between_calls(self):
        client = FakeClient(response_for((CHILD, NAME)))

        child_lookup.lookup_child_name(CHILD, client=client)
        child_lookup.lookup_child_name(CHILD, client=client)

        self.assertEqual(len(client.calls), 2)  # asked again, not remembered


class LookupFallbackTests(unittest.TestCase):
    """Every way a lookup can go wrong yields None, which callers show as the id."""

    def lookup(self, **client_kwargs):
        return child_lookup.lookup_child_name(CHILD, client=FakeClient(**client_kwargs))

    def test_a_timeout_falls_back(self):
        for exc in (
            requests.exceptions.Timeout("slow"),
            requests.exceptions.ReadTimeout("slow read"),
            requests.exceptions.ConnectTimeout("slow connect"),
        ):
            self.assertIsNone(self.lookup(raises=exc), type(exc).__name__)

    def test_a_network_error_falls_back(self):
        self.assertIsNone(self.lookup(raises=requests.exceptions.ConnectionError("down")))

    def test_an_http_error_response_falls_back(self):
        # What the live API really does: 500 for an unknown id, 400 for a bad one.
        for status in (500, 400, 401, 502):
            error = GraphQLHTTPError(f"HTTP {status}", status_code=status, body="{}")
            self.assertIsNone(self.lookup(raises=error), status)

    def test_a_graphql_errors_response_falls_back(self):
        error = GraphQLError("boom", errors=[{"message": "boom"}])

        self.assertIsNone(self.lookup(raises=error))

    def test_a_missing_token_falls_back(self):
        with mock.patch.object(child_lookup, "_public_client", side_effect=ConfigError("no token")):
            self.assertIsNone(child_lookup.lookup_child_name(CHILD))

    def test_nothing_raises_whatever_goes_wrong(self):
        for exc in (KeyError("k"), ValueError("v"), RuntimeError("r"), OSError("o"), Exception("e")):
            self.assertIsNone(self.lookup(raises=exc), type(exc).__name__)

    def test_an_empty_result_is_no_match(self):
        self.assertIsNone(self.lookup(outcome=response_for()))

    def test_a_row_for_a_different_child_is_never_used(self):
        # A name beside the wrong id would be worse than no name.
        self.assertIsNone(self.lookup(outcome=response_for((OTHER_CHILD, "Someone Else"))))

    def test_the_right_row_is_found_among_others(self):
        outcome = response_for((OTHER_CHILD, "Someone Else"), (CHILD, NAME))

        self.assertEqual(self.lookup(outcome=outcome), NAME)

    def test_a_row_with_no_usable_name_is_no_match(self):
        for name_node in (None, {}, {"fullName": None}, {"fullName": ""}, {"fullName": "   "}, {"fullName": 7}, "Alex"):
            body = {
                "data": {
                    "children": {"listByChildIds": {"result": [{"id": CHILD, "name": name_node}]}}
                }
            }
            self.assertIsNone(self.lookup(outcome=body), repr(name_node))

    def test_unexpected_response_shapes_are_no_match(self):
        for body in (
            None, [], "text", 7, {}, {"data": None}, {"data": {}},
            {"data": {"children": None}}, {"data": {"children": {}}},
            {"data": {"children": {"listByChildIds": None}}},
            {"data": {"children": {"listByChildIds": {"result": None}}}},
            {"data": {"children": {"listByChildIds": {"result": "x"}}}},
            {"data": {"children": {"listByChildIds": {"result": [None, 3, "x"]}}}},
        ):
            self.assertIsNone(self.lookup(outcome=body), repr(body))

    def test_a_blank_or_non_string_id_never_calls_famly(self):
        for child_id in (None, "", "   ", 7, [CHILD]):
            client = FakeClient(response_for((CHILD, NAME)))

            self.assertIsNone(child_lookup.lookup_child_name(child_id, client=client))
            self.assertEqual(client.calls, [], repr(child_id))


class LookupWireTests(unittest.TestCase):
    """The REAL client path, with only `requests.post` faked."""

    def setUp(self):
        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_ACCESS_TOKEN": "test-token",
                "FAMLY_GRAPHQL_URL": "https://internal.example/graphql",
                "FAMLY_PUBLIC_GRAPHQL_URL": "https://public.example/v1/graphql",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def fake_post(self, body=None, status=200, raises=None):
        response = SimpleNamespace(
            status_code=status, text=json.dumps(body or {}), json=lambda: body
        )
        patcher = mock.patch(
            "core.client.requests.post",
            side_effect=raises if raises else None,
            return_value=response,
        )
        post = patcher.start()
        self.addCleanup(patcher.stop)
        return post

    def test_it_posts_one_id_to_the_public_api_with_a_short_timeout(self):
        post = self.fake_post(response_for((CHILD, NAME)))

        name = child_lookup.lookup_child_name(CHILD)

        self.assertEqual(name, NAME)
        (url,), kwargs = post.call_args
        self.assertEqual(url, "https://public.example/v1/graphql")  # NOT the internal API
        self.assertEqual(kwargs["timeout"], child_lookup.LOOKUP_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["headers"]["x-famly-accesstoken"], "test-token")
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["variables"], {"childIds": [CHILD]})
        self.assertEqual(payload["operationName"], "ChildName")

    def test_the_timeout_is_short(self):
        self.assertLessEqual(child_lookup.LOOKUP_TIMEOUT_SECONDS, 5)

    def test_a_real_timeout_from_requests_falls_back(self):
        self.fake_post(raises=requests.exceptions.ReadTimeout("slow"))

        self.assertIsNone(child_lookup.lookup_child_name(CHILD))

    def test_an_http_500_from_requests_falls_back(self):
        self.fake_post({"data": None, "errors": [{"message": "Something went wrong"}]}, status=500)

        self.assertIsNone(child_lookup.lookup_child_name(CHILD))

    def test_the_internal_apis_url_is_left_alone(self):
        # The public client must not disturb FAMLY_GRAPHQL_URL.
        child_lookup._public_client()

        import os

        self.assertEqual(os.environ["FAMLY_GRAPHQL_URL"], "https://internal.example/graphql")


class NothingIsLoggedTests(unittest.TestCase):
    """The name must not reach a log -- on success or failure."""

    def test_a_successful_lookup_logs_no_name(self):
        client = FakeClient(response_for((CHILD, SECRET_NAME)))

        with captured_logs() as lines:
            self.assertEqual(child_lookup.lookup_child_name(CHILD, client=client), SECRET_NAME)

        self.assertNotIn(SECRET_NAME, "\n".join(lines))

    def test_a_failure_logs_the_exception_type_only(self):
        # An HTTP error's text can echo the whole response body -- which may
        # carry the very name asked for. Only the TYPE is ever logged.
        error = GraphQLHTTPError(
            f"HTTP 500: partial body containing {SECRET_NAME}", status_code=500, body=SECRET_NAME
        )

        with captured_logs() as lines:
            self.assertIsNone(
                child_lookup.lookup_child_name(CHILD, client=FakeClient(raises=error))
            )

        text = "\n".join(lines)
        self.assertNotIn(SECRET_NAME, text)
        self.assertIn("GraphQLHTTPError", text)
        self.assertNotIn("Traceback", text)

    def test_no_match_logs_no_name(self):
        outcome = response_for((OTHER_CHILD, SECRET_NAME))  # a DIFFERENT child's name

        with captured_logs() as lines:
            self.assertIsNone(child_lookup.lookup_child_name(CHILD, client=FakeClient(outcome=outcome)))

        self.assertNotIn(SECRET_NAME, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
class ChildLineTests(unittest.TestCase):
    def test_a_name_comes_first_then_the_id_in_code_formatting(self):
        self.assertEqual(slack.child_line(CHILD, NAME), f"• Child: {NAME} (`{CHILD}`)")

    def test_no_name_is_exactly_the_old_bare_id_line(self):
        self.assertEqual(slack.child_line(CHILD, None), f"• Child: `{CHILD}`")
        self.assertEqual(slack.child_line(CHILD), f"• Child: `{CHILD}`")

    def test_a_blank_name_is_no_name(self):
        for name in ("", "   ", "\n\t"):
            self.assertEqual(slack.child_line(CHILD, name), f"• Child: `{CHILD}`", repr(name))

    def test_an_unknown_child_id_still_renders(self):
        self.assertEqual(slack.child_line(None, NAME), f"• Child: {NAME} (`unknown`)")
        self.assertEqual(slack.child_line(None), "• Child: `unknown`")

    def test_slack_control_characters_in_a_name_are_escaped(self):
        # `<!channel>` / `<@U123>` in free text would otherwise ping people.
        line = slack.child_line(CHILD, "<!channel> Tom & <@U123>")

        self.assertIn("&lt;!channel&gt; Tom &amp; &lt;@U123&gt;", line)
        self.assertNotIn("<", line)
        self.assertNotIn(">", line.replace("&gt;", ""))

    def test_a_name_is_kept_to_one_line(self):
        line = slack.child_line(CHILD, "Alex\nSmith\r\n  Jr")

        self.assertEqual(line, f"• Child: Alex Smith Jr (`{CHILD}`)")
        self.assertEqual(len(line.splitlines()), 1)

    def test_an_absurdly_long_name_is_capped(self):
        line = slack.child_line(CHILD, "A" * 5000)

        self.assertLess(len(line), 200)

    def test_the_summary_uses_it_in_the_same_place_as_before(self):
        plan = parse_plan({"childId": CHILD, "from": "2026-09-01"})

        with_name = slack.build_summary(plan, [], child_name=NAME).splitlines()
        without = slack.build_summary(plan, []).splitlines()

        self.assertEqual(with_name[2], f"• Child: {NAME} (`{CHILD}`)")
        self.assertEqual(without[2], f"• Child: `{CHILD}`")
        # Only that one line differs.
        self.assertEqual(with_name[:2] + with_name[3:], without[:2] + without[3:])


# --------------------------------------------------------------------------- #
# Both Slack messages, end to end
# --------------------------------------------------------------------------- #
PLAN_BODY = {
    "plan": {
        "id": "",
        "childId": CHILD,
        "from": "2026-09-01",
        "planParts": [{"planPartId": "pp-1", "sessionBookings": []}],
    }
}


def make_plan():
    return parse_plan(
        {
            "id": "plan-1",
            "childId": CHILD,
            "from": "2026-09-01",
            "monthlyEstimate": 800.0,
            "pricingGroupId": "pg-1",
            "planParts": [{"planPartId": "pp-1"}],
        }
    )


class MessageFlowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        env = mock.patch.dict(
            "os.environ",
            {
                "PREVIEW_STORE_PATH": str(self.tmp / "store.sqlite3"),
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

        self.plan = make_plan()
        self.client = FakeClient(response_for((CHILD, NAME)))
        self.preview_posts = []
        self.adjusted_posts = []
        self.adjusted_ids = []  # the NEW preview id each adjusted message was posted with

        def fake_preview(body, version, client=None, old_plan_id=None, replace_old_plan=False):
            return SimpleNamespace(plan=self.plan, warnings=[], raw={})

        for target, name, replacement in (
            (approval.runner, "preview", fake_preview),
            (slack, "post_preview", lambda text, pid: self.preview_posts.append(text) or True),
            (
                slack,
                "post_adjusted",
                lambda text, pid: self.adjusted_posts.append(text)
                or self.adjusted_ids.append(pid)
                or True,
            ),
            (slack, "post_notice", lambda text: True),
            (slack, "update_message", lambda url, text: True),
            # The REAL lookup runs; only the client it uses is faked.
            (child_lookup, "_public_client", lambda *a, **k: self.client),
        ):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_preview(self):
        intake = hubspot_intake.IntakeResult(
            ok=True,
            plan_body=PLAN_BODY,
            result=SimpleNamespace(plan=self.plan, warnings=[], raw={}),
        )
        with mock.patch.object(web.hubspot_intake, "handle_intake", return_value=intake):
            data, status = web.run_preview_and_post({"childId": CHILD})
        return data, status

    def adjust(self, preview_id, value="-0.50"):
        return approval.handle_adjust_submission(
            preview_id, value, "drew", spawn=lambda work: work()
        )

    def child_line_of(self, text):
        return next(l for l in text.splitlines() if l.startswith("• Child:"))


class PreviewMessageTests(MessageFlowTestCase):
    def test_success_shows_the_name_then_the_id(self):
        data, status = self.run_preview()

        self.assertEqual(status, 200)
        self.assertEqual(self.child_line_of(self.preview_posts[-1]), f"• Child: {NAME} (`{CHILD}`)")

    def test_a_timeout_shows_the_bare_id_and_the_preview_still_succeeds(self):
        self.client.raises = requests.exceptions.ReadTimeout("slow")

        data, status = self.run_preview()

        self.assertEqual(status, 200)
        self.assertTrue(data["slack_posted"])
        self.assertTrue(data["preview_id"])
        self.assertEqual(self.child_line_of(self.preview_posts[-1]), f"• Child: `{CHILD}`")

    def test_an_error_response_shows_the_bare_id_and_the_preview_still_succeeds(self):
        self.client.raises = GraphQLHTTPError("HTTP 500", status_code=500, body="{}")

        data, status = self.run_preview()

        self.assertEqual(status, 200)
        self.assertTrue(data["slack_posted"])
        self.assertEqual(self.child_line_of(self.preview_posts[-1]), f"• Child: `{CHILD}`")

    def test_no_match_shows_the_bare_id(self):
        self.client.outcome = response_for()

        self.run_preview()

        self.assertEqual(self.child_line_of(self.preview_posts[-1]), f"• Child: `{CHILD}`")

    def test_a_missing_token_shows_the_bare_id(self):
        with mock.patch.object(child_lookup, "_public_client", side_effect=ConfigError("no token")):
            data, status = self.run_preview()

        self.assertEqual(status, 200)
        self.assertEqual(self.child_line_of(self.preview_posts[-1]), f"• Child: `{CHILD}`")

    def test_one_child_is_looked_up_and_only_once(self):
        self.run_preview()

        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0]["variables"], {"childIds": [CHILD]})

    def test_a_failed_lookup_does_not_stop_the_preview_being_stored(self):
        self.client.raises = requests.exceptions.ConnectionError("down")

        data, _ = self.run_preview()

        self.assertIsNotNone(preview_store.get(data["preview_id"]))


class AdjustedMessageTests(MessageFlowTestCase):
    def preview_and_adjust(self):
        data, _ = self.run_preview()
        self.client.calls.clear()
        self.assertIsNone(self.adjust(data["preview_id"]))
        return self.adjusted_posts[-1]

    def test_success_shows_the_same_name_line_as_the_preview(self):
        text = self.preview_and_adjust()

        self.assertEqual(self.child_line_of(text), f"• Child: {NAME} (`{CHILD}`)")
        self.assertEqual(self.child_line_of(text), self.child_line_of(self.preview_posts[-1]))

    def test_the_child_line_sits_right_under_the_heading(self):
        text = self.preview_and_adjust()

        lines = text.splitlines()
        self.assertEqual(lines[0], "*Adjusted plan — awaiting final confirmation*")
        self.assertTrue(lines[1].startswith("• Child:"))

    def test_a_timeout_shows_the_bare_id_and_the_message_is_still_posted(self):
        data, _ = self.run_preview()
        self.client.raises = requests.exceptions.ReadTimeout("slow")

        self.assertIsNone(self.adjust(data["preview_id"]))

        text = self.adjusted_posts[-1]
        self.assertEqual(self.child_line_of(text), f"• Child: `{CHILD}`")
        self.assertIn("Total to be billed", text)  # the money lines are unaffected

    def test_an_error_response_shows_the_bare_id(self):
        data, _ = self.run_preview()
        self.client.raises = GraphQLError("boom", errors=[{"message": "boom"}])

        self.adjust(data["preview_id"])

        self.assertEqual(self.child_line_of(self.adjusted_posts[-1]), f"• Child: `{CHILD}`")

    def test_no_match_shows_the_bare_id(self):
        data, _ = self.run_preview()
        self.client.outcome = response_for((OTHER_CHILD, "Someone Else"))

        self.adjust(data["preview_id"])

        self.assertEqual(self.child_line_of(self.adjusted_posts[-1]), f"• Child: `{CHILD}`")
        self.assertNotIn("Someone Else", self.adjusted_posts[-1])

    def test_the_adjustment_figures_are_unchanged_by_the_name(self):
        data, _ = self.run_preview()
        self.adjust(data["preview_id"])
        with_name = self.adjusted_posts[-1]

        self.client.raises = requests.exceptions.ReadTimeout("slow")
        data, _ = self.run_preview()
        self.adjust(data["preview_id"])
        without = self.adjusted_posts[-1]

        def money_lines(text):
            return [l for l in text.splitlines() if not l.startswith("• Child:")]

        self.assertEqual(money_lines(with_name), money_lines(without))

    def test_a_preview_with_no_child_id_has_no_child_line(self):
        text = approval._adjusted_summary(-0.5, 800.0, 800.0, SimpleNamespace(warnings=[]), "pg-1")

        self.assertNotIn("Child", text)


class NothingIsPersistedTests(MessageFlowTestCase):
    """The name is rendered into one Slack message and dropped."""

    def setUp(self):
        super().setUp()
        self.client.outcome = response_for((CHILD, SECRET_NAME))

    def everything_on_disk(self):
        """All bytes of every file the flow wrote -- including the SQLite WAL."""
        return b"".join(p.read_bytes() for p in sorted(self.tmp.rglob("*")) if p.is_file())

    def test_the_name_does_reach_slack_as_the_control(self):
        # Without this, the checks below could pass because the lookup never ran.
        self.run_preview()

        self.assertIn(SECRET_NAME, self.preview_posts[-1])

    def test_the_name_is_not_in_the_preview_store(self):
        data, _ = self.run_preview()

        stored = preview_store.get(data["preview_id"])

        self.assertNotIn(SECRET_NAME, json.dumps(stored.__dict__, default=str))
        self.assertNotIn(SECRET_NAME.encode(), self.everything_on_disk())

    def test_the_name_is_not_in_the_preview_response(self):
        data, _ = self.run_preview()

        self.assertNotIn(SECRET_NAME, json.dumps(data, default=str))

    def test_the_name_is_not_on_disk_after_an_adjustment_either(self):
        data, _ = self.run_preview()
        self.adjust(data["preview_id"])

        self.assertIn(SECRET_NAME, self.adjusted_posts[-1])  # control
        self.assertNotIn(SECRET_NAME.encode(), self.everything_on_disk())
        # The NEW (adjusted) stored preview, not just the original.
        adjusted = preview_store.get(self.adjusted_ids[-1])
        self.assertIsNotNone(adjusted)
        self.assertNotIn(SECRET_NAME, json.dumps(adjusted.__dict__, default=str))

    def test_the_adjusted_plans_note_and_body_do_not_carry_the_name(self):
        data, _ = self.run_preview()
        self.adjust(data["preview_id"])

        # The note written into the plan body is the audit line -- no name.
        flat = self.everything_on_disk().decode("utf-8", errors="replace")
        self.assertIn("Adjustment:", flat)  # the note really was stored
        self.assertNotIn(SECRET_NAME, flat)

    def test_the_name_is_in_no_log_record_through_a_whole_preview_and_adjustment(self):
        with captured_logs() as lines:
            data, _ = self.run_preview()
            self.adjust(data["preview_id"])

        self.assertTrue(lines)  # logging really was on
        self.assertNotIn(SECRET_NAME, "\n".join(lines))

    def test_the_name_is_in_no_log_record_when_the_lookup_fails(self):
        self.client.raises = GraphQLHTTPError(
            f"HTTP 500 {SECRET_NAME}", status_code=500, body=SECRET_NAME
        )

        with captured_logs() as lines:
            data, _ = self.run_preview()
            self.adjust(data["preview_id"])

        self.assertNotIn(SECRET_NAME, "\n".join(lines))

    def test_the_module_keeps_no_cache(self):
        self.run_preview()

        public = {
            name: value
            for name, value in vars(child_lookup).items()
            if not name.startswith("__")
        }
        # No container-valued module state a name could be remembered in.
        self.assertFalse(
            [n for n, v in public.items() if isinstance(v, (dict, list, set)) and n != "__all__"]
        )


if __name__ == "__main__":
    unittest.main()
