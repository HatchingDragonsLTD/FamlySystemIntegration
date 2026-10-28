"""onboard-quality-check: tiers, scope, the diff against the last run, what is posted and when, and that
nothing in it can write. Fake data only (deal ids and codes); no Famly or HubSpot call."""

import argparse
import ast
import csv
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from actions.onboard_audit import runner as audit
from actions.onboard_quality import cli, config, runner

TODAY = date(2026, 10, 27)


def row(deal_id, stage="Active In School", codes=()):
    return audit.Row(deal_id=str(deal_id), site_code="HDCW", stage=stage, child_id=f"child-{deal_id}",
                     found_via="deal_reference", codes=list(codes))


class FakeHub:
    def __init__(self, deals, stages):
        self.deals, self.stages, self.calls = deals, stages, 0

    def stage_names(self):
        return self.stages

    def search_deals(self, filters, properties, limit=100):
        return self.deals


class QualityCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.reports = self.root / "reports"
        self.posts = []
        self.hub = mock.Mock(calls=3)
        self.gql, self.rest = mock.Mock(calls=2), mock.Mock(calls=5)
        scope = mock.patch.object(runner, "scope_deals", return_value=[])
        scope.start()
        self.addCleanup(scope.stop)

    def audit_returns(self, rows):
        patcher = mock.patch.object(runner.audit, "run", return_value=({}, rows))
        patcher.start()
        self.addCleanup(patcher.stop)

    def slack_ok(self, text):
        self.posts.append(text)
        return True                                                  # the real notifier returns a bool

    def go(self, **kwargs):
        kwargs.setdefault("post", self.slack_ok)
        return runner.run(self.hub, self.gql, self.rest, today=TODAY, state_file=self.state,
                          reports_dir=self.reports, **kwargs)


class TierTests(unittest.TestCase):
    def test_alert_count_and_ignore_tiers(self):
        for code in ("contact_missing", "plan_missing", "double_billing_risk", "bill_payer_share_zero",
                     "child_end_famly_open_ended", "child_end_famly_later_over_31_days"):
            self.assertEqual(config.tier(code), config.ALERT, code)
        for code in ("plan_fee_over_10_percent", "child_start_differs", "child_end_differs",
                     "child_end_famly_later_1_to_7_days", "child_start_famly_earlier_8_to_31_days"):
            self.assertEqual(config.tier(code), config.COUNT, code)
        for code in ("child_group_differs", "child_name_differs", "child_name_not_comparable", "name_one_contains_the_other",
                     "plan_input_sessions_missing", "plan_not_derivable", "no_hubspot_reference",
                     "some_code_in_no_list"):
            self.assertEqual(config.tier(code), config.IGNORE, code)

    def test_child_missing_is_an_alert_only_for_stages_that_should_have_a_child(self):
        self.assertEqual(runner.alert_codes_of(row(1, "Deposit Pending", ["child_missing", "plan_missing"])),
                         ["plan_missing"])
        self.assertEqual(runner.alert_codes_of(row(1, "Deposit Paid", ["child_missing"])), ["child_missing"])

    def test_classify_keeps_alerts_and_counts_the_count_tier_without_alerting_it(self):
        rows = [row(1, codes=["contact_missing", "contact_missing", "child_group_differs", "child_start_differs"]),
                row(2, codes=["plan_fee_over_10_percent", "child_end_differs"]), row(3, codes=["plan_matches"])]
        alerts, tiers = runner.classify(rows)
        self.assertEqual(alerts, {"1": ["contact_missing"]})
        self.assertEqual(tiers["alert"], {"contact_missing": 1})
        self.assertEqual(tiers["count"], {"child_start_differs": 1, "plan_fee_over_10_percent": 1, "child_end_differs": 1})


class ScopeTests(unittest.TestCase):
    def test_left_nursery_counts_only_within_45_days_of_its_end_date(self):
        def deal(end):
            return {"id": "1", "end_date": end}
        recent = (TODAY - timedelta(days=45)).isoformat()
        old = (TODAY - timedelta(days=46)).isoformat()
        self.assertTrue(runner.in_scope(deal(recent), "Left Nursery", TODAY))
        self.assertTrue(runner.in_scope(deal(TODAY.isoformat()), "left  nursery", TODAY))
        self.assertFalse(runner.in_scope(deal(old), "Left Nursery", TODAY))
        self.assertFalse(runner.in_scope(deal(None), "Left Nursery", TODAY))
        self.assertFalse(runner.in_scope(deal("not a date"), "Left Nursery", TODAY))
        for stage in ("Deposit Pending", "Active In School", "Gave Notice", "Frozen Account"):
            self.assertTrue(runner.in_scope(deal(old), stage, TODAY), stage)

    def test_scope_deals_filters_the_operations_search(self):
        stages = {"s1": ("Operations", "Active In School"), "s2": ("Operations", "Left Nursery"),
                  "s3": ("Sales", "Contract Signed")}
        deals = [{"id": "1", "dealstage": "s1"},
                 {"id": "2", "dealstage": "s2", "end_date": (TODAY - timedelta(days=10)).isoformat()},
                 {"id": "3", "dealstage": "s2", "end_date": (TODAY - timedelta(days=90)).isoformat()}]
        self.assertEqual([d["id"] for d in runner.scope_deals(FakeHub(deals, stages), TODAY)], ["1", "2"])


class DiffTests(unittest.TestCase):
    def test_new_resolved_and_persisting(self):
        previous = {"1": ["contact_missing", "plan_missing"], "2": ["plan_missing"]}
        current = {"1": ["contact_missing"], "3": ["plan_missing", "double_billing_risk"]}
        new, resolved, persisting = runner.diff(previous, current)
        self.assertEqual(new, {"plan_missing": ["3"], "double_billing_risk": ["3"]})
        self.assertEqual((resolved, persisting), (2, 1))

    def test_a_missing_or_damaged_state_file_is_the_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            self.assertIsNone(runner.load_state(path))
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(runner.load_state(path))
            runner.save_state(path, {"1": ["plan_missing"]}, TODAY)
            self.assertEqual(runner.load_state(path), {"1": ["plan_missing"]})

    def test_the_state_file_sits_beside_the_onboard_store(self):
        with mock.patch.dict(os.environ, {"ONBOARD_STORE_PATH": str(Path("somewhere") / "jobs.sqlite3")}):
            self.assertEqual(runner.state_path(), Path("somewhere") / config.STATE_FILE_NAME)


class RunTests(QualityCase):
    def test_the_first_run_and_baseline_post_one_line_with_no_ids(self):
        self.audit_returns([row(111, codes=["contact_missing"]), row(222, codes=["plan_missing", "contact_missing"])])
        result = self.go()                                           # no state yet: behaves as --baseline
        self.assertEqual(result["mode"], "baseline")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0].count("\n"), 0)
        self.assertIn("2 deal(s) checked, 3 open issue(s) across 2 code(s)", self.posts[0])
        for deal in ("111", "222"):
            self.assertNotIn(deal, self.posts[0])
        self.assertEqual(runner.load_state(self.state), {"111": ["contact_missing"], "222": ["contact_missing", "plan_missing"]})
        self.posts.clear()
        self.audit_returns([row(333, codes=["plan_missing"])])
        self.go(baseline=True)                                       # an explicit baseline records, no ids
        self.assertEqual(len(self.posts), 1)
        self.assertNotIn("333", self.posts[0])

    def test_nothing_is_posted_when_nothing_is_new(self):
        self.audit_returns([row(1, codes=["contact_missing"])])
        self.go()
        self.posts.clear()
        result = self.go()
        self.assertEqual((self.posts, result["posted"], result["new"], result["persisting"]), ([], False, 0, 1))

    def test_new_issues_are_posted_with_ids_resolved_and_persisting_counts(self):
        self.audit_returns([row(1, codes=["contact_missing"]), row(2, codes=["plan_missing"])])
        self.go()
        self.posts.clear()
        self.audit_returns([row(1, codes=["contact_missing"]), row(3, codes=["plan_missing"])])
        result = self.go()
        self.assertEqual((result["new"], result["resolved"], result["persisting"]), (1, 1, 1))
        self.assertEqual(len(self.posts), 1)
        self.assertIn("1 new, 1 resolved, 1 persisting", self.posts[0])
        self.assertIn("plan_missing: 1 new (1 open): 3", self.posts[0])

    def test_at_most_25_ids_per_code_then_and_n_more(self):
        self.audit_returns([row(0, codes=["contact_missing"])])
        self.go()
        self.posts.clear()
        self.audit_returns([row(i, codes=["plan_missing"]) for i in range(1, 31)])
        self.go()
        text = self.posts[0]
        self.assertIn("30 new", text)
        line = next(l for l in text.splitlines() if l.startswith("• plan_missing"))
        shown, tail = line.split(": ", 2)[2].rsplit(" and ", 1)
        self.assertEqual(len(shown.split(", ")), 25)
        self.assertEqual(tail, "5 more")

    def test_the_digest_always_posts_counts_by_tier(self):
        self.audit_returns([row(1, codes=["contact_missing", "child_start_differs"]),
                            row(2, codes=["plan_fee_over_10_percent"])])
        self.go()
        self.posts.clear()
        self.go(digest=True)                                         # nothing new, but a digest posts
        self.assertEqual(len(self.posts), 1)
        text = self.posts[0]
        self.assertIn("Alert codes: 1 issue(s) in 1 code(s)", text)
        self.assertIn("Counted only: 2 issue(s) in 2 code(s)", text)
        self.assertIn("child_start_differs: 1", text)

    def test_a_failed_post_leaves_the_state_file_byte_identical_and_the_cli_exits_non_zero(self):
        self.audit_returns([row(1, codes=["contact_missing"])])
        self.go()
        before = self.state.read_bytes()
        self.audit_returns([row(1, codes=["contact_missing"]), row(2, codes=["plan_missing"])])
        for failure in (RuntimeError("slack down"), False):          # raises, or returns False
            def broken(text, failure=failure):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            result = self.go(post=broken)
            self.assertEqual((result["posted"], result["postFailed"], result["new"]), (False, True, 1))
            self.assertEqual(self.state.read_bytes(), before)
        self.assertTrue(list(self.reports.glob("quality_*.csv")))     # the CSV is written either way
        args = argparse.Namespace(baseline=False, digest=False)
        with mock.patch.object(runner, "run", return_value={"postFailed": True}), \
                mock.patch("actions.urn_lookup.runner._public_client"), \
                mock.patch("core.rest_client.RestClient"), \
                mock.patch.object(audit, "ReadOnlyHubSpot"):
            with self.assertRaises(SystemExit) as ctx:
                cli.handle(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_a_good_post_advances_the_state_and_nothing_to_post_still_advances_it(self):
        self.audit_returns([row(1, codes=["contact_missing"]), row(2, codes=["plan_missing"])])
        self.go()
        self.audit_returns([row(1, codes=["contact_missing"]), row(3, codes=["plan_missing"])])
        result = self.go()                                           # new issue, post succeeds
        self.assertEqual((result["posted"], result["postFailed"]), (True, False))
        self.assertEqual(runner.load_state(self.state), {"1": ["contact_missing"], "3": ["plan_missing"]})
        self.posts.clear()
        self.audit_returns([row(1, codes=["contact_missing"])])      # only a resolution: nothing to post
        result = self.go()
        self.assertEqual((self.posts, result["posted"], result["postFailed"]), ([], False, False))
        self.assertEqual(runner.load_state(self.state), {"1": ["contact_missing"]})

    def test_a_failed_post_then_a_good_run_announces_the_same_new_issues(self):
        self.audit_returns([row(1, codes=["contact_missing"])])
        self.go()
        self.audit_returns([row(1, codes=["contact_missing"]), row(2, codes=["plan_missing"])])

        def broken(text):
            raise RuntimeError("slack down")
        self.go(post=broken)
        self.posts.clear()
        result = self.go()
        self.assertEqual((result["new"], result["persisting"], result["posted"]), (1, 1, True))
        self.assertIn("plan_missing: 1 new", self.posts[0])

    def test_a_failed_first_run_post_leaves_no_state_so_the_next_run_is_a_first_run_again(self):
        self.audit_returns([row(1, codes=["contact_missing"])])

        def broken(text):
            raise RuntimeError("slack down")
        result = self.go(post=broken)
        self.assertEqual((result["mode"], result["postFailed"]), ("baseline", True))
        self.assertFalse(self.state.exists())
        self.assertEqual(self.go()["mode"], "baseline")

    def test_an_audit_failure_posts_a_failure_notice_with_the_code_only_and_raises(self):
        self.audit_returns([row(1, codes=["contact_missing"])])
        self.go()
        self.posts.clear()
        with mock.patch.object(runner.audit, "run", side_effect=ValueError("secret detail 12345")):
            with self.assertRaises(ValueError):
                self.go()
        self.assertEqual(len(self.posts), 1)
        self.assertIn("quality check failed", self.posts[0])
        self.assertIn("ValueError", self.posts[0])
        self.assertNotIn("secret", self.posts[0])
        self.assertEqual(runner.load_state(self.state), {"1": ["contact_missing"]})    # state left alone

    def test_the_cli_exits_non_zero_when_the_audit_fails(self):
        args = argparse.Namespace(baseline=False, digest=False)
        with mock.patch.object(runner, "run", side_effect=RuntimeError("x")), \
                mock.patch("actions.urn_lookup.runner._public_client"), \
                mock.patch("core.rest_client.RestClient"), \
                mock.patch.object(audit, "ReadOnlyHubSpot"):
            with self.assertRaises(SystemExit) as ctx:
                cli.handle(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_the_csv_has_the_audit_columns_and_only_the_last_8_are_kept(self):
        self.reports.mkdir()
        for day in range(1, 11):
            (self.reports / f"quality_2026-09-{day:02d}.csv").write_text("x", encoding="utf-8")
        self.audit_returns([row(1, codes=["contact_missing", "child_group_differs"])])
        self.go()
        kept = sorted(p.name for p in self.reports.glob("quality_*.csv"))
        self.assertEqual(len(kept), 8)
        self.assertEqual(kept[-1], "quality_2026-10-27.csv")
        self.assertNotIn("quality_2026-09-01.csv", kept)
        with open(self.reports / "quality_2026-10-27.csv", newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["dealId", "siteCode", "stage", "childId", "found_via", "codes"])
        self.assertEqual(rows[1][0], "1")


class NothingCanWriteTests(unittest.TestCase):
    FORBIDDEN_NAMES = ("write_deal_properties", "write_plan_uploaded", "HubSpotDealWriter", "famly_writes",
                       "bill_payer_writes", "create_contact", "create_child", "add_contact_relation",
                       "link_recipient", "send_once")

    def test_the_package_imports_and_calls_no_write_function(self):
        for path in Path("actions/onboard_quality").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                    {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                    {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            for forbidden in self.FORBIDDEN_NAMES:
                self.assertNotIn(forbidden, names, f"{path.name}: {forbidden}")
            text = path.read_text(encoding="utf-8")
            for verb in ('"PATCH"', '"POST"', '"PUT"', '"DELETE"', ".patch(", ".post(", ".put(", ".delete("):
                self.assertNotIn(verb, text, f"{path.name}: {verb}")

    def test_the_clients_the_command_builds_are_the_audits_read_only_wrappers(self):
        hub = audit.ReadOnlyHubSpot(token="t")
        for method, path in (("PATCH", "/crm/v3/objects/deals/1"), ("POST", "/crm/v3/objects/deals"),
                             ("DELETE", "/crm/v3/objects/deals/1")):
            with self.assertRaises(audit.NotReadOnly):
                hub._call(method, path, json_body={})
        with self.assertRaises(audit.NotReadOnly):
            audit.ReadOnlyGraphQL(mock.Mock()).execute(Path("actions/onboard/create_contact.graphql"), {}, "X")
        self.assertFalse(hasattr(audit.ReadOnlyRest(mock.Mock()), "post"))
        source = Path("actions/onboard_quality/cli.py").read_text(encoding="utf-8")
        for wrapper in ("ReadOnlyHubSpot", "ReadOnlyGraphQL", "ReadOnlyRest"):
            self.assertIn(f"audit.{wrapper}(", source)

    def test_no_value_reaches_the_output(self):
        # The messages carry deal references, counts and codes only.
        from actions.onboard import messages
        text = messages.quality_report({"plan_missing": ["1", "2"]}, {"plan_missing": 2}, 0, 3, 25)
        self.assertNotIn("@", text)
        self.assertEqual(messages.quality_failed("ValueError"),
                         "Onboarding quality check failed (code: ValueError). Nothing was recorded; run it again.")


if __name__ == "__main__":
    unittest.main()
