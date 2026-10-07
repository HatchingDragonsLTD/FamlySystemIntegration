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
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.plan_write import hubspot_flatten as flatten
from actions.plan_write import hubspot_intake, input_schema, runner
from actions.read_child_plans.runner import parse_plan
from integrations import catalogue, slack

logging.disable(logging.CRITICAL)

CHILD = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000005"
SCHEDULE = "00000000-0000-0000-0000-000000000003"
PRODUCT_1 = "aaaaaaaa-0000-0000-0000-000000000001"
PRODUCT_2 = "bbbbbbbb-0000-0000-0000-000000000002"
HALF_1 = "cccccccc-1111-0000-0000-000000000001"  # "(1/2) Meals & Snacks"
HALF_2 = "dddddddd-2222-0000-0000-000000000002"  # "(1/2) Educational ... Extras"

# Session-booking resolution is not what this file is about (see
# test_session_catalogue.py and test_session_booking.py for that), so every
# slot for this institution resolves to the same SESSION UUID regardless of
# funded/meals/activities -- these tests only care about day distribution.
INSTITUTION = "HDCITY"
# A second institution whose addonProducts has the full pair but NO half pair
# (e.g. no "(1/2)" products exist in Famly for it yet).
NO_HALF_INSTITUTION = "HDNOHALF"
SLOT = "full_day"


def _slot_map(uuid_value):
    return {"morning": uuid_value, "afternoon": uuid_value, "full_day": uuid_value}


def _full_session_catalogue(uuid_value):
    entry = {
        "funded": {
            "with_meals_and_activities": _slot_map(uuid_value),
            "no_meals": _slot_map(uuid_value),
            "no_activities": _slot_map(uuid_value),
            "neither": _slot_map(uuid_value),
        },
        "non_funded": _slot_map(uuid_value),
    }
    return {"institutions": {INSTITUTION: entry, NO_HALF_INSTITUTION: entry}}


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

    def entry(addon_products):
        return {
            "ruleGroupId": "01RULEGROUP0000000000000000",
            "schedules": {"all_year_round": bucket, "term_only": bucket},
            "addonProducts": addon_products,
        }

    # Reuses the same PRODUCT_1/PRODUCT_2 constants every test below already
    # asserts against, so resolving via institution_defaults (instead of
    # reading product_1_id/product_2_id straight from the payload) needs no
    # other change to this file's expectations.
    full_pair = {"mealsProductId": PRODUCT_1, "activitiesProductId": PRODUCT_2}
    half_pair = {"halfMealsProductId": HALF_1, "halfActivitiesProductId": HALF_2}

    return {
        "institutions": {
            institution: entry({**full_pair, **half_pair}),
            NO_HALF_INSTITUTION: entry(full_pair),
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


# Mon/Wed/Thu/Fri booked -- deliberately NOT contiguous, so "the first three"
# is a real ordering question rather than just "the first three weekdays".
FOUR_DAYS = {
    "monday": SLOT,
    "wednesday": SLOT,
    "thursday": SLOT,
    "friday": SLOT,
}


def flat_payload(days=None, **overrides) -> dict:
    payload = {
        "childId": CHILD,
        "from": "2026-09-01",
        "institution": INSTITUTION,
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
                addon_quantity="3"
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
                addon_quantity="3"
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
                addon_quantity="2"
            )
        )

        # Mon then Wed -- not Thu/Fri, and not payload order.
        self.assertEqual([b["day"] for b in bookings][:2], ["MONDAY", "MONDAY"])
        self.assertEqual(sorted({b["day"] for b in bookings}), ["MONDAY", "WEDNESDAY"])

    def test_quantity_equal_to_booked_days_covers_them_all_up_to_the_cap(self):
        # Three booked days, quantity three: every one gets products.
        payload = flat_payload(
            days={
                "monday": SLOT,
                "wednesday": SLOT,
                "thursday": SLOT,
            },
            addon_quantity="3",
        )
        bookings = bookings_of(payload)

        self.assertEqual(len(bookings), 6)
        self.assertEqual(
            sorted({b["day"] for b in bookings}),
            ["MONDAY", "THURSDAY", "WEDNESDAY"],
        )
        self.assertEqual(errors_of(payload), [])

    def test_the_cap_limits_a_four_day_plan_to_three(self):
        # Four booked days and quantity four: the cap wins, so the fourth day
        # gets no products even though it was available.
        bookings = bookings_of(
            flat_payload(
                addon_quantity="4"
            )
        )

        self.assertEqual(len(bookings), 6)
        self.assertEqual(
            sorted({b["day"] for b in bookings}),
            ["MONDAY", "THURSDAY", "WEDNESDAY"],
        )
        self.assertNotIn("FRIDAY", [b["day"] for b in bookings])

    def test_a_funded_deal_sends_none_of_the_fields_and_books_nothing(self):
        payload = flat_payload(funded="true")

        self.assertEqual(bookings_of(payload), [])
        self.assertEqual(errors_of(payload), [])

    def test_a_blank_addon_quantity_books_nothing(self):
        payload = flat_payload(addon_quantity="")

        self.assertEqual(bookings_of(payload), [])
        self.assertEqual(errors_of(payload), [])

    def test_quantity_zero_books_nothing_without_erroring(self):
        payload = flat_payload(
            addon_quantity="0"
        )

        self.assertEqual(bookings_of(payload), [])
        self.assertEqual(errors_of(payload), [])


class HubspotProductIdsAreIgnoredTests(unittest.TestCase):
    """HubSpot no longer sends product_1_id/product_2_id -- see
    hubspot_flatten's module docstring. Sending them anyway must change
    NOTHING: institution_defaults is the only authoritative source now.
    """

    def test_sending_bogus_product_ids_does_not_override_the_resolved_ones(self):
        bookings = bookings_of(
            flat_payload(
                addon_quantity="1",
                product_1_id="00000000-bad0-bad0-bad0-badbadbadbad",
                product_2_id="00000000-bad1-bad1-bad1-badbadbadbad",
            )
        )

        product_ids = {b["productId"] for b in bookings}
        self.assertEqual(product_ids, {PRODUCT_1, PRODUCT_2})
        self.assertNotIn("00000000-bad0-bad0-bad0-badbadbadbad", product_ids)
        self.assertNotIn("00000000-bad1-bad1-bad1-badbadbadbad", product_ids)

    def test_sending_only_one_bogus_id_still_resolves_both_from_institution_defaults(self):
        # Under the OLD contract this was a "half-specified" hard error.
        # Under the new one, product_1_id/product_2_id do not exist as a
        # concept at all -- addon_quantity alone is authoritative.
        bookings = bookings_of(flat_payload(addon_quantity="1", product_1_id=PRODUCT_1))

        self.assertEqual(
            {b["productId"] for b in bookings}, {PRODUCT_1, PRODUCT_2}
        )
        self.assertEqual(errors_of(flat_payload(addon_quantity="1", product_1_id=PRODUCT_1)), [])


FULL = frozenset({PRODUCT_1, PRODUCT_2})
HALF = frozenset({HALF_1, HALF_2})

MON_WED_THU_FRI = ["MONDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
ALL_FIVE = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]


class HalfDayAddonTests(unittest.TestCase):
    """addon_quantity is days in halves: `full = min(floor(q), 3)` whole days
    of both full products, then -- if q ends in .5 and floor(q) < 3 -- ONE
    more day of both HALF products ("(1/2) Meals & Snacks", "(1/2)
    Educational Activities & Extras"). Replaces the old round-up-and-cap rule,
    under which 2.5 booked three FULL days.
    """

    def plan(self, quantity, booked=None, **overrides):
        """[(day, product-id set)] in booking order, plus the errors."""
        payload = flat_payload(
            days={day.lower(): SLOT for day in (booked or MON_WED_THU_FRI)},
            addon_quantity=quantity,
            **overrides,
        )
        ordered = []
        for booking in bookings_of(payload):
            if not ordered or ordered[-1][0] != booking["day"]:
                ordered.append((booking["day"], set()))
            ordered[-1][1].add(booking["productId"])
        return [(day, frozenset(ids)) for day, ids in ordered], errors_of(payload)

    # --- each row of the rule --------------------------------------------- #
    def test_zero_books_nothing(self):
        days, errors = self.plan("0")
        self.assertEqual((days, errors), ([], []))

    def test_half_a_day_books_only_half_products_on_the_first_day(self):
        days, errors = self.plan("0.5")
        self.assertEqual(errors, [])
        self.assertEqual(days, [("MONDAY", HALF)])

    def test_one_day_books_the_full_pair_on_the_first_day(self):
        days, errors = self.plan("1")
        self.assertEqual(errors, [])
        self.assertEqual(days, [("MONDAY", FULL)])

    def test_one_and_a_half_is_one_full_day_then_a_half_day(self):
        days, errors = self.plan("1.5")
        self.assertEqual(errors, [])
        self.assertEqual(days, [("MONDAY", FULL), ("WEDNESDAY", HALF)])

    def test_two_days_books_the_full_pair_on_the_first_two_days(self):
        days, errors = self.plan("2")
        self.assertEqual(errors, [])
        self.assertEqual(days, [("MONDAY", FULL), ("WEDNESDAY", FULL)])

    def test_two_and_a_half_is_two_full_days_then_a_half_day(self):
        days, errors = self.plan("2.5")
        self.assertEqual(errors, [])
        # Booked days are Mon/Wed/Thu/Fri, so the half lands on Thursday.
        self.assertEqual(
            days, [("MONDAY", FULL), ("WEDNESDAY", FULL), ("THURSDAY", HALF)]
        )

    def test_three_days_books_three_full_days(self):
        days, errors = self.plan("3")
        self.assertEqual(errors, [])
        self.assertEqual(
            days, [("MONDAY", FULL), ("WEDNESDAY", FULL), ("THURSDAY", FULL)]
        )

    def test_three_and_a_half_is_capped_to_three_full_days_with_no_half(self):
        days, errors = self.plan("3.5")
        self.assertEqual(errors, [])
        self.assertEqual(
            days, [("MONDAY", FULL), ("WEDNESDAY", FULL), ("THURSDAY", FULL)]
        )

    def test_anything_above_three_is_capped_to_three_full_days(self):
        for quantity in ("4", "4.5", "5"):
            days, errors = self.plan(quantity, ALL_FIVE)
            self.assertEqual(errors, [], quantity)
            self.assertEqual(
                days,
                [("MONDAY", FULL), ("TUESDAY", FULL), ("WEDNESDAY", FULL)],
                quantity,
            )

    def test_a_non_half_fraction_floors_with_no_half_day(self):
        # Only a trailing .5 means a half day; 2.3 is two full days.
        days, errors = self.plan("2.3")
        self.assertEqual(errors, [])
        self.assertEqual(days, [("MONDAY", FULL), ("WEDNESDAY", FULL)])

    def test_quantity_fits_exactly_when_full_plus_half_equals_the_booked_days(self):
        days, errors = self.plan("1.5", ["MONDAY", "FRIDAY"])
        self.assertEqual(errors, [])
        self.assertEqual(days, [("MONDAY", FULL), ("FRIDAY", HALF)])

    def test_every_booking_is_one_unit(self):
        payload = flat_payload(addon_quantity="2.5")
        bookings = bookings_of(payload)

        self.assertEqual(len(bookings), 6)  # (2 full + 1 half) days x 2 products
        self.assertEqual({b["amount"] for b in bookings}, {1})

    def test_full_products_come_before_the_half_ones(self):
        bookings = bookings_of(flat_payload(addon_quantity="1.5"))

        self.assertEqual(
            [b["productId"] for b in bookings], [PRODUCT_1, PRODUCT_2, HALF_1, HALF_2]
        )

    # --- exceeds the booked days ------------------------------------------ #
    def test_a_half_day_that_does_not_fit_is_a_hard_error(self):
        # 2.5 = 2 full + 1 half = 3 days, but only 2 are booked.
        days, errors = self.plan("2.5", ["MONDAY", "WEDNESDAY"])

        self.assertEqual(days, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("2.5 -> 2 full + 1 half", errors[0])
        self.assertIn("exceeds the number of booked days (2)", errors[0])

    def test_half_a_day_with_no_booked_days_is_a_hard_error(self):
        payload = flat_payload(days={}, addon_quantity="0.5")

        self.assertEqual(bookings_of(payload), [])
        errors = errors_of(payload)
        self.assertEqual(len(errors), 1)
        self.assertIn("exceeds the number of booked days (0)", errors[0])

    def test_nothing_is_booked_when_it_does_not_fit(self):
        payload = flat_payload(
            days={"monday": SLOT, "wednesday": SLOT}, addon_quantity="2.5"
        )
        # Not trimmed to the days that would fit -- nothing at all.
        self.assertEqual(bookings_of(payload), [])

    def test_a_capped_quantity_that_still_does_not_fit_is_a_hard_error(self):
        _, errors = self.plan("3.5", ["MONDAY", "WEDNESDAY"])

        self.assertEqual(len(errors), 1)
        self.assertIn("3.5 -> 3 after capping at 3", errors[0])
        self.assertIn("booked days (2)", errors[0])

    # --- missing half products: only a problem when a half is needed ------ #
    def test_a_missing_half_pair_is_an_error_only_when_a_half_is_needed(self):
        days, errors = self.plan("2.5", institution=NO_HALF_INSTITUTION)

        self.assertEqual(days, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("needs a half day of add-ons", errors[0])
        self.assertIn(NO_HALF_INSTITUTION, errors[0])
        # Names BOTH missing products, exactly as they must be titled in Famly.
        self.assertIn("(1/2) Meals & Snacks", errors[0])
        self.assertIn("(1/2) Educational Activities & Extras", errors[0])

    def test_half_a_day_alone_also_needs_the_half_pair(self):
        _, errors = self.plan("0.5", institution=NO_HALF_INSTITUTION)

        self.assertEqual(len(errors), 1)
        self.assertIn("needs a half day of add-ons", errors[0])

    def test_whole_days_are_unaffected_by_missing_half_products(self):
        for quantity in ("0", "1", "2", "3", "3.5", "4.5", "2.3"):
            days, errors = self.plan(quantity, institution=NO_HALF_INSTITUTION)
            self.assertEqual(errors, [], quantity)
            self.assertTrue(all(ids == FULL for _, ids in days), quantity)

    def test_only_the_missing_half_is_named_when_one_of_the_pair_resolved(self):
        resolved = {
            "mealsProductId": PRODUCT_1,
            "activitiesProductId": PRODUCT_2,
            "halfMealsProductId": HALF_1,
            "halfActivitiesProductId": None,
        }
        with mock.patch.object(
            flatten.institution_defaults, "resolve_addon_products", return_value=resolved
        ):
            _, errors = self.plan("1.5")

        self.assertEqual(len(errors), 1)
        self.assertIn("(1/2) Educational Activities & Extras", errors[0])
        self.assertNotIn("(1/2) Meals & Snacks", errors[0])

    def test_a_missing_half_product_is_a_hard_error_not_a_warning(self):
        nested = flatten.flatten_to_nested(
            flat_payload(addon_quantity="1.5", institution=NO_HALF_INSTITUTION)
        )

        self.assertIn(flatten.ERRORS_KEY, nested)
        self.assertNotIn(flatten.PROBLEMS_KEY, nested)

    # --- the removed half-day DISCOUNT's fields --------------------------- #
    def test_half_day_discount_fields_never_turn_a_whole_quantity_into_a_half(self):
        plain, plain_errors = self.plan("2")
        noisy, noisy_errors = self.plan(
            "2", half_day_adjustment="yes", half_day_amount="2.94"
        )

        self.assertEqual(noisy, plain)
        self.assertEqual(noisy_errors, plain_errors)

    def test_half_day_discount_fields_alone_book_nothing(self):
        payload = flat_payload(half_day_adjustment="yes", half_day_amount="3.53")

        self.assertEqual(bookings_of(payload), [])
        self.assertEqual(errors_of(payload), [])

    # --- bad input -------------------------------------------------------- #
    def test_non_finite_quantities_are_not_numbers(self):
        for quantity in ("nan", "inf", "-inf"):
            _, errors = self.plan(quantity)
            self.assertEqual(len(errors), 1, quantity)
            self.assertIn("not a number", errors[0])


class ProductHardErrorTests(unittest.TestCase):
    """These must block: capping or guessing would bill someone wrongly."""

    def test_quantity_exceeding_booked_days_is_an_error(self):
        # Two booked days, quantity three: the capped figure still exceeds.
        errors = errors_of(
            flat_payload(
                days={"monday": SLOT, "wednesday": SLOT},
                addon_quantity="3",
            )
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0], "addon_quantity (3) exceeds the number of booked days (2)"
        )

    def test_a_capped_quantity_is_checked_against_the_booked_days(self):
        # 4.5 rounds to 5 and caps to 3; with only two days booked that is
        # still too many, and the message explains how it got to three.
        errors = errors_of(
            flat_payload(
                days={"monday": SLOT, "wednesday": SLOT},
                addon_quantity="4.5",
            )
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("4.5 -> 3", errors[0])
        self.assertIn("capping at 3", errors[0])
        self.assertIn("booked days (2)", errors[0])

    def test_a_large_quantity_is_fine_when_enough_days_are_booked(self):
        # The cap means 5 no longer exceeds four booked days -- it books three.
        payload = flat_payload(
            addon_quantity="5"
        )

        self.assertEqual(errors_of(payload), [])
        self.assertEqual(len({b["day"] for b in bookings_of(payload)}), 3)

    def test_nothing_is_booked_when_the_quantity_is_too_high(self):
        bookings = bookings_of(
            flat_payload(
                days={"monday": SLOT, "wednesday": SLOT},
                addon_quantity="3",
            )
        )
        # Not trimmed to the two available days -- nothing at all.
        self.assertEqual(bookings, [])

    def test_a_non_numeric_quantity_is_an_error(self):
        errors = errors_of(
            flat_payload(
                addon_quantity="three"
            )
        )
        self.assertIn("not a number", errors[0])

    def test_a_negative_quantity_is_an_error(self):
        errors = errors_of(
            flat_payload(
                addon_quantity="-1"
            )
        )
        self.assertIn("cannot be negative", errors[0])

    def test_these_are_hard_errors_not_advisory_warnings(self):
        nested = flatten.flatten_to_nested(
            flat_payload(
                days={"monday": SLOT, "wednesday": SLOT},
                addon_quantity="3",
            )
        )

        # The hard channel, not the warning channel.
        self.assertIn(flatten.ERRORS_KEY, nested)
        self.assertNotIn(flatten.PROBLEMS_KEY, nested)

    def test_validate_reports_them_alongside_its_own_errors(self):
        nested = flatten.flatten_to_nested(
            flat_payload(
                days={"monday": SLOT, "wednesday": SLOT},
                addon_quantity="3",
            )
        )
        errors = input_schema.validate(input_schema.from_dict(nested))

        self.assertTrue(any("addon_quantity (3) exceeds" in e for e in errors))


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

    def intake(self, days=None, **overrides):
        return hubspot_intake.handle_intake(
            flat_payload(days=days, **overrides), version=3
        )

    def test_quantity_exceeding_booked_days_blocks_with_no_famly_call(self):
        result = self.intake(
            days={"monday": SLOT, "wednesday": SLOT},
            addon_quantity="4.5",
        )

        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])
        self.assertTrue(
            any("exceeds the number of booked days" in e for e in result.errors)
        )

    def test_an_addon_resolution_failure_blocks_with_no_famly_call(self):
        result = self.intake(days={}, institution="HDNODEFAULTS", addon_quantity="2")

        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("HDNODEFAULTS" in e for e in result.errors))

    def test_a_valid_setup_previews_and_sends_the_bookings(self):
        result = self.intake(
            addon_quantity="2"
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 1)

        sent = self.calls[0]["plan"]["planParts"][0]["productBookings"]
        self.assertEqual(len(sent), 4)
        self.assertEqual(sorted({b["day"] for b in sent}), ["MONDAY", "WEDNESDAY"])

    def test_a_half_day_previews_and_sends_the_half_products(self):
        result = self.intake(addon_quantity="2.5")

        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 1)

        sent = self.calls[0]["plan"]["planParts"][0]["productBookings"]
        self.assertEqual(
            [(b["day"], b["productId"], b["amount"]) for b in sent],
            [
                ("MONDAY", PRODUCT_1, 1),
                ("MONDAY", PRODUCT_2, 1),
                ("WEDNESDAY", PRODUCT_1, 1),
                ("WEDNESDAY", PRODUCT_2, 1),
                ("THURSDAY", HALF_1, 1),
                ("THURSDAY", HALF_2, 1),
            ],
        )

    def test_a_half_day_that_does_not_fit_blocks_with_no_famly_call(self):
        result = self.intake(
            days={"monday": SLOT, "wednesday": SLOT}, addon_quantity="2.5"
        )

        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("2.5 -> 2 full + 1 half" in e for e in result.errors))

    def test_a_missing_half_product_blocks_only_a_half_day_request(self):
        blocked = self.intake(addon_quantity="2.5", institution=NO_HALF_INSTITUTION)

        self.assertFalse(blocked.ok)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("(1/2) Meals & Snacks" in e for e in blocked.errors))

        allowed = self.intake(addon_quantity="2", institution=NO_HALF_INSTITUTION)

        self.assertTrue(allowed.ok)
        self.assertEqual(len(self.calls), 1)

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

    # --- half-day products ------------------------------------------------ #
    PRODUCT_NAMES = {
        PRODUCT_1: "Meals & Snacks",
        PRODUCT_2: "Educational Activities & Extras",
        HALF_1: "(1/2) Meals & Snacks",
        HALF_2: "(1/2) Educational Activities & Extras",
    }

    def _two_and_a_half_day_plan(self):
        """A plan built from what hubspot_flatten ACTUALLY emits for 2.5."""
        nested = flatten.flatten_to_nested(flat_payload(addon_quantity="2.5"))
        part = nested["planParts"][0]
        return parse_plan(
            {
                "childId": CHILD,
                "from": "2026-09-01",
                "planParts": [
                    {
                        "planPartId": "pp-1",
                        "sessionBookings": [
                            {"sessionId": SESSION, "day": b["day"]}
                            for b in part["sessionBookings"]
                        ],
                        "productBookings": part["productBookings"],
                    }
                ],
            }
        )

    def test_a_two_and_a_half_day_plan_shows_two_full_days_and_one_half_day(self):
        text = slack.build_summary(
            self._two_and_a_half_day_plan(), [], product_titles=self.PRODUCT_NAMES
        )

        self.assertIn("*Products* (6)", text)
        for day in ("Monday", "Wednesday"):
            self.assertIn(f"• {day} — 1× Meals & Snacks", text)
            self.assertIn(f"• {day} — 1× Educational Activities & Extras", text)
        self.assertIn("• Thursday — 1× (1/2) Meals & Snacks", text)
        self.assertIn("• Thursday — 1× (1/2) Educational Activities & Extras", text)
        # The half day carries ONLY the half products -- no full-price lines.
        self.assertNotIn("• Thursday — 1× Meals & Snacks", text)
        self.assertNotIn("• Thursday — 1× Educational Activities & Extras", text)

    def test_half_products_show_in_week_order_after_the_full_days(self):
        text = slack.build_summary(
            self._two_and_a_half_day_plan(), [], product_titles=self.PRODUCT_NAMES
        )

        self.assertLess(text.index("• Wednesday — 1× Meals"), text.index("• Thursday — 1× (1/2)"))

    def test_half_products_use_catalogue_names_from_the_real_loader(self):
        # The names come from reference/catalogue.json's `products` map via
        # catalogue.load_titles() -- exactly what web.py hands the summary.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalogue.json"
            path.write_text(
                json.dumps({"products": self.PRODUCT_NAMES}), encoding="utf-8"
            )
            with mock.patch.dict("os.environ", {"FAMLY_CATALOGUE_FILE": str(path)}):
                _, product_titles = catalogue.load_titles()

        text = slack.build_summary(
            self._two_and_a_half_day_plan(), [], product_titles=product_titles
        )
        self.assertIn("• Thursday — 1× (1/2) Meals & Snacks", text)

    def test_a_half_product_missing_from_the_catalogue_falls_back_to_its_uuid(self):
        names = {k: v for k, v in self.PRODUCT_NAMES.items() if k not in (HALF_1, HALF_2)}
        text = slack.build_summary(
            self._two_and_a_half_day_plan(), [], product_titles=names
        )

        self.assertIn(f"• Thursday — 1× {HALF_1}", text)


if __name__ == "__main__":
    unittest.main()
