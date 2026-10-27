"""Tests for actions/plan_write/timeline.py.

Run from the project root:

    python -m unittest tests.test_timeline -v

`build_segments` is pure, so there is nothing to stub: every test hands it
dates and reads back segments.
"""

import random
import unittest
from datetime import date, timedelta

from actions.plan_write.timeline import TimelineError, build_segments


def seg(first, last, funded=False, advanced=False):
    return {"from": first, "to": last, "funded": funded, "advanced": advanced}


class SegmentTests(unittest.TestCase):
    def test_base_only(self):
        segments, warnings = build_segments("2026-01-05", "2026-07-31")
        self.assertEqual(segments, [seg("2026-01-05", "2026-07-31")])
        self.assertEqual(warnings, [])

    def test_base_funded_base(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-12-18", funding=("2026-04-01", "2026-06-30")
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-03-31"),
                seg("2026-04-01", "2026-06-30", funded=True),
                seg("2026-07-01", "2026-12-18"),
            ],
        )
        self.assertEqual(warnings, [])

    def test_funded_from_the_start_then_base(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-12-18", funding=("2026-01-05", "2026-03-31")
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-03-31", funded=True),
                seg("2026-04-01", "2026-12-18"),
            ],
        )
        self.assertEqual(warnings, [])

    def test_funding_starting_before_start_is_funded_from_the_first_day(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-12-18", funding=("2025-09-01", "2026-03-31")
        )
        self.assertEqual(segments[0], seg("2026-01-05", "2026-03-31", funded=True))
        self.assertEqual(segments[1], seg("2026-04-01", "2026-12-18"))
        self.assertEqual(warnings, [])

    def test_funded_through_to_leave(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-12-18", funding=("2026-04-01", "2026-12-18")
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-03-31"),
                seg("2026-04-01", "2026-12-18", funded=True),
            ],
        )
        self.assertEqual(warnings, [])

    def test_funding_running_past_leave_is_clamped_to_leave(self):
        segments, _ = build_segments(
            "2026-01-05", "2026-12-18", funding=("2026-04-01", "2027-08-31")
        )
        self.assertEqual(segments[-1], seg("2026-04-01", "2026-12-18", funded=True))

    def test_advanced_inside_base(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-12-18", advanced=("2026-05-01", "2026-05-31")
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-04-30"),
                seg("2026-05-01", "2026-05-31", advanced=True),
                seg("2026-06-01", "2026-12-18"),
            ],
        )
        self.assertEqual(warnings, [])

    def test_advanced_inside_funded_gives_three_segments(self):
        segments, warnings = build_segments(
            "2026-02-01",
            "2026-06-30",
            funding=("2026-02-01", "2026-06-30"),
            advanced=("2026-03-01", "2026-03-31"),
        )
        self.assertEqual(
            segments,
            [
                seg("2026-02-01", "2026-02-28", funded=True),
                seg("2026-03-01", "2026-03-31", funded=True, advanced=True),
                seg("2026-04-01", "2026-06-30", funded=True),
            ],
        )
        self.assertEqual(warnings, [])

    def test_advanced_overlapping_the_funding_start(self):
        segments, warnings = build_segments(
            "2026-01-05",
            "2026-12-18",
            funding=("2026-03-01", "2026-08-31"),
            advanced=("2026-02-01", "2026-03-31"),
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-01-31"),
                seg("2026-02-01", "2026-02-28", advanced=True),
                seg("2026-03-01", "2026-03-31", funded=True, advanced=True),
                seg("2026-04-01", "2026-08-31", funded=True),
                seg("2026-09-01", "2026-12-18"),
            ],
        )
        self.assertEqual(warnings, [])

    def test_advanced_and_funding_ending_the_same_day_leave_one_unflagged_tail(self):
        segments, _ = build_segments(
            "2026-01-05",
            "2026-12-18",
            funding=("2026-03-01", "2026-05-31"),
            advanced=("2026-04-01", "2026-05-31"),
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-02-28"),
                seg("2026-03-01", "2026-03-31", funded=True),
                seg("2026-04-01", "2026-05-31", funded=True, advanced=True),
                seg("2026-06-01", "2026-12-18"),
            ],
        )

    def test_identical_flags_are_merged_into_one_segment(self):
        # Both periods cut at the same dates; the stretch between must come
        # back as one segment, not several with identical flags.
        segments, _ = build_segments(
            "2026-01-05",
            "2026-12-18",
            funding=("2026-01-05", "2026-12-18"),
            advanced=("2026-01-05", "2026-12-18"),
        )
        self.assertEqual(
            segments, [seg("2026-01-05", "2026-12-18", funded=True, advanced=True)]
        )

    def test_open_ended_leave(self):
        segments, warnings = build_segments("2026-01-05")
        self.assertEqual(segments, [seg("2026-01-05", None)])
        self.assertEqual(warnings, [])

    def test_open_ended_with_funding_then_base(self):
        segments, _ = build_segments("2026-01-05", funding=("2026-04-01", "2026-06-30"))
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-03-31"),
                seg("2026-04-01", "2026-06-30", funded=True),
                seg("2026-07-01", None),
            ],
        )

    def test_open_ended_with_funding_that_is_still_running(self):
        segments, _ = build_segments("2026-01-05", funding=("2026-04-01", "2099-12-31"))
        self.assertEqual(segments[-2], seg("2026-04-01", "2099-12-31", funded=True))
        self.assertEqual(segments[-1], seg("2100-01-01", None))

    def test_one_day_placement(self):
        segments, _ = build_segments("2026-01-05", "2026-01-05")
        self.assertEqual(segments, [seg("2026-01-05", "2026-01-05")])

    def test_a_period_of_none_none_means_no_period(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-02-01", funding=(None, None), advanced=(None, None)
        )
        self.assertEqual(segments, [seg("2026-01-05", "2026-02-01")])
        self.assertEqual(warnings, [])

    def test_inputs_are_not_mutated(self):
        funding = ("2026-04-01", "2026-06-30")
        build_segments("2026-01-05", "2026-12-18", funding=funding)
        self.assertEqual(funding, ("2026-04-01", "2026-06-30"))


class ClampAndDropTests(unittest.TestCase):
    def test_funding_straddling_start_is_clamped_without_a_warning_cases(self):
        """Folded cases: funding_straddling_start_is_clamped_without_a_warning; funding_ending_on_start_is_kept."""
        for _i, (_p0, _p1, _p2, _p3) in enumerate([
            ("2025-06-01", "2026-02-28", "2026-02-28", "2026-03-01"),
            ("2025-09-01", "2026-01-05", "2026-01-05", "2026-01-06"),
        ]):
            with self.subTest(case=_i):
                segments, warnings = build_segments(
                    "2026-01-05", "2026-06-30", funding=(_p0, _p1)
                )
                self.assertEqual(
                    segments,
                    [
                        seg("2026-01-05", _p2, funded=True),
                        seg(_p3, "2026-06-30"),
                    ],
                )
                self.assertEqual(warnings, [])

    def test_funding_straddling_leave_is_clamped_without_a_warning(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-06-30", funding=("2026-05-01", "2027-02-28")
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-04-30"),
                seg("2026-05-01", "2026-06-30", funded=True),
            ],
        )
        self.assertEqual(warnings, [])

    def test_funding_wholly_before_start_is_dropped_with_a_warning(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-06-30", funding=("2025-01-01", "2025-12-31")
        )
        self.assertEqual(segments, [seg("2026-01-05", "2026-06-30")])
        self.assertEqual(len(warnings), 1)
        self.assertIn("funding", warnings[0])
        self.assertIn("2025-01-01", warnings[0])

    def test_funding_ending_the_day_before_start_is_dropped(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-06-30", funding=("2025-09-01", "2026-01-04")
        )
        self.assertEqual(segments, [seg("2026-01-05", "2026-06-30")])
        self.assertEqual(len(warnings), 1)

    def test_advanced_wholly_after_leave_is_dropped_with_a_warning(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-06-30", advanced=("2026-07-01", "2026-07-31")
        )
        self.assertEqual(segments, [seg("2026-01-05", "2026-06-30")])
        self.assertEqual(len(warnings), 1)
        self.assertIn("advanced", warnings[0])

    def test_period_starting_on_leave_is_kept(self):
        segments, warnings = build_segments(
            "2026-01-05", "2026-06-30", advanced=("2026-06-30", "2026-07-31")
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2026-06-29"),
                seg("2026-06-30", "2026-06-30", advanced=True),
            ],
        )
        self.assertEqual(warnings, [])

    def test_both_periods_dropped_gives_two_warnings(self):
        segments, warnings = build_segments(
            "2026-01-05",
            "2026-06-30",
            funding=("2024-01-01", "2024-12-31"),
            advanced=("2027-01-01", "2027-01-31"),
        )
        self.assertEqual(segments, [seg("2026-01-05", "2026-06-30")])
        self.assertEqual(len(warnings), 2)

    def test_open_ended_leave_only_drops_periods_before_start(self):
        segments, warnings = build_segments(
            "2026-01-05",
            funding=("2025-01-01", "2025-12-31"),
            advanced=("2030-01-01", "2030-01-31"),
        )
        self.assertEqual(
            segments,
            [
                seg("2026-01-05", "2029-12-31"),
                seg("2030-01-01", "2030-01-31", advanced=True),
                seg("2030-02-01", None),
            ],
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("funding", warnings[0])


class ErrorTests(unittest.TestCase):
    def test_leave_before_start(self):
        with self.assertRaisesRegex(TimelineError, "leave"):
            build_segments("2026-06-30", "2026-06-29")

    def test_funding_ending_before_it_starts(self):
        with self.assertRaisesRegex(TimelineError, "funding.*before"):
            build_segments("2026-01-05", "2026-12-18", funding=("2026-06-30", "2026-06-01"))

    def test_advanced_ending_before_it_starts(self):
        with self.assertRaisesRegex(TimelineError, "advanced.*before"):
            build_segments("2026-01-05", "2026-12-18", advanced=("2026-06-30", "2026-06-01"))

    def test_funding_with_only_a_from_date(self):
        with self.assertRaisesRegex(TimelineError, "funding.*only one date"):
            build_segments("2026-01-05", "2026-12-18", funding=("2026-06-01", None))

    def test_funding_with_only_a_to_date(self):
        with self.assertRaisesRegex(TimelineError, "funding.*only one date"):
            build_segments("2026-01-05", "2026-12-18", funding=(None, "2026-06-01"))

    def test_advanced_with_only_one_date(self):
        with self.assertRaisesRegex(TimelineError, "advanced.*only one date"):
            build_segments("2026-01-05", "2026-12-18", advanced=("2026-06-01", None))

    def test_a_period_that_is_outside_the_placement_is_still_validated(self):
        # A bad period must not be hidden because it would have been dropped.
        with self.assertRaises(TimelineError):
            build_segments("2026-01-05", "2026-06-30", funding=("2030-02-01", "2030-01-01"))
        with self.assertRaises(TimelineError):
            build_segments("2026-01-05", "2026-06-30", advanced=("2030-02-01", None))

    def test_a_bad_advanced_period_is_reported_even_when_funding_is_dropped(self):
        with self.assertRaisesRegex(TimelineError, "advanced"):
            build_segments(
                "2026-01-05",
                "2026-06-30",
                funding=("2020-01-01", "2020-12-31"),
                advanced=("2026-03-01", None),
            )

    def test_malformed_dates(self):
        for kwargs in (
            {"start": "05/01/2026"},
            {"start": "2026-02-30"},
            {"start": None},
            {"start": "2026-01-05", "leave": "tomorrow"},
            {"start": "2026-01-05", "funding": ("2026-1-1", "2026-02-01")},
            {"start": "2026-01-05", "funding": "2026-02-01"},
            {"start": "2026-01-05", "advanced": ("2026-01-01",)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(TimelineError):
                build_segments(**kwargs)

    def test_errors_are_value_errors(self):
        self.assertTrue(issubclass(TimelineError, ValueError))


class PropertyTests(unittest.TestCase):
    """Random valid inputs against an independent day-by-day reference."""

    EPOCH = date(2026, 1, 1)

    def random_period(self, rng):
        if rng.random() < 0.3:
            return None
        first = self.EPOCH + timedelta(days=rng.randint(-60, 400))
        last = first + timedelta(days=rng.randint(0, 200))
        return first.isoformat(), last.isoformat()

    def test_segments_are_contiguous_cover_the_range_and_never_overlap(self):
        rng = random.Random(20261013)
        for _ in range(2000):
            start = self.EPOCH + timedelta(days=rng.randint(0, 120))
            leave = (
                None
                if rng.random() < 0.25
                else start + timedelta(days=rng.randint(0, 300))
            )
            funding, advanced = self.random_period(rng), self.random_period(rng)
            case = (start.isoformat(), leave and leave.isoformat(), funding, advanced)

            with self.subTest(case=case):
                segments, warnings = build_segments(*case)
                self.assert_well_formed(segments, start, leave)
                self.assert_flags_match_reference(segments, start, leave, funding, advanced)
                self.assertIsInstance(warnings, list)

    def assert_well_formed(self, segments, start, leave):
        self.assertTrue(segments)
        self.assertEqual(segments[0]["from"], start.isoformat())
        self.assertEqual(
            segments[-1]["to"], None if leave is None else leave.isoformat()
        )

        for earlier, later in zip(segments, segments[1:]):
            self.assertIsNotNone(earlier["to"])
            # Contiguous AND non-overlapping: the next one starts exactly the
            # day after this one ends.
            self.assertEqual(
                date.fromisoformat(later["from"]),
                date.fromisoformat(earlier["to"]) + timedelta(days=1),
            )
            # Merged: neighbours always differ in at least one flag.
            self.assertNotEqual(
                (earlier["funded"], earlier["advanced"]),
                (later["funded"], later["advanced"]),
            )

        for each in segments:
            if each["to"] is not None:
                self.assertLessEqual(each["from"], each["to"])

    def assert_flags_match_reference(self, segments, start, leave, funding, advanced):
        # Open-ended placements are checked over a horizon past every period.
        horizon = leave or (self.EPOCH + timedelta(days=1000))
        spans = [
            None if period is None else tuple(map(date.fromisoformat, period))
            for period in (funding, advanced)
        ]
        day = start
        while day <= horizon:
            owners = [
                each
                for each in segments
                if date.fromisoformat(each["from"]) <= day
                and (each["to"] is None or day <= date.fromisoformat(each["to"]))
            ]
            self.assertEqual(len(owners), 1, f"{day} is in {len(owners)} segments")
            expected = tuple(
                span is not None and span[0] <= day <= span[1] for span in spans
            )
            self.assertEqual((owners[0]["funded"], owners[0]["advanced"]), expected, str(day))
            day += timedelta(days=1)


if __name__ == "__main__":
    unittest.main()
