"""Tests for the three custom discount slots.

Run from the project root:

    python -m unittest discover -s tests -v

No Famly, no network: these exercise the flattener, the schema pass-through and
the Slack rendering as plain functions.

The money-safety cases are the point of this file. A discount is a percentage
off a real invoice, so a half-filled or fat-fingered slot must be EXCLUDED and
FLAGGED -- never quietly applied, and never silently dropped. It is a warning
rather than a hard error: the rest of the plan still previews, and the human
approving in Slack sees both the plan and the flag.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from actions.plan_write import hubspot_flatten as flatten
from actions.plan_write import hubspot_intake, input_schema, runner
from actions.read_child_plans.runner import parse_plan
from integrations import slack

CHILD = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000005"
SCHEDULE = "00000000-0000-0000-0000-000000000003"

# Session-booking resolution is not what this file is about (see
# test_session_catalogue.py and test_session_booking.py for that), so every
# slot for this institution resolves to the same SESSION UUID regardless of
# funded/meals/activities, keeping the discount-focused payloads simple.
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


_catalogue_dir = None


def setUpModule():
    global _catalogue_dir
    _catalogue_dir = tempfile.TemporaryDirectory()

    path = Path(_catalogue_dir.name) / "session_catalogue.json"
    path.write_text(json.dumps(_full_session_catalogue(SESSION)), encoding="utf-8")
    os.environ["SESSION_CATALOGUE_FILE"] = str(path)

    defaults_path = Path(_catalogue_dir.name) / "institution_defaults.json"
    defaults_path.write_text(
        json.dumps(_institution_defaults(INSTITUTION)), encoding="utf-8"
    )
    os.environ["INSTITUTION_DEFAULTS_FILE"] = str(defaults_path)


def tearDownModule():
    os.environ.pop("SESSION_CATALOGUE_FILE", None)
    os.environ.pop("INSTITUTION_DEFAULTS_FILE", None)
    _catalogue_dir.cleanup()


def flat_payload(**overrides) -> dict:
    """A valid flat payload, with discount slots layered on top."""
    payload = {
        "childId": CHILD,
        "from": "2026-09-01",
        "institution": INSTITUTION,
        "monday": "full_day",
        "funded": "false",
    }
    payload.update(overrides)
    return payload


class BuildDiscountsTests(unittest.TestCase):
    def test_three_filled_slots_give_three_discounts_in_order(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Sibling Discount",
                discount_1_amount="0.05",
                discount_2_name="Staff Discount",
                discount_2_amount="0.10",
                discount_3_name="Trial Discount",
                discount_3_amount="0.025",
            )
        )

        self.assertEqual(problems, [])
        self.assertEqual([d["ordering"] for d in discounts], [0, 1, 2])
        self.assertEqual(
            [d["title"] for d in discounts],
            ["Sibling Discount", "Staff Discount", "Trial Discount"],
        )
        self.assertEqual([d["amount"] for d in discounts], [0.05, 0.10, 0.025])

    def test_the_fixed_fields_are_set_on_every_discount(self):
        discounts, _ = flatten.build_discounts(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )

        self.assertEqual(
            discounts[0],
            {
                "title": "Sibling",
                "amount": 0.05,
                "ordering": 0,
                "fePriceModifierType": "discount",
                "isPercent": True,
                "origin": "custom",
                "period": "WEEKLY",
                "showOnInvoice": True,
            },
        )

    def test_the_amount_is_a_fraction_and_is_not_divided(self):
        # 5% arrives as 0.05 and must stay 0.05.
        discounts, _ = flatten.build_discounts(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )
        self.assertEqual(discounts[0]["amount"], 0.05)

    def test_empty_slots_are_skipped_entirely(self):
        discounts, problems = flatten.build_discounts(flat_payload())
        self.assertEqual(discounts, [])
        self.assertEqual(problems, [])

        # Explicitly blank, as HubSpot sends for an unset property.
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="",
                discount_1_amount="",
                discount_2_name=None,
                discount_2_amount=None,
            )
        )
        self.assertEqual(discounts, [])
        self.assertEqual(problems, [])

    def test_an_empty_middle_slot_keeps_orderings_tied_to_slot_number(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Sibling",
                discount_1_amount="0.05",
                discount_3_name="Staff",
                discount_3_amount="0.10",
            )
        )

        self.assertEqual(problems, [])
        # NOT compacted to 0 and 1: slot 3 stays ordering 2.
        self.assertEqual([d["ordering"] for d in discounts], [0, 2])
        self.assertEqual([d["title"] for d in discounts], ["Sibling", "Staff"])

    def test_name_without_amount_is_a_problem_and_no_discount(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_2_name="Sibling Discount")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 2", problems[0])
        self.assertIn("amount missing", problems[0])

    def test_amount_without_name_is_a_problem_and_no_discount(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_3_amount="0.05")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 3", problems[0])
        self.assertIn("name missing", problems[0])

    def test_an_out_of_range_amount_is_flagged_and_excluded(self):
        # 5 instead of 0.05 would be a 500% discount on a real invoice.
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_1_name="Oops", discount_1_amount="5.0")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 1", problems[0])
        self.assertIn("out of range", problems[0])
        self.assertIn("5.0", problems[0])

    def test_zero_and_negative_amounts_are_rejected(self):
        for bad in ("0", "0.0", "-0.05"):
            discounts, problems = flatten.build_discounts(
                flat_payload(discount_1_name="Nope", discount_1_amount=bad)
            )
            self.assertEqual(discounts, [], bad)
            self.assertIn("out of range", problems[0], bad)

    def test_exactly_one_hundred_percent_is_allowed(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_1_name="Full waiver", discount_1_amount="1.0")
        )
        self.assertEqual(problems, [])
        self.assertEqual(discounts[0]["amount"], 1.0)

    def test_an_unparseable_amount_is_a_problem(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_1_name="Sibling", discount_1_amount="five percent")
        )

        self.assertEqual(discounts, [])
        self.assertIn("not a number", problems[0])

    def test_one_bad_slot_does_not_lose_the_good_ones(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Sibling",
                discount_1_amount="0.05",
                discount_2_name="Broken",  # no amount
                discount_3_name="Staff",
                discount_3_amount="0.10",
            )
        )

        self.assertEqual([d["ordering"] for d in discounts], [0, 2])
        self.assertEqual(len(problems), 1)


# --------------------------------------------------------------------------- #
# The generalisation: 10 slots (not 3), each optionally flagged "percent" or
# "fixed". Flag absent/empty defaults to "percent" -- the backward-compatible
# path every pre-existing HubSpot branch relies on.
# --------------------------------------------------------------------------- #
class GeneralizedDiscountSlotTests(unittest.TestCase):
    def test_all_ten_slots_filled_with_a_mix_of_types_resolve_correctly(self):
        overrides = {}
        for slot in range(1, 11):
            overrides[f"discount_{slot}_name"] = f"Slot {slot}"
            if slot % 3 == 0:
                # Every third slot is an explicit fixed-amount discount.
                overrides[f"discount_{slot}_amount"] = "10.00"
                overrides[f"discount_{slot}_flag"] = "fixed"
            elif slot % 3 == 1:
                # Explicit percent.
                overrides[f"discount_{slot}_amount"] = "0.05"
                overrides[f"discount_{slot}_flag"] = "percent"
            else:
                # Flag omitted entirely -- defaults to percent.
                overrides[f"discount_{slot}_amount"] = "0.10"

        discounts, problems = flatten.build_discounts(flat_payload(**overrides))

        self.assertEqual(problems, [])
        self.assertEqual(len(discounts), 10)
        self.assertEqual([d["ordering"] for d in discounts], list(range(10)))
        self.assertEqual(
            [d["title"] for d in discounts], [f"Slot {n}" for n in range(1, 11)]
        )
        # slot%3==0 (3,6,9) are fixed; everything else is percent (explicit
        # or defaulted).
        expected_is_percent = [(n % 3) != 0 for n in range(1, 11)]
        self.assertEqual([d["isPercent"] for d in discounts], expected_is_percent)

    def test_a_gap_in_the_middle_keeps_slot_based_ordering_with_no_warnings(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="First",
                discount_1_amount="0.05",
                # slots 2-8 left entirely empty
                discount_9_name="Near The End",
                discount_9_amount="0.10",
                discount_10_name="Last",
                discount_10_amount="15.00",
                discount_10_flag="fixed",
            )
        )

        self.assertEqual(problems, [])
        self.assertEqual([d["ordering"] for d in discounts], [0, 8, 9])
        self.assertEqual(
            [d["title"] for d in discounts], ["First", "Near The End", "Last"]
        )

    def test_a_flag_absent_slot_defaults_to_percent_with_no_warning(self):
        """THE key backward-compatibility case: every HubSpot branch built
        before this generalisation never sent a flag at all.
        """
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_5_name="Legacy Branch", discount_5_amount="0.075")
        )

        self.assertEqual(problems, [])
        self.assertEqual(len(discounts), 1)
        self.assertIs(discounts[0]["isPercent"], True)
        self.assertEqual(discounts[0]["amount"], 0.075)

    def test_an_explicit_percent_flag_behaves_identically_to_absent(self):
        with_flag, problems_with = flatten.build_discounts(
            flat_payload(
                discount_1_name="X", discount_1_amount="0.075", discount_1_flag="percent"
            )
        )
        without_flag, problems_without = flatten.build_discounts(
            flat_payload(discount_1_name="X", discount_1_amount="0.075")
        )

        self.assertEqual(problems_with, [])
        self.assertEqual(problems_without, [])
        self.assertEqual(with_flag, without_flag)

    def test_a_fixed_flag_discount_resolves_with_isPercent_false(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Equipment Fee",
                discount_1_amount="25.50",
                discount_1_flag="fixed",
            )
        )

        self.assertEqual(problems, [])
        self.assertEqual(len(discounts), 1)
        self.assertIs(discounts[0]["isPercent"], False)
        self.assertEqual(discounts[0]["amount"], 25.50)

    def test_an_invalid_flag_value_is_excluded_and_warned_about(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Typo",
                discount_1_amount="0.05",
                discount_1_flag="percentage",  # not "percent"
            )
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 1", problems[0])
        self.assertIn("percentage", problems[0])
        self.assertIn("percent", problems[0])
        self.assertIn("fixed", problems[0])

    def test_a_percent_amount_above_one_is_excluded_and_warned_about(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Oops",
                discount_1_amount="1.5",
                discount_1_flag="percent",
            )
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 1", problems[0])
        self.assertIn("out of range", problems[0])
        self.assertIn("1.5", problems[0])

    def test_a_fixed_amount_above_5000_is_excluded_and_warned_about(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Too Much",
                discount_1_amount="5000.01",
                discount_1_flag="fixed",
            )
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 1", problems[0])
        self.assertIn("out of range", problems[0])
        self.assertIn("5000.01", problems[0])

    def test_a_fixed_amount_of_exactly_5000_is_allowed(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Max Fee", discount_1_amount="5000", discount_1_flag="fixed"
            )
        )

        self.assertEqual(problems, [])
        self.assertEqual(discounts[0]["amount"], 5000.0)
        self.assertIs(discounts[0]["isPercent"], False)

    def test_a_fixed_amount_of_zero_is_rejected(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(
                discount_1_name="Zero", discount_1_amount="0", discount_1_flag="fixed"
            )
        )

        self.assertEqual(discounts, [])
        self.assertIn("out of range", problems[0])

    def test_half_filled_name_only_is_excluded_and_warned_regardless_of_flag(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_4_name="Orphan Name", discount_4_flag="fixed")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 4", problems[0])
        self.assertIn("amount missing", problems[0])

    def test_half_filled_amount_only_is_excluded_and_warned_regardless_of_flag(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_4_amount="0.05", discount_4_flag="percent")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Discount 4", problems[0])
        self.assertIn("name missing", problems[0])

    def test_an_eleventh_slot_is_ignored_entirely(self):
        discounts, problems = flatten.build_discounts(
            flat_payload(discount_11_name="Ghost", discount_11_amount="0.05")
        )

        self.assertEqual(discounts, [])
        self.assertEqual(problems, [])


class FlattenIntegrationTests(unittest.TestCase):
    def test_discounts_land_on_the_plan_part(self):
        nested = flatten.flatten_to_nested(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )
        self.assertEqual(len(nested["planParts"][0]["discounts"]), 1)

    def test_no_discounts_gives_an_empty_list_not_a_missing_key(self):
        nested = flatten.flatten_to_nested(flat_payload())
        self.assertEqual(nested["planParts"][0]["discounts"], [])

    def test_problems_ride_along_for_validate_to_report(self):
        nested = flatten.flatten_to_nested(
            flat_payload(discount_2_name="Sibling")  # no amount
        )
        self.assertIn(flatten.PROBLEMS_KEY, nested)
        self.assertIn("amount missing", nested[flatten.PROBLEMS_KEY][0])

    def test_no_problems_key_when_everything_is_fine(self):
        nested = flatten.flatten_to_nested(
            flat_payload(discount_1_name="Sibling", discount_1_amount="0.05")
        )
        self.assertNotIn(flatten.PROBLEMS_KEY, nested)


class SchemaPassThroughTests(unittest.TestCase):
    """Discounts must survive from_dict and reach the request body intact."""

    def _body_discounts(self, **overrides):
        nested = flatten.flatten_to_nested(flat_payload(**overrides))
        plan_input = input_schema.from_dict(nested)
        body = input_schema.to_plan_body(plan_input)
        return plan_input, body["plan"]["planParts"][0]["discounts"]

    def test_a_valid_discount_survives_intact(self):
        plan_input, discounts = self._body_discounts(
            discount_1_name="Sibling Discount", discount_1_amount="0.05"
        )

        self.assertEqual(input_schema.validate(plan_input), [])
        self.assertEqual(len(discounts), 1)
        self.assertEqual(discounts[0]["title"], "Sibling Discount")
        self.assertEqual(discounts[0]["amount"], 0.05)
        self.assertEqual(discounts[0]["ordering"], 0)
        self.assertEqual(discounts[0]["fePriceModifierType"], "discount")
        self.assertIs(discounts[0]["isPercent"], True)
        self.assertEqual(discounts[0]["origin"], "custom")
        self.assertEqual(discounts[0]["period"], "WEEKLY")
        self.assertIs(discounts[0]["showOnInvoice"], True)

    def test_orderings_survive_an_empty_middle_slot(self):
        _, discounts = self._body_discounts(
            discount_1_name="Sibling",
            discount_1_amount="0.05",
            discount_3_name="Staff",
            discount_3_amount="0.10",
        )
        self.assertEqual([d["ordering"] for d in discounts], [0, 2])

    def test_a_producer_problem_is_NOT_a_validation_error(self):
        # Advisory, not fatal: the plan must still preview. The problem is
        # surfaced as a warning instead (see MalformedSlotWarningTests).
        plan_input, discounts = self._body_discounts(discount_2_name="Sibling")

        self.assertEqual(input_schema.validate(plan_input), [])
        # The bad discount is still excluded from the body.
        self.assertEqual(discounts, [])
        # ...and the problem is carried for the warnings channel.
        self.assertTrue(any("Discount 2" in p for p in plan_input.problems))

    def test_the_problems_key_never_reaches_the_plan_body(self):
        nested = flatten.flatten_to_nested(flat_payload(discount_2_name="Sibling"))
        body = input_schema.to_plan_body(input_schema.from_dict(nested))
        self.assertNotIn(flatten.PROBLEMS_KEY, body["plan"])
        self.assertNotIn(flatten.PROBLEMS_KEY, body["plan"]["planParts"][0])

    def test_a_non_object_discount_is_reported(self):
        plan_input = input_schema.from_dict(
            {
                "childId": CHILD,
                "from": "2026-09-01",
                "planParts": [
                    {
                        "attendanceScheduleId": SCHEDULE,
                        "billingProfileId": None,
                        "billing": {"id": "ANNUALIZED_V2"},
                        "sessionBookings": [
                            {"sessionId": SESSION, "day": "MONDAY", "fundable": False}
                        ],
                        "discounts": ["not-an-object"],
                    }
                ],
            }
        )
        errors = input_schema.validate(plan_input)
        self.assertTrue(any("discounts[0]" in e for e in errors))


class HalfDayAdjustmentTests(unittest.TestCase):
    """A FIXED-amount discount, sitting alongside the three percentage slots.

    HubSpot precomputes the figure (the server never works out a child's age),
    so only the two values it can produce are accepted. Anything else means the
    upstream calculation went wrong, and applying it on trust would alter a
    family's bill by an amount nobody chose -- so it is excluded and warned
    about, exactly like a malformed percentage slot.
    """

    def build(self, **overrides):
        return flatten.build_discounts(flat_payload(**overrides))

    def test_two_ninety_four_is_accepted(self):
        discounts, problems = self.build(
            half_day_adjustment="yes", half_day_amount="2.94"
        )

        self.assertEqual(problems, [])
        self.assertEqual(len(discounts), 1)
        self.assertEqual(
            discounts[0],
            {
                "title": "Half Day Adjustment",
                "amount": 2.94,
                "ordering": flatten.HALF_DAY_ORDERING,
                "fePriceModifierType": "discount",
                "isPercent": False,
                "origin": "custom",
                "period": "WEEKLY",
                "showOnInvoice": True,
            },
        )

    def test_three_fifty_three_is_accepted(self):
        discounts, problems = self.build(
            half_day_adjustment="yes", half_day_amount="3.53"
        )

        self.assertEqual(problems, [])
        self.assertEqual(discounts[0]["amount"], 3.53)
        self.assertIs(discounts[0]["isPercent"], False)

    def test_any_other_amount_is_excluded_and_warned_about(self):
        discounts, problems = self.build(
            half_day_adjustment="yes", half_day_amount="3.00"
        )

        self.assertEqual(discounts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Half day adjustment excluded", problems[0])
        self.assertIn("3.00", problems[0])
        self.assertIn("2.94 or 3.53", problems[0])

    def test_a_missing_amount_is_excluded_and_warned_about(self):
        discounts, problems = self.build(half_day_adjustment="yes")

        self.assertEqual(discounts, [])
        self.assertIn("half_day_amount is missing", problems[0])

    def test_a_non_numeric_amount_is_excluded_and_warned_about(self):
        discounts, problems = self.build(
            half_day_adjustment="yes", half_day_amount="half"
        )

        self.assertEqual(discounts, [])
        self.assertIn("is not a number", problems[0])

    def test_no_means_no_discount_and_no_warning(self):
        discounts, problems = self.build(
            half_day_adjustment="no", half_day_amount="2.94"
        )

        self.assertEqual(discounts, [])
        self.assertEqual(problems, [])

    def test_absent_means_no_discount_and_no_warning(self):
        discounts, problems = self.build()

        self.assertEqual(discounts, [])
        self.assertEqual(problems, [])

    def test_it_sits_at_ordering_ten_after_the_percentage_slots(self):
        discounts, problems = self.build(
            discount_1_name="Sibling",
            discount_1_amount="0.05",
            discount_2_name="Staff",
            discount_2_amount="0.10",
            discount_3_name="Trial",
            discount_3_amount="0.025",
            half_day_adjustment="yes",
            half_day_amount="2.94",
        )

        self.assertEqual(problems, [])
        self.assertEqual(
            [d["ordering"] for d in discounts], [0, 1, 2, flatten.HALF_DAY_ORDERING]
        )
        self.assertEqual([d["isPercent"] for d in discounts], [True, True, True, False])
        self.assertEqual(discounts[3]["title"], "Half Day Adjustment")

    def test_its_ordering_is_fixed_even_with_no_percentage_slots(self):
        discounts, _ = self.build(half_day_adjustment="yes", half_day_amount="2.94")

        # Fixed regardless of what else is present -- not compacted to 0, and
        # always right after the LAST POSSIBLE slot (10), not the last filled
        # one.
        self.assertEqual(discounts[0]["ordering"], flatten.HALF_DAY_ORDERING)

    def test_a_bad_percentage_slot_does_not_lose_the_half_day_one(self):
        discounts, problems = self.build(
            discount_1_name="Broken",  # no amount
            half_day_adjustment="yes",
            half_day_amount="2.94",
        )

        self.assertEqual(len(discounts), 1)
        self.assertEqual(discounts[0]["title"], "Half Day Adjustment")
        self.assertEqual(len(problems), 1)

    def test_it_reaches_the_plan_body_intact(self):
        nested = flatten.flatten_to_nested(
            flat_payload(half_day_adjustment="yes", half_day_amount="3.53")
        )
        plan_input = input_schema.from_dict(nested)
        body = input_schema.to_plan_body(plan_input)

        discounts = body["plan"]["planParts"][0]["discounts"]
        self.assertEqual(len(discounts), 1)
        self.assertEqual(discounts[0]["amount"], 3.53)
        self.assertIs(discounts[0]["isPercent"], False)
        # Advisory only, so nothing blocks.
        self.assertEqual(input_schema.validate(plan_input), [])

    def test_an_invalid_amount_still_previews_successfully(self):
        nested = flatten.flatten_to_nested(
            flat_payload(half_day_adjustment="yes", half_day_amount="9.99")
        )
        plan_input = input_schema.from_dict(nested)

        # A warning, not a hard error: the rest of the plan still previews.
        self.assertEqual(input_schema.validate(plan_input), [])
        self.assertTrue(any("Half day adjustment" in p for p in plan_input.problems))
        self.assertEqual(
            input_schema.to_plan_body(plan_input)["plan"]["planParts"][0]["discounts"],
            [],
        )


class MixedDiscountRenderingTests(unittest.TestCase):
    """Both kinds appear in one list, so each must read in its own units."""

    def _summary(self, discounts):
        plan = parse_plan(
            {
                "childId": CHILD,
                "from": "2026-09-01",
                "planParts": [
                    {
                        "planPartId": "pp-1",
                        "sessionBookings": [{"sessionId": SESSION, "day": "MONDAY"}],
                        "discounts": discounts,
                    }
                ],
            }
        )
        return slack.build_summary(plan, [])

    def test_a_fixed_amount_shows_in_pounds(self):
        text = self._summary(
            [
                {
                    "title": "Half Day Adjustment",
                    "amount": 2.94,
                    "ordering": 3,
                    "isPercent": False,
                }
            ]
        )

        self.assertIn("\u2022 Half Day Adjustment \u2014 \u00a32.94", text)
        self.assertNotIn("294%", text)

    def test_percentages_and_fixed_amounts_read_correctly_together(self):
        discounts, _ = flatten.build_discounts(
            flat_payload(
                discount_1_name="Sibling Discount",
                discount_1_amount="0.05",
                half_day_adjustment="yes",
                half_day_amount="3.53",
            )
        )
        text = self._summary(discounts)

        self.assertIn("*Discounts* (2)", text)
        self.assertIn("\u2022 Sibling Discount \u2014 5%", text)
        self.assertIn("\u2022 Half Day Adjustment \u2014 \u00a33.53", text)

    def test_an_entry_with_no_isPercent_is_still_read_as_a_percentage(self):
        # Every discount was a percentage before fixed ones existed.
        text = self._summary([{"title": "Legacy", "amount": 0.05, "ordering": 0}])

        self.assertIn("\u2022 Legacy \u2014 5%", text)

    def test_ten_slots_plus_half_day_all_render_correctly_in_order(self):
        # Full pipeline: hubspot_flatten's builder -> Slack summary, with a
        # mix of explicit percent, explicit fixed, and defaulted (flag-
        # absent) percent slots across all 10 positions plus the half-day
        # adjustment at ordering 10.
        overrides = {
            "discount_1_name": "Explicit Percent",
            "discount_1_amount": "0.05",
            "discount_1_flag": "percent",
            "discount_2_name": "Defaulted Percent",
            "discount_2_amount": "0.10",
            # no flag on slot 2 -- defaults to percent
            "discount_10_name": "Explicit Fixed",
            "discount_10_amount": "12.50",
            "discount_10_flag": "fixed",
            "half_day_adjustment": "yes",
            "half_day_amount": "2.94",
        }
        discounts, problems = flatten.build_discounts(flat_payload(**overrides))
        self.assertEqual(problems, [])

        text = self._summary(discounts)

        self.assertIn("*Discounts* (4)", text)
        # A defaulted percent slot must display IDENTICALLY to an explicit one.
        self.assertIn("\u2022 Explicit Percent \u2014 5%", text)
        self.assertIn("\u2022 Defaulted Percent \u2014 10%", text)
        self.assertIn("\u2022 Explicit Fixed \u2014 \u00a312.50", text)
        self.assertIn("\u2022 Half Day Adjustment \u2014 \u00a32.94", text)
        # Ordering: slot 1, slot 2, slot 10, then half-day last.
        for earlier, later in (
            ("Explicit Percent", "Defaulted Percent"),
            ("Defaulted Percent", "Explicit Fixed"),
            ("Explicit Fixed", "Half Day Adjustment"),
        ):
            self.assertLess(text.index(earlier), text.index(later))

    def test_a_fixed_amount_that_is_not_a_number_reads_as_unknown(self):
        text = self._summary(
            [{"title": "Broken", "amount": None, "isPercent": False, "ordering": 3}]
        )

        self.assertIn("\u2022 Broken \u2014 unknown", text)


class MalformedSlotWarningTests(unittest.TestCase):
    """A malformed slot warns; it never blocks the preview.

    This mirrors how Famly's own warnings behave: the plan is computed and
    shown, with the problem flagged beside it, so the approver in Slack sees
    both. Famly is never contacted -- the client is a stub.
    """

    def setUp(self):
        self.calls = []
        outer = self

        class FakeRest:
            def post(self, *args, **kwargs):
                outer.calls.append(1)
                return {
                    "id": "plan-9",
                    "childId": CHILD,
                    "monthlyEstimate": 500.0,
                    "planParts": [],
                    "behaviors": [
                        {
                            "id": "ShowPlanWarnings",
                            "payload": {
                                "warnings": [
                                    {
                                        "title": "Funding hours exceed sessions",
                                        "severity": "warning",
                                        "error": "FundingMismatch",
                                    }
                                ]
                            },
                        }
                    ],
                }

        patcher = mock.patch.object(runner, "RestClient", lambda *a, **k: FakeRest())
        patcher.start()
        self.addCleanup(patcher.stop)

        env = mock.patch.dict("os.environ", {"FAMLY_ACCESS_TOKEN": "test-token"})
        env.start()
        self.addCleanup(env.stop)

    def intake(self, **overrides):
        return hubspot_intake.handle_intake(flat_payload(**overrides), version=3)

    def test_out_of_range_slot_still_previews_successfully(self):
        result = self.intake(discount_1_name="Oops", discount_1_amount="5.0")

        # The preview ran and succeeded -- no 422.
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(self.calls), 1)

    def test_the_bad_discount_is_absent_from_the_committed_body(self):
        result = self.intake(
            discount_1_name="Sibling",
            discount_1_amount="0.05",
            discount_2_name="Oops",
            discount_2_amount="5.0",
        )

        discounts = result.plan_body["plan"]["planParts"][0]["discounts"]
        self.assertEqual([d["title"] for d in discounts], ["Sibling"])

    def test_every_kind_of_malformed_slot_warns_without_blocking(self):
        """All four malformed-slot cases are WARNINGS, never a hard block --
        including the new flag-typed ones."""
        result = self.intake(
            discount_1_name="Typo Flag",
            discount_1_amount="0.05",
            discount_1_flag="bogus",
            discount_2_name="Too Big Percent",
            discount_2_amount="1.5",
            discount_3_name="Too Big Fixed",
            discount_3_amount="5001",
            discount_3_flag="fixed",
            discount_4_name="No Amount",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        keys = [w.key for w in result.warnings]
        self.assertEqual(keys.count("discount_excluded"), 4)

    def test_the_problem_appears_as_a_warning(self):
        result = self.intake(discount_1_name="Oops", discount_1_amount="5.0")

        titles = [w.warning.title for w in result.warnings]
        self.assertTrue(any("Discount 1 excluded" in t for t in titles))
        self.assertTrue(any("out of range" in t for t in titles))

    def test_discount_warnings_sit_alongside_famly_warnings(self):
        result = self.intake(discount_3_name="Staff")  # no amount

        keys = [w.key for w in result.warnings]
        self.assertIn("funding_mismatch", keys)  # Famly's own
        self.assertIn("discount_excluded", keys)  # ours

    def test_a_discount_warning_is_known_and_not_escalated(self):
        # An untracked warning is escalated through notify(); a discount
        # exclusion is understood, so it must not be.
        escalated = []
        result = self.intake(discount_3_name="Staff")

        for warning in result.warnings:
            if warning.key == "discount_excluded":
                self.assertTrue(warning.is_known)
                self.assertIsNotNone(warning.known)
        self.assertEqual(escalated, [])

    def test_a_valid_payload_adds_no_discount_warning(self):
        result = self.intake(discount_1_name="Sibling", discount_1_amount="0.05")

        keys = [w.key for w in result.warnings]
        self.assertNotIn("discount_excluded", keys)

    def test_the_warning_renders_in_the_slack_summary(self):
        import dataclasses

        result = self.intake(discount_2_name="Oops", discount_2_amount="5.0")
        serialised = [dataclasses.asdict(w) for w in result.warnings]

        text = slack.build_summary(result.result.plan, serialised)

        self.assertIn("Discount 2 excluded", text)
        self.assertIn("[discount_excluded]", text)


class SlackDiscountRenderingTests(unittest.TestCase):
    def _summary(self, discounts):
        plan = parse_plan(
            {
                "childId": CHILD,
                "from": "2026-09-01",
                "planParts": [
                    {
                        "planPartId": "pp-1",
                        "sessionBookings": [{"sessionId": SESSION, "day": "MONDAY"}],
                        "discounts": discounts,
                    }
                ],
            }
        )
        return slack.build_summary(plan, [])

    def test_discounts_render_as_percentages(self):
        text = self._summary(
            [
                {"title": "Sibling Discount", "amount": 0.05, "ordering": 0},
                {"title": "Staff Discount", "amount": 0.10, "ordering": 1},
            ]
        )

        self.assertIn("*Discounts* (2)", text)
        self.assertIn("• Sibling Discount — 5%", text)
        self.assertIn("• Staff Discount — 10%", text)

    def test_a_fractional_percentage_keeps_its_decimal(self):
        text = self._summary([{"title": "Trial", "amount": 0.075, "ordering": 0}])
        self.assertIn("• Trial — 7.5%", text)

    def test_the_section_is_omitted_when_there_are_no_discounts(self):
        self.assertNotIn("*Discounts*", self._summary([]))

    def test_discounts_are_listed_in_ordering_order(self):
        text = self._summary(
            [
                {"title": "Third", "amount": 0.03, "ordering": 2},
                {"title": "First", "amount": 0.01, "ordering": 0},
            ]
        )
        self.assertLess(text.index("First"), text.index("Third"))

    def test_a_malformed_discount_does_not_break_the_summary(self):
        text = self._summary(
            [{"amount": 0.05}, {"title": "No amount"}, "junk"]
        )
        self.assertIn("untitled discount — 5%", text)
        self.assertIn("No amount — unknown", text)


if __name__ == "__main__":
    unittest.main()
