"""Tests for integrations/session_catalogue.py.

Run from the project root:

    python -m unittest discover -s tests -v

FUNCTIONALLY LOAD-BEARING, unlike integrations/catalogue.py: a gap here must
raise, never fall back to None or a guessed UUID. These tests pin the variant
naming trap (the variant name describes what is MISSING, not what is present)
and the exact-path error messages that make a catalogue gap actionable.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations import session_catalogue

INSTITUTION = "HDCITY"

MORNING = "10000000-0000-0000-0000-000000000001"
AFTERNOON = "20000000-0000-0000-0000-000000000002"
FULL_DAY = "30000000-0000-0000-0000-000000000003"

WITH_MEALS_AND_ACTIVITIES = "40000000-0000-0000-0000-000000000004"
NO_ACTIVITIES = "50000000-0000-0000-0000-000000000005"  # meals only
NO_MEALS = "60000000-0000-0000-0000-000000000006"  # activities only
NEITHER = "70000000-0000-0000-0000-000000000007"

NON_FUNDED_FULL_DAY = "80000000-0000-0000-0000-000000000008"


def _variant_slots(uuid_value):
    return {"morning": uuid_value, "afternoon": uuid_value, "full_day": uuid_value}


CATALOGUE = {
    "institutions": {
        INSTITUTION: {
            "funded": {
                "with_meals_and_activities": _variant_slots(WITH_MEALS_AND_ACTIVITIES),
                "no_meals": _variant_slots(NO_MEALS),
                "no_activities": _variant_slots(NO_ACTIVITIES),
                "neither": {
                    "morning": MORNING,
                    "afternoon": AFTERNOON,
                    "full_day": FULL_DAY,
                },
            },
            "non_funded": {
                "morning": "",
                "afternoon": "",
                "full_day": NON_FUNDED_FULL_DAY,
            },
        },
        "HDGAP": {
            # An institution that exists but has an incomplete funded branch,
            # for testing the "no UUID for ... > funded > <variant>" message.
            "funded": {"neither": {"morning": "", "afternoon": "", "full_day": ""}},
            "non_funded": {"morning": "", "afternoon": "", "full_day": ""},
        },
    }
}


class SessionCatalogueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        path = Path(self._tmp.name) / "session_catalogue.json"
        path.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        env = mock.patch.dict("os.environ", {"SESSION_CATALOGUE_FILE": str(path)})
        env.start()
        self.addCleanup(env.stop)


class VariantSelectionTests(SessionCatalogueTestCase):
    """The naming trap: the variant name describes what is MISSING."""

    def test_both_meals_and_activities_uses_with_meals_and_activities(self):
        uuid = session_catalogue.resolve_session(
            INSTITUTION, True, "morning", has_meals=True, has_activities=True
        )
        self.assertEqual(uuid, WITH_MEALS_AND_ACTIVITIES)

    def test_meals_only_uses_no_activities_variant(self):
        # has_meals=True, has_activities=False -> "no_activities" (meals only).
        uuid = session_catalogue.resolve_session(
            INSTITUTION, True, "morning", has_meals=True, has_activities=False
        )
        self.assertEqual(uuid, NO_ACTIVITIES)

    def test_activities_only_uses_no_meals_variant(self):
        # has_meals=False, has_activities=True -> "no_meals" (activities only).
        uuid = session_catalogue.resolve_session(
            INSTITUTION, True, "morning", has_meals=False, has_activities=True
        )
        self.assertEqual(uuid, NO_MEALS)

    def test_neither_uses_neither_variant(self):
        uuid = session_catalogue.resolve_session(
            INSTITUTION, True, "morning", has_meals=False, has_activities=False
        )
        self.assertEqual(uuid, MORNING)

    def test_defaults_are_false_false(self):
        # has_meals/has_activities default to False -> "neither".
        uuid = session_catalogue.resolve_session(INSTITUTION, True, "afternoon")
        self.assertEqual(uuid, AFTERNOON)


class SlotResolutionTests(SessionCatalogueTestCase):
    def test_each_slot_resolves_within_the_neither_variant(self):
        self.assertEqual(
            session_catalogue.resolve_session(INSTITUTION, True, "morning"), MORNING
        )
        self.assertEqual(
            session_catalogue.resolve_session(INSTITUTION, True, "afternoon"),
            AFTERNOON,
        )
        self.assertEqual(
            session_catalogue.resolve_session(INSTITUTION, True, "full_day"), FULL_DAY
        )

    def test_full_day_with_a_space_is_normalised(self):
        uuid = session_catalogue.resolve_session(INSTITUTION, True, "full day")
        self.assertEqual(uuid, FULL_DAY)

    def test_slot_is_case_insensitive(self):
        uuid = session_catalogue.resolve_session(INSTITUTION, True, "MORNING")
        self.assertEqual(uuid, MORNING)

    def test_an_invalid_slot_raises(self):
        with self.assertRaises(session_catalogue.SessionCatalogueError) as ctx:
            session_catalogue.resolve_session(INSTITUTION, True, "midnight")
        self.assertIn("not a valid slot", str(ctx.exception))


class NonFundedTests(SessionCatalogueTestCase):
    def test_non_funded_looks_up_the_non_funded_branch_directly(self):
        uuid = session_catalogue.resolve_session(INSTITUTION, False, "full_day")
        self.assertEqual(uuid, NON_FUNDED_FULL_DAY)

    def test_non_funded_ignores_meals_and_activities_flags(self):
        uuid = session_catalogue.resolve_session(
            INSTITUTION, False, "full_day", has_meals=True, has_activities=True
        )
        self.assertEqual(uuid, NON_FUNDED_FULL_DAY)


class InstitutionMatchingTests(SessionCatalogueTestCase):
    def test_matching_is_case_insensitive_and_trimmed(self):
        for variant in ("hdcity", "HdCity", "  HDCITY  "):
            uuid = session_catalogue.resolve_session(variant, True, "morning")
            self.assertEqual(uuid, MORNING)

    def test_an_unknown_institution_raises_naming_it(self):
        with self.assertRaises(session_catalogue.SessionCatalogueError) as ctx:
            session_catalogue.resolve_session("HDXYZ", True, "afternoon")
        self.assertIn("no institution 'HDXYZ'", str(ctx.exception))


class GapErrorMessageTests(SessionCatalogueTestCase):
    """Every gap names exactly which part of the path was missing."""

    def test_empty_non_funded_slot_names_institution_and_slot(self):
        with self.assertRaises(session_catalogue.SessionCatalogueError) as ctx:
            session_catalogue.resolve_session(INSTITUTION, False, "morning")

        message = str(ctx.exception)
        self.assertIn("session_catalogue: no UUID for", message)
        self.assertIn(f"institution {INSTITUTION!r}", message)
        self.assertIn("non_funded", message)
        self.assertIn("morning", message)

    def test_empty_funded_variant_slot_names_institution_variant_and_slot(self):
        with self.assertRaises(session_catalogue.SessionCatalogueError) as ctx:
            session_catalogue.resolve_session("HDGAP", True, "morning")

        message = str(ctx.exception)
        self.assertIn("session_catalogue: no UUID for", message)
        self.assertIn("'HDGAP'", message)
        self.assertIn("funded", message)
        self.assertIn("neither", message)
        self.assertIn("morning", message)

    def test_missing_funded_branch_entirely_names_institution_and_funded(self):
        catalogue = {
            "institutions": {"HDNOFUNDED": {"non_funded": {"full_day": "x"}}}
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session_catalogue.json"
            path.write_text(json.dumps(catalogue), encoding="utf-8")
            with mock.patch.dict("os.environ", {"SESSION_CATALOGUE_FILE": str(path)}):
                with self.assertRaises(session_catalogue.SessionCatalogueError) as ctx:
                    session_catalogue.resolve_session("HDNOFUNDED", True, "morning")

        message = str(ctx.exception)
        self.assertIn("'HDNOFUNDED'", message)
        self.assertIn("funded", message)


class MissingFileTests(unittest.TestCase):
    def test_a_missing_file_raises(self):
        with mock.patch.dict(
            "os.environ", {"SESSION_CATALOGUE_FILE": "/nope/missing.json"}
        ):
            with self.assertRaises(session_catalogue.SessionCatalogueError):
                session_catalogue.resolve_session(INSTITUTION, True, "morning")


if __name__ == "__main__":
    unittest.main()
