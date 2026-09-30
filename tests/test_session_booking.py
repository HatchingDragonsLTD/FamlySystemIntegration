"""Integration tests: hubspot_flatten's session-booking resolution.

Run from the project root:

    python -m unittest discover -s tests -v

Covers the new contract end to end -- HubSpot sends a raw slot per day plus
institution/funded/meals/activities, and hubspot_flatten resolves each
booked day's Famly session UUID via integrations.session_catalogue. A gap in
the catalogue is a HARD failure that must block the preview with zero Famly
calls, exactly like a malformed product booking.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.plan_write import hubspot_flatten as flatten
from actions.plan_write import hubspot_intake, runner

logging.disable(logging.CRITICAL)

CHILD = "00000000-0000-0000-0000-000000000001"
SCHEDULE = "00000000-0000-0000-0000-000000000003"
INSTITUTION = "HDCITY"

FUNDED_WITH_MEALS_AND_ACTIVITIES = "11111111-0000-0000-0000-000000000001"
FUNDED_NO_ACTIVITIES = "22222222-0000-0000-0000-000000000002"  # meals only
FUNDED_NO_MEALS = "33333333-0000-0000-0000-000000000003"  # activities only
FUNDED_NEITHER = "44444444-0000-0000-0000-000000000004"
NON_FUNDED = "55555555-0000-0000-0000-000000000005"


def _slots(uuid_value):
    return {"morning": uuid_value, "afternoon": uuid_value, "full_day": uuid_value}


CATALOGUE = {
    "institutions": {
        INSTITUTION: {
            "funded": {
                "with_meals_and_activities": _slots(FUNDED_WITH_MEALS_AND_ACTIVITIES),
                "no_meals": _slots(FUNDED_NO_MEALS),
                "no_activities": _slots(FUNDED_NO_ACTIVITIES),
                "neither": _slots(FUNDED_NEITHER),
            },
            "non_funded": _slots(NON_FUNDED),
        },
        # A real institution with an entirely unfilled catalogue -- every
        # booking against it must fail loudly, not resolve to "".
        "HDEMPTY": {
            "funded": {
                "with_meals_and_activities": {
                    "morning": "",
                    "afternoon": "",
                    "full_day": "",
                },
                "no_meals": {"morning": "", "afternoon": "", "full_day": ""},
                "no_activities": {"morning": "", "afternoon": "", "full_day": ""},
                "neither": {"morning": "", "afternoon": "", "full_day": ""},
            },
            "non_funded": {"morning": "", "afternoon": "", "full_day": ""},
        },
    }
}

BILLING_PROFILE = "cccccccc-0000-0000-0000-000000000099"


def _institution_defaults(institution) -> dict:
    """A complete institution_defaults.json fixture for one institution.

    Every test here leaves `termTimeOnly` unset (-> all_year_round), so only
    that bucket needs real values; term_only is filled in identically so a
    stray resolution against it would fail loudly rather than silently reuse
    the wrong numbers.
    """
    bucket = {
        "billingProfileId": BILLING_PROFILE,
        "attendanceScheduleId": SCHEDULE,
        "weeksOfCare": 51,
        "billingId": "ANNUALIZED_V2",
        "billingTitle": "Monthly",
        "billingInvoices": "ADVANCE",
    }
    return {
        "institutions": {
            institution: {
                "ruleGroupId": "01RULEGROUP0000000000000000",
                "schedules": {"all_year_round": bucket, "term_only": bucket},
            }
        }
    }


def flat_payload(**overrides) -> dict:
    payload = {
        "childId": CHILD,
        "from": "2026-09-01",
        "institution": INSTITUTION,
        "monday": "morning",
        "funded": "false",
    }
    payload.update(overrides)
    return payload


class SessionBookingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        path = Path(self._tmp.name) / "session_catalogue.json"
        path.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        defaults_path = Path(self._tmp.name) / "institution_defaults.json"
        defaults_path.write_text(
            json.dumps(_institution_defaults(INSTITUTION)), encoding="utf-8"
        )

        env = mock.patch.dict(
            "os.environ",
            {
                "SESSION_CATALOGUE_FILE": str(path),
                "INSTITUTION_DEFAULTS_FILE": str(defaults_path),
            },
        )
        env.start()
        self.addCleanup(env.stop)


class FundedVariantResolutionTests(SessionBookingTestCase):
    """A funded deal resolves each booked day through the right variant."""

    def test_meals_only_resolves_the_no_activities_variant_for_every_booked_day(self):
        nested = flatten.flatten_to_nested(
            flat_payload(
                funded="true",
                has_meals="true",
                has_activities="false",
                monday="morning",
                wednesday="afternoon",
                friday="full day",
            )
        )

        bookings = nested["planParts"][0]["sessionBookings"]
        self.assertEqual(len(bookings), 3)
        for booking in bookings:
            self.assertEqual(booking["sessionId"], FUNDED_NO_ACTIVITIES)
            self.assertIs(booking["fundable"], True)

    def test_both_flags_true_resolves_with_meals_and_activities(self):
        nested = flatten.flatten_to_nested(
            flat_payload(
                funded="true", has_meals="true", has_activities="true", monday="morning"
            )
        )
        booking = nested["planParts"][0]["sessionBookings"][0]
        self.assertEqual(booking["sessionId"], FUNDED_WITH_MEALS_AND_ACTIVITIES)

    def test_neither_flag_resolves_the_neither_variant(self):
        nested = flatten.flatten_to_nested(flat_payload(funded="true", monday="morning"))
        booking = nested["planParts"][0]["sessionBookings"][0]
        self.assertEqual(booking["sessionId"], FUNDED_NEITHER)


class NonFundedResolutionTests(SessionBookingTestCase):
    def test_a_non_funded_deal_resolves_via_the_non_funded_branch(self):
        nested = flatten.flatten_to_nested(
            flat_payload(funded="false", monday="morning", wednesday="full day")
        )

        bookings = nested["planParts"][0]["sessionBookings"]
        self.assertEqual(len(bookings), 2)
        for booking in bookings:
            self.assertEqual(booking["sessionId"], NON_FUNDED)
            self.assertIs(booking["fundable"], False)


class CatalogueGapHardErrorTests(SessionBookingTestCase):
    """A gap blocks the whole preview -- zero Famly calls, no partial plan."""

    def setUp(self):
        super().setUp()
        self.calls = []
        outer = self

        class FakeRest:
            def post(self, path, params=None, json_body=None):
                outer.calls.append(json_body)
                return {"id": "plan-9", "childId": CHILD, "planParts": [], "behaviors": []}

        patcher = mock.patch.object(runner, "RestClient", lambda *a, **k: FakeRest())
        patcher.start()
        self.addCleanup(patcher.stop)

        env = mock.patch.dict("os.environ", {"FAMLY_ACCESS_TOKEN": "test-token"})
        env.start()
        self.addCleanup(env.stop)

    def test_unknown_institution_is_a_hard_error_naming_it(self):
        nested = flatten.flatten_to_nested(flat_payload(institution="HDXYZ"))

        self.assertIn(flatten.ERRORS_KEY, nested)
        self.assertTrue(
            any("HDXYZ" in e for e in nested[flatten.ERRORS_KEY]), nested[flatten.ERRORS_KEY]
        )
        self.assertEqual(nested["planParts"][0]["sessionBookings"], [])

    def test_a_known_institution_with_an_empty_uuid_is_a_hard_error(self):
        nested = flatten.flatten_to_nested(
            flat_payload(institution="HDEMPTY", monday="morning")
        )

        self.assertIn(flatten.ERRORS_KEY, nested)
        message = nested[flatten.ERRORS_KEY][0]
        self.assertIn("HDEMPTY", message)
        self.assertIn("non_funded", message)
        self.assertIn("morning", message)

    def test_a_missing_institution_with_a_booked_day_is_a_hard_error(self):
        nested = flatten.flatten_to_nested(flat_payload(institution=""))

        self.assertIn(flatten.ERRORS_KEY, nested)
        self.assertTrue(
            any("institution" in e for e in nested[flatten.ERRORS_KEY]),
            nested[flatten.ERRORS_KEY],
        )

    def test_a_catalogue_gap_blocks_the_preview_with_zero_famly_calls(self):
        result = hubspot_intake.handle_intake(
            flat_payload(institution="HDXYZ"), version=3
        )

        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("HDXYZ" in e for e in result.errors))

    def test_a_valid_institution_still_previews_normally(self):
        result = hubspot_intake.handle_intake(flat_payload(), version=3)

        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 1)
        sent = self.calls[0]["plan"]["planParts"][0]["sessionBookings"]
        self.assertEqual(sent[0]["sessionId"], NON_FUNDED)


# --------------------------------------------------------------------------- #
# ruleGroupId/billingProfileId/attendanceScheduleId/billing.* are resolved via
# institution_defaults, not read from the flat HubSpot payload -- see
# hubspot_flatten's module docstring.
# --------------------------------------------------------------------------- #
class InstitutionDefaultsResolutionTests(SessionBookingTestCase):
    def test_the_resolved_defaults_land_on_the_plan_part(self):
        nested = flatten.flatten_to_nested(flat_payload())
        part = nested["planParts"][0]

        self.assertEqual(part["billingProfileId"], BILLING_PROFILE)
        self.assertEqual(part["attendanceScheduleId"], SCHEDULE)
        self.assertEqual(part["billing"]["id"], "ANNUALIZED_V2")
        self.assertEqual(part["billing"]["title"], "Monthly")
        self.assertEqual(part["billing"]["weeksOfCare"], 51)
        self.assertEqual(part["billing"]["invoices"], "ADVANCE")
        self.assertEqual(nested["ruleGroupId"], "01RULEGROUP0000000000000000")

    def test_hubspot_sending_the_removed_fields_is_a_no_op_not_an_override(self):
        # Every one of these used to be read straight from the flat payload.
        # Sending them now must change NOTHING: the resolved values from
        # institution_defaults are the only authoritative source.
        nested = flatten.flatten_to_nested(
            flat_payload(
                ruleGroupId="bogus-rule-group",
                billingProfileId="00000000-bad0-bad0-bad0-badbadbadbad",
                attendanceScheduleId="00000000-bad1-bad1-bad1-badbadbadbad",
                weeksOfCare=999,
                billingId="NOT_A_REAL_SCHEME",
                billingTitle="Wrong Title",
                billingInvoices="WRONG",
            )
        )
        part = nested["planParts"][0]

        self.assertEqual(part["billingProfileId"], BILLING_PROFILE)
        self.assertEqual(part["attendanceScheduleId"], SCHEDULE)
        self.assertEqual(part["billing"]["id"], "ANNUALIZED_V2")
        self.assertEqual(part["billing"]["title"], "Monthly")
        self.assertEqual(part["billing"]["weeksOfCare"], 51)
        self.assertEqual(part["billing"]["invoices"], "ADVANCE")
        self.assertEqual(nested["ruleGroupId"], "01RULEGROUP0000000000000000")
        self.assertNotIn("bogus-rule-group", json.dumps(nested))

    def test_an_institution_absent_from_institution_defaults_is_a_hard_error(self):
        # HDEMPTY is a real institution in the session catalogue (used by
        # CatalogueGapHardErrorTests) but was never added to the
        # institution_defaults fixture -- resolution for it must fail loudly,
        # not fall back to another institution's values.
        nested = flatten.flatten_to_nested(flat_payload(institution="HDEMPTY", monday=""))

        self.assertIn(flatten.ERRORS_KEY, nested)
        self.assertTrue(any("HDEMPTY" in e for e in nested[flatten.ERRORS_KEY]))
        self.assertIsNone(nested["planParts"][0]["billingProfileId"])
        self.assertIsNone(nested["ruleGroupId"])

    def test_resolution_runs_even_with_no_booked_day(self):
        # Unlike session-booking resolution, institution_defaults resolution
        # is not gated on there being a booked day -- every plan part needs a
        # billing profile regardless of what (if anything) is booked.
        nested = flatten.flatten_to_nested(flat_payload(monday=""))

        self.assertNotIn(flatten.ERRORS_KEY, nested)
        self.assertEqual(nested["planParts"][0]["billingProfileId"], BILLING_PROFILE)


if __name__ == "__main__":
    unittest.main()
