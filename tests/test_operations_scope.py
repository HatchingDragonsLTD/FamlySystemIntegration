"""The shared --operations scope: which Operations deals a whole-pipeline run covers. Fakes only."""

import argparse
import os
import unittest
from datetime import date
from unittest import mock

from actions.hubspot_plan_uploaded import runner as plan_uploaded
from actions.onboard_audit import cli as audit_cli
from actions.onboard_audit import runner as audit
from actions.onboard_audit import scope

STAGES = {"s-active": ("Operations", "Active In School"), "s-left": ("Operations", "Left Nursery"),
          "s-dep": ("Operations", "Deposit Pending")}
CUTOFF = date(2026, 1, 1)


def deal(deal_id, end_date=None, stage="s-active"):
    return {"id": deal_id, "end_date": end_date, "dealstage": stage}


def apply(deals, **kwargs):
    return scope.apply(deals, STAGES, scope.Scope(CUTOFF), **kwargs)


class CutoffTests(unittest.TestCase):
    def test_the_day_before_the_day_of_and_the_day_after_the_cutoff(self):
        kept, counts = apply([deal("1", "2025-12-31"), deal("2", "2026-01-01"), deal("3", "2026-01-02")])
        self.assertEqual([d["id"] for d in kept], ["2", "3"])
        self.assertEqual(counts["byReason"], {scope.EXCLUDED_BEFORE: 1, scope.INCLUDED_ON_OR_AFTER: 2})
        self.assertEqual((counts["included"], counts["excluded"], counts["cutoff"]), (2, 1, "2026-01-01"))

    def test_hubspot_date_formats_are_read_the_way_the_pipeline_reads_them(self):
        epoch_2025_12_31 = "1767139200000"                                   # midnight UTC, 2025-12-31
        epoch_2026_01_01 = "1767225600000"
        kept, _counts = apply([deal("1", epoch_2025_12_31), deal("2", epoch_2026_01_01),
                               deal("3", "2026-01-01T00:00:00Z")])
        self.assertEqual([d["id"] for d in kept], ["2", "3"])

    def test_an_empty_end_date_is_in_scope_unless_the_stage_is_left_nursery(self):
        kept, counts = apply([deal("1", None), deal("2", "", "s-dep"), deal("3", "  ", "s-left"),
                              deal("4", None, "s-left"), deal("5", None, "stage-not-in-the-list")])
        self.assertEqual([d["id"] for d in kept], ["1", "2", "5"])
        self.assertEqual(counts["byReason"], {scope.INCLUDED_NO_LEAVE_DATE: 3, scope.EXCLUDED_LEFT_NO_DATE: 2})

    def test_a_left_nursery_deal_with_a_recent_end_date_is_in_scope(self):
        kept, _ = apply([deal("1", "2026-03-01", "s-left")])
        self.assertEqual(len(kept), 1)

    def test_an_unparseable_end_date_is_excluded_reported_and_never_guessed(self):
        with mock.patch.object(scope.logger, "warning") as warning:
            kept, counts = apply([deal("1", "next summer"), deal("2", "31/12/2026"), deal("3", "2026-06-01")])
        self.assertEqual([d["id"] for d in kept], ["3"])
        self.assertEqual(counts["byReason"][scope.EXCLUDED_UNPARSEABLE], 2)
        self.assertEqual(counts["excluded"], 2)
        logged = warning.call_args.args
        self.assertEqual((logged[1], logged[2]), (2, "1, 2"))                # counts, then ids only


class FlagTests(unittest.TestCase):
    def test_the_default_is_the_first_of_january_2026(self):
        self.assertEqual(scope.resolve(environ={}), scope.Scope(date(2026, 1, 1)))

    def test_the_variable_sets_the_cutoff_and_the_flag_overrides_it(self):
        env = {scope.ENV_VAR: "2026-06-01"}
        self.assertEqual(scope.resolve(environ=env).cutoff, date(2026, 6, 1))
        self.assertEqual(scope.resolve("2025-01-01", environ=env).cutoff, date(2025, 1, 1))

    def test_all_dates_restores_the_old_behaviour_and_keeps_every_deal(self):
        chosen = scope.resolve(all_dates=True, environ={scope.ENV_VAR: "2026-06-01"})
        kept, counts = scope.apply([deal("1", "2001-01-01"), deal("2", "garbage"), deal("3", None, "s-left")],
                                   STAGES, chosen)
        self.assertEqual(len(kept), 3)
        self.assertEqual((counts["cutoff"], counts["byReason"]), ("all_dates", {scope.INCLUDED_ALL_DATES: 3}))

    def test_a_bad_flag_a_bad_variable_or_both_flags_are_refused(self):
        for call in (lambda: scope.resolve("01/01/2026"), lambda: scope.resolve("2026-1-1"),
                     lambda: scope.resolve(environ={scope.ENV_VAR: "soon"}),
                     lambda: scope.resolve("2026-01-01", all_dates=True)):
            with self.assertRaises(scope.ScopeError):
                call()


class WhoUsesItTests(unittest.TestCase):
    def test_the_audit_cli_passes_the_scope_and_refuses_the_flags_without_operations(self):
        args = argparse.Namespace(file=None, operations=True, stage=None, out=None, codes=None, include_left=False,
                                  leave_after="2026-03-01", all_dates=False)
        with mock.patch.object(audit, "run", return_value=({}, [])) as run, \
                mock.patch("actions.urn_lookup.runner._public_client"), mock.patch("core.rest_client.RestClient"), \
                mock.patch.object(audit, "ReadOnlyHubSpot"):
            audit_cli.handle(args)
        self.assertEqual(run.call_args.kwargs["scope"], scope.Scope(date(2026, 3, 1)))
        args.operations, args.file = False, "x.txt"
        with self.assertRaises(SystemExit):
            audit_cli.handle(args)

    def test_the_audit_summary_reports_the_counts_and_every_other_caller_is_unscoped(self):
        hub = mock.Mock(calls=1)
        hub.stage_names.return_value = STAGES
        rows = [{"id": "1", "end_date": "2025-01-01", "dealstage": "s-active"},
                {"id": "2", "end_date": "2026-05-01", "dealstage": "s-active"}]
        with mock.patch.object(audit, "operations_deals", return_value=rows), \
                mock.patch("actions.urn_lookup.runner._all_institution_ids", return_value=[]):
            summary, _rows = audit.run(hub, mock.Mock(calls=0), mock.Mock(calls=0), operations=True,
                                       scope=scope.Scope(CUTOFF))
            unscoped, _ = audit.run(hub, mock.Mock(calls=0), mock.Mock(calls=0), operations=True)
        self.assertEqual(summary["dealsRead"], 1)
        self.assertEqual(summary["scope"]["included"], 1)
        self.assertEqual(summary["scope"]["byReason"], {scope.EXCLUDED_BEFORE: 1, scope.INCLUDED_ON_OR_AFTER: 1})
        self.assertEqual(unscoped["dealsRead"], 2)
        self.assertNotIn("scope", unscoped)

    def test_the_plan_uploaded_reconcile_uses_the_same_scope(self):
        class Writer:
            def stage_names(self):
                return STAGES

            def search_deals(self, filters, properties, limit=100):
                self.properties = properties
                return [{"id": "1", "famly_childid": "c1", "famly_plan_uploaded": None, "end_date": "2025-01-01",
                         "dealstage": "s-active"},
                        {"id": "2", "famly_childid": "c2", "famly_plan_uploaded": None, "end_date": None,
                         "dealstage": "s-active"}]
        writer, report = Writer(), {}
        states = plan_uploaded.operations_states(writer, scope.Scope(CUTOFF), report)
        self.assertEqual([s[0] for s in states], ["2"])
        self.assertEqual(report["scope"]["excluded"], 1)
        self.assertIn("end_date", writer.properties)
        self.assertEqual(len(plan_uploaded.operations_states(writer)), 2)         # no scope: as before


if __name__ == "__main__":
    unittest.main()
