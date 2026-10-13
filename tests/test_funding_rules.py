"""Tests for integrations/funding_rules.py and reference/funding_rules.json.

Run from the project root:

    python -m unittest tests.test_funding_rules -v

Funding settings come from the server, per institution. A gap is a hard error
naming the exact path, and nothing ever falls back to another site's numbers.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations import funding_rules
from integrations.funding_rules import FundingRulesError, resolve_funding

RULES = {
    "fundingMethod": "ASSISTED",
    "institutions": {
        "HDCW": {
            "maxFundedMinutes": 660,
            "fundedWeeklyHours": {
                "30": {"all_year_round": 24.58823529, "term_only": 33},
                "15": {"all_year_round": 12.29411765, "term_only": 16.5},
            },
        },
        "HDCITY": {"maxFundedMinutes": 660, "fundedWeeklyHours": None},
        "HDWEST": {"maxFundedMinutes": 600, "fundedWeeklyHours": None},
    },
}

PROJECT = Path(__file__).resolve().parent.parent


class RulesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "funding_rules.json"
        self.write(RULES)
        env = mock.patch.dict("os.environ", {"FUNDING_RULES_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, rules):
        self.path.write_text(json.dumps(rules), encoding="utf-8")

    def assert_error(self, expected, *args):
        with self.assertRaises(FundingRulesError) as caught:
            resolve_funding(*args)
        self.assertEqual(str(caught.exception), expected)


class ResolveTests(RulesTestCase):
    def test_hdcws_four_hours_values(self):
        cases = [
            ("30", False, 24.58823529),
            ("30", True, 33),
            ("15", False, 12.29411765),
            ("15", True, 16.5),
        ]
        for entitlement, term_only, hours in cases:
            with self.subTest(entitlement=entitlement, term_only=term_only):
                result = resolve_funding("HDCW", entitlement, term_only)
                self.assertEqual(result["fundingHours"], hours)

    def test_it_returns_the_method_hours_and_cap(self):
        self.assertEqual(
            resolve_funding("HDCW", "30", True),
            {"fundingMethod": "ASSISTED", "fundingHours": 33, "maxFundedMinutes": 660},
        )

    def test_each_sites_cap(self):
        rules = json.loads(json.dumps(RULES))
        for site in ("HDCITY", "HDWEST"):
            rules["institutions"][site]["fundedWeeklyHours"] = {
                "30": {"all_year_round": 20, "term_only": 25}
            }
        self.write(rules)

        self.assertEqual(resolve_funding("HDCW", "30", False)["maxFundedMinutes"], 660)
        self.assertEqual(resolve_funding("HDCITY", "30", False)["maxFundedMinutes"], 660)
        self.assertEqual(resolve_funding("HDWEST", "30", False)["maxFundedMinutes"], 600)

    def test_two_sites_with_different_hours_give_different_results(self):
        rules = json.loads(json.dumps(RULES))
        rules["institutions"]["HDWEST"]["fundedWeeklyHours"] = {
            "30": {"all_year_round": 22, "term_only": 30}
        }
        self.write(rules)

        west = resolve_funding("HDWEST", "30", False)
        cw = resolve_funding("HDCW", "30", False)

        self.assertNotEqual(west, cw)
        self.assertEqual(west["fundingHours"], 22)
        self.assertEqual(cw["fundingHours"], 24.58823529)

    def test_institution_matching_is_case_insensitive_and_trimmed(self):
        self.assertEqual(
            resolve_funding("  hdcw ", "30", False), resolve_funding("HDCW", "30", False)
        )

    def test_a_whole_number_entitlement_is_read_as_its_string(self):
        self.assertEqual(resolve_funding("HDCW", 30, False)["fundingHours"], 24.58823529)
        self.assertEqual(resolve_funding("HDCW", " 15 ", True)["fundingHours"], 16.5)

    def test_term_only_is_decided_by_truthiness_like_the_rest_of_the_code(self):
        self.assertEqual(resolve_funding("HDCW", "30", 1)["fundingHours"], 33)
        self.assertEqual(resolve_funding("HDCW", "30", 0)["fundingHours"], 24.58823529)


class HardErrorTests(RulesTestCase):
    def test_a_null_entry_names_the_exact_path(self):
        self.assert_error(
            "funding_rules: no fundedWeeklyHours for HDCITY > 30 > term_only",
            "HDCITY", "30", True,
        )
        self.assert_error(
            "funding_rules: no fundedWeeklyHours for HDWEST > 15 > all_year_round",
            "HDWEST", "15", False,
        )

    def test_a_missing_entitlement_names_the_exact_path(self):
        self.assert_error(
            "funding_rules: no fundedWeeklyHours for HDCW > 45 > all_year_round",
            "HDCW", "45", False,
        )

    def test_a_missing_schedule_names_the_exact_path(self):
        rules = json.loads(json.dumps(RULES))
        del rules["institutions"]["HDCW"]["fundedWeeklyHours"]["30"]["term_only"]
        self.write(rules)

        self.assert_error(
            "funding_rules: no fundedWeeklyHours for HDCW > 30 > term_only",
            "HDCW", "30", True,
        )

    def test_a_missing_fundedWeeklyHours_key_is_the_same_error(self):
        rules = json.loads(json.dumps(RULES))
        del rules["institutions"]["HDCITY"]["fundedWeeklyHours"]
        self.write(rules)

        self.assert_error(
            "funding_rules: no fundedWeeklyHours for HDCITY > 30 > all_year_round",
            "HDCITY", "30", False,
        )

    def test_an_unknown_institution(self):
        self.assert_error("funding_rules: no institution HDXYZ", "hdxyz", "30", False)

    def test_a_missing_cap(self):
        rules = json.loads(json.dumps(RULES))
        del rules["institutions"]["HDCW"]["maxFundedMinutes"]
        self.write(rules)

        self.assert_error("funding_rules: no maxFundedMinutes for HDCW", "HDCW", "30", False)

    def test_a_missing_method(self):
        rules = json.loads(json.dumps(RULES))
        del rules["fundingMethod"]
        self.write(rules)

        self.assert_error("funding_rules: no fundingMethod", "HDCW", "30", False)

    def test_a_non_numeric_hours_value_is_an_error_not_a_guess(self):
        rules = json.loads(json.dumps(RULES))
        rules["institutions"]["HDCW"]["fundedWeeklyHours"]["30"]["term_only"] = "33"
        self.write(rules)

        self.assert_error(
            "funding_rules: no fundedWeeklyHours for HDCW > 30 > term_only",
            "HDCW", "30", True,
        )

    def test_no_entitlement(self):
        for value in (None, "", "  ", True, 3.5):
            with self.subTest(value=value):
                with self.assertRaisesRegex(FundingRulesError, "funding_entitlement"):
                    resolve_funding("HDCW", value, False)

    def test_a_missing_or_unreadable_file(self):
        self.path.unlink()
        with self.assertRaisesRegex(FundingRulesError, "file not found"):
            resolve_funding("HDCW", "30", False)

        self.path.write_text("{ nope", encoding="utf-8")
        with self.assertRaisesRegex(FundingRulesError, "could not read"):
            resolve_funding("HDCW", "30", False)

        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(FundingRulesError, "not a JSON object"):
            resolve_funding("HDCW", "30", False)


class NoFallbackTests(RulesTestCase):
    def test_a_null_site_never_gets_another_sites_numbers(self):
        for site in ("HDCITY", "HDWEST"):
            for entitlement in ("30", "15"):
                for term_only in (False, True):
                    with self.subTest(site=site, entitlement=entitlement, term_only=term_only):
                        with self.assertRaises(FundingRulesError) as caught:
                            resolve_funding(site, entitlement, term_only)
                        self.assertIn(site, str(caught.exception))

    def test_an_unknown_site_does_not_borrow_the_only_configured_one(self):
        with self.assertRaises(FundingRulesError):
            resolve_funding("HDELSEWHERE", "30", False)

    def test_a_site_missing_one_entitlement_does_not_borrow_the_other(self):
        rules = json.loads(json.dumps(RULES))
        del rules["institutions"]["HDCW"]["fundedWeeklyHours"]["15"]
        self.write(rules)

        with self.assertRaises(FundingRulesError):
            resolve_funding("HDCW", "15", False)


class ReReadTests(RulesTestCase):
    def test_an_edit_takes_effect_without_a_restart(self):
        self.assertEqual(resolve_funding("HDCW", "30", False)["fundingHours"], 24.58823529)

        rules = json.loads(json.dumps(RULES))
        rules["institutions"]["HDCW"]["fundedWeeklyHours"]["30"]["all_year_round"] = 25
        rules["institutions"]["HDCW"]["maxFundedMinutes"] = 700
        self.write(rules)

        result = resolve_funding("HDCW", "30", False)
        self.assertEqual(result["fundingHours"], 25)
        self.assertEqual(result["maxFundedMinutes"], 700)

    def test_filling_in_a_null_entry_takes_effect_at_once(self):
        with self.assertRaises(FundingRulesError):
            resolve_funding("HDCITY", "30", False)

        rules = json.loads(json.dumps(RULES))
        rules["institutions"]["HDCITY"]["fundedWeeklyHours"] = {
            "30": {"all_year_round": 20, "term_only": 26}
        }
        self.write(rules)

        self.assertEqual(resolve_funding("HDCITY", "30", False)["fundingHours"], 20)


class ShippedFileTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {"FUNDING_RULES_FILE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_the_shipped_file_matches_the_agreed_rules(self):
        shipped = json.loads((PROJECT / "reference" / "funding_rules.json").read_text(encoding="utf-8"))

        self.assertEqual(shipped, RULES)

    def test_the_default_path_is_that_file(self):
        self.assertEqual(funding_rules.rules_path(), PROJECT / "reference" / "funding_rules.json")

    def test_hdcw_resolves_from_the_shipped_file(self):
        self.assertEqual(resolve_funding("HDCW", "15", True)["fundingHours"], 16.5)

    def test_the_sites_still_to_be_filled_in_are_hard_errors_today(self):
        with self.assertRaisesRegex(
            FundingRulesError, "no fundedWeeklyHours for HDCITY > 30 > term_only"
        ):
            resolve_funding("HDCITY", "30", True)

    def test_no_pull_command_touches_the_file(self):
        for module in PROJECT.glob("actions/pull_*/*.py"):
            self.assertNotIn("funding_rules", module.read_text(encoding="utf-8"), module)


if __name__ == "__main__":
    unittest.main()
