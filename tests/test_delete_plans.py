"""Tests for the TEMPORARY delete_plans action.

Run from the project root:

    python -m unittest tests.test_delete_plans -v

NOTHING here touches Famly or Slack. Famly is a small fake holding one child's
plans; `_delete_plan` -- which is not written yet -- is replaced by a fake that
records each call and removes the plan, so every guard can be exercised without a
real delete existing. The fake refuses any POST, so the commit path is never
touched.
"""

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from actions.delete_plans import runner
from actions.delete_plans import web as delete_web
from actions.plan_write import preview_store
from actions.read_child_plans import runner as read_child_plans
from core.rest_client import RestHTTPError
from integrations import child_lookup, slack
from tests import fast_sqlite  # noqa: F401  (SQLite without fsync: see the module)

logging.disable(logging.CRITICAL)

REAL_DELETE_PLAN = runner._delete_plan  # captured before any test patches it

CHILD = "00000000-0000-0000-0000-0000000000a1"
OTHER = "00000000-0000-0000-0000-0000000000a2"
# On the allowlist but never sent a request in most tests: the digest only posts
# on its own once EVERY allowlist child has reported, so this keeps it quiet.
NEVER_SENT = "00000000-0000-0000-0000-0000000000a9"


def plan_node(plan_id, start, end=None, child=CHILD, deletable=True, estimate=500.0):
    return {
        "id": plan_id,
        "childId": child,
        "from": start,
        "to": end,
        "monthlyEstimate": estimate,
        "behaviors": [{"id": "CanDeletePlan", "payload": {}}] if deletable else [],
        "planStates": [
            {"from": start, "to": end, "monthlyEstimate": estimate, "pricingGroupId": "pg-1"}
        ],
    }


class FakeFamly:
    """The child's plans, readable. Any write is a test failure."""

    def __init__(self):
        self.plans: list[dict] = []
        self.gets = 0
        self.get_failures: list[Exception] = []
        self.requests: list[tuple] = []  # (method, path, params, json_body) of every request()
        self.delete_mode = "normal"  # normal | noop | two | http500 | reopen_previous | dict_body

    def get(self, path, params=None):
        self.gets += 1
        if self.get_failures:
            raise self.get_failures.pop(0)
        wanted = (params or {}).get("childId")
        return {"plans": [dict(p) for p in self.plans if not wanted or p["childId"] == wanted]}

    def post(self, *args, **kwargs):
        raise AssertionError("delete_plans must never POST (the commit path)")

    # -- what the real client's `request` does, for the real `_delete_plan` ---- #
    def request(self, method, path, params=None, json_body=None):
        self.requests.append((method, path, params, json_body))
        plan_id = path.rsplit("/", 1)[-1]
        mode = self.delete_mode
        ordered = sorted(self.plans, key=lambda p: p["from"])
        index = next((i for i, p in enumerate(ordered) if p["id"] == plan_id), None)

        if mode == "http500":
            raise RestHTTPError(f"{method} {path} failed with HTTP 500: boom", 500, "boom")
        if mode == "noop" or index is None:
            return []
        gone = {plan_id}
        if mode == "wrong_plan" and index > 0:
            gone = {ordered[index - 1]["id"]}  # the count drops by one, but the WRONG plan went
        if mode == "two" and index > 0:
            gone.add(ordered[index - 1]["id"])  # Famly removed the one before it too
        if mode == "reopen_previous" and index > 0:
            ordered[index - 1]["to"] = None  # the plan before is left open-ended
        self.plans = [p for p in self.plans if p["id"] not in gone]
        return {"unexpected": "body"} if mode == "dict_body" else []


class DeleteTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        self.allowlist_file = root / "allowlist.txt"
        self.allowlist_file.write_text(f"{CHILD}\n{NEVER_SENT}\n", encoding="utf-8")
        self.backups = root / "backups"

        env = mock.patch.dict(
            "os.environ",
            {
                "PREVIEW_STORE_PATH": str(root / "store.sqlite3"),
                "FAMLY_ACCESS_TOKEN": "test-token",
                "DELETE_PLANS_MODE": "live",
                "DELETE_PLANS_ALLOWLIST_FILE": str(self.allowlist_file),
                "DELETE_PLANS_BACKUP_DIR": str(self.backups),
                "DELETE_PLANS_MAX_PER_DAY": "40",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        self.famly = FakeFamly()
        patcher = mock.patch.object(read_child_plans, "RestClient", lambda *a, **k: self.famly)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.notices: list[str] = []
        patcher = mock.patch.object(
            slack, "post_notice", lambda text: self.notices.append(text) or True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # A name is NEVER fetched during a webhook request: a lookup there is a
        # failure. Only a digest posted from the CLI may look names up.
        self.names_allowed = False
        self.names = {}
        self.name_lookups: list[str] = []

        def no_names(child_id, *args, **kwargs):
            if not self.names_allowed:
                raise AssertionError("delete_plans must not look up a name during a webhook request")
            self.name_lookups.append(child_id)
            return self.names.get(child_id)

        patcher = mock.patch.object(child_lookup, "lookup_child_name", no_names)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.clock = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
        patcher = mock.patch.object(runner, "_now", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The delete that does not exist yet: records each call, removes the plan.
        self.delete_calls: list[str] = []
        self.backup_present_at_delete: list[bool] = []
        self.delete_effect = self.remove_plan
        self.delete_hook = None
        patcher = mock.patch.object(runner, "_delete_plan", self.fake_delete)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- the fake delete --------------------------------------------------- #
    def fake_delete(self, plan_id):
        plan = SimpleNamespaceId(plan_id)
        self.delete_calls.append(plan.id)
        self.backup_present_at_delete.append(bool(list(self.backups.glob(f"*/{plan.id}.json"))))
        if self.delete_hook:
            self.delete_hook(plan)
        self.delete_effect(plan)

    def remove_plan(self, plan):
        self.famly.plans = [p for p in self.famly.plans if p["id"] != plan.id]

    def leave_plan_in_place(self, plan):
        pass

    # -- requests ---------------------------------------------------------- #
    def request(self, child=CHILD, confirm="DELETE", **extra):
        body = {"action": "delete_plans", "childId": child, "confirm": confirm, **extra}
        return delete_web.handle(body)

    def three_plans(self):
        self.famly.plans = [
            plan_node("plan-a", "2026-01-01", "2026-04-01"),
            plan_node("plan-b", "2026-04-01", "2026-09-01"),
            plan_node("plan-c", "2026-09-01", None),
        ]

    def log(self, child=None):
        return runner.log_rows(child)

    def outcomes(self):
        return [r["outcome"] for r in self.log()]

    def assert_nothing_happened(self):
        self.assertEqual(self.famly.gets, 0)
        self.assertEqual(self.delete_calls, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.log(), [])
        self.assertFalse(self.backups.exists())


# --------------------------------------------------------------------------- #
# The settings
# --------------------------------------------------------------------------- #
class ModeTests(DeleteTestCase):
    def test_off_does_nothing(self):
        self.three_plans()
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "off"}):
            data, status = self.request()

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["outcome"], "off")
        self.assert_nothing_happened()

    def test_unset_is_off(self):
        self.three_plans()
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": ""}):
            data, status = self.request()

        self.assertEqual((status, data["outcome"]), (200, "off"))
        self.assert_nothing_happened()

    def test_an_unknown_mode_counts_as_off(self):
        self.three_plans()
        for value in ("yes", "LIVE!", "true", "dryrun", "delete"):
            with self.subTest(value=value), mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": value}):
                self.assertEqual(runner.mode(), runner.MODE_OFF)
                data, status = self.request()
                self.assertEqual((status, data["outcome"]), (200, "off"))
        self.assert_nothing_happened()

    def test_the_mode_is_case_insensitive_and_read_each_time(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": " Dry_Run "}):
            self.assertEqual(runner.mode(), runner.MODE_DRY_RUN)
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "LIVE"}):
            self.assertEqual(runner.mode(), runner.MODE_LIVE)

    def test_the_action_is_registered_and_named(self):
        import web_registry

        self.assertEqual(delete_web.ACTION_NAME, "delete_plans")
        self.assertIs(web_registry.get_handler("delete_plans"), delete_web.handle)


class MalformedRequestTests(DeleteTestCase):
    def test_confirm_is_required_and_exact(self):
        self.three_plans()
        for confirm in (None, "", "delete", "Delete", "DELETE ", " DELETE", "yes", True, "DELETEDELETE"):
            with self.subTest(confirm=confirm):
                data, status = self.request(confirm=confirm)
                self.assertEqual(status, 422)
                self.assertFalse(data["ok"])
                self.assertTrue(any("confirm" in e for e in data["errors"]))
        self.assert_nothing_happened()

    def test_a_missing_confirm_key(self):
        data, status = delete_web.handle({"action": "delete_plans", "childId": CHILD})

        self.assertEqual(status, 422)
        self.assert_nothing_happened()

    def test_the_child_id_must_be_a_uuid(self):
        for child in (None, "", "not-a-uuid", 123, "00000000-0000-0000-0000", ["x"]):
            with self.subTest(child=child):
                data, status = delete_web.handle({"childId": child, "confirm": "DELETE"})
                self.assertEqual(status, 422)
                self.assertTrue(any("childId" in e for e in data["errors"]))
        self.assert_nothing_happened()

    def test_both_problems_are_reported_together(self):
        data, status = delete_web.handle({"childId": "x", "confirm": "no"})

        self.assertEqual(status, 422)
        self.assertEqual(len(data["errors"]), 2)

    def test_a_body_that_is_not_an_object_is_malformed(self):
        data, status = delete_web.handle("nope")

        self.assertEqual(status, 422)

    def test_a_malformed_request_is_422_even_when_the_mode_is_off(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "off"}):
            _, status = self.request(confirm="no")

        self.assertEqual(status, 422)


class AllowlistTests(DeleteTestCase):
    def test_a_child_off_the_allowlist_is_refused_and_logged_and_nothing_else(self):
        self.famly.plans = [plan_node("x", "2026-01-01", child=OTHER)]

        data, status = self.request(child=OTHER)

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["outcome"], "not_allowlisted")
        self.assertEqual(self.famly.gets, 0)  # no further action: the plans were never read
        self.assertEqual(self.delete_calls, [])
        self.assertEqual(self.notices, [])  # refused: reported in the digest, not at once
        self.assertEqual(self.outcomes(), ["not_allowlisted"])
        self.assertEqual(self.log(OTHER)[0]["child_id"], OTHER)
        self.assertFalse(self.backups.exists())

    def test_a_missing_file_allows_nothing(self):
        self.allowlist_file.unlink()
        self.three_plans()

        data, status = self.request()

        self.assertEqual((status, data["outcome"]), (200, "not_allowlisted"))
        self.assertEqual(self.delete_calls, [])

    def test_an_empty_file_allows_nothing(self):
        for content in ("", "\n\n", "   \n", "# only a comment\n"):
            with self.subTest(content=content):
                self.allowlist_file.write_text(content, encoding="utf-8")
                data, _ = self.request()
                self.assertEqual(data["outcome"], "not_allowlisted")
        self.assertEqual(self.delete_calls, [])

    def test_the_file_is_re_read_on_every_request(self):
        self.three_plans()
        self.allowlist_file.write_text("", encoding="utf-8")
        refused, _ = self.request()
        self.assertEqual(refused["outcome"], "not_allowlisted")

        self.allowlist_file.write_text(f"{CHILD}\n", encoding="utf-8")  # an edit, no restart
        allowed, _ = self.request()
        self.assertEqual(allowed["outcome"], "deleted")

        self.allowlist_file.write_text("", encoding="utf-8")  # and removed again
        self.famly.plans = [plan_node("late", "2027-01-01")]
        again, _ = self.request()
        self.assertEqual(again["outcome"], "not_allowlisted")

    def test_matching_is_case_insensitive_and_trimmed_and_comments_are_ignored(self):
        self.allowlist_file.write_text(f"# the clean-up list\n\n  {CHILD.upper()}  \n", encoding="utf-8")
        self.three_plans()

        data, _ = self.request()

        self.assertEqual(data["outcome"], "deleted")

    def test_one_child_per_line(self):
        self.allowlist_file.write_text(f"{OTHER}\n{CHILD}\n", encoding="utf-8")
        self.three_plans()

        self.assertEqual(self.request()[0]["outcome"], "deleted")

    def test_the_allowlist_decides_before_the_mode_reads_anything(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            data, _ = self.request(child=OTHER)

        self.assertEqual(data["outcome"], "not_allowlisted")
        self.assertEqual(self.famly.gets, 0)

    def test_the_default_paths_are_under_var_and_gitignored(self):
        self.assertEqual(runner.DEFAULT_ALLOWLIST_FILE.name, "delete_plans_allowlist.txt")
        self.assertEqual(runner.DEFAULT_ALLOWLIST_FILE.parent.name, "var")
        self.assertEqual(runner.DEFAULT_BACKUP_DIR, runner.PROJECT_ROOT / "var" / "plan-backups")
        ignored = (runner.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("var/delete_plans_allowlist.txt", ignored)
        self.assertIn("var/plan-backups/", ignored)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
class NothingToDeleteTests(DeleteTestCase):
    def test_no_plans_is_ok_and_nothing_to_delete(self):
        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["outcome"], "nothing_to_delete")
        self.assertIn("nothing to delete", data["message"])
        self.assertEqual(self.delete_calls, [])
        self.assertEqual(self.outcomes(), ["nothing_to_delete"])

    def test_a_repeated_request_after_a_full_delete_is_a_no_op(self):
        self.three_plans()
        first, _ = self.request()
        calls = list(self.delete_calls)
        backups = sorted(p.name for p in (self.backups / CHILD).iterdir())

        second, status = self.request()
        third, _ = self.request()

        self.assertEqual(first["outcome"], "deleted")
        self.assertEqual((status, second["ok"], second["outcome"]), (200, True, "nothing_to_delete"))
        self.assertEqual(third["outcome"], "nothing_to_delete")
        self.assertEqual(self.delete_calls, calls)  # nothing more was deleted
        self.assertEqual(sorted(p.name for p in (self.backups / CHILD).iterdir()), backups)

    def test_a_repeat_makes_no_slack_noise(self):
        self.three_plans()
        self.request()
        before = len(self.notices)

        self.request()

        self.assertEqual(len(self.notices), before)

    def test_a_failed_read_is_a_200_with_the_outcome_and_a_notice(self):
        self.famly.get_failures = [RuntimeError("Famly is down")]

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["outcome"], "error")
        self.assertIn("RuntimeError", data["message"])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.delete_calls, [])


class DryRunTests(DeleteTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"})
        env.start()
        self.addCleanup(env.stop)

    def test_it_never_calls_delete(self):
        self.three_plans()
        with mock.patch.object(runner, "_delete_plan", side_effect=AssertionError("deleted!")) as delete:
            data, status = self.request()

        delete.assert_not_called()
        self.assertEqual((status, data["ok"], data["outcome"]), (200, True, "dry_run"))

    def test_it_changes_nothing_and_writes_no_backups(self):
        self.three_plans()
        before = [dict(p) for p in self.famly.plans]
        self.request()

        self.assertEqual(self.famly.plans, before)
        self.assertEqual(self.delete_calls, [])
        self.assertFalse(self.backups.exists())

    def test_a_dry_run_posts_nothing_of_its_own(self):
        self.three_plans()
        self.request()

        self.assertEqual(self.notices, [])

    def test_the_plans_that_would_be_deleted_are_in_the_log_row_latest_first(self):
        self.famly.plans = [
            plan_node("plan-a", "2026-01-01", "2026-04-01", estimate=410.5),
            plan_node("plan-b", "2026-04-01", None, deletable=False, estimate=520.0),
        ]
        self.request()

        plans = json.loads(self.log()[0]["detail"])
        self.assertEqual([p["planId"] for p in plans], ["plan-b", "plan-a"])
        self.assertEqual(plans[1], {"planId": "plan-a", "from": "2026-01-01", "to": "2026-04-01", "monthlyEstimate": 410.5, "canDelete": True})
        self.assertEqual(plans[0]["canDelete"], False)

    def test_the_response_lists_the_plans(self):
        self.three_plans()
        data, _ = self.request()

        self.assertEqual([p["planId"] for p in data["plans"]], ["plan-c", "plan-b", "plan-a"])
        self.assertTrue(all(p["canDelete"] for p in data["plans"]))

    def test_it_writes_one_log_row_and_nothing_else(self):
        self.three_plans()
        self.request()

        rows = self.log()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["mode"], rows[0]["outcome"], rows[0]["child_id"]), ("dry_run", "dry_run", CHILD))
        self.assertEqual(runner.children_today(self.clock), set())  # a dry run uses none of the cap

    def test_a_dry_run_does_not_touch_the_breaker(self):
        self.three_plans()
        self.request()

        self.assertEqual(runner.consecutive_failures(), 0)
        self.assertFalse(runner.is_paused())


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
class LiveDeleteTests(DeleteTestCase):
    def test_all_plans_are_deleted_latest_starting_first(self):
        self.three_plans()

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual((data["ok"], data["outcome"]), (True, "deleted"))
        self.assertEqual(self.delete_calls, ["plan-c", "plan-b", "plan-a"])
        self.assertEqual(self.famly.plans, [])
        self.assertIn("3 plan(s) deleted", data["message"])

    def test_the_order_does_not_depend_on_how_famly_lists_them(self):
        self.famly.plans = [
            plan_node("middle", "2026-04-01"),
            plan_node("latest", "2026-09-01"),
            plan_node("earliest", "2026-01-01"),
        ]
        self.request()

        self.assertEqual(self.delete_calls, ["latest", "middle", "earliest"])

    def test_a_backup_is_written_before_each_delete(self):
        self.three_plans()
        self.request()

        self.assertEqual(self.backup_present_at_delete, [True, True, True])
        files = sorted(p.name for p in (self.backups / CHILD).iterdir())
        self.assertEqual(files, ["plan-a.json", "plan-b.json", "plan-c.json"])

    def test_the_backup_is_the_plans_full_read_back_json(self):
        self.three_plans()
        original = {p["id"]: dict(p) for p in self.famly.plans}
        self.request()

        saved = json.loads((self.backups / CHILD / "plan-b.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, original["plan-b"])

    def test_no_partial_backup_file_is_left_behind(self):
        self.three_plans()
        self.request()

        self.assertFalse(list((self.backups / CHILD).glob("*.part")))

    def test_a_failed_backup_prevents_the_delete(self):
        self.three_plans()
        self.backups.parent.mkdir(parents=True, exist_ok=True)
        self.backups.write_text("a file where the directory should be", encoding="utf-8")

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("NOT deleted", data["message"])
        self.assertEqual(self.delete_calls, [])
        self.assertEqual(len(self.famly.plans), 3)
        self.assertIn("backup_failed", self.outcomes())
        self.assertEqual(len(self.notices), 1)

    def test_a_backup_that_fails_midway_stops_the_child_after_the_ones_that_succeeded(self):
        self.three_plans()
        real = runner.write_backup

        def flaky(child_id, plan):
            if plan.id == "plan-b":
                raise OSError("disk full")
            return real(child_id, plan)

        with mock.patch.object(runner, "write_backup", flaky):
            data, _ = self.request()

        self.assertEqual(self.delete_calls, ["plan-c"])  # plan-b was never deleted
        self.assertEqual(data["outcome"], "failed")
        self.assertEqual([p["id"] for p in self.famly.plans], ["plan-a", "plan-b"])

    def test_deleting_is_one_attempt_and_never_retried(self):
        self.three_plans()

        def boom(plan):
            raise RuntimeError("Famly said no")

        self.delete_effect = boom
        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(self.delete_calls, ["plan-c"])  # once, and then the child stops
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("RuntimeError", data["message"])
        self.assertIn("delete_failed", self.outcomes())

    def test_a_repeated_failing_request_is_a_new_single_attempt_not_a_retry_loop(self):
        self.three_plans()
        self.delete_effect = lambda plan: (_ for _ in ()).throw(RuntimeError("no"))

        self.request()
        self.request()

        self.assertEqual(self.delete_calls, ["plan-c", "plan-c"])  # one per request

    def test_the_read_back_proves_the_plan_is_gone(self):
        self.three_plans()
        before = self.famly.gets
        self.request()

        # one read at the start, one after each of the three deletes
        self.assertEqual(self.famly.gets - before, 4)

    def test_a_plan_that_is_still_there_stops_the_child(self):
        self.three_plans()
        self.delete_effect = self.leave_plan_in_place

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(self.delete_calls, ["plan-c"])  # stopped on the first
        self.assertFalse(data["ok"])
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("still there", data["message"])
        self.assertIn("unexpected", self.outcomes())
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(len(self.famly.plans), 3)

    def test_the_count_not_dropping_by_exactly_one_is_unexpected(self):
        self.three_plans()
        self.delete_effect = lambda plan: setattr(self.famly, "plans", [])  # took everything

        data, _ = self.request()

        self.assertEqual(data["outcome"], "failed")
        self.assertIn("count should drop by exactly one", data["message"])

    def test_a_failed_read_back_stops_the_child(self):
        self.three_plans()

        def delete_then_break_the_next_read(plan):
            self.remove_plan(plan)
            self.famly.get_failures = [RuntimeError("read-back failed")]

        self.delete_effect = delete_then_break_the_next_read
        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(self.delete_calls, ["plan-c"])
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("read-back failed", data["message"])
        self.assertIn("readback_failed", self.outcomes())

    def test_the_neighbours_end_dates_are_reported_in_the_log(self):
        self.three_plans()

        def delete_and_extend_the_previous_plan(plan):
            self.remove_plan(plan)
            if plan.id == "plan-c":  # Famly reopens the end date of the plan before it
                for p in self.famly.plans:
                    if p["id"] == "plan-b":
                        p["to"] = None

        self.delete_effect = delete_and_extend_the_previous_plan
        self.request()

        row = next(r for r in self.log(CHILD) if r["outcome"] == "deleted" and r["plan_id"] == "plan-c")
        self.assertIn("previous plan `plan-b`: end date 2026-09-01 -> open-ended (changed)", row["detail"])

    def test_an_unchanged_neighbour_is_reported_as_unchanged(self):
        self.three_plans()
        self.request()

        row = next(r for r in self.log(CHILD) if r["outcome"] == "deleted" and r["plan_id"] == "plan-c")
        self.assertIn("previous plan `plan-b`: end date 2026-09-01 -> 2026-09-01 (unchanged)", row["detail"])

    def test_each_live_delete_is_a_log_row(self):
        self.three_plans()
        self.request()

        rows = self.log(CHILD)
        deleted = [r for r in rows if r["outcome"] == "deleted"]
        self.assertEqual([r["plan_id"] for r in deleted], ["plan-c", "plan-b", "plan-a"])
        for row in rows:
            self.assertEqual((row["child_id"], row["mode"]), (CHILD, "live"))
            self.assertTrue(row["at"])
        self.assertEqual(rows[0]["outcome"], "started")

    def test_a_routine_live_child_posts_nothing_of_its_own(self):
        self.three_plans()
        self.request()

        self.assertEqual(self.notices, [])


class SimpleNamespaceId:
    def __init__(self, plan_id):
        self.id = plan_id


class RealDeleteTestCase(DeleteTestCase):
    """The REAL `_delete_plan`, talking to the fake Famly's `request`."""

    def setUp(self):
        super().setUp()
        for target in (
            mock.patch.object(runner, "_delete_plan", REAL_DELETE_PLAN),
            mock.patch.object(runner, "RestClient", lambda *a, **k: self.famly),
        ):
            target.start()
            self.addCleanup(target.stop)

    def delete_paths(self):
        return [path for method, path, _, _ in self.famly.requests if method == "DELETE"]


class DeletePlanRequestTests(RealDeleteTestCase):
    def test_the_request_is_a_delete_to_the_right_path_with_no_params_or_body(self):
        client = mock.Mock()
        client.request.return_value = []
        with mock.patch.object(runner, "RestClient", lambda *a, **k: client):
            runner._delete_plan("plan-x")

        client.request.assert_called_once_with("DELETE", "v2/plans/plan-x")  # nothing else passed

    def test_what_reaches_the_client_has_no_params_and_no_body(self):
        self.famly.plans = [plan_node("plan-x", "2026-01-01")]

        runner._delete_plan("plan-x")

        self.assertEqual(self.famly.requests, [("DELETE", "v2/plans/plan-x", None, None)])

    def test_an_empty_list_response_is_accepted(self):
        self.famly.plans = [plan_node("plan-x", "2026-01-01")]

        self.assertIsNone(runner._delete_plan("plan-x"))  # no exception, nothing returned

    def test_the_response_body_is_never_inspected(self):
        self.famly.plans = [plan_node("plan-x", "2026-01-01")]
        self.famly.delete_mode = "dict_body"  # an odd body: still accepted

        runner._delete_plan("plan-x")

        self.assertEqual(len(self.famly.requests), 1)

    def test_a_500_raises_and_is_not_retried(self):
        self.famly.delete_mode = "http500"

        with self.assertRaises(RestHTTPError) as caught:
            runner._delete_plan("plan-x")

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(len(self.famly.requests), 1)  # one request, no retry

    def test_a_plan_id_that_is_not_a_plain_token_is_refused_before_any_request(self):
        for bad in ("", "../x", "a/b", "a?b=1", "a b", "plan-x/../../y", None, 12, "x\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                runner._delete_plan(bad)

        self.assertEqual(self.famly.requests, [])

    def test_real_plan_ids_are_accepted(self):
        for good in ("01K8Z3J2X0V6Y9QJ4M7N1P5R8T", "7f3c9a52-1b0e-4c6e-9c1f-2a6d0b8e4d11", "plan_1-a"):
            runner._delete_plan(good)

        self.assertEqual(len(self.famly.requests), 3)


class RealDeleteChildTests(RealDeleteTestCase):
    def test_every_plan_is_deleted_latest_first_with_one_delete_each(self):
        self.three_plans()

        data, status = self.request()

        self.assertEqual((status, data["outcome"]), (200, "deleted"))
        self.assertEqual(self.delete_paths(), ["v2/plans/plan-c", "v2/plans/plan-b", "v2/plans/plan-a"])
        self.assertEqual(self.famly.plans, [])

    def test_every_request_is_a_bare_delete(self):
        self.three_plans()
        self.request()

        for method, path, params, body in self.famly.requests:
            self.assertEqual((method, params, body), ("DELETE", None, None))

    def test_a_backup_is_still_written_before_each_delete(self):
        self.three_plans()
        seen = []
        real_request = self.famly.request

        def spying(method, path, params=None, json_body=None):
            plan_id = path.rsplit("/", 1)[-1]
            seen.append((self.backups / CHILD / f"{plan_id}.json").is_file())
            return real_request(method, path, params, json_body)

        self.famly.request = spying
        self.request()

        self.assertEqual(seen, [True, True, True])

    def test_an_empty_list_is_not_proof_the_read_back_decides(self):
        self.three_plans()
        self.famly.delete_mode = "noop"  # Famly says [] and does nothing

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("still there", data["message"])
        self.assertEqual(self.delete_paths(), ["v2/plans/plan-c"])  # stopped on the first
        self.assertEqual(len(self.famly.plans), 3)
        self.assertEqual(len(self.notices), 1)  # an unexpected stop posts at once

    def test_a_500_stops_that_child_and_nothing_is_retried(self):
        self.three_plans()
        self.famly.delete_mode = "http500"

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("RestHTTPError", data["message"])
        self.assertIn("HTTP 500", data["message"])
        self.assertEqual(self.delete_paths(), ["v2/plans/plan-c"])  # once
        self.assertEqual(len(self.famly.plans), 3)
        self.assertIn("delete_failed", self.outcomes())
        self.assertEqual(len(self.notices), 1)

    def test_a_500_on_a_later_plan_stops_after_the_earlier_deletes(self):
        self.three_plans()
        real_request = self.famly.request

        def fail_on_b(method, path, params=None, json_body=None):
            if path.endswith("plan-b"):
                self.famly.delete_mode = "http500"
            return real_request(method, path, params, json_body)

        self.famly.request = fail_on_b
        data, _ = self.request()

        self.assertEqual(data["outcome"], "failed")
        self.assertEqual(self.delete_paths(), ["v2/plans/plan-c", "v2/plans/plan-b"])
        self.assertEqual([p["id"] for p in self.famly.plans], ["plan-a", "plan-b"])

    def test_the_plan_still_present_stops_even_when_the_count_dropped_by_one(self):
        self.three_plans()
        self.famly.delete_mode = "wrong_plan"  # plan-c stays, plan-b goes: 3 -> 2

        data, _ = self.request()

        self.assertEqual(data["outcome"], "failed")
        self.assertIn("plan-c it is still there", data["message"])
        self.assertEqual(self.delete_paths(), ["v2/plans/plan-c"])

    def test_a_count_that_drops_by_two_stops_the_child(self):
        self.three_plans()
        self.famly.delete_mode = "two"  # deleting plan-c also takes plan-b

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("1 plan(s), not 2", data["message"])
        self.assertIn("exactly one", data["message"])
        self.assertEqual(self.delete_paths(), ["v2/plans/plan-c"])
        self.assertIn("unexpected", self.outcomes())

    def test_a_neighbours_end_date_change_is_reported_but_does_not_stop(self):
        self.three_plans()
        self.famly.delete_mode = "reopen_previous"  # each delete leaves the plan before it open-ended

        data, status = self.request()

        self.assertEqual((status, data["outcome"], data["ok"]), (200, "deleted", True))
        self.assertEqual(self.famly.plans, [])  # every plan was deleted anyway
        self.assertEqual(len(self.delete_paths()), 3)
        self.assertNotIn("unexpected", self.outcomes())
        self.assertEqual(self.notices, [])  # no stop message

    def test_the_end_date_change_is_logged(self):
        self.three_plans()
        self.famly.delete_mode = "reopen_previous"
        self.request()

        row = next(r for r in self.log(CHILD) if r["outcome"] == "deleted" and r["plan_id"] == "plan-c")
        self.assertIn("previous plan `plan-b`: end date 2026-09-01 -> open-ended (changed)", row["detail"])

    def test_the_end_date_change_is_reported_in_slack_in_the_digest(self):
        from actions.delete_plans import digest

        self.three_plans()
        self.famly.delete_mode = "reopen_previous"
        self.request()

        digest.post_digest(names=False)

        message = self.notices[-1]
        self.assertIn("3 plan(s) deleted", message)
        self.assertIn("neighbour end dates changed", message)
        self.assertIn("`plan-b` 2026-09-01 -> open-ended", message)

    def test_a_neighbour_that_did_not_change_is_not_reported_in_slack(self):
        from actions.delete_plans import digest

        self.three_plans()
        self.request()

        digest.post_digest(names=False)

        self.assertNotIn("neighbour end dates changed", self.notices[-1])

    def test_a_dry_run_never_sends_a_delete(self):
        self.three_plans()
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            self.request()

        self.assertEqual(self.famly.requests, [])
        self.assertEqual(len(self.famly.plans), 3)

    def test_a_plan_without_the_capability_never_gets_a_delete(self):
        self.famly.plans = [plan_node("locked", "2026-01-01", deletable=False)]
        self.request()

        self.assertEqual(self.famly.requests, [])

    def test_a_failed_backup_means_no_delete_request(self):
        self.three_plans()
        self.backups.parent.mkdir(parents=True, exist_ok=True)
        self.backups.write_text("in the way", encoding="utf-8")

        self.request()

        self.assertEqual(self.famly.requests, [])

    def test_the_commit_path_is_still_never_touched(self):
        self.three_plans()
        self.request()  # FakeFamly.post raises on any POST

        self.assertEqual({m for m, *_ in self.famly.requests}, {"DELETE"})


class RestClientRequestTests(unittest.TestCase):
    """The client handles a DELETE with no body and a `[]` response."""

    def client(self):
        from core.rest_client import RestClient

        config = mock.Mock(rest_base_url="https://famly.example/api")
        session = mock.Mock()
        session.auth_headers.return_value = {"x-famly-accesstoken": "t"}
        return RestClient(session=session, config=config)

    def response(self, status=200, body="[]"):
        return mock.Mock(status_code=status, text=body, json=lambda: json.loads(body))

    def test_a_delete_with_no_body_and_no_params_is_sent_as_such(self):
        with mock.patch("core.rest_client.requests.request", return_value=self.response()) as sent:
            self.client().request("DELETE", "v2/plans/plan-x")

        (method, url), kwargs = sent.call_args
        self.assertEqual(method, "DELETE")
        self.assertEqual(url, "https://famly.example/api/v2/plans/plan-x")
        self.assertIsNone(kwargs["params"])
        self.assertIsNone(kwargs["data"])

    def test_an_empty_list_body_is_returned_as_an_empty_list(self):
        with mock.patch("core.rest_client.requests.request", return_value=self.response(200, "[]")):
            self.assertEqual(self.client().request("DELETE", "v2/plans/plan-x"), [])

    def test_a_non_2xx_raises_rest_http_error(self):
        with mock.patch("core.rest_client.requests.request", return_value=self.response(500, "boom")):
            with self.assertRaises(RestHTTPError) as caught:
                self.client().request("DELETE", "v2/plans/plan-x")

        self.assertEqual(caught.exception.status_code, 500)

    def test_get_and_post_are_unchanged(self):
        with mock.patch("core.rest_client.requests.request", return_value=self.response(200, '{"a": 1}')) as sent:
            client = self.client()
            self.assertEqual(client.get("v2/x", params={"q": "1"}), {"a": 1})
            self.assertEqual(client.post("v2/y", params={"preview": "true"}, json_body={"k": "v"}), {"a": 1})

        get_call, post_call = sent.call_args_list
        self.assertEqual((get_call.args[0], get_call.kwargs["params"], get_call.kwargs["data"]), ("GET", {"q": "1"}, None))
        self.assertEqual(post_call.args[0], "POST")
        self.assertEqual(json.loads(post_call.kwargs["data"]), {"k": "v"})

    def test_request_is_the_path_get_and_post_use(self):
        from core.rest_client import RestClient

        with mock.patch.object(RestClient, "_request", return_value="sentinel") as inner:
            result = self.client().request("PUT", "v2/z", params={"a": 1}, json_body={"b": 2})

        self.assertEqual(result, "sentinel")
        inner.assert_called_once_with("PUT", "v2/z", params={"a": 1}, json_body={"b": 2})


class CanDeletePlanTests(DeleteTestCase):
    def test_a_plan_without_the_capability_is_skipped_and_reported(self):
        self.famly.plans = [
            plan_node("plan-a", "2026-01-01", "2026-09-01"),
            plan_node("plan-b", "2026-09-01", None, deletable=False),
        ]

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(self.delete_calls, ["plan-a"])  # plan-b skipped, plan-a still deleted
        self.assertFalse(data["ok"])  # not everything is gone
        self.assertEqual(data["outcome"], "partial")
        self.assertTrue(any("plan-b" in w and "CanDeletePlan" in w for w in data["warnings"]))
        self.assertEqual([p["result"] for p in data["plans"]], ["skipped", "deleted"])
        self.assertIn("1 skipped", data["message"])
        self.assertEqual(self.notices, [])  # routine: the digest reports it
        skipped = [r for r in self.log(CHILD) if r["outcome"] == "skipped"]
        self.assertEqual([r["plan_id"] for r in skipped], ["plan-b"])
        self.assertEqual([p["id"] for p in self.famly.plans], ["plan-b"])

    def test_a_skipped_plan_is_not_backed_up_or_deleted(self):
        self.famly.plans = [plan_node("only", "2026-01-01", deletable=False)]

        self.request()

        self.assertEqual(self.delete_calls, [])
        self.assertFalse((self.backups / CHILD / "only.json").exists())
        self.assertIn("skipped", self.outcomes())

    def test_when_no_plan_can_be_deleted_nothing_is_deleted(self):
        self.famly.plans = [
            plan_node("a", "2026-01-01", deletable=False),
            plan_node("b", "2026-09-01", deletable=False),
        ]

        data, _ = self.request()

        self.assertEqual(self.delete_calls, [])
        self.assertEqual(data["outcome"], "partial")

    def test_a_skip_is_not_a_failed_child(self):
        self.famly.plans = [plan_node("a", "2026-01-01", deletable=False)]
        self.request()

        self.assertEqual(runner.consecutive_failures(), 0)

    def test_the_capability_is_read_from_the_plans_behaviours(self):
        deletable = read_child_plans.parse_plan(plan_node("a", "2026-01-01"))
        locked = read_child_plans.parse_plan(plan_node("b", "2026-01-01", deletable=False))

        self.assertTrue(runner.can_delete(deletable))
        self.assertFalse(runner.can_delete(locked))


# --------------------------------------------------------------------------- #
# Caps, the pause flag and the breaker
# --------------------------------------------------------------------------- #
class DailyCapTests(DeleteTestCase):
    CHILDREN = [f"00000000-0000-0000-0000-0000000000b{n}" for n in range(1, 5)]

    def setUp(self):
        super().setUp()
        self.allowlist_file.write_text("\n".join(self.CHILDREN) + "\n", encoding="utf-8")
        for index, child in enumerate(self.CHILDREN):
            self.famly.plans.append(plan_node(f"p{index}", "2026-01-01", child=child))

    def test_the_cap_limits_children_per_day(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "2"}):
            results = [self.request(child=c)[0] for c in self.CHILDREN]

        self.assertEqual([r["outcome"] for r in results], ["deleted", "deleted", "failed", "failed"])
        self.assertIn("daily limit of 2", results[2]["message"])
        self.assertEqual(self.delete_calls, ["p0", "p1"])
        self.assertEqual(runner.children_today(self.clock), set(self.CHILDREN[:2]))

    def test_the_cap_is_a_200_and_a_slack_message(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "0"}):
            data, status = self.request(child=self.CHILDREN[0])

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(self.delete_calls, [])
        self.assertIn("daily limit", self.notices[-1])
        self.assertIn("cap_reached", self.outcomes())

    def test_a_child_already_started_today_may_finish_even_at_the_cap(self):
        self.famly.plans.append(plan_node("p0b", "2026-06-01", child=self.CHILDREN[0]))
        calls_seen = []

        def hook(plan):
            calls_seen.append(plan.id)

        self.delete_hook = hook
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "1"}):
            data, _ = self.request(child=self.CHILDREN[0])
            other, _ = self.request(child=self.CHILDREN[1])

        self.assertEqual(data["outcome"], "deleted")
        self.assertEqual(calls_seen, ["p0b", "p0"])  # both of that child's plans
        self.assertEqual(other["outcome"], "failed")  # but no second child

    def test_the_cap_is_checked_before_every_delete_not_once_per_request(self):
        self.famly.plans.append(plan_node("p0b", "2026-06-01", child=self.CHILDREN[0]))
        # The cap is lowered by the time the second plan is reached: a child that
        # has already started is still counted, so it goes on -- the check ran.
        checks = []
        real = runner.children_today

        def counting(now=None):
            checks.append(1)
            return real(now)

        with mock.patch.object(runner, "children_today", counting):
            self.request(child=self.CHILDREN[0])

        self.assertEqual(len(checks), 2)  # once per delete

    def test_the_count_starts_again_the_next_utc_day(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "1"}):
            self.assertEqual(self.request(child=self.CHILDREN[0])[0]["outcome"], "deleted")
            self.assertEqual(self.request(child=self.CHILDREN[1])[0]["outcome"], "failed")

            self.clock += timedelta(days=1)
            self.assertEqual(self.request(child=self.CHILDREN[1])[0]["outcome"], "deleted")

    def test_the_default_is_forty(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": ""}):
            self.assertEqual(runner.max_per_day(), 40)
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "nonsense"}):
            self.assertEqual(runner.max_per_day(), 40)

    def test_a_cap_stop_is_not_a_failed_child(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MAX_PER_DAY": "0"}):
            for child in self.CHILDREN:
                self.request(child=child)

        self.assertEqual(runner.consecutive_failures(), 0)
        self.assertFalse(runner.is_paused())


class PauseTests(DeleteTestCase):
    def test_a_paused_run_deletes_nothing(self):
        self.three_plans()
        runner.set_paused(True)

        data, status = self.request()

        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(self.delete_calls, [])
        self.assertIn("pause", data["message"])
        self.assertEqual(len(self.famly.plans), 3)

    def test_the_pause_flag_is_checked_before_each_delete_not_just_at_the_start(self):
        self.three_plans()

        def pause_after_the_first(plan):
            runner.set_paused(True)

        self.delete_hook = pause_after_the_first
        data, _ = self.request()

        self.assertEqual(self.delete_calls, ["plan-c"])  # the second delete never happened
        self.assertEqual(data["outcome"], "failed")
        self.assertIn("paused", self.outcomes())
        self.assertEqual([p["id"] for p in self.famly.plans], ["plan-a", "plan-b"])

    def test_a_pause_is_not_a_failed_child(self):
        self.three_plans()
        runner.set_paused(True)
        self.request()

        self.assertEqual(runner.consecutive_failures(), 0)

    def test_the_pause_flag_does_not_stop_a_dry_run(self):
        self.three_plans()
        runner.set_paused(True)
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            data, _ = self.request()

        self.assertEqual(data["outcome"], "dry_run")

    def test_clearing_the_flag_lets_it_run_again_and_resets_the_count(self):
        self.three_plans()
        runner.set_paused(True)
        runner._state_set(runner.FAILURES_KEY, "2")

        runner.set_paused(False)

        self.assertEqual((runner.is_paused(), runner.consecutive_failures()), (False, 0))
        self.assertEqual(self.request()[0]["outcome"], "deleted")


class CircuitBreakerTests(DeleteTestCase):
    CHILDREN = [f"00000000-0000-0000-0000-0000000000c{n}" for n in range(1, 6)]

    def setUp(self):
        super().setUp()
        self.allowlist_file.write_text("\n".join(self.CHILDREN) + "\n", encoding="utf-8")
        for index, child in enumerate(self.CHILDREN):
            self.famly.plans.append(plan_node(f"q{index}", "2026-01-01", child=child))
        self.fail_for: set[str] = set()

        def maybe_fail(plan):
            if plan.id in self.fail_for:
                raise RuntimeError("Famly refused")
            self.remove_plan(plan)

        self.delete_effect = maybe_fail

    def test_three_failed_children_in_a_row_pause_everything(self):
        self.fail_for = {"q0", "q1", "q2"}

        outcomes = [self.request(child=c)[0]["outcome"] for c in self.CHILDREN[:3]]

        self.assertEqual(outcomes, ["failed", "failed", "failed"])
        self.assertTrue(runner.is_paused())
        self.assertEqual(runner.consecutive_failures(), 3)
        paused = [n for n in self.notices if "paused" in n]
        self.assertEqual(len(paused), 1)

    def test_nothing_is_deleted_after_the_pause(self):
        self.fail_for = {"q0", "q1", "q2"}
        for child in self.CHILDREN[:3]:
            self.request(child=child)
        calls = len(self.delete_calls)

        data, status = self.request(child=self.CHILDREN[3])

        self.assertEqual(status, 200)
        self.assertEqual(len(self.delete_calls), calls)
        self.assertFalse(data["ok"])

    def test_two_failures_do_not_pause(self):
        self.fail_for = {"q0", "q1"}
        for child in self.CHILDREN[:2]:
            self.request(child=child)

        self.assertFalse(runner.is_paused())

    def test_a_success_resets_the_count(self):
        self.fail_for = {"q0", "q1", "q3", "q4"}
        for child in self.CHILDREN:  # fail, fail, succeed, fail, fail
            self.request(child=child)

        self.assertFalse(runner.is_paused())
        self.assertEqual(runner.consecutive_failures(), 2)

    def test_the_pause_is_announced_with_the_child_id(self):
        self.fail_for = {"q0", "q1", "q2"}
        for child in self.CHILDREN[:3]:
            self.request(child=child)

        self.assertIn(self.CHILDREN[2], [n for n in self.notices if "paused" in n][0])


class FailuresReturn200Tests(DeleteTestCase):
    def test_every_failure_is_a_200(self):
        self.three_plans()
        scenarios = []

        def refuse():
            with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "off"}):
                return self.request()

        def not_allowed():
            return self.request(child=OTHER)

        def delete_raises():
            self.delete_effect = lambda plan: (_ for _ in ()).throw(RuntimeError("no"))
            return self.request()

        def still_there():
            self.delete_effect = self.leave_plan_in_place
            return self.request()

        def read_fails():
            self.famly.get_failures = [RuntimeError("down")]
            return self.request()

        for scenario in (refuse, not_allowed, delete_raises, still_there, read_fails):
            data, status = scenario()
            scenarios.append((scenario.__name__, status, data["ok"]))

        self.assertEqual([s[1] for s in scenarios], [200] * 5, scenarios)
        self.assertEqual([s[2] for s in scenarios], [False] * 5, scenarios)

    def test_only_a_malformed_request_is_not_a_200(self):
        _, status = self.request(confirm="nope")

        self.assertEqual(status, 422)


class NoNamesAndNoCommitTests(DeleteTestCase):
    def test_slack_messages_use_the_child_id_only(self):
        self.three_plans()
        for plan in self.famly.plans:
            plan["childName"] = "Alex Smith"  # should a name ever be in the data
        self.request()
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            self.famly.plans = [plan_node("x", "2026-01-01")]
            self.request()

        for message in self.notices:
            self.assertNotIn("Smith", message)
            self.assertIn(CHILD, message)

    def test_the_commit_path_is_never_touched(self):
        self.three_plans()
        # FakeFamly.post raises on any POST, so a commit or preview would fail.
        data, _ = self.request()

        self.assertEqual(data["outcome"], "deleted")
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertNotIn("runner.commit", source)
        self.assertNotIn("import approval", source)
        self.assertNotIn(".post(", source.replace("slack.post_notice(", ""))


# --------------------------------------------------------------------------- #
# delete-resume, the separate pause flags, and the documented outcomes
# --------------------------------------------------------------------------- #
class DeleteResumeTests(DeleteTestCase):
    def test_it_clears_the_pause_flag_and_the_failure_count(self):
        runner.set_paused(True)
        runner._state_set(runner.FAILURES_KEY, "3")

        result = runner.resume()

        self.assertTrue(result["wasPaused"])
        self.assertFalse(result["paused"])
        self.assertEqual(result["consecutiveFailures"], 0)
        self.assertFalse(runner.is_paused())

    def test_it_reports_when_it_was_not_paused(self):
        self.assertFalse(runner.resume()["wasPaused"])

    def test_it_prints_the_state(self):
        self.three_plans()
        self.request()  # three deleted today

        state = runner.resume()

        self.assertEqual(state["mode"], "live")
        self.assertEqual(state["paused"], False)
        self.assertEqual(state["childrenDoneToday"], 1)
        self.assertEqual(state["maxPerDay"], 40)
        self.assertEqual(
            set(state),
            {"wasPaused", "mode", "paused", "consecutiveFailures", "childrenDoneToday", "maxPerDay", "lastLogRows"},
        )

    def test_it_shows_the_last_five_log_rows_newest_first(self):
        self.three_plans()
        self.request()  # started + 3 deleted = 4 rows
        self.request()  # nothing_to_delete: the 5th
        self.request()  # and a 6th

        rows = runner.resume()["lastLogRows"]

        self.assertEqual(len(rows), 5)
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(rows[0]["outcome"], "nothing_to_delete")
        self.assertEqual(rows[-1]["outcome"], "deleted")  # `started`, the oldest, fell off
        for key in ("child_id", "plan_id", "at", "mode", "outcome", "detail"):
            self.assertIn(key, rows[0])

    def test_fewer_than_five_rows_shows_what_there_is(self):
        self.assertEqual(runner.resume()["lastLogRows"], [])
        self.request()  # nothing to delete: one row
        self.assertEqual(len(runner.resume()["lastLogRows"]), 1)

    def test_children_done_today_counts_children_not_plans_and_resets_each_day(self):
        self.three_plans()
        self.request()

        self.assertEqual(runner.resume()["childrenDoneToday"], 1)
        self.clock += timedelta(days=1)
        self.assertEqual(runner.resume()["childrenDoneToday"], 0)

    def test_a_dry_run_does_not_count_as_done(self):
        self.three_plans()
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            self.request()

        self.assertEqual(runner.resume()["childrenDoneToday"], 0)

    def test_the_mode_shown_is_the_current_one(self):
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "bogus"}):
            self.assertEqual(runner.resume()["mode"], "off")

    def test_a_cleared_pause_lets_a_delete_run_again(self):
        self.three_plans()
        runner.set_paused(True)
        self.assertEqual(self.request()[0]["outcome"], "failed")

        runner.resume()

        self.assertEqual(self.request()[0]["outcome"], "deleted")

    def test_the_cli_command_is_registered_and_runs(self):
        import argparse

        import main
        from actions.delete_plans import delete_resume

        runner.set_paused(True)
        self.assertEqual(main.build_parser().parse_args(["delete-resume"]).action, "delete-resume")

        result = delete_resume.handle(argparse.Namespace())

        self.assertTrue(result["wasPaused"])
        self.assertFalse(runner.is_paused())
        self.assertEqual(delete_resume.NAME, "delete-resume")

    def test_the_command_prints_json_through_main(self):
        import io
        from contextlib import redirect_stdout

        import main

        with redirect_stdout(io.StringIO()) as out:
            code = main.main(["delete-resume"])

        self.assertEqual(code, 0)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["mode"], "live")
        self.assertIn("lastLogRows", printed)


class SeparatePauseFlagsTests(DeleteTestCase):
    def queue_keys(self):
        with preview_store.open_store() as connection:
            return {r["key"] for r in connection.execute("SELECT key FROM queue_state")}

    def test_the_keys_are_different(self):
        from actions.queue_timeline import jobs

        self.assertEqual(runner.PAUSED_KEY, "delete_plans:paused")
        self.assertEqual(runner.FAILURES_KEY, "delete_plans:consecutive_failures")
        self.assertNotEqual(runner.PAUSED_KEY, "paused")
        self.assertNotEqual(runner.FAILURES_KEY, "consecutive_failures")
        self.assertIsNone(jobs.state_get("paused"))

    def test_the_delete_plans_pause_is_stored_under_its_own_key(self):
        runner.set_paused(True)

        self.assertEqual(self.queue_keys(), {"delete_plans:paused"})

    def test_tripping_the_delete_pause_does_not_pause_the_plan_queue(self):
        from actions.queue_timeline import jobs

        runner.set_paused(True)

        self.assertTrue(runner.is_paused())
        self.assertFalse(jobs.is_paused())

    def test_tripping_the_queue_pause_does_not_pause_delete_plans(self):
        from actions.queue_timeline import jobs

        jobs.set_paused(True)

        self.assertTrue(jobs.is_paused())
        self.assertFalse(runner.is_paused())
        self.three_plans()
        self.assertEqual(self.request()[0]["outcome"], "deleted")

    def test_the_failure_counters_are_separate_too(self):
        from actions.queue_timeline import jobs

        runner._state_set(runner.FAILURES_KEY, "2")
        jobs.state_set("consecutive_failures", "1")

        self.assertEqual(runner.consecutive_failures(), 2)
        self.assertEqual(jobs.consecutive_failures(), 1)

    def test_the_breaker_trips_only_the_delete_flag(self):
        from actions.queue_timeline import jobs

        children = [f"00000000-0000-0000-0000-0000000000d{n}" for n in range(1, 4)]
        self.allowlist_file.write_text("\n".join(children) + "\n", encoding="utf-8")
        for index, child in enumerate(children):
            self.famly.plans.append(plan_node(f"z{index}", "2026-01-01", child=child))
        self.delete_effect = lambda plan: (_ for _ in ()).throw(RuntimeError("no"))

        for child in children:
            self.request(child=child)

        self.assertTrue(runner.is_paused())
        self.assertFalse(jobs.is_paused())
        self.assertEqual(jobs.consecutive_failures(), 0)

    def test_the_queue_breaker_trips_only_the_queue_flag(self):
        from actions.queue_timeline import jobs

        for _ in range(3):
            jobs.state_increment("consecutive_failures")
        jobs.set_paused(True)

        self.assertFalse(runner.is_paused())
        self.assertEqual(runner.consecutive_failures(), 0)

    def test_delete_resume_does_not_clear_the_plan_queues_pause(self):
        from actions.queue_timeline import jobs

        jobs.set_paused(True)
        jobs.state_set("consecutive_failures", "3")
        runner.set_paused(True)

        runner.resume()

        self.assertFalse(runner.is_paused())
        self.assertTrue(jobs.is_paused())  # still paused
        self.assertEqual(jobs.consecutive_failures(), 3)

    def test_queue_resume_does_not_clear_the_delete_plans_pause(self):
        from actions.queue_timeline import jobs, worker

        runner.set_paused(True)
        runner._state_set(runner.FAILURES_KEY, "3")
        jobs.set_paused(True)

        worker.resume()

        self.assertFalse(jobs.is_paused())
        self.assertTrue(runner.is_paused())  # still paused
        self.assertEqual(runner.consecutive_failures(), 3)

    def test_both_flags_can_be_set_and_cleared_independently(self):
        from actions.queue_timeline import jobs

        jobs.set_paused(True)
        runner.set_paused(True)
        self.assertEqual(self.queue_keys(), {"paused", "delete_plans:paused"})

        runner.set_paused(False)
        self.assertEqual(self.queue_keys(), {"paused"})


class DocumentedOutcomesTests(DeleteTestCase):
    README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

    def section(self):
        start = self.README.index("## `delete_plans`")
        end = self.README.index("\n## ", start + 1)
        return self.README[start:end]

    def test_there_is_a_delete_plans_section_marked_temporary(self):
        section = self.section()

        self.assertIn("TEMPORARY", section)
        self.assertIn("delete-resume", section)

    def test_every_outcome_is_listed_in_the_readme_with_a_meaning(self):
        section = self.section()

        for outcome, meaning in runner.OUTCOMES.items():
            with self.subTest(outcome=outcome):
                self.assertRegex(section, rf"\| `{outcome}` \|")
                self.assertTrue(meaning)

    def test_the_readme_lists_no_outcome_the_code_does_not_have(self):
        import re

        section = self.section()
        table = section[section.index("| `data.outcome` |"):]
        table = table[: table.index("\n\n")]
        listed = set(re.findall(r"^\| `(\w+)` \|", table, flags=re.M)) - {"data"}

        self.assertEqual(listed, set(runner.OUTCOMES))

    def test_every_outcome_the_code_can_emit_is_in_the_table(self):
        import re

        source = Path(runner.__file__).read_text(encoding="utf-8") + Path(delete_web.__file__).read_text(
            encoding="utf-8"
        )
        emitted = set(re.findall(r'result\.outcome = "(\w+)"', source))
        emitted |= set(re.findall(r'_refuse\(\s*result,\s*"(\w+)"', source))
        emitted |= set(re.findall(r'"outcome": "(\w+)"', source))
        emitted |= set(re.findall(r'"deleted" if skipped == 0 else "(\w+)"', source)) | {"deleted"}

        self.assertTrue(emitted)
        self.assertEqual(emitted - set(runner.OUTCOMES), set())
        self.assertEqual(set(runner.OUTCOMES) - emitted, set())

    def test_the_outcomes_seen_in_responses_are_all_documented(self):
        seen = set()
        self.three_plans()
        seen.add(self.request()[0]["outcome"])  # deleted
        seen.add(self.request()[0]["outcome"])  # nothing_to_delete
        seen.add(self.request(child=OTHER)[0]["outcome"])  # not_allowlisted
        seen.add(self.request(confirm="x")[0]["outcome"])  # malformed
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "off"}):
            seen.add(self.request()[0]["outcome"])  # off
        with mock.patch.dict("os.environ", {"DELETE_PLANS_MODE": "dry_run"}):
            self.famly.plans = [plan_node("a", "2026-01-01")]
            seen.add(self.request()[0]["outcome"])  # dry_run
        self.famly.get_failures = [RuntimeError("down")]
        seen.add(self.request()[0]["outcome"])  # error
        self.delete_effect = self.leave_plan_in_place
        seen.add(self.request()[0]["outcome"])  # failed
        self.famly.plans = [plan_node("b", "2026-01-01", deletable=False)]
        seen.add(self.request()[0]["outcome"])  # partial

        self.assertEqual(seen, set(runner.OUTCOMES))


if __name__ == "__main__":
    unittest.main()
