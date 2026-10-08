"""Tests for the pull-references combined command.

Run from the project root:

    python -m unittest discover -s tests -v

Runs actions/pull_sessions + actions/pull_groups + actions/pull_products
together. These tests stub each pull's own `pull_all`/`write_catalogue` so
they exercise ONLY the orchestration in actions/pull_references/runner.py --
each pull's own behaviour is covered by its own test file
(test_pull_sessions.py, test_pull_groups.py, test_pull_products.py).
"""

import unittest
from unittest import mock

from actions.pull_references import runner as pull_references


class FakePull:
    """Stands in for one pull's runner module."""

    def __init__(self, result="a-result", backup="a-backup-path", raises=None):
        self._result = result
        self._backup = backup
        self._raises = raises
        self.pull_all_calls = []
        self.write_catalogue_calls = []

    def pull_all(self, client=None, institutions=None):
        self.pull_all_calls.append(institutions)
        if self._raises is not None:
            raise self._raises
        return self._result

    def write_catalogue(self, result, path=None):
        self.write_catalogue_calls.append(result)
        return self._backup

    def summary_payload(self, result, *, backup=None, dry_run=False):
        return {"result": result, "backup": backup, "dryRun": dry_run}


class PullReferencesTestCase(unittest.TestCase):
    def setUp(self):
        self.sessions = FakePull(result="sessions-result", backup="sessions.bak")
        self.groups = FakePull(result="groups-result", backup="groups.bak")
        self.products = FakePull(result="products-result", backup="products.bak")

        patcher = mock.patch.object(
            pull_references,
            "PULLS",
            {
                "sessions": self.sessions,
                "groups": self.groups,
                "products": self.products,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class RunsAllThreeTests(PullReferencesTestCase):
    def test_every_pull_is_run_and_written(self):
        outcome = pull_references.pull_all_references()

        self.assertEqual(outcome.results["sessions"], "sessions-result")
        self.assertEqual(outcome.results["groups"], "groups-result")
        self.assertEqual(outcome.results["products"], "products-result")
        self.assertEqual(outcome.backups["sessions"], "sessions.bak")
        self.assertEqual(self.sessions.write_catalogue_calls, ["sessions-result"])
        self.assertEqual(self.groups.write_catalogue_calls, ["groups-result"])
        self.assertEqual(self.products.write_catalogue_calls, ["products-result"])
        self.assertEqual(outcome.errors, {})

    def test_institutions_are_passed_through_to_every_pull(self):
        pull_references.pull_all_references(institutions=["HDCITY"])

        self.assertEqual(self.sessions.pull_all_calls, [["HDCITY"]])
        self.assertEqual(self.groups.pull_all_calls, [["HDCITY"]])
        self.assertEqual(self.products.pull_all_calls, [["HDCITY"]])

    def test_each_pull_is_individually_still_callable(self):
        # Each pull module remains an ordinary, independently importable
        # runner -- pull_references does not wrap or replace them.
        from actions.pull_groups import runner as real_pull_groups
        from actions.pull_products import runner as real_pull_products
        from actions.pull_sessions import runner as real_pull_sessions

        for module in (real_pull_sessions, real_pull_groups, real_pull_products):
            self.assertTrue(hasattr(module, "pull_all"))
            self.assertTrue(hasattr(module, "write_catalogue"))
            self.assertTrue(hasattr(module, "summary_payload"))


class DryRunTests(PullReferencesTestCase):
    def test_dry_run_never_calls_write_catalogue_on_any_pull(self):
        outcome = pull_references.pull_all_references(dry_run=True)

        self.assertEqual(self.sessions.write_catalogue_calls, [])
        self.assertEqual(self.groups.write_catalogue_calls, [])
        self.assertEqual(self.products.write_catalogue_calls, [])
        self.assertIsNone(outcome.backups["sessions"])
        self.assertIsNone(outcome.backups["groups"])
        self.assertIsNone(outcome.backups["products"])

    def test_dry_run_still_runs_every_pull(self):
        outcome = pull_references.pull_all_references(dry_run=True)

        self.assertEqual(len(self.sessions.pull_all_calls), 1)
        self.assertEqual(len(self.groups.pull_all_calls), 1)
        self.assertEqual(len(self.products.pull_all_calls), 1)
        self.assertEqual(outcome.results["sessions"], "sessions-result")


class IsolationTests(PullReferencesTestCase):
    def test_one_pull_failing_entirely_does_not_stop_the_others(self):
        self.groups._raises = RuntimeError("groups source is down")

        outcome = pull_references.pull_all_references()

        self.assertIn("groups", outcome.errors)
        self.assertIn("groups source is down", outcome.errors["groups"])
        # The other two still ran and were written.
        self.assertEqual(outcome.results["sessions"], "sessions-result")
        self.assertEqual(outcome.results["products"], "products-result")
        self.assertEqual(self.sessions.write_catalogue_calls, ["sessions-result"])
        self.assertEqual(self.products.write_catalogue_calls, ["products-result"])
        # The failed pull was never written.
        self.assertEqual(self.groups.write_catalogue_calls, [])

    def test_an_unknown_institution_filter_is_isolated_per_pull(self):
        from integrations.catalogue import UnknownInstitutionError

        self.sessions._raises = UnknownInstitutionError("unknown institution code(s): NOPE")

        outcome = pull_references.pull_all_references(institutions=["NOPE"])

        self.assertIn("sessions", outcome.errors)
        self.assertIn("NOPE", outcome.errors["sessions"])
        # groups/products still ran with the same filter.
        self.assertEqual(outcome.results["groups"], "groups-result")
        self.assertEqual(outcome.results["products"], "products-result")

    def test_multiple_pulls_failing_are_all_reported(self):
        self.groups._raises = RuntimeError("groups down")
        self.products._raises = RuntimeError("products down")

        outcome = pull_references.pull_all_references()

        self.assertEqual(set(outcome.errors), {"groups", "products"})
        self.assertEqual(outcome.results, {"sessions": "sessions-result"})


class SummaryPayloadTests(PullReferencesTestCase):
    def test_the_summary_has_one_entry_per_pull(self):
        outcome = pull_references.pull_all_references()
        summary = pull_references.summary_payload(outcome)

        self.assertEqual(summary["dryRun"], False)
        self.assertEqual(set(summary["pulls"]), {"sessions", "groups", "products"})
        self.assertEqual(
            summary["pulls"]["sessions"],
            {"result": "sessions-result", "backup": "sessions.bak", "dryRun": False},
        )

    def test_a_failed_pull_shows_up_as_a_failed_entry(self):
        self.products._raises = RuntimeError("products down")

        outcome = pull_references.pull_all_references()
        summary = pull_references.summary_payload(outcome)

        self.assertEqual(summary["pulls"]["products"], {"failed": "products down"})
        # The other two still have their normal summary shape.
        self.assertIn("result", summary["pulls"]["sessions"])

    def test_summary_dry_run_flag_is_passed_through(self):
        outcome = pull_references.pull_all_references(dry_run=True)
        summary = pull_references.summary_payload(outcome, dry_run=True)

        self.assertTrue(summary["dryRun"])
        self.assertTrue(summary["pulls"]["sessions"]["dryRun"])


class FakePullWithExtras(FakePull):
    """A pull that has a SECONDARY output, like sessions and products."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_extras_calls = []

    def write_extras(self, result):
        self.write_extras_calls.append(result)


class WriteExtrasHookTests(unittest.TestCase):
    def setUp(self):
        self.with_extras = FakePullWithExtras(result="extras-result")
        self.without = FakePull(result="plain-result")
        patcher = mock.patch.object(
            pull_references,
            "PULLS",
            {"sessions": self.with_extras, "groups": self.without},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_hook_is_called_after_write_catalogue_when_a_pull_has_one(self):
        pull_references.pull_all_references()

        self.assertEqual(self.with_extras.write_catalogue_calls, ["extras-result"])
        self.assertEqual(self.with_extras.write_extras_calls, ["extras-result"])

    def test_a_pull_without_the_hook_is_unaffected(self):
        outcome = pull_references.pull_all_references()

        self.assertEqual(self.without.write_catalogue_calls, ["plain-result"])
        self.assertEqual(outcome.errors, {})

    def test_a_dry_run_calls_neither_writer(self):
        pull_references.pull_all_references(dry_run=True)

        self.assertEqual(self.with_extras.write_catalogue_calls, [])
        self.assertEqual(self.with_extras.write_extras_calls, [])

    def test_a_failed_pull_gets_no_extras_written(self):
        self.with_extras._raises = RuntimeError("down")

        outcome = pull_references.pull_all_references()

        self.assertIn("sessions", outcome.errors)
        self.assertEqual(self.with_extras.write_extras_calls, [])
        # ...and the other pull still ran.
        self.assertEqual(self.without.write_catalogue_calls, ["plain-result"])


if __name__ == "__main__":
    unittest.main()
