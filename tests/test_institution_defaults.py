"""Tests for integrations/institution_defaults.py -- the resolver
hubspot_flatten uses in place of HubSpot sending ruleGroupId/billingProfileId/
attendanceScheduleId/weeksOfCare/billingId/billingTitle/billingInvoices
directly.

Run from the project root:

    python -m unittest discover -s tests -v

No Famly, no network: reads a temp JSON file via INSTITUTION_DEFAULTS_FILE,
the same convention as SESSION_CATALOGUE_FILE.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations import institution_defaults as idf

INSTITUTION = "HDCITY"

ALL_YEAR_ROUND = {
    "billingProfileId": "11111111-0000-0000-0000-000000000001",
    "attendanceScheduleId": "22222222-0000-0000-0000-000000000002",
    "weeksOfCare": 51,
    "billingId": "ANNUALIZED_V2",
    "billingTitle": "Full Year",
    "billingInvoices": 12,
}

TERM_ONLY = {
    "billingProfileId": "33333333-0000-0000-0000-000000000003",
    "attendanceScheduleId": "44444444-0000-0000-0000-000000000004",
    "weeksOfCare": 38,
    "billingId": "ANNUALIZED_V2",
    "billingTitle": "Term Only",
    "billingInvoices": 12,
}

CATALOGUE = {
    "institutions": {
        INSTITUTION: {
            "ruleGroupId": "01RULEGROUP0000000000000000",
            "schedules": {
                "all_year_round": ALL_YEAR_ROUND,
                "term_only": TERM_ONLY,
            },
        },
        # An institution with a rule group but only ONE schedule captured --
        # e.g. a fresh pull that failed on the other bucket's anomaly.
        "HDPARTIAL": {
            "ruleGroupId": "01RULEGROUP1111111111111111",
            "schedules": {"all_year_round": ALL_YEAR_ROUND},
        },
    }
}


class InstitutionDefaultsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        path = Path(self._tmp.name) / "institution_defaults.json"
        path.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        env = mock.patch.dict("os.environ", {"INSTITUTION_DEFAULTS_FILE": str(path)})
        env.start()
        self.addCleanup(env.stop)


class ScheduleKeyTests(unittest.TestCase):
    def test_false_and_falsy_values_resolve_to_all_year_round(self):
        for value in (False, None, "", 0):
            self.assertEqual(idf.schedule_key(value), idf.SCHEDULE_ALL_YEAR_ROUND)

    def test_true_and_truthy_values_resolve_to_term_only(self):
        for value in (True, "yes", 1):
            self.assertEqual(idf.schedule_key(value), idf.SCHEDULE_TERM_ONLY)


class ResolveDefaultsTests(InstitutionDefaultsTestCase):
    def test_a_known_institution_and_schedule_returns_the_right_five_values(self):
        result = idf.resolve_defaults(INSTITUTION, idf.SCHEDULE_ALL_YEAR_ROUND)

        self.assertEqual(
            result,
            {
                "ruleGroupId": "01RULEGROUP0000000000000000",
                "billingProfileId": ALL_YEAR_ROUND["billingProfileId"],
                "attendanceScheduleId": ALL_YEAR_ROUND["attendanceScheduleId"],
                "weeksOfCare": ALL_YEAR_ROUND["weeksOfCare"],
                "billingId": ALL_YEAR_ROUND["billingId"],
                "billingTitle": ALL_YEAR_ROUND["billingTitle"],
                "billingInvoices": ALL_YEAR_ROUND["billingInvoices"],
            },
        )

    def test_the_other_schedule_bucket_returns_its_own_values(self):
        result = idf.resolve_defaults(INSTITUTION, idf.SCHEDULE_TERM_ONLY)

        self.assertEqual(result["billingProfileId"], TERM_ONLY["billingProfileId"])
        self.assertEqual(result["weeksOfCare"], TERM_ONLY["weeksOfCare"])
        # ruleGroupId does not vary by schedule.
        self.assertEqual(result["ruleGroupId"], "01RULEGROUP0000000000000000")

    def test_matching_is_case_insensitive_and_trimmed(self):
        for variant in ("hdcity", "HdCity", "  HDCITY  "):
            result = idf.resolve_defaults(variant, idf.SCHEDULE_ALL_YEAR_ROUND)
            self.assertEqual(result["ruleGroupId"], "01RULEGROUP0000000000000000")

    def test_an_unknown_institution_raises_naming_it(self):
        with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
            idf.resolve_defaults("HDXYZ", idf.SCHEDULE_ALL_YEAR_ROUND)
        self.assertIn("HDXYZ", str(ctx.exception))

    def test_an_institution_missing_the_requested_schedule_bucket_raises_naming_it(self):
        with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
            idf.resolve_defaults("HDPARTIAL", idf.SCHEDULE_TERM_ONLY)

        message = str(ctx.exception)
        self.assertIn("HDPARTIAL", message)
        self.assertIn("term_only", message)

    def test_an_institution_missing_the_requested_schedule_bucket_still_resolves_the_other(self):
        result = idf.resolve_defaults("HDPARTIAL", idf.SCHEDULE_ALL_YEAR_ROUND)
        self.assertEqual(result["billingProfileId"], ALL_YEAR_ROUND["billingProfileId"])

    def test_an_invalid_schedule_name_raises(self):
        with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
            idf.resolve_defaults(INSTITUTION, "bogus")
        self.assertIn("bogus", str(ctx.exception))

    def test_a_missing_field_in_the_bucket_raises_naming_it(self):
        broken = {
            "institutions": {
                INSTITUTION: {
                    "ruleGroupId": "01RULEGROUP0000000000000000",
                    "schedules": {
                        "all_year_round": {**ALL_YEAR_ROUND, "billingProfileId": ""},
                    },
                }
            }
        }
        path = Path(self._tmp.name) / "broken.json"
        path.write_text(json.dumps(broken), encoding="utf-8")

        with mock.patch.dict("os.environ", {"INSTITUTION_DEFAULTS_FILE": str(path)}):
            with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
                idf.resolve_defaults(INSTITUTION, idf.SCHEDULE_ALL_YEAR_ROUND)

        self.assertIn("billingProfileId", str(ctx.exception))

    def test_a_missing_rule_group_id_raises_naming_the_institution(self):
        broken = {
            "institutions": {
                INSTITUTION: {
                    "schedules": {"all_year_round": ALL_YEAR_ROUND},
                }
            }
        }
        path = Path(self._tmp.name) / "no_rule_group.json"
        path.write_text(json.dumps(broken), encoding="utf-8")

        with mock.patch.dict("os.environ", {"INSTITUTION_DEFAULTS_FILE": str(path)}):
            with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
                idf.resolve_defaults(INSTITUTION, idf.SCHEDULE_ALL_YEAR_ROUND)

        self.assertIn("ruleGroupId", str(ctx.exception))
        self.assertIn(INSTITUTION, str(ctx.exception))


class MissingFileTests(unittest.TestCase):
    def test_a_missing_file_raises(self):
        with mock.patch.dict(
            "os.environ", {"INSTITUTION_DEFAULTS_FILE": "/nope/missing.json"}
        ):
            with self.assertRaises(idf.InstitutionDefaultsError):
                idf.resolve_defaults(INSTITUTION, idf.SCHEDULE_ALL_YEAR_ROUND)


if __name__ == "__main__":
    unittest.main()
