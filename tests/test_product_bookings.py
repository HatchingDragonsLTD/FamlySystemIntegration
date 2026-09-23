"""Tests for product bookings (non-funded deals).

Run from the project root:

    python -m unittest discover -s tests -v

No Famly, no network: the REST client is stubbed, and the tests that expect a
hard error assert ZERO calls were made.

Products are billed to a family, so a contradictory setup must BLOCK the
preview rather than be quietly capped or half-applied. That is the difference
from the discount slots, which only warn: there, an excluded discount is
visible and costs nothing; here, guessing would silently bill for something
nobody chose.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.plan_write import hubspot_flatten as flatten
from actions.plan_write import hubspot_intake, input_schema, runner
from actions.read_child_plans.runner import parse_plan
from integrations import slack

logging.disable(logging.CRITICAL)

CHILD = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000005"
SCHEDULE = "00000000-0000-0000-0000-000000000003"
PRODUCT_1 = "aaaaaaaa-0000-0000-0000-000000000001"
PRODUCT_2 = "bbbbbbbb-0000-0000-0000-000000000002"

# Mon/Wed/Thu/Fri booked -- deliberately NOT contiguous, so "the first three"
# is a real ordering question rather than just "the first three weekdays".
FOUR_DAYS = {
    "monday_session": SESSION,
    "wednesday_session": SESSION,
    "thursday_session": SESSION,
    "friday_session": SESSION,
}


def flat_payload(days=None, **overrides) -> dict:
    payload = {
        "childId": CHILD,
        "from": "2026-09-01",
        "attendanceScheduleId": SCHEDULE,
        "weeksOfCare": 51,
        "billingId": "ANNUALIZED_V2",
        "billingTitle": "Monthly",
        "billingInvoices": "ADVANCE",
        "funded": "false",
    }
    payload.update(FOUR_DAYS if days is None else days)
    payload.update(overrides)
    return payload


def bookings_of(payload) -> list:
    return flatten.flatten_to_nested(payload)["planParts"][0]["productBookings"]


def errors_of(payload) -> list:
    return flatten.flatten_to_nested(payload).get(flatten.ERRORS_KEY, [])


class ProductBookingTests(unittest.TestCase):
    def test_quantity_three_over_four_days_books_the_first_three(self):
        bookings = bookings_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="3"
            )
        )

        # Two products on each of three days.
        self.assertEqual(len(bookings), 6)

        days = sorted({b["day"] for b in bookings})
        self.assertEqual(days, ["MONDAY", "THURSDAY", "WEDNESDAY"])
        self.assertNotIn("FRIDAY", [b["day"] for b in bookings])

    def test_both_products_appear_on_each_selected_day_with_amount_one(self):
        bookings = bookings_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="3"
            )
        )

        for day in ("MONDAY", "WEDNESDAY", "THURSDAY"):
            for product in (PRODUCT_1, PRODUCT_2):
                match = [
                    b
                    for b in bookings
                    if b["day"] == day and b["productId"] == product
                ]
                self.assertEqual(len(match), 1, f"{day}/{product}")
                self.assertEqual(match[0]["amount"], 1)

    def test_days_are_selected_in_week_order(self):
        bookings = bookings_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="2"
            )
        )

        # Mon then Wed -- not Thu/Fri, and not payload order.
        self.assertEqual([b["day"] for b in bookings][:2], ["MONDAY", "MONDAY"])
        self.assertEqual(sorted({b["day"] for b in bookings}), ["MONDAY", "WEDNESDAY"])

    def test_quantity_equal_to_booked_days_covers_them_all(self):
        bookings = bookings_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="4"
            )
        )

        self.assertEqual(len(bookings), 8)
        self.assertEqual(
            sorted({b["day"] for b in bookings}),
            ["FRIDAY", "MONDAY", "THURSDAY", "WEDNESDAY"],
        )
        self.assertEqual(errors_of(flat_payload(
            product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="4"
        )), [])

    def test_a_funded_deal_sends_none_of_the_fields_and_books_nothing(self):
        payload = flat_payload(funded="true")

        self.assertEqual(bookings_of(payload), [])
        self.assertEqual(errors_of(payload), [])

    def test_blank_product_fields_are_treated_as_absent(self):
        payload = flat_payload(
            product_1_id="", product_2_id="", addon_quantity=""
        )

        self.assertEqual(bookings_of(payload), [])
        self.assertEqual(errors_of(payload), [])

    def test_quantity_zero_books_nothing_without_erroring(self):
        payload = flat_payload(
            product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="0"
        )

        self.assertEqual(bookings_of(payload), [])
        self.assertEqual(errors_of(payload), [])


class ProductHardErrorTests(unittest.TestCase):
    """These must block: capping or guessing would bill someone wrongly."""

    def test_quantity_exceeding_booked_days_is_an_error(self):
        errors = errors_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="5"
            )
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0], "addon_quantity (5) exceeds the number of booked days (4)"
        )

    def test_nothing_is_booked_when_the_quantity_is_too_high(self):
        bookings = bookings_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="5"
            )
        )
        # Not capped at four -- nothing at all.
        self.assertEqual(bookings, [])

    def test_only_product_1_sent_is_an_error(self):
        errors = errors_of(flat_payload(product_1_id=PRODUCT_1))

        self.assertEqual(len(errors), 1)
        self.assertIn("half-specified", errors[0])
        self.assertIn("product_2_id", errors[0])
        self.assertIn("addon_quantity", errors[0])

    def test_only_product_2_sent_is_an_error(self):
        errors = errors_of(flat_payload(product_2_id=PRODUCT_2))

        self.assertIn("half-specified", errors[0])
        self.assertIn("product_1_id", errors[0])

    def test_quantity_without_products_is_an_error(self):
        errors = errors_of(flat_payload(addon_quantity="2"))

        self.assertIn("half-specified", errors[0])
        self.assertIn("product_1_id", errors[0])
        self.assertIn("product_2_id", errors[0])

    def test_a_non_numeric_quantity_is_an_error(self):
        errors = errors_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="three"
            )
        )
        self.assertIn("not an integer", errors[0])

    def test_a_negative_quantity_is_an_error(self):
        errors = errors_of(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="-1"
            )
        )
        self.assertIn("cannot be negative", errors[0])

    def test_these_are_hard_errors_not_advisory_warnings(self):
        nested = flatten.flatten_to_nested(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="5"
            )
        )

        # The hard channel, not the warning channel.
        self.assertIn(flatten.ERRORS_KEY, nested)
        self.assertNotIn(flatten.PROBLEMS_KEY, nested)

    def test_validate_reports_them_alongside_its_own_errors(self):
        nested = flatten.flatten_to_nested(
            flat_payload(
                product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="5"
            )
        )
        errors = input_schema.validate(input_schema.from_dict(nested))

        self.assertTrue(any("addon_quantity (5) exceeds" in e for e in errors))


class ProductPreviewTests(unittest.TestCase):
    """End to end: a bad setup reaches no Famly call at all."""

    def setUp(self):
        self.calls = []
        outer = self

        class FakeRest:
            def post(self, path, params=None, json_body=None):
                outer.calls.append(json_body)
                return {
                    "id": "plan-9",
                    "childId": CHILD,
                    "planParts": [],
                    "behaviors": [],
                }

        patcher = mock.patch.object(runner, "RestClient", lambda *a, **k: FakeRest())
        patcher.start()
        self.addCleanup(patcher.stop)

        env = mock.patch.dict("os.environ", {"FAMLY_ACCESS_TOKEN": "test-token"})
        env.start()
        self.addCleanup(env.stop)

    def intake(self, **overrides):
        return hubspot_intake.handle_intake(flat_payload(**overrides), version=3)

    def test_quantity_exceeding_booked_days_blocks_with_no_famly_call(self):
        result = self.intake(
            product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="5"
        )

        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])
        self.assertTrue(
            any("exceeds the number of booked days" in e for e in result.errors)
        )

    def test_a_half_specified_setup_blocks_with_no_famly_call(self):
        result = self.intake(product_1_id=PRODUCT_1)

        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])

    def test_a_valid_setup_previews_and_sends_the_bookings(self):
        result = self.intake(
            product_1_id=PRODUCT_1, product_2_id=PRODUCT_2, addon_quantity="2"
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 1)

        sent = self.calls[0]["plan"]["planParts"][0]["productBookings"]
        self.assertEqual(len(sent), 4)
        self.assertEqual(sorted({b["day"] for b in sent}), ["MONDAY", "WEDNESDAY"])

    def test_a_funded_deal_previews_with_no_product_bookings(self):
        result = self.intake(funded="true")

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertEqual(
            self.calls[0]["plan"]["planParts"][0]["productBookings"], []
        )


class ProductSummaryTests(unittest.TestCase):
    """The approver sees the products by name, in the existing style."""

    def _summary(self, product_titles=None):
        plan = parse_plan(
            {
                "childId": CHILD,
                "from": "2026-09-01",
                "planParts": [
                    {
                        "planPartId": "pp-1",
                        "sessionBookings": [
                            {"sessionId": SESSION, "day": d}
                            for d in ("MONDAY", "WEDNESDAY")
                        ],
                        "productBookings": [
                            {"productId": p, "day": d, "amount": 1}
                            for d in ("MONDAY", "WEDNESDAY")
                            for p in (PRODUCT_1, PRODUCT_2)
                        ],
                    }
                ],
            }
        )
        return slack.build_summary(plan, [], product_titles=product_titles)

    def test_products_are_listed_by_catalogue_name(self):
        text = self._summary(
            {PRODUCT_1: "Meals & Snacks", PRODUCT_2: "Educational Activities"}
        )

        self.assertIn("*Products* (4)", text)
        self.assertIn("• Monday — 1× Meals & Snacks", text)
        self.assertIn("• Monday — 1× Educational Activities", text)
        self.assertIn("• Wednesday — 1× Meals & Snacks", text)

    def test_an_unresolved_product_falls_back_to_its_uuid(self):
        text = self._summary({})
        self.assertIn(f"• Monday — 1× {PRODUCT_1}", text)


if __name__ == "__main__":
    unittest.main()
