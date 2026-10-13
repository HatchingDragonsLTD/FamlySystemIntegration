"""Tests for actions/plan_write/rounding.py: the pure rounding rule.

Run from the project root:

    python -m unittest tests.test_rounding -v

Everything is in Decimal, so these assert exact values, not approximations.
"""

import random
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from actions.plan_write import rounding
from actions.plan_write.rounding import adjustment_for, plan_rounding, round_target

D = Decimal

# (estimate, target, adjustment). Worked by hand from the rule: steps 10, 7.5
# and 5, a window of 1.00, otherwise the nearest whole pound.
ROWS = [
    # snaps to a multiple of 10 / 7.5 / 5 within a pound
    (269.03, "270.00", "0.97"),
    (270.00, "270.00", "0.00"),
    (269.50, "270.00", "0.50"),
    (1500.50, "1500.00", "-0.50"),
    (22.50, "22.50", "0.00"),  # a multiple of 7.5 already
    (23.10, "22.50", "-0.60"),  # 7.5 is the nearest within a pound
    (24.90, "25.00", "0.10"),  # 5
    (1005.80, "1005.00", "-0.80"),  # 5 and 7.5 agree
    (1010.99, "1010.00", "-0.99"),
    # nothing within a pound: the nearest whole pound
    (1003.30, "1003.00", "-0.30"),
    (1003.49, "1003.00", "-0.49"),
    (1003.51, "1004.00", "0.49"),
    (1003.50, "1004.00", "0.50"),  # exactly .50 rounds UP
    (1012.50, "1012.50", "0.00"),  # 7.5 x 135
]


class RoundTargetTests(unittest.TestCase):
    def test_every_row(self):
        for estimate, target, adjustment in ROWS:
            with self.subTest(estimate=estimate):
                self.assertEqual(round_target(estimate), D(target))
                self.assertEqual(adjustment_for(estimate), D(adjustment))

    def test_the_window_boundary_snaps(self):
        self.assertEqual(round_target(1501.00), D("1500.00"))
        self.assertEqual(adjustment_for(1501.00), D("-1.00"))

    def test_just_outside_the_window_goes_to_a_whole_pound(self):
        self.assertEqual(round_target(1501.01), D("1501.00"))
        self.assertEqual(adjustment_for(1501.01), D("-0.01"))

    def test_halves_round_up(self):
        self.assertEqual(round_target(1003.50), D("1004.00"))
        self.assertEqual(round_target(1002.50), D("1003.00"))  # 7.5/5 window misses 1002.5

    def test_each_step_can_win(self):
        # 22.5 is only a multiple of 7.5; 25 only of 5 (and not of 10 or 7.5).
        self.assertEqual(round_target(22.60), D("22.50"))
        self.assertEqual(round_target(25.30), D("25.00"))
        self.assertEqual(round_target(30.20), D("30.00"))

    def test_the_closest_candidate_wins(self):
        # 14.80: 15 (5, 7.5x2) is 0.20 away; 10 is 4.8 away.
        self.assertEqual(round_target(14.80), D("15.00"))

    def test_a_tie_goes_to_the_larger_step(self):
        with mock.patch.object(rounding, "SNAP_STEPS", (4, 6)), mock.patch.object(
            rounding, "SNAP_WINDOW", 2.0
        ):
            # 5 is 1 away from both 4 (step 4) and 6 (step 6).
            self.assertEqual(round_target(5), D("6.00"))
        with mock.patch.object(rounding, "SNAP_STEPS", (6, 4)), mock.patch.object(
            rounding, "SNAP_WINDOW", 2.0
        ):
            self.assertEqual(round_target(5), D("6.00"))  # order of the steps is irrelevant

    def test_the_constants_are_the_single_place_to_change_the_rule(self):
        self.assertEqual(rounding.SNAP_STEPS, (10, 7.5, 5))
        self.assertEqual(rounding.SNAP_WINDOW, 1.00)

        with mock.patch.object(rounding, "SNAP_WINDOW", 0.10):
            self.assertEqual(round_target(269.03), D("269.00"))  # no longer snaps
        with mock.patch.object(rounding, "SNAP_STEPS", (10,)):
            self.assertEqual(round_target(24.90), D("25.00"))  # whole pound, not step 5

    def test_floats_are_converted_through_str(self):
        # 269.03 as a binary float is 269.02999999...; through str it is 269.03.
        self.assertEqual(adjustment_for(269.03), D("0.97"))
        self.assertEqual(adjustment_for(0.1 + 0.2 + 269.0), D("0.70"))

    def test_estimates_at_or_below_zero_are_not_rounded(self):
        for value in (0, 0.0, -0.01, -50, "0"):
            with self.subTest(value=value):
                self.assertIsNone(round_target(value))
                self.assertIsNone(adjustment_for(value))

    def test_things_that_are_not_numbers_are_not_rounded(self):
        for value in (None, "abc", True, False, float("nan"), float("inf"), [], {}):
            with self.subTest(value=value):
                self.assertIsNone(round_target(value))

    def test_strings_and_integers_work(self):
        self.assertEqual(round_target("269.03"), D("270.00"))
        self.assertEqual(round_target(270), D("270.00"))
        self.assertEqual(round_target(D("269.03")), D("270.00"))

    def test_the_result_is_a_two_decimal_decimal(self):
        self.assertEqual(round_target(269.03).as_tuple().exponent, -2)
        self.assertEqual(adjustment_for(269.03).as_tuple().exponent, -2)


class PropertyTests(unittest.TestCase):
    def test_10000_random_estimates(self):
        rng = random.Random(20261013)
        for _ in range(10_000):
            estimate = D(rng.randint(100, 500_000)) / 100  # 1.00 .. 5000.00
            target = round_target(estimate)
            adjustment = adjustment_for(estimate)

            self.assertIsNotNone(target, estimate)
            self.assertLessEqual(abs(adjustment), D("1.00"), estimate)
            self.assertEqual(target - estimate, adjustment)
            self.assertTrue(
                target % 5 == 0 or target % D("7.5") == 0 or target == target.to_integral_value(),
                (estimate, target),
            )

    def test_the_float_path_agrees_with_the_decimal_path(self):
        rng = random.Random(7)
        for _ in range(2_000):
            cents = rng.randint(100, 500_000)
            self.assertEqual(round_target(cents / 100), round_target(D(cents) / 100), cents)

    def test_a_tiny_estimate_is_never_rounded_down_to_nothing(self):
        for estimate in ("0.01", "0.40", "0.49", "0.50"):
            with self.subTest(estimate=estimate):
                target = round_target(estimate)
                self.assertTrue(target is None or target > 0, estimate)


class PlanRoundingTests(unittest.TestCase):
    def test_one_entry_per_distinct_group(self):
        result = plan_rounding(
            [("2026-09-01", "pg-1", 269.03), ("2027-01-01", "pg-2", 1503.30)]
        )

        self.assertIsNone(result.reason)
        self.assertEqual(
            [(e.group, e.adjustment) for e in result.entries],
            [("pg-1", D("0.97")), ("pg-2", D("-0.30"))],
        )
        self.assertEqual(
            [(p.from_, p.estimate, p.target) for p in result.periods],
            [("2026-09-01", D("269.03"), D("270.00")), ("2027-01-01", D("1503.30"), D("1503.00"))],
        )

    def test_a_shared_group_with_the_same_estimate_is_one_entry(self):
        result = plan_rounding(
            [("2026-09-01", "pg-1", 269.03), ("2027-01-01", "pg-1", 269.03)]
        )

        self.assertEqual(len(result.entries), 1)
        self.assertEqual(len(result.periods), 2)  # both periods are shown

    def test_a_shared_group_with_different_estimates_refuses_the_whole_plan(self):
        result = plan_rounding(
            [("2026-09-01", "pg-1", 269.03), ("2027-01-01", "pg-1", 275.10), ("2027-06-01", "pg-2", 300.40)]
        )

        self.assertEqual(result.entries, [])
        self.assertIn("pg-1", result.reason)
        self.assertIn("different", result.reason)

    def test_a_zero_adjustment_is_skipped(self):
        result = plan_rounding([("2026-09-01", "pg-1", 270.00), ("2027-01-01", "pg-2", 269.03)])

        self.assertEqual([e.group for e in result.entries], ["pg-2"])
        self.assertEqual(len(result.periods), 1)

    def test_nothing_to_round_is_not_a_refusal(self):
        result = plan_rounding([("2026-09-01", "pg-1", 270.00)])

        self.assertEqual((result.entries, result.reason), ([], None))

    def test_an_estimate_at_or_below_zero_is_left_alone(self):
        result = plan_rounding([("2026-09-01", "pg-1", 0), ("2027-01-01", "pg-2", 269.03)])

        self.assertEqual([e.group for e in result.entries], ["pg-2"])

    def test_a_period_missing_its_group_or_estimate_refuses_the_plan(self):
        for periods in (
            [("2026-09-01", None, 269.03)],
            [("2026-09-01", "", 269.03)],
            [("2026-09-01", "pg-1", None)],
            [("2026-09-01", "pg-1", "n/a")],
            [("2026-09-01", "pg-1", 269.03), ("2027-01-01", None, 300.0)],
        ):
            with self.subTest(periods=periods):
                result = plan_rounding(periods)
                self.assertEqual(result.entries, [])
                self.assertIn("pricingGroupId", result.reason)

    def test_no_periods_refuses(self):
        self.assertIn("no pricing periods", plan_rounding([]).reason)


def body(note=None, adjustments=None):
    part = {"planPartId": "pp-1"}
    if adjustments is not None:
        part["totalAdjustments"] = adjustments
    plan = {"childId": "c", "planParts": [part]}
    if note is not None:
        plan["note"] = note
    return {"plan": plan}


class BodyTests(unittest.TestCase):
    ENTRIES = plan_rounding(
        [("2026-09-01", "pg-1", 269.03), ("2027-01-01", "pg-2", 1503.30)]
    ).entries

    def test_with_entries_adds_one_adjustment_per_group_and_the_note(self):
        adjusted = rounding.with_entries(body(), self.ENTRIES, "2026-10-13")

        self.assertEqual(
            adjusted["plan"]["planParts"][0]["totalAdjustments"],
            [
                {"pricingGroupId": "pg-1", "adjustment": 0.97},
                {"pricingGroupId": "pg-2", "adjustment": -0.30},
            ],
        )
        self.assertEqual(adjusted["plan"]["note"], "Auto-rounded: +0.97 / -0.30, 2026-10-13")

    def test_the_note_is_appended_to_an_existing_one(self):
        adjusted = rounding.with_entries(body(note="Starter"), self.ENTRIES, "2026-10-13")

        self.assertEqual(
            adjusted["plan"]["note"], "Starter\nAuto-rounded: +0.97 / -0.30, 2026-10-13"
        )

    def test_the_original_body_is_not_mutated(self):
        original = body()
        rounding.with_entries(original, self.ENTRIES, "2026-10-13")

        self.assertNotIn("totalAdjustments", original["plan"]["planParts"][0])
        self.assertNotIn("note", original["plan"])

    def test_a_group_never_gets_two_entries(self):
        existing = body(adjustments=[{"pricingGroupId": "pg-1", "adjustment": 0.5}])

        adjusted = rounding.with_entries(existing, self.ENTRIES, "2026-10-13")

        groups = [e["pricingGroupId"] for e in adjusted["plan"]["planParts"][0]["totalAdjustments"]]
        self.assertEqual(sorted(groups), ["pg-1", "pg-2"])

    def test_without_group_removes_the_entry_and_rewrites_the_note(self):
        rounded = rounding.with_entries(body(note="Starter"), self.ENTRIES, "2026-10-13")

        adjusted = rounding.without_group(rounded, "pg-1")

        self.assertEqual(
            adjusted["plan"]["planParts"][0]["totalAdjustments"],
            [{"pricingGroupId": "pg-2", "adjustment": -0.30}],
        )
        self.assertEqual(adjusted["plan"]["note"], "Starter\nAuto-rounded: -0.30, 2026-10-13")

    def test_without_the_last_group_the_auto_line_goes(self):
        single = plan_rounding([("2026-09-01", "pg-1", 269.03)]).entries
        rounded = rounding.with_entries(body(note="Starter"), single, "2026-10-13")

        adjusted = rounding.without_group(rounded, "pg-1")

        self.assertEqual(adjusted["plan"]["planParts"][0]["totalAdjustments"], [])
        self.assertEqual(adjusted["plan"]["note"], "Starter")

    def test_without_group_for_an_unknown_group_changes_nothing(self):
        rounded = rounding.with_entries(body(), self.ENTRIES, "2026-10-13")

        self.assertEqual(rounding.without_group(rounded, "pg-9"), rounded)

    def test_without_group_on_a_plain_body_is_a_copy(self):
        plain = body(note="Starter")

        self.assertEqual(rounding.without_group(plain, "pg-1"), plain)


def saved_plan(note, states, adjustments):
    return SimpleNamespace(
        note=note,
        plan_states=[
            SimpleNamespace(from_=start, pricing_group_id=group, monthly_estimate=estimate)
            for start, group, estimate in states
        ],
        plan_parts=[
            SimpleNamespace(
                total_adjustments=[
                    {"pricingGroupId": g, "adjustment": a} for g, a in adjustments
                ]
            )
        ],
    )


class CheckSavedTests(unittest.TestCase):
    NOTE = "Auto-rounded: +0.97 / -0.30, 2026-10-13"
    STATES = [("2026-09-01", "pg-1", 269.03), ("2027-01-01", "pg-2", 1503.30)]

    def test_a_plan_saved_as_intended_has_no_problems(self):
        plan = saved_plan(self.NOTE, self.STATES, [("pg-1", 0.97), ("pg-2", -0.30)])

        self.assertEqual(rounding.check_saved(plan), [])

    def test_within_a_penny_is_fine(self):
        plan = saved_plan(self.NOTE, self.STATES, [("pg-1", 0.9701), ("pg-2", -0.2999)])

        self.assertEqual(rounding.check_saved(plan), [])

    def test_a_missing_adjustment_is_reported_per_period(self):
        plan = saved_plan(self.NOTE, self.STATES, [("pg-2", -0.30)])

        problems = rounding.check_saved(plan)

        self.assertEqual(len(problems), 1)
        self.assertIn("2026-09-01", problems[0])
        self.assertIn("expected 270.00", problems[0])

    def test_adjustments_saved_against_the_wrong_groups_are_reported(self):
        plan = saved_plan(self.NOTE, self.STATES, [("pg-1", -0.30), ("pg-2", 0.97)])

        self.assertEqual(len(rounding.check_saved(plan)), 2)

    def test_a_figure_not_in_the_note_is_read_as_a_manual_takeover(self):
        # The note lists what auto-rounding applied; a different saved figure
        # for a group is taken to be a manual adjustment and left alone.
        plan = saved_plan(self.NOTE, self.STATES, [("pg-1", 0.97), ("pg-2", -0.50)])

        self.assertEqual(rounding.check_saved(plan), [])

    def test_a_plan_that_was_not_auto_rounded_is_not_checked(self):
        plan = saved_plan("Starter", self.STATES, [])

        self.assertEqual(rounding.check_saved(plan), [])
        self.assertEqual(rounding.check_saved(saved_plan(None, self.STATES, [])), [])

    def test_a_group_taken_over_by_a_manual_adjustment_is_left_alone(self):
        plan = saved_plan("Auto-rounded: -0.30, 2026-10-13", self.STATES, [("pg-1", 0.50), ("pg-2", -0.30)])

        self.assertEqual(rounding.check_saved(plan), [])

    def test_an_estimate_that_was_already_a_target_needs_no_adjustment(self):
        plan = saved_plan(self.NOTE, [("2026-09-01", "pg-1", 270.00), ("2027-01-01", "pg-2", 1503.30)], [("pg-2", -0.30)])

        self.assertEqual(rounding.check_saved(plan), [])


if __name__ == "__main__":
    unittest.main()
