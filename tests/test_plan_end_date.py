"""Tests for the plan end date (`to`).

Run from the project root:

    python -m unittest discover -s tests -v

BUG THIS PINS: `PlanInput` had no `to`/`to_date` field at all, so
`hubspot_flatten`'s correctly-computed `"to"` was read by nothing in
`from_dict` and `to_plan_body` had nothing to put in the final body -- a plan
end date was silently dropped on every write. `from`/`from_date` worked, `to`
did not, purely because the schema forgot the second field.

The end-to-end tests below exist because each of `hubspot_flatten`,
`from_dict` and `to_plan_body` could pass its own unit tests while the SEAM
between them stayed broken -- which is exactly what happened here.
"""

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from actions.plan_write import hubspot_flatten as flatten
from actions.plan_write import input_schema

CHILD = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000005"
SCHEDULE = "00000000-0000-0000-0000-000000000003"
INSTITUTION = "HDCITY"


def _slot_map(uuid_value):
    return {"morning": uuid_value, "afternoon": uuid_value, "full_day": uuid_value}


def _full_session_catalogue(uuid_value):
    return {
        "institutions": {
            INSTITUTION: {
                "funded": {
                    "with_meals_and_activities": _slot_map(uuid_value),
                    "no_meals": _slot_map(uuid_value),
                    "no_activities": _slot_map(uuid_value),
                    "neither": _slot_map(uuid_value),
                },
                "non_funded": _slot_map(uuid_value),
            }
        }
    }


BILLING_PROFILE = "cccccccc-0000-0000-0000-000000000099"


def _institution_defaults(institution) -> dict:
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
        "monday": "full_day",
        "funded": "false",
    }
    payload.update(overrides)
    return payload


def _epoch_ms(iso_date: str) -> int:
    """The epoch-milliseconds form HubSpot sends for a date property."""
    dt = datetime.combine(date.fromisoformat(iso_date), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Unit: from_dict reads it, validate() checks it, to_plan_body emits it.
# --------------------------------------------------------------------------- #
class FromDictTests(unittest.TestCase):
    def test_reads_the_camel_case_to_field(self):
        plan_input = input_schema.from_dict({"to": "2026-12-19"})
        self.assertEqual(plan_input.to_date, "2026-12-19")

    def test_reads_the_snake_case_to_date_field(self):
        plan_input = input_schema.from_dict({"to_date": "2026-12-19"})
        self.assertEqual(plan_input.to_date, "2026-12-19")

    def test_absent_to_is_none(self):
        plan_input = input_schema.from_dict({"from": "2026-09-01"})
        self.assertIsNone(plan_input.to_date)

    def test_explicit_null_to_is_none(self):
        plan_input = input_schema.from_dict({"to": None})
        self.assertIsNone(plan_input.to_date)


class ValidateToDateTests(unittest.TestCase):
    def _plan_input(self, **overrides):
        base = dict(
            child_id="11111111-1111-1111-1111-111111111111",
            from_date="2026-09-01",
        )
        base.update(overrides)
        return input_schema.PlanInput(
            **base,
            plan_parts=[
                input_schema.PlanPartInput(
                    attendance_schedule_id="22222222-2222-2222-2222-222222222222",
                    billing=input_schema.BillingInput(id="ANNUALIZED_V2"),
                    session_bookings=[
                        input_schema.SessionBookingInput(
                            session_id=SESSION, day="MONDAY", fundable=False
                        )
                    ],
                )
            ],
        )

    def test_a_valid_to_date_is_not_an_error(self):
        errors = input_schema.validate(self._plan_input(to_date="2026-12-19"))
        self.assertEqual([e for e in errors if e.startswith("to:")], [])

    def test_a_missing_to_date_is_open_ended_not_an_error(self):
        # This is the normal, expected case -- every prior real capture sends
        # "to": null, and it must never be flagged as missing data.
        errors = input_schema.validate(self._plan_input(to_date=None))
        self.assertEqual([e for e in errors if e.startswith("to:")], [])

    def test_a_malformed_to_date_is_an_error(self):
        errors = input_schema.validate(self._plan_input(to_date="not-a-date"))
        matching = [e for e in errors if e.startswith("to:")]
        self.assertEqual(len(matching), 1)
        self.assertIn("not-a-date", matching[0])
        self.assertIn("ISO date", matching[0])

    def test_the_to_date_error_mirrors_the_from_date_ones_wording(self):
        to_errors = input_schema.validate(self._plan_input(to_date="banana"))
        from_errors = input_schema.validate(self._plan_input(from_date="banana"))

        to_message = next(e for e in to_errors if e.startswith("to:"))
        from_message = next(e for e in from_errors if e.startswith("from:"))

        self.assertEqual(
            to_message.split(":", 1)[1], from_message.split(":", 1)[1]
        )


class ToPlanBodyTests(unittest.TestCase):
    def _plan_input(self, to_date):
        return input_schema.PlanInput(
            child_id="11111111-1111-1111-1111-111111111111",
            from_date="2026-09-01",
            to_date=to_date,
            plan_parts=[
                input_schema.PlanPartInput(
                    attendance_schedule_id="22222222-2222-2222-2222-222222222222",
                    billing=input_schema.BillingInput(id="ANNUALIZED_V2"),
                    session_bookings=[
                        input_schema.SessionBookingInput(
                            session_id=SESSION, day="MONDAY", fundable=False
                        )
                    ],
                )
            ],
        )

    def test_a_present_to_date_survives_into_the_body_unchanged(self):
        body = input_schema.to_plan_body(self._plan_input("2026-12-19"))
        self.assertEqual(body["plan"]["to"], "2026-12-19")

    def test_an_absent_to_date_is_null_in_the_body(self):
        body = input_schema.to_plan_body(self._plan_input(None))
        self.assertIsNone(body["plan"]["to"])
        # And the key is actually present -- an open-ended plan sends "to":
        # null, it does not omit the field.
        self.assertIn("to", body["plan"])


# --------------------------------------------------------------------------- #
# End to end: hubspot_flatten -> from_dict -> to_plan_body. This is the exact
# chain that silently broke -- each function's own unit tests could (and did)
# pass while the seam between them stayed broken.
# --------------------------------------------------------------------------- #
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        path = Path(self._tmp.name) / "session_catalogue.json"
        path.write_text(
            json.dumps(_full_session_catalogue(SESSION)), encoding="utf-8"
        )

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

    def _final_body(self, **overrides):
        nested = flatten.flatten_to_nested(flat_payload(**overrides))
        plan_input = input_schema.from_dict(nested)
        return plan_input, input_schema.to_plan_body(plan_input)

    def test_a_real_to_value_reaches_the_final_plan_body(self):
        # The exact regression: a real end date, sent the way HubSpot sends a
        # date property (epoch milliseconds), must come out the other end
        # as the same ISO date -- not be silently dropped anywhere along
        # flatten_to_nested -> from_dict -> to_plan_body.
        plan_input, body = self._final_body(to=_epoch_ms("2026-12-19"))

        self.assertEqual(plan_input.to_date, "2026-12-19")
        self.assertEqual(body["plan"]["to"], "2026-12-19")
        self.assertEqual(input_schema.validate(plan_input), [])

    def test_an_already_iso_to_value_reaches_the_final_plan_body(self):
        plan_input, body = self._final_body(to="2026-12-19")

        self.assertEqual(body["plan"]["to"], "2026-12-19")

    def test_no_to_value_gives_an_open_ended_plan(self):
        plan_input, body = self._final_body()

        self.assertIsNone(plan_input.to_date)
        self.assertIsNone(body["plan"]["to"])
        self.assertIn("to", body["plan"])
        self.assertEqual(input_schema.validate(plan_input), [])

    def test_an_empty_string_to_value_is_open_ended_not_an_error(self):
        # hubspot_flatten already maps "" -> None for an unset HubSpot property.
        plan_input, body = self._final_body(to="")

        self.assertIsNone(plan_input.to_date)
        self.assertIsNone(body["plan"]["to"])
        self.assertEqual(input_schema.validate(plan_input), [])

    def test_a_malformed_to_value_is_a_validation_error_end_to_end(self):
        # No hyphen, so _to_iso_date's epoch-milliseconds branch runs, fails
        # to parse it as a number, and passes it through unchanged for
        # validate() to catch -- exactly the same path `from` already relies
        # on for an unparseable value.
        nested = flatten.flatten_to_nested(flat_payload(to="notarealdate"))
        plan_input = input_schema.from_dict(nested)

        errors = input_schema.validate(plan_input)
        matching = [e for e in errors if e.startswith("to:")]
        self.assertEqual(len(matching), 1)
        self.assertIn("notarealdate", matching[0])


if __name__ == "__main__":
    unittest.main()
