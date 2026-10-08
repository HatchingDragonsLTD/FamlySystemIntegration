"""Tests for readable Slack names derived from the pulled catalogues.

Run from the project root:

    python -m unittest discover -s tests -v

Two layers:

  * `integrations/display_names.py` -- pure functions turning a RAW Famly title
    ("(FNM) Full Day", "(1/2) Meals & Snacks") into readable wording. Nothing
    it produces is ever stored.
  * `integrations/catalogue.load_titles` -- the id -> name maps handed to the
    Slack summary, resolved in this order:
        (a) manual overrides: the flat maps in catalogue.json
        (b) the derived readable name, from session_titles.json /
            products_catalogue.json, flattened across institutions
        (c) the raw Famly title (what (b) yields for a title that does not parse)
        (d) the raw UUID (an id in no map; the summary shows it as it is)

NOTHING HERE TOUCHES FAMLY. Every file the code reads is a temp file named by
environment variable, so the repo's real reference files are never read.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.pull_sessions import runner as pull_sessions
from actions.read_child_plans.runner import parse_plan
from integrations import catalogue, display_names, slack

logging.disable(logging.CRITICAL)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
class SessionDisplayNameTests(unittest.TestCase):
    SLOTS = (("Morning", "Morning"), ("Afternoon", "Afternoon"), ("Full Day", "Full Day"))

    def check(self, prefix, expected_template):
        for raw_slot, label in self.SLOTS:
            raw = f"{prefix}{raw_slot}"
            self.assertEqual(
                display_names.session_display_name(raw),
                expected_template.format(slot=label),
                raw,
            )

    def test_funded_with_meals_and_activities_has_no_suffix(self):
        self.check("(F) ", "Funded {slot}")

    def test_funded_no_meals(self):
        self.check("(FNM) ", "Funded {slot} (No Meals)")

    def test_funded_no_activities(self):
        self.check("(FNA) ", "Funded {slot} (No Activities)")

    def test_funded_neither_is_no_extras(self):
        self.check("(FNE) ", "Funded {slot} (No Extras)")

    def test_non_funded_gets_just_the_slot_label(self):
        self.check("", "{slot}")

    def test_the_exact_names_called_out_by_the_spec(self):
        for raw, expected in (
            ("(FNM) Full Day", "Funded Full Day (No Meals)"),
            ("(FNA) Afternoon", "Funded Afternoon (No Activities)"),
            ("(FNE) Morning", "Funded Morning (No Extras)"),
            ("(F) Full Day", "Funded Full Day"),
            ("Afternoon", "Afternoon"),
        ):
            self.assertEqual(display_names.session_display_name(raw), expected, raw)

    def test_the_slot_text_is_normalised_the_way_the_pull_normalises_it(self):
        # parse_title lower-cases and underscores the slot, so these classify.
        self.assertEqual(display_names.session_display_name("(F) full day"), "Funded Full Day")
        self.assertEqual(display_names.session_display_name("  (F)   MORNING "), "Funded Morning")

    def test_it_classifies_with_the_existing_parse_title_not_its_own_table(self):
        # Whatever parse_title says is what is named: the prefix table lives in
        # pull_sessions only.
        parsed = pull_sessions.ParsedTitle(funded=True, variant="no_meals", slot="afternoon")
        with mock.patch.object(pull_sessions, "parse_title", return_value=parsed) as spy:
            name = display_names.session_display_name("anything at all")

        spy.assert_called_once_with("anything at all")
        self.assertEqual(name, "Funded Afternoon (No Meals)")

    def test_a_title_that_does_not_parse_falls_back_to_the_raw_title(self):
        for raw in (
            "Weekend School",  # not a slot
            "(FX) Morning",  # unknown prefix
            "Afternoon (Funded)",  # suffix-style, not the prefix convention
            "Full Week",
            "(F)",  # prefix, nothing after it
            "(F) ",
        ):
            self.assertEqual(display_names.session_display_name(raw), raw, raw)

    def test_a_blank_title_comes_back_as_it_is_and_a_non_string_as_empty(self):
        self.assertEqual(display_names.session_display_name(""), "")
        self.assertEqual(display_names.session_display_name("   "), "   ")
        for raw in (None, 7, ["(F) Morning"]):
            self.assertEqual(display_names.session_display_name(raw), "")


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
class ProductDisplayNameTests(unittest.TestCase):
    def test_funded_prefix(self):
        self.assertEqual(
            display_names.product_display_name("(F) Meals & Snacks"), "Funded Meals & Snacks"
        )

    def test_half_day_prefix(self):
        self.assertEqual(
            display_names.product_display_name("(1/2) Meals & Snacks"),
            "Half Day Meals & Snacks",
        )

    def test_half_day_funded_prefix(self):
        self.assertEqual(
            display_names.product_display_name("(1/2,F) Educational Activities & Extras"),
            "Half Day Funded Educational Activities & Extras",
        )

    def test_the_funded_prefix_alone_does_not_swallow_the_half_day_funded_one(self):
        # "(1/2,F) " starts with neither "(F) " nor "(1/2) ".
        self.assertEqual(
            display_names.product_display_name("(1/2,F) Meals & Snacks"),
            "Half Day Funded Meals & Snacks",
        )

    def test_plain_titles_are_unchanged(self):
        for raw in ("Meals & Snacks", "Educational Activities & Extras", "Trip"):
            self.assertEqual(display_names.product_display_name(raw), raw)

    def test_only_a_leading_exact_prefix_counts(self):
        for raw in (
            "Meals & Snacks (F)",  # suffix style (the old HDWEST naming)
            "Meals (F) Snacks",  # mid-string
            "(F)Meals",  # no space after the prefix
            "(f) Meals & Snacks",  # wrong case
            " (F) Meals & Snacks",  # leading space
        ):
            self.assertEqual(display_names.product_display_name(raw), raw, raw)

    def test_the_prefix_is_replaced_once(self):
        self.assertEqual(
            display_names.product_display_name("(F) (F) Meals"), "Funded (F) Meals"
        )

    def test_a_non_string_title_is_empty_not_an_error(self):
        for raw in (None, 7):
            self.assertEqual(display_names.product_display_name(raw), "")

    def test_every_title_in_the_shipped_products_catalogue_names_cleanly(self):
        raw = json.loads(display_names.DEFAULT_PRODUCTS_CATALOGUE_PATH.read_text(encoding="utf-8"))
        titles = [t for e in raw["institutions"].values() for t in e["products"].values()]
        self.assertTrue(titles)

        for title in titles:
            name = display_names.product_display_name(title)
            self.assertTrue(name.strip(), title)
            # No coded prefix survives into the readable name.
            self.assertFalse(name.startswith(("(F) ", "(1/2) ", "(1/2,F) ")), name)


# --------------------------------------------------------------------------- #
# load_titles: the resolution order
# --------------------------------------------------------------------------- #
CITY_SESSIONS = {
    "s-f": "(F) Morning",
    "s-fnm": "(FNM) Full Day",
    "s-fna": "(FNA) Afternoon",
    "s-fne": "(FNE) Morning",
    "s-plain": "Afternoon",
    "s-odd": "Weekend School",  # does not classify
}
CW_SESSIONS = {"s-cw": "(F) Full Day"}

CITY_PRODUCTS = {
    "p-f": "(F) Meals & Snacks",
    "p-half": "(1/2) Meals & Snacks",
    "p-halff": "(1/2,F) Educational Activities & Extras",
    "p-plain": "Meals & Snacks",
}


class LoadTitlesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.catalogue_file = self.tmp / "catalogue.json"
        self.session_titles_file = self.tmp / "session_titles.json"
        self.products_file = self.tmp / "products_catalogue.json"

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(self.catalogue_file),
                "SESSION_TITLES_FILE": str(self.session_titles_file),
                "PRODUCTS_CATALOGUE_FILE": str(self.products_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

        warn = mock.patch.object(catalogue.logger, "warning")
        self.warn = warn.start()
        self.addCleanup(warn.stop)

    def write_catalogue(self, sessions=None, products=None):
        self.catalogue_file.write_text(
            json.dumps({"sessions": sessions or {}, "products": products or {}}),
            encoding="utf-8",
        )

    def write_session_titles(self, by_institution=None):
        by_institution = by_institution or {"HDCITY": CITY_SESSIONS, "HDCW": CW_SESSIONS}
        self.session_titles_file.write_text(
            json.dumps(
                {"institutions": {c: {"sessions": s} for c, s in by_institution.items()}}
            ),
            encoding="utf-8",
        )

    def write_products(self, by_institution=None):
        by_institution = by_institution or {"HDCITY": CITY_PRODUCTS}
        self.products_file.write_text(
            json.dumps(
                {"institutions": {c: {"products": p} for c, p in by_institution.items()}}
            ),
            encoding="utf-8",
        )


class DerivedNameTests(LoadTitlesTestCase):
    def test_sessions_are_named_from_the_pulled_raw_titles(self):
        self.write_catalogue()
        self.write_session_titles()

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions["s-f"], "Funded Morning")
        self.assertEqual(sessions["s-fnm"], "Funded Full Day (No Meals)")
        self.assertEqual(sessions["s-fna"], "Funded Afternoon (No Activities)")
        self.assertEqual(sessions["s-fne"], "Funded Morning (No Extras)")
        self.assertEqual(sessions["s-plain"], "Afternoon")

    def test_products_are_named_from_the_pulled_raw_titles(self):
        self.write_catalogue()
        self.write_products()

        _, products = catalogue.load_titles()

        self.assertEqual(products["p-f"], "Funded Meals & Snacks")
        self.assertEqual(products["p-half"], "Half Day Meals & Snacks")
        self.assertEqual(
            products["p-halff"], "Half Day Funded Educational Activities & Extras"
        )
        self.assertEqual(products["p-plain"], "Meals & Snacks")

    def test_institutions_are_flattened_into_one_map(self):
        self.write_catalogue()
        self.write_session_titles()

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions["s-cw"], "Funded Full Day")  # from HDCW
        self.assertEqual(sessions["s-f"], "Funded Morning")  # from HDCITY

    def test_a_repeated_uuid_keeps_the_first_seen_title(self):
        self.write_catalogue()
        self.write_session_titles({"HDCITY": {"s-1": "(F) Morning"}, "HDCW": {"s-1": "Afternoon"}})

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions["s-1"], "Funded Morning")

    def test_an_unparseable_session_title_falls_back_to_the_raw_famly_title(self):
        self.write_catalogue()
        self.write_session_titles()

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions["s-odd"], "Weekend School")

    def test_an_id_in_no_map_is_left_for_the_summary_to_show_as_the_uuid(self):
        self.write_catalogue()
        self.write_session_titles()

        sessions, products = catalogue.load_titles()

        self.assertNotIn("s-unknown", sessions)
        self.assertNotIn("p-unknown", products)

    def test_blank_or_non_string_pulled_entries_are_skipped(self):
        self.write_catalogue()
        self.write_session_titles(
            {"HDCITY": {"s-blank": "  ", "s-num": 7, "s-none": None, "s-ok": "(F) Morning"}}
        )

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions, {"s-ok": "Funded Morning"})

    def test_nothing_derived_is_ever_stored(self):
        self.write_catalogue()
        self.write_session_titles()
        self.write_products()
        before = {
            f: f.read_text(encoding="utf-8")
            for f in (self.catalogue_file, self.session_titles_file, self.products_file)
        }

        catalogue.load_titles()

        after = {f: f.read_text(encoding="utf-8") for f in before}
        self.assertEqual(before, after)
        # ...and the pulled file still holds the RAW title, not the readable one.
        self.assertIn("(FNM) Full Day", self.session_titles_file.read_text(encoding="utf-8"))


class OverrideTests(LoadTitlesTestCase):
    def test_a_manual_override_beats_the_derived_name(self):
        self.write_catalogue(
            sessions={"s-fnm": "Our Special Full Day"}, products={"p-f": "Hot Lunch"}
        )
        self.write_session_titles()
        self.write_products()

        sessions, products = catalogue.load_titles()

        self.assertEqual(sessions["s-fnm"], "Our Special Full Day")
        self.assertEqual(products["p-f"], "Hot Lunch")

    def test_an_override_does_not_disturb_the_other_derived_names(self):
        self.write_catalogue(sessions={"s-fnm": "Our Special Full Day"})
        self.write_session_titles()

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions["s-f"], "Funded Morning")

    def test_an_override_for_an_id_the_pull_never_returned_still_applies(self):
        self.write_catalogue(sessions={"s-manual": "Hand Named"})
        self.write_session_titles()

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions["s-manual"], "Hand Named")

    def test_a_blank_override_placeholder_falls_through_to_the_derived_name(self):
        self.write_catalogue(sessions={"s-fnm": "   "}, products={"p-f": ""})
        self.write_session_titles()
        self.write_products()

        sessions, products = catalogue.load_titles()

        self.assertEqual(sessions["s-fnm"], "Funded Full Day (No Meals)")
        self.assertEqual(products["p-f"], "Funded Meals & Snacks")

    def test_overrides_alone_still_work_with_no_pulled_files(self):
        self.write_catalogue(sessions={"s-1": "Full Day (Funded)"}, products={"p-1": "Hot Lunch"})

        self.assertEqual(
            catalogue.load_titles(),
            ({"s-1": "Full Day (Funded)"}, {"p-1": "Hot Lunch"}),
        )


class MalformedPulledFileTests(LoadTitlesTestCase):
    """A bad or absent pulled file costs names, never the preview."""

    def test_missing_pulled_files_fall_through_with_a_warning(self):
        self.write_catalogue(sessions={"s-1": "Manual"})

        result = catalogue.load_titles()  # must not raise

        self.assertEqual(result, ({"s-1": "Manual"}, {}))
        self.assertTrue(self.warn.called)

    def test_invalid_json_falls_through_with_a_warning(self):
        self.write_catalogue(sessions={"s-1": "Manual"})
        self.session_titles_file.write_text("{not json", encoding="utf-8")
        self.products_file.write_text("", encoding="utf-8")

        result = catalogue.load_titles()

        self.assertEqual(result, ({"s-1": "Manual"}, {}))
        self.assertGreaterEqual(self.warn.call_count, 2)

    def test_wrong_shapes_fall_through_with_a_warning(self):
        self.write_catalogue()
        for payload in ("[]", '"text"', "7", '{"institutions": []}', '{"institutions": 3}', "{}"):
            self.warn.reset_mock()
            self.session_titles_file.write_text(payload, encoding="utf-8")

            sessions, _ = catalogue.load_titles()

            self.assertEqual(sessions, {}, payload)
            self.assertTrue(self.warn.called, payload)

    def test_malformed_entries_inside_a_valid_file_are_skipped(self):
        self.write_catalogue()
        self.session_titles_file.write_text(
            json.dumps(
                {
                    "institutions": {
                        "HDBAD1": "not a dict",
                        "HDBAD2": {"sessions": ["not", "a", "dict"]},
                        "HDBAD3": {},
                        "HDOK": {"sessions": {"s-1": "(F) Morning"}},
                    }
                }
            ),
            encoding="utf-8",
        )

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions, {"s-1": "Funded Morning"})

    def test_a_bad_sessions_file_does_not_cost_the_product_names(self):
        self.write_catalogue()
        self.session_titles_file.write_text("{not json", encoding="utf-8")
        self.write_products()

        sessions, products = catalogue.load_titles()

        self.assertEqual(sessions, {})
        self.assertEqual(products["p-f"], "Funded Meals & Snacks")

    def test_a_bad_products_file_does_not_cost_the_session_names(self):
        self.write_catalogue()
        self.write_session_titles()
        self.products_file.write_text("{not json", encoding="utf-8")

        sessions, products = catalogue.load_titles()

        self.assertEqual(products, {})
        self.assertEqual(sessions["s-f"], "Funded Morning")

    def test_a_bad_pulled_file_does_not_cost_the_manual_overrides(self):
        self.write_catalogue(sessions={"s-1": "Manual"}, products={"p-1": "Hot Lunch"})
        self.session_titles_file.write_text("{not json", encoding="utf-8")
        self.products_file.write_text("{not json", encoding="utf-8")

        self.assertEqual(
            catalogue.load_titles(), ({"s-1": "Manual"}, {"p-1": "Hot Lunch"})
        )

    def test_a_missing_manual_catalogue_still_yields_the_derived_names(self):
        # catalogue.json absent entirely (no overrides at all).
        self.write_session_titles()

        sessions, _ = catalogue.load_titles()

        self.assertEqual(sessions["s-fnm"], "Funded Full Day (No Meals)")


class EndToEndSlackTests(LoadTitlesTestCase):
    """What the approver actually reads."""

    def plan(self):
        return parse_plan(
            {
                "childId": "child-1",
                "from": "2026-09-01",
                "planParts": [
                    {
                        "planPartId": "pp-1",
                        "sessionBookings": [
                            {"sessionId": "s-fnm", "day": "MONDAY"},
                            {"sessionId": "s-fna", "day": "TUESDAY"},
                            {"sessionId": "s-fne", "day": "WEDNESDAY"},
                            {"sessionId": "s-odd", "day": "THURSDAY"},
                            {"sessionId": "s-unknown", "day": "FRIDAY"},
                        ],
                        "productBookings": [
                            {"productId": "p-plain", "day": "MONDAY", "amount": 1},
                            {"productId": "p-half", "day": "TUESDAY", "amount": 1},
                            {"productId": "p-unknown", "day": "TUESDAY", "amount": 1},
                        ],
                    }
                ],
            }
        )

    def test_the_summary_reads_in_derived_wording_with_the_right_fallbacks(self):
        self.write_catalogue()
        self.write_session_titles()
        self.write_products()
        sessions, products = catalogue.load_titles()

        text = slack.build_summary(
            self.plan(), [], session_titles=sessions, product_titles=products
        )

        self.assertIn("• Monday — Funded Full Day (No Meals)", text)
        self.assertIn("• Tuesday — Funded Afternoon (No Activities)", text)
        self.assertIn("• Wednesday — Funded Morning (No Extras)", text)
        # Does not classify -> the raw Famly title.
        self.assertIn("• Thursday — Weekend School", text)
        # In no file at all -> the raw UUID.
        self.assertIn("• Friday — s-unknown", text)
        self.assertIn("• Monday — 1× Meals & Snacks", text)
        self.assertIn("• Tuesday — 1× Half Day Meals & Snacks", text)
        self.assertIn("• Tuesday — 1× p-unknown", text)

    def test_a_manual_override_wins_in_the_summary(self):
        self.write_catalogue(sessions={"s-fnm": "Custom Monday Name"})
        self.write_session_titles()
        sessions, products = catalogue.load_titles()

        text = slack.build_summary(
            self.plan(), [], session_titles=sessions, product_titles=products
        )

        self.assertIn("• Monday — Custom Monday Name", text)
        self.assertNotIn("Funded Full Day (No Meals)", text)


if __name__ == "__main__":
    unittest.main()
