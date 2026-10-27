"""The server owns famly_plan_uploaded: set after a confirmed commit, and reconciled from Famly.

Fakes only: runner.commit is mocked (nothing reaches Famly) and HubSpot is a fake writer. Ids, counts
and codes only."""

import argparse
import contextlib
import io
import json
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

from actions.hubspot_plan_uploaded import cli, runner
from actions.plan_write import approval, hubspot_intake, plan_flag, preview_store, runner as plan_runner
from actions.read_child_plans.runner import Plan
from core.hubspot import HubSpotError
from core.rest_client import RestHTTPError
from tests.test_commit_approval import CHILD_ID, PLAN_BODY, PREVIEW_ID, FakeResult, StoreTestCase

logging.disable(logging.CRITICAL)
BOOL = {"type": "bool", "fieldType": "booleancheckbox", "options": []}


class FakeWriter:
    """Reads and the one plan-uploaded write."""

    def __init__(self, flags=None, reject=()):
        self.flags = dict(flags or {})              # deal id -> "true" | "false" | None
        self.reject = set(reject)
        self.reads, self.writes = [], []
        self.children = {}                          # deal id -> famly_childid
        self.stages = {}
        self.search_rows = []

    def deal_property_definitions(self):
        return {"famly_plan_uploaded": BOOL}

    def read_plan_uploaded(self, deal_id):
        self.reads.append(deal_id)
        return self.flags.get(deal_id)

    def read_plan_state(self, deal_id):
        if deal_id not in self.flags:
            raise HubSpotError("GET failed with HTTP 404", 404)
        return self.children.get(deal_id), self.flags.get(deal_id)

    def write_plan_uploaded(self, deal_id, value):
        self.writes.append((deal_id, value))
        if deal_id in self.reject:
            raise HubSpotError("PATCH failed with HTTP 400", 400)
        self.flags[deal_id] = value

    def stage_names(self):
        return self.stages

    def search_deals(self, filters, properties, limit=100):
        return self.search_rows


# --------------------------------------------------------------------------- #
# After a commit
# --------------------------------------------------------------------------- #
class AfterCommitCase(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.writer = FakeWriter({"555": None})
        patcher = mock.patch.object(plan_flag, "HubSpotDealWriter", return_value=self.writer)
        patcher.start()
        self.addCleanup(patcher.stop)
        spawn = mock.patch.object(approval, "_spawn", lambda work: work())      # run the flag write inline
        spawn.start()
        self.addCleanup(spawn.stop)
        self.notices = []
        notice = mock.patch.object(plan_flag.slack, "post_notice", side_effect=lambda t: self.notices.append(t))
        notice.start()
        self.addCleanup(notice.stop)

    def save(self, deal_id="555", old_plan_id=None, child_id=CHILD_ID):
        if old_plan_id:
            self.live_plans = [Plan(id=old_plan_id, from_="2026-01-01")]
        preview_store.save(PREVIEW_ID, PLAN_BODY, child_id, 3, old_plan_id=old_plan_id, deal_id=deal_id)

    def commit_lands(self, plan_id="plan-created-1"):
        """A commit that returns and leaves the plan in Famly (for the live read-back)."""
        def commit(*args, **kwargs):
            self.live_plans = list(self.live_plans) + [Plan(id=plan_id, from_="2026-09-01")]
            return FakeResult()
        return mock.patch.object(plan_runner, "commit", side_effect=commit)


class CommitSetsTheFlag(AfterCommitCase):
    def test_a_confirmed_commit_sets_the_flag_once(self):
        self.save()
        self.assertEqual(preview_store.get(PREVIEW_ID).deal_id, "555")      # carried on the stored preview
        with self.commit_lands():
            text = self.approve()
        self.assertTrue(text.startswith("✅ Plan committed"))
        self.assertEqual(self.writer.writes, [("555", "true")])

    def test_a_value_that_is_already_true_writes_nothing(self):
        self.writer.flags["555"] = "true"
        self.save()
        with self.commit_lands():
            self.approve()
        self.assertEqual(self.writer.writes, [])
        self.assertEqual(self.writer.reads, ["555"])                        # it was read first

    def test_a_double_click_sets_it_once(self):
        self.save()
        with self.commit_lands():
            self.approve()
            self.approve()
        self.assertEqual(self.writer.writes, [("555", "true")])

    def test_a_plan_added_alongside_another_leaves_the_flag_alone(self):
        self.save(old_plan_id="existing-plan-1")
        with self.commit_lands():
            self.approve()
        self.assertEqual((self.writer.reads, self.writer.writes), ([], []))

    def test_a_preview_with_no_deal_id_sets_nothing(self):
        self.save(deal_id=None)
        with self.commit_lands():
            self.approve()
        self.assertEqual((self.writer.reads, self.writer.writes), ([], []))

    def test_a_commit_that_cannot_be_read_back_sets_nothing(self):
        self.save()
        with mock.patch.object(plan_runner, "commit", return_value=FakeResult()):   # the plan never shows
            self.approve()
        self.assertEqual((self.writer.reads, self.writer.writes), ([], []))


class NoCommitNoFlag(AfterCommitCase):
    def assert_untouched(self):
        self.assertEqual((self.writer.reads, self.writer.writes), ([], []))

    def test_rejected_refused_failed_and_blocked_commits_write_nothing(self):
        self.save()
        self.reject()
        self.assert_untouched()

        self.save()
        with mock.patch.object(plan_runner, "commit",
                               side_effect=plan_runner.PlanCommitRefused("guard")):
            self.approve()
        self.assert_untouched()

        self.save()
        with mock.patch.object(plan_runner, "commit", side_effect=RestHTTPError("boom", 500, "")):
            self.approve()
        self.assert_untouched()

        self.save(child_id="some-other-child")                                # not in the allow-list
        self.approve()
        self.assert_untouched()

        with mock.patch.dict("os.environ", {"COMMIT_ENABLED": "false"}):
            self.save()
            self.approve()
        self.assert_untouched()

    def test_an_in_progress_commit_writes_nothing(self):
        self.save()
        preview_store.mark(PREVIEW_ID, preview_store.STATUS_COMMITTING)
        self.approve()
        self.assert_untouched()


class HubSpotProblemsNeverTouchFamly(AfterCommitCase):
    def test_a_hubspot_failure_leaves_the_famly_result_alone_and_posts_one_coded_line(self):
        self.writer.reject = {"555"}
        self.save()
        with self.commit_lands() as commit:
            text = self.approve()
        self.assertTrue(text.startswith("✅ Plan committed"))
        self.assertEqual(preview_store.get(PREVIEW_ID).status, preview_store.STATUS_COMMITTED)
        self.assertEqual(commit.call_count, 1)                              # Famly written once, not retried
        self.assertEqual(self.writer.writes, [("555", "true")])             # one PATCH, not retried
        self.assertEqual(len(self.notices), 1)
        self.assertIn("plan_flag_hubspot_failed", self.notices[0])
        self.assertIn("555", self.notices[0])

    def test_an_error_inside_the_flag_code_never_reaches_the_approval(self):
        self.save()
        with self.commit_lands(), mock.patch.object(plan_flag, "after_commit", side_effect=RuntimeError("x")):
            # the spawn runs the work inline: an exception here would break the click
            try:
                text = self.approve()
            except RuntimeError:
                self.fail("the flag write broke the approval")
        self.assertTrue(text.startswith("✅ Plan committed") or "failed" in text.lower())


class DealIdTests(unittest.TestCase):
    def test_the_deal_id_comes_from_the_webhook_payload_digits_only(self):
        self.assertEqual(hubspot_intake.deal_id_of({"dealId": "123"}), "123")
        self.assertEqual(hubspot_intake.deal_id_of({"properties": {"hs_object_id": 456}}), "456")
        self.assertEqual(hubspot_intake.deal_id_of({"deal_id": " 789 "}), "789")
        for bad in ({"dealId": "12a"}, {"dealId": ""}, {}, None, "text", {"dealId": "a@b.co"}):
            self.assertIsNone(hubspot_intake.deal_id_of(bad), bad)


# --------------------------------------------------------------------------- #
# Reconcile: hubspot-set-plan-uploaded --from-famly
# --------------------------------------------------------------------------- #
class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.writer = FakeWriter({"1": None, "2": "true", "3": "true", "4": None, "5": "true"})
        self.plans = {"c1": True, "c2": False, "c3": True, "c5": None}          # None = unreadable

    def has_plan(self, child_id):
        value = self.plans[child_id]
        if value is None:
            raise RuntimeError("unreadable")
        return value

    def states(self):
        return [("1", "c1", None), ("2", "c2", "true"), ("3", "c3", "true"), ("4", None, None),
                ("5", "c5", "true"), ("9", "not_found")]

    def test_a_dry_run_counts_and_writes_nothing(self):
        result = runner.reconcile(self.writer, self.has_plan, self.states(), confirm=False)
        self.assertEqual(self.writer.writes, [])
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["results"], {"no_child": 1, "not_found": 1, "plan_unreadable": 1,
                                             "unchanged": 1, "would_set_false": 1, "would_set_true": 1})
        self.assertIsNone(result["perDeal"])

    def test_confirm_writes_only_the_deals_whose_flag_differs(self):
        result = runner.reconcile(self.writer, self.has_plan, self.states(), confirm=True)
        self.assertEqual(sorted(self.writer.writes), [("1", "true"), ("2", "false")])
        self.assertEqual(result["perDeal"], {"1": "changed_true", "2": "changed_false", "3": "unchanged",
                                             "4": "no_child", "5": "plan_unreadable", "9": "not_found"})
        self.assertEqual(result["written"], 2)

    def test_a_plan_that_cannot_be_read_changes_nothing(self):
        runner.reconcile(self.writer, self.has_plan, [("5", "c5", "true")], confirm=True)
        self.assertEqual(self.writer.writes, [])

    def test_the_cap_limits_writes_per_run_and_defers_the_rest(self):
        self.plans = {f"c{i}": True for i in range(10, 15)}
        states = [(str(i), f"c{i}", None) for i in range(10, 15)]
        result = runner.reconcile(self.writer, self.has_plan, states, confirm=True, cap=2)
        self.assertEqual(len(self.writer.writes), 2)
        self.assertEqual(result["results"], {"changed_true": 2, "deferred": 3})

    def test_a_rejected_write_is_reported_and_the_rest_go(self):
        self.writer.reject = {"1"}
        result = runner.reconcile(self.writer, self.has_plan, self.states(), confirm=True)
        self.assertEqual(result["perDeal"]["1"], "rejected")
        self.assertEqual(result["perDeal"]["2"], "changed_false")

    def test_the_property_missing_stops_everything(self):
        class NoProperty(FakeWriter):
            def deal_property_definitions(self):
                return {}
        writer = NoProperty()
        result = runner.reconcile(writer, self.has_plan, self.states(), confirm=True)
        self.assertEqual(result["stopped"], "property_missing")
        self.assertEqual(writer.writes, [])

    def test_operations_include_every_operations_stage_including_left_nursery(self):
        self.writer.stages = {"s1": ("Operations", "Deposit Pending"), "s2": ("Operations", "Left Nursery"),
                              "s3": ("Sales", "Contract Signed")}
        self.writer.search_rows = [{"id": 7, "famly_childid": "c7", "famly_plan_uploaded": None},
                                   {"id": 8, "famly_childid": "", "famly_plan_uploaded": "true"}]
        self.assertEqual(runner.operations_states(self.writer), [("7", "c7", None), ("8", None, "true")])

    def test_the_output_is_ids_counts_and_codes_only(self):
        result = runner.reconcile(self.writer, self.has_plan, self.states(), confirm=True)
        text = json.dumps(result)
        for word in ("@", "name", "phone", "address", "c1"):                # a child id is not printed either
            self.assertNotIn(word, text)


class ReconcileCliTests(unittest.TestCase):
    def parse(self, *argv):
        parser = argparse.ArgumentParser()
        cli.add_args(parser)
        return parser.parse_args(list(argv))

    def test_from_famly_refuses_a_value_and_an_oversized_file(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.handle(self.parse("--file", "x", "--from-famly", "--value", "true"))
        self.assertEqual(ctx.exception.code, 3)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write("\n".join(str(n) for n in range(1, 62)))
        with mock.patch.object(cli, "HubSpotDealWriter", return_value=FakeWriter()), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.handle(self.parse("--file", handle.name, "--from-famly"))
        self.assertEqual(ctx.exception.code, 3)

    def test_the_old_command_still_needs_its_value(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.handle(self.parse("--file", "x"))
        self.assertEqual(ctx.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
