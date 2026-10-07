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

FULL_ADDON_PRODUCTS = {
    "mealsProductId": "55555555-0000-0000-0000-000000000005",
    "activitiesProductId": "66666666-0000-0000-0000-000000000006",
}
HALF_ADDON_PRODUCTS = {
    "halfMealsProductId": "77777777-0000-0000-0000-000000000007",
    "halfActivitiesProductId": "88888888-0000-0000-0000-000000000008",
}
ADDON_PRODUCTS = {**FULL_ADDON_PRODUCTS, **HALF_ADDON_PRODUCTS}

CATALOGUE = {
    "institutions": {
        INSTITUTION: {
            "ruleGroupId": "01RULEGROUP0000000000000000",
            "schedules": {
                "all_year_round": ALL_YEAR_ROUND,
                "term_only": TERM_ONLY,
            },
            "addonProducts": ADDON_PRODUCTS,
        },
        # Full pair only: no "(1/2)" products resolved (yet) for it.
        "HDNOHALF": {
            "ruleGroupId": "01RULEGROUP2222222222222222",
            "schedules": {"all_year_round": ALL_YEAR_ROUND},
            "addonProducts": FULL_ADDON_PRODUCTS,
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


# --------------------------------------------------------------------------- #
# resolve_addon_products -- separate accessor, written by actions/pull_products
# via exact product-title match (see that module's docstring).
# --------------------------------------------------------------------------- #
class ResolveAddonProductsTests(InstitutionDefaultsTestCase):
    def test_a_known_institution_returns_the_full_and_half_ids(self):
        result = idf.resolve_addon_products(INSTITUTION)
        self.assertEqual(result, ADDON_PRODUCTS)

    def test_no_half_products_is_not_an_error_and_reads_as_none(self):
        result = idf.resolve_addon_products("HDNOHALF")

        self.assertEqual(result["mealsProductId"], FULL_ADDON_PRODUCTS["mealsProductId"])
        self.assertEqual(
            result["activitiesProductId"], FULL_ADDON_PRODUCTS["activitiesProductId"]
        )
        self.assertIsNone(result["halfMealsProductId"])
        self.assertIsNone(result["halfActivitiesProductId"])

    def test_one_half_product_resolved_reads_the_other_as_none(self):
        only_meals = {
            "institutions": {
                INSTITUTION: {
                    "addonProducts": {
                        **FULL_ADDON_PRODUCTS,
                        "halfMealsProductId": HALF_ADDON_PRODUCTS["halfMealsProductId"],
                        "halfActivitiesProductId": "",
                    }
                }
            }
        }
        path = Path(self._tmp.name) / "one_half.json"
        path.write_text(json.dumps(only_meals), encoding="utf-8")

        with mock.patch.dict("os.environ", {"INSTITUTION_DEFAULTS_FILE": str(path)}):
            result = idf.resolve_addon_products(INSTITUTION)

        self.assertEqual(result["halfMealsProductId"], HALF_ADDON_PRODUCTS["halfMealsProductId"])
        self.assertIsNone(result["halfActivitiesProductId"])

    def test_a_missing_FULL_id_is_still_an_error_even_with_halves_present(self):
        broken = {
            "institutions": {
                INSTITUTION: {"addonProducts": {**HALF_ADDON_PRODUCTS, "mealsProductId": ""}}
            }
        }
        path = Path(self._tmp.name) / "no_full.json"
        path.write_text(json.dumps(broken), encoding="utf-8")

        with mock.patch.dict("os.environ", {"INSTITUTION_DEFAULTS_FILE": str(path)}):
            with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
                idf.resolve_addon_products(INSTITUTION)

        self.assertIn("mealsProductId", str(ctx.exception))
        self.assertNotIn("half", str(ctx.exception).lower())

    def test_the_titles_match_what_pull_products_looks_for(self):
        self.assertEqual(
            idf.ADDON_PRODUCT_TITLES,
            {
                "mealsProductId": "Meals & Snacks",
                "activitiesProductId": "Educational Activities & Extras",
                "halfMealsProductId": "(1/2) Meals & Snacks",
                "halfActivitiesProductId": "(1/2) Educational Activities & Extras",
            },
        )

    def test_matching_is_case_insensitive_and_trimmed(self):
        for variant in ("hdcity", "HdCity", "  HDCITY  "):
            result = idf.resolve_addon_products(variant)
            self.assertEqual(result, ADDON_PRODUCTS)

    def test_an_unknown_institution_raises_naming_it(self):
        with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
            idf.resolve_addon_products("HDXYZ")
        self.assertIn("HDXYZ", str(ctx.exception))

    def test_an_institution_with_no_addon_products_configured_raises(self):
        # HDPARTIAL has a ruleGroupId/schedules but no addonProducts -- this
        # accessor must not require the other one's fields at all.
        with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
            idf.resolve_addon_products("HDPARTIAL")

        message = str(ctx.exception)
        self.assertIn("HDPARTIAL", message)
        self.assertIn("addonProducts", message)

    def test_a_missing_id_in_addon_products_raises_naming_it(self):
        broken = {
            "institutions": {
                INSTITUTION: {
                    "addonProducts": {
                        "mealsProductId": ADDON_PRODUCTS["mealsProductId"],
                        "activitiesProductId": "",
                    }
                }
            }
        }
        path = Path(self._tmp.name) / "broken_addons.json"
        path.write_text(json.dumps(broken), encoding="utf-8")

        with mock.patch.dict("os.environ", {"INSTITUTION_DEFAULTS_FILE": str(path)}):
            with self.assertRaises(idf.InstitutionDefaultsError) as ctx:
                idf.resolve_addon_products(INSTITUTION)

        self.assertIn("activitiesProductId", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
