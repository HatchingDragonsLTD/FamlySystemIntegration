"""Tests for pricing-period selection: `Plan.current_plan_state` and everything
that quotes or adjusts a price from it.

Run from the project root:

    python -m unittest discover -s tests -v

BACKGROUND. A plan has one `planState` per pricing period, and live plans
routinely have two (the rate moves when the child changes age group). The
plan-level `monthlyEstimate`, `publicFunding` and (absent) `pricingGroupId`
only mirror the FIRST period -- confirmed against 21 live plans -- so the Slack
summary and the adjustment flow used to quote and adjust the first period even
when it had already ended. Every period carries its own `monthlyEstimate`,
`publicFunding` and `pricingGroupId`; these tests pin that the period covering
TODAY is the one used.

NOTHING HERE TOUCHES FAMLY OR SLACK. The model and Slack-summary tests pass
`today=` explicitly. The end-to-end flow tests build their plan dates relative
to the real today (far from any boundary), because the write path takes no
`today` argument.
"""

import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from actions.plan_write import approval, cli, hubspot_intake, preview_store, web
from actions.read_child_plans.runner import (
    current_monthly_estimate,
    current_pricing_summary,
    parse_plan,
)
from integrations import child_lookup, slack

logging.disable(logging.CRITICAL)

CHILD = "child-under-test"
G1, G2, G3 = "pg-first", "pg-second", "pg-third"
PRICE = {G1: 111.11, G2: 99.99, G3: 88.88}


def period(start, end, monthly, group, weekly, funding_amount=0, funding_hours=0):
    """One raw `planState`, shaped like the live API's."""
    return {
        "from": start,
        "to": end,
        "monthlyEstimate": monthly,
        "pricingGroupId": group,
        "ageGroupId": "ag-1",
        "publicFunding": {
            "amount": funding_amount,
            "hours": funding_hours,
            "minutes": 0,
        },
        "planPartStates": [
            {
                "planPartId": "pp-1",
                "weeklyTotal": weekly,
                "monthlyEstimate": monthly,
                "pricingGroupId": group,
            }
        ],
    }


def plan_node(*periods):
    """A raw plan whose plan-level fields MIRROR THE FIRST PERIOD, as live."""
    first = periods[0]
    groups = []
    for p in periods:
        if p["pricingGroupId"] not in groups:
            groups.append(p["pricingGroupId"])

    return {
        "id": "plan-1",
        "childId": CHILD,
        "from": first["from"],
        "to": periods[-1]["to"],
        "monthlyEstimate": first["monthlyEstimate"],
        "publicFunding": first["publicFunding"],
        "pricingGroupId": None,  # null on every live plan checked
        "planStates": list(periods),
        "planParts": [
            {
                "planPartId": "pp-1",
                "sessionBookings": [
                    {
                        "bookingId": "b-1",
                        "sessionId": "s-1",
                        "day": "MONDAY",
                        # One price per pricing group -- including every period's.
                        "monthlyPrices": [
                            {"pricingGroupId": g, "price": PRICE[g]} for g in groups
                        ],
                    }
                ],
            }
        ],
    }


FIRST = period("2026-01-20", "2026-07-31", 749.96, G1, 173.0, 129.65, 12)
SECOND = period("2026-08-01", "2027-08-30", 705.08, G2, 162.0, 116.24, 12)
THIRD = period("2027-08-31", None, 650.00, G3, 150.0, 100.00, 12)

AFTER_SWITCH = date(2026, 10, 8)
BEFORE_SWITCH = date(2026, 6, 15)


# --------------------------------------------------------------------------- #
# Plan.current_plan_state
# --------------------------------------------------------------------------- #
class CurrentPlanStateTests(unittest.TestCase):
    def pick(self, node, today):
        state = parse_plan(node).current_plan_state(today)
        return state.pricing_group_id if state else None

    def test_two_periods_with_today_after_the_switch_picks_the_second(self):
        self.assertEqual(self.pick(plan_node(FIRST, SECOND), AFTER_SWITCH), G2)

    def test_two_periods_with_today_before_the_switch_picks_the_first(self):
        self.assertEqual(self.pick(plan_node(FIRST, SECOND), BEFORE_SWITCH), G1)

    def test_the_boundaries_are_inclusive_on_both_sides(self):
        node = plan_node(FIRST, SECOND)

        self.assertEqual(self.pick(node, date(2026, 1, 20)), G1)  # first day of FIRST
        self.assertEqual(self.pick(node, date(2026, 7, 31)), G1)  # last day of FIRST
        self.assertEqual(self.pick(node, date(2026, 8, 1)), G2)  # first day of SECOND
        self.assertEqual(self.pick(node, date(2027, 8, 30)), G2)  # last day of SECOND

    def test_an_open_ended_last_period_covers_every_later_day(self):
        node = plan_node(FIRST, SECOND, THIRD)

        self.assertEqual(self.pick(node, date(2027, 8, 31)), G3)
        self.assertEqual(self.pick(node, date(2040, 1, 1)), G3)

    def test_three_periods_pick_the_middle_one_when_it_covers_today(self):
        self.assertEqual(self.pick(plan_node(FIRST, SECOND, THIRD), AFTER_SWITCH), G2)

    def test_a_plan_wholly_in_the_future_picks_the_first_period(self):
        self.assertEqual(self.pick(plan_node(FIRST, SECOND), date(2025, 12, 1)), G1)

    def test_a_plan_wholly_in_the_past_picks_the_last_period(self):
        self.assertEqual(self.pick(plan_node(FIRST, SECOND), date(2028, 1, 1)), G2)

    def test_a_single_period_plan_always_picks_that_period(self):
        node = plan_node(FIRST)

        for today in (date(2025, 1, 1), date(2026, 3, 1), date(2030, 1, 1)):
            self.assertEqual(self.pick(node, today), G1, today)

    def test_a_gap_between_periods_picks_the_one_most_recently_started(self):
        before = period("2026-01-01", "2026-03-31", 100.0, G1, 10.0)
        after = period("2026-06-01", None, 200.0, G2, 20.0)

        self.assertEqual(self.pick(plan_node(before, after), date(2026, 4, 15)), G1)

    def test_overlapping_periods_pick_the_latest_starting(self):
        a = period("2026-01-01", "2026-12-31", 100.0, G1, 10.0)
        b = period("2026-06-01", "2026-12-31", 200.0, G2, 20.0)

        self.assertEqual(self.pick(plan_node(a, b), date(2026, 7, 1)), G2)

    def test_no_plan_states_gives_none(self):
        self.assertIsNone(parse_plan({}).current_plan_state(AFTER_SWITCH))

    def test_states_with_no_dates_fall_back_to_the_first(self):
        # Legacy fixtures / an unexpected response: the old behaviour.
        node = {"planStates": [{"pricingGroupId": G1}, {"pricingGroupId": G2}]}

        self.assertEqual(self.pick(node, AFTER_SWITCH), G1)

    def test_unparseable_dates_are_ignored_not_raised(self):
        junk = {"from": "not-a-date", "to": "x", "pricingGroupId": G1}
        good = period("2026-08-01", None, 1.0, G2, 1.0)

        self.assertEqual(self.pick({"planStates": [junk, good]}, AFTER_SWITCH), G2)

    def test_datetime_strings_use_their_date_part(self):
        first = period("2026-01-20T00:00:00Z", "2026-07-31T00:00:00Z", 1.0, G1, 1.0)
        second = period("2026-08-01T00:00:00Z", None, 2.0, G2, 2.0)

        self.assertEqual(self.pick(plan_node(first, second), AFTER_SWITCH), G2)

    def test_today_may_be_a_datetime(self):
        state = parse_plan(plan_node(FIRST, SECOND)).current_plan_state(
            datetime(2026, 10, 8, 23, 59)
        )
        self.assertEqual(state.pricing_group_id, G2)

    def test_today_defaults_to_the_real_today(self):
        today = date.today()
        old = period(
            (today - timedelta(days=400)).isoformat(),
            (today - timedelta(days=101)).isoformat(),
            1.0, G1, 1.0,
        )
        current = period(
            (today - timedelta(days=100)).isoformat(),
            (today + timedelta(days=100)).isoformat(),
            2.0, G2, 2.0,
        )

        state = parse_plan(plan_node(old, current)).current_plan_state()
        self.assertEqual(state.pricing_group_id, G2)


class CurrentPeriodFiguresTests(unittest.TestCase):
    def test_the_state_carries_its_own_figures(self):
        state = parse_plan(plan_node(FIRST, SECOND)).plan_states[1]

        self.assertEqual(state.monthly_estimate, 705.08)
        self.assertEqual(state.pricing_group_id, G2)
        self.assertEqual(state.public_funding.amount, 116.24)

    def test_the_plan_level_figures_are_the_first_periods_not_todays(self):
        # The trap this feature exists for: on a live plan the plan-level
        # estimate IS the first period's, however stale that is.
        plan = parse_plan(plan_node(FIRST, SECOND))

        self.assertEqual(plan.monthly_estimate, 749.96)
        self.assertEqual(plan.current_monthly_estimate(AFTER_SWITCH), 705.08)

    def test_current_funding_comes_from_the_current_period(self):
        plan = parse_plan(plan_node(FIRST, SECOND))

        self.assertEqual(plan.public_funding.amount, 129.65)
        self.assertEqual(plan.current_public_funding(AFTER_SWITCH).amount, 116.24)
        self.assertEqual(plan.current_public_funding(BEFORE_SWITCH).amount, 129.65)

    def test_the_pricing_group_is_the_current_periods(self):
        plan = parse_plan(plan_node(FIRST, SECOND))

        self.assertEqual(
            plan.pricing_group_source(AFTER_SWITCH), (G2, "planStates[].pricingGroupId")
        )
        self.assertEqual(
            plan.pricing_group_source(BEFORE_SWITCH), (G1, "planStates[].pricingGroupId")
        )

    def test_a_plan_level_group_does_not_override_the_current_period(self):
        # Live plans never carry one, but if one ever does it must not pull the
        # adjustment onto a different period's pricing.
        node = plan_node(FIRST, SECOND)
        node["pricingGroupId"] = "pg-plan-level"

        self.assertEqual(
            parse_plan(node).pricing_group_source(AFTER_SWITCH)[0], G2
        )

    def test_a_plan_level_group_is_used_when_the_current_period_has_none(self):
        node = {
            "pricingGroupId": "pg-plan-level",
            "planStates": [{"from": "2026-01-01", "to": None}],
        }

        self.assertEqual(
            parse_plan(node).pricing_group_source(AFTER_SWITCH),
            ("pg-plan-level", "plan.pricingGroupId"),
        )

    def test_another_periods_group_is_never_borrowed(self):
        # Current period has no group of its own; the FIRST does. Using the
        # first's would price an adjustment under the wrong period.
        no_group = {"from": "2026-08-01", "to": None}
        first = period("2026-01-20", "2026-07-31", 1.0, G1, 1.0)

        group, source = parse_plan({"planStates": [first, no_group]}).pricing_group_source(
            AFTER_SWITCH
        )
        self.assertNotEqual(group, G1)
        self.assertEqual(source, "unresolved")

    def test_missing_state_figures_fall_back_to_the_plan_level_ones(self):
        node = {
            "monthlyEstimate": 812.5,
            "publicFunding": {"amount": 9.0},
            "planStates": [{"from": "2026-01-01", "to": None}],
        }
        plan = parse_plan(node)

        self.assertEqual(plan.current_monthly_estimate(AFTER_SWITCH), 812.5)
        self.assertEqual(plan.current_public_funding(AFTER_SWITCH).amount, 9.0)

    def test_the_accessor_reads_a_duck_typed_plan_as_before(self):
        self.assertEqual(
            current_monthly_estimate(SimpleNamespace(monthly_estimate=812.5)), 812.5
        )
        self.assertIsNone(current_monthly_estimate(SimpleNamespace()))


# --------------------------------------------------------------------------- #
# The Slack summary
# --------------------------------------------------------------------------- #
class SlackSummaryTests(unittest.TestCase):
    def summary(self, node, today, **kwargs):
        return slack.build_summary(parse_plan(node), [], today=today, **kwargs)

    def test_two_periods_with_today_after_the_switch_shows_the_second(self):
        text = self.summary(plan_node(FIRST, SECOND), AFTER_SWITCH)

        self.assertIn("*Current pricing* (From 2026-08-01)", text)
        self.assertIn("• Weekly total: 162.00", text)
        self.assertIn("• Monthly estimate: 705.08", text)
        self.assertIn("• Public funding: 116.24 (12h)", text)

    def test_the_stale_first_period_figures_are_not_shown_as_current(self):
        text = self.summary(plan_node(FIRST, SECOND), AFTER_SWITCH)

        self.assertNotIn("• Weekly total: 173.00", text)
        self.assertNotIn("• Monthly estimate: 749.96", text)
        self.assertNotIn("• Public funding: 129.65", text)

    def test_the_other_period_is_listed_on_one_line(self):
        text = self.summary(plan_node(FIRST, SECOND), AFTER_SWITCH)

        self.assertIn("• _(rate changes during plan — 2 periods)_", text)
        self.assertEqual(text.count("Other periods:"), 1)
        # The first period is the "other" one here, with its own estimate.
        self.assertIn("• Other periods: From 2026-01-20 — 749.96", text)

    def test_with_three_periods_both_others_are_listed_in_order(self):
        text = self.summary(plan_node(FIRST, SECOND, THIRD), AFTER_SWITCH)

        self.assertIn(
            "• Other periods: From 2026-01-20 — 749.96; From 2027-08-31 — 650.00", text
        )
        self.assertIn("• Monthly estimate: 705.08", text)

    def test_session_prices_use_the_current_periods_pricing_group(self):
        text = self.summary(plan_node(FIRST, SECOND), AFTER_SWITCH)

        self.assertIn(f"— {PRICE[G2]:.2f}", text)
        self.assertNotIn(f"{PRICE[G1]:.2f}", text)

    def test_today_before_the_switch_shows_the_first_and_its_prices(self):
        text = self.summary(plan_node(FIRST, SECOND), BEFORE_SWITCH)

        self.assertIn("*Current pricing* (From 2026-01-20)", text)
        self.assertIn("• Monthly estimate: 749.96", text)
        self.assertIn("• Weekly total: 173.00", text)
        self.assertIn("• Other periods: From 2026-08-01 — 705.08", text)
        self.assertIn(f"— {PRICE[G1]:.2f}", text)
        self.assertNotIn(f"{PRICE[G2]:.2f}", text)

    def test_a_future_start_plan_uses_the_first_period(self):
        text = self.summary(plan_node(FIRST, SECOND), date(2025, 12, 1))

        self.assertIn("*Current pricing* (From 2026-01-20)", text)
        self.assertIn("• Monthly estimate: 749.96", text)
        self.assertIn(f"— {PRICE[G1]:.2f}", text)

    def test_a_plan_wholly_in_the_past_uses_the_last_period(self):
        text = self.summary(plan_node(FIRST, SECOND), date(2028, 1, 1))

        self.assertIn("*Current pricing* (From 2026-08-01)", text)
        self.assertIn("• Monthly estimate: 705.08", text)
        self.assertIn(f"— {PRICE[G2]:.2f}", text)

    def test_a_single_period_plan_is_unchanged(self):
        node = plan_node(FIRST)
        for today in (BEFORE_SWITCH, AFTER_SWITCH, date(2030, 1, 1)):
            text = self.summary(node, today)

            self.assertIn("• Weekly total: 173.00", text)
            self.assertIn("• Monthly estimate: 749.96", text)
            self.assertIn("• Public funding: 129.65 (12h)", text)
            self.assertIn(f"— {PRICE[G1]:.2f}", text)
            # No second period, so no rate-change note and no other-periods line.
            self.assertNotIn("rate changes during plan", text)
            self.assertNotIn("Other periods", text)

    def test_a_plan_with_no_period_dates_reads_as_it_always_did(self):
        # The pre-existing shape (and test_slack's fixtures): states carry no
        # dates, so there is no label and the first state is the current one.
        node = {
            "monthlyEstimate": 812.5,
            "planStates": [
                {"planPartStates": [{"weeklyTotal": 187.5}]},
                {"planPartStates": [{"weeklyTotal": 200.0}]},
            ],
        }
        text = self.summary(node, AFTER_SWITCH)

        self.assertNotIn("Current pricing", text)
        self.assertIn("• Weekly total: 187.50", text)
        self.assertIn("• Monthly estimate: 812.50", text)
        self.assertIn("rate changes during plan", text)

    def test_the_default_today_is_the_real_today(self):
        today = date.today()
        old = period((today - timedelta(days=400)).isoformat(),
                     (today - timedelta(days=101)).isoformat(), 111.0, G1, 1.0)
        now = period((today - timedelta(days=100)).isoformat(), None, 222.0, G2, 2.0)

        text = slack.build_summary(parse_plan(plan_node(old, now)), [])

        self.assertIn("• Monthly estimate: 222.00", text)


# --------------------------------------------------------------------------- #
# The preview store and the adjustment flow, end to end
# --------------------------------------------------------------------------- #
def _relative_two_period_plan():
    """First period ended 100 days ago; the second is in force today."""
    today = date.today()

    def iso(days):
        return (today + timedelta(days=days)).isoformat()

    return parse_plan(
        plan_node(
            period(iso(-400), iso(-101), 1913.99, G1, 400.0, 129.65, 12),
            period(iso(-100), iso(500), 1791.97, G2, 380.0, 116.24, 12),
        )
    )


PLAN_BODY = {
    "plan": {
        "id": "",
        "childId": CHILD,
        "from": "2026-09-01",
        "planParts": [{"planPartId": "pp-1", "sessionBookings": []}],
    }
}


class AdjustmentFlowTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        env = mock.patch.dict(
            "os.environ",
            {
                "PREVIEW_STORE_PATH": str(Path(tmp.name) / "store.sqlite3"),
                "PREVIEW_TTL_HOURS": "24",
                "FAMLY_ACCESS_TOKEN": "test-token",
                "COMMIT_ENABLED": "true",
                "COMMIT_ALLOWED_CHILD_IDS": CHILD,
                "SLACK_BOT_TOKEN": "xoxb-test",
                "SLACK_CHANNEL_ID": "C123",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        self.plan = _relative_two_period_plan()
        # The live child-name lookup must never reach the network from a test.
        self.child_name = None
        self.previews = []  # (body, version) sent to Famly's preview
        self.preview_posts = []  # (text, preview_id) posted to Slack
        self.adjusted_posts = []  # (text, preview_id) posted after an adjustment

        def fake_preview(body, version, client=None, old_plan_id=None, replace_old_plan=False):
            self.previews.append((body, version))
            return SimpleNamespace(plan=self.plan, warnings=[], raw={})

        for target, replacement in (
            (approval.runner, ("preview", fake_preview)),
            (
                slack,
                (
                    "post_preview",
                    lambda text, pid: self.preview_posts.append((text, pid)) or True,
                ),
            ),
            (
                slack,
                (
                    "post_adjusted",
                    lambda text, pid: self.adjusted_posts.append((text, pid)) or True,
                ),
            ),
            (slack, ("post_notice", lambda text: True)),
            (slack, ("update_message", lambda url, text: True)),
            (
                child_lookup,
                ("lookup_child_name", lambda child_id, client=None: self.child_name),
            ),
        ):
            patcher = mock.patch.object(target, replacement[0], replacement[1])
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_preview(self):
        """Run the real preview path with the intake stubbed to our plan."""
        intake = hubspot_intake.IntakeResult(
            ok=True,
            plan_body=PLAN_BODY,
            result=SimpleNamespace(plan=self.plan, warnings=[], raw={}),
        )
        with mock.patch.object(web.hubspot_intake, "handle_intake", return_value=intake):
            data, status = web.run_preview_and_post({"childId": CHILD})
        self.assertEqual(status, 200)
        return data["preview_id"]

    def adjust(self, preview_id, value="-0.50"):
        return approval.handle_adjust_submission(
            preview_id, value, "drew", spawn=lambda work: work()
        )


class StoredBaselineTests(AdjustmentFlowTestCase):
    def test_the_stored_baseline_is_the_current_periods(self):
        stored = preview_store.get(self.run_preview())

        self.assertEqual(stored.monthly_estimate, 1791.97)
        self.assertNotEqual(stored.monthly_estimate, self.plan.monthly_estimate)

    def test_the_stored_pricing_group_is_the_current_periods(self):
        stored = preview_store.get(self.run_preview())

        self.assertEqual(stored.pricing_group_id, G2)

    def test_the_posted_summary_shows_the_current_period(self):
        self.run_preview()

        text, _ = self.preview_posts[-1]
        self.assertIn("• Monthly estimate: 1,791.97", text)
        self.assertNotIn("• Monthly estimate: 1,913.99", text)
        # The stale first period is still visible, but only as an OTHER period.
        self.assertIn("• Other periods: From", text)
        self.assertIn("— 1,913.99", text)
        self.assertIn(f"— {PRICE[G2]:.2f}", text)

    def test_a_single_period_plan_is_stored_as_before(self):
        today = date.today()
        self.plan = parse_plan(
            plan_node(
                period(
                    (today - timedelta(days=30)).isoformat(), None, 812.5, G1, 187.5
                )
            )
        )

        stored = preview_store.get(self.run_preview())

        self.assertEqual(stored.monthly_estimate, 812.5)
        self.assertEqual(stored.pricing_group_id, G1)


class AdjustmentUsesTheCurrentPeriodTests(AdjustmentFlowTestCase):
    def test_total_adjustments_names_the_second_periods_group(self):
        preview_id = self.run_preview()

        self.assertIsNone(self.adjust(preview_id))

        body, _ = self.previews[-1]
        adjustments = body["plan"]["planParts"][0]["totalAdjustments"]
        self.assertEqual(adjustments, [{"pricingGroupId": G2, "adjustment": -0.5}])
        self.assertNotIn(G1, str(body))

    def test_the_base_in_total_to_bill_is_the_second_periods_estimate(self):
        preview_id = self.run_preview()

        self.adjust(preview_id)

        text, _ = self.adjusted_posts[-1]
        self.assertIn("1,791.97", text)  # base
        self.assertIn("Total to be billed: 1,791.47", text)  # base - 0.50
        self.assertNotIn("1,913.99", text)
        self.assertNotIn("1,913.49", text)

    def test_the_adjusted_copys_stored_baseline_is_the_second_periods(self):
        preview_id = self.run_preview()

        self.adjust(preview_id)

        new_stored = preview_store.get(self.adjusted_posts[-1][1])
        self.assertEqual(new_stored.monthly_estimate, 1791.97)
        self.assertEqual(new_stored.pricing_group_id, G2)

    def test_the_adjustment_modal_shows_the_second_periods_estimate(self):
        preview_id = self.run_preview()
        opened = []

        with mock.patch.object(
            slack, "open_modal", lambda trigger, view: opened.append(view) or True
        ):
            self.assertIsNone(approval.handle_adjust_click(preview_id, "trigger-1", "drew"))

        self.assertIn("1,791.97", str(opened[0]))
        self.assertNotIn("1,913.99", str(opened[0]))

    def test_the_total_is_the_current_estimate_plus_the_adjustment(self):
        self.assertAlmostEqual(
            approval.total_to_bill(self.plan.current_monthly_estimate(), 0.35),
            1792.32,
            places=2,
        )

    def test_a_single_period_adjustment_is_unchanged(self):
        today = date.today()
        self.plan = parse_plan(
            plan_node(
                period((today - timedelta(days=30)).isoformat(), None, 812.5, G1, 187.5)
            )
        )
        preview_id = self.run_preview()

        self.adjust(preview_id, "0.35")

        body, _ = self.previews[-1]
        self.assertEqual(
            body["plan"]["planParts"][0]["totalAdjustments"],
            [{"pricingGroupId": G1, "adjustment": 0.35}],
        )
        self.assertIn("Total to be billed: 812.85", self.adjusted_posts[-1][0])


# --------------------------------------------------------------------------- #
# What the web response and the CLI REPORT
# --------------------------------------------------------------------------- #
# The keys the web response carried before pricing periods were reported. They
# must all still be there, under the same names, so nothing reading them breaks.
EXISTING_WEB_KEYS = {
    "planId", "childId", "from", "to", "billingScheme", "monthlyEstimate",
    "planPartIds", "sessionCount", "publicFundingAmount", "publicFundingHours",
    "publicFundingMinutes",
}


class CurrentPricingSummaryTests(unittest.TestCase):
    """The shared helper, pinned to dates: FIRST then SECOND, today after the switch."""

    def summary(self, node, today=AFTER_SWITCH):
        return current_pricing_summary(parse_plan(node), today)

    def test_the_existing_keys_carry_the_second_periods_figures(self):
        summary = self.summary(plan_node(FIRST, SECOND))

        self.assertEqual(summary["monthlyEstimate"], 705.08)
        self.assertEqual(summary["weeklyTotal"], 162.0)
        self.assertEqual(summary["publicFundingAmount"], 116.24)
        self.assertEqual(summary["publicFundingHours"], 12)
        self.assertEqual(summary["publicFundingMinutes"], 0)

    def test_none_of_the_first_periods_figures_leak_into_those_keys(self):
        summary = self.summary(plan_node(FIRST, SECOND))

        self.assertNotEqual(summary["monthlyEstimate"], 749.96)
        self.assertNotEqual(summary["weeklyTotal"], 173.0)
        self.assertNotEqual(summary["publicFundingAmount"], 129.65)

    def test_pricing_from_is_the_current_periods_start_date(self):
        self.assertEqual(self.summary(plan_node(FIRST, SECOND))["pricingFrom"], "2026-08-01")

    def test_periods_lists_both_periods_in_order(self):
        self.assertEqual(
            self.summary(plan_node(FIRST, SECOND))["periods"],
            [
                {"from": "2026-01-20", "to": "2026-07-31", "monthlyEstimate": 749.96},
                {"from": "2026-08-01", "to": "2027-08-30", "monthlyEstimate": 705.08},
            ],
        )

    def test_periods_lists_every_period_whichever_is_current(self):
        summary = self.summary(plan_node(FIRST, SECOND, THIRD))

        self.assertEqual(
            [p["from"] for p in summary["periods"]],
            ["2026-01-20", "2026-08-01", "2027-08-31"],
        )
        self.assertIsNone(summary["periods"][2]["to"])  # open-ended stays null
        self.assertEqual(summary["pricingFrom"], "2026-08-01")

    def test_before_the_switch_the_first_periods_figures_are_reported(self):
        summary = self.summary(plan_node(FIRST, SECOND), BEFORE_SWITCH)

        self.assertEqual(summary["monthlyEstimate"], 749.96)
        self.assertEqual(summary["weeklyTotal"], 173.0)
        self.assertEqual(summary["publicFundingAmount"], 129.65)
        self.assertEqual(summary["pricingFrom"], "2026-01-20")
        self.assertEqual(len(summary["periods"]), 2)

    def test_a_single_period_plan_reports_its_figures_and_one_period(self):
        summary = self.summary(plan_node(FIRST))

        self.assertEqual(summary["monthlyEstimate"], 749.96)
        self.assertEqual(summary["weeklyTotal"], 173.0)
        self.assertEqual(summary["pricingFrom"], "2026-01-20")
        self.assertEqual(len(summary["periods"]), 1)

    def test_a_plan_with_no_periods_reports_plan_level_figures_and_no_periods(self):
        summary = self.summary(
            {"monthlyEstimate": 812.5, "publicFunding": {"amount": 9.0, "hours": 3}}
        )

        self.assertEqual(summary["monthlyEstimate"], 812.5)
        self.assertEqual(summary["publicFundingAmount"], 9.0)
        self.assertIsNone(summary["weeklyTotal"])
        self.assertIsNone(summary["pricingFrom"])
        self.assertEqual(summary["periods"], [])

    def test_a_duck_typed_plan_is_read_from_its_plain_attributes(self):
        summary = current_pricing_summary(
            SimpleNamespace(
                monthly_estimate=812.5,
                public_funding=SimpleNamespace(amount=9.0, hours=3, minutes=30),
            )
        )

        self.assertEqual(summary["monthlyEstimate"], 812.5)
        self.assertEqual(summary["publicFundingMinutes"], 30)
        self.assertEqual(summary["periods"], [])
        self.assertIsNone(summary["pricingFrom"])


class WebPlanSummaryTests(unittest.TestCase):
    """`web._plan_summary` -- the `plan` object in the HubSpot-facing response."""

    def test_the_existing_keys_carry_the_second_periods_figures(self):
        plan = _relative_two_period_plan()  # first period over, second in force

        summary = web._plan_summary(plan)

        self.assertEqual(summary["monthlyEstimate"], 1791.97)
        self.assertEqual(summary["weeklyTotal"], 380.0)
        self.assertEqual(summary["publicFundingAmount"], 116.24)
        # ...where the plan-level values (the first period's) say otherwise.
        self.assertEqual(plan.monthly_estimate, 1913.99)

    def test_the_pricing_period_is_identified_and_all_periods_are_listed(self):
        plan = _relative_two_period_plan()
        summary = web._plan_summary(plan)

        self.assertEqual(summary["pricingFrom"], plan.plan_states[1].from_)
        self.assertEqual(
            summary["periods"],
            [
                {"from": s.from_, "to": s.to, "monthlyEstimate": s.monthly_estimate}
                for s in plan.plan_states
            ],
        )
        self.assertEqual(
            [p["monthlyEstimate"] for p in summary["periods"]], [1913.99, 1791.97]
        )

    def test_every_pre_existing_key_is_still_present(self):
        summary = web._plan_summary(_relative_two_period_plan())

        self.assertTrue(EXISTING_WEB_KEYS <= set(summary), EXISTING_WEB_KEYS - set(summary))

    def test_the_non_pricing_keys_are_unchanged(self):
        summary = web._plan_summary(_relative_two_period_plan())

        self.assertEqual(summary["planId"], "plan-1")
        self.assertEqual(summary["childId"], CHILD)
        self.assertEqual(summary["planPartIds"], ["pp-1"])
        self.assertEqual(summary["sessionCount"], 1)

    def test_no_plan_is_still_none(self):
        self.assertIsNone(web._plan_summary(None))


class PlanWriteCliOutputTests(unittest.TestCase):
    """The preview-csv rows the plan-write CLI prints and returns."""

    def row_for(self, plan):
        plan_input = SimpleNamespace(child_id=CHILD)
        result = SimpleNamespace(plan=plan, warnings=[])
        args = SimpleNamespace(file="plans.csv", version=3)

        with mock.patch.object(cli.csv_loader, "load_csv", return_value=[plan_input]), \
             mock.patch.object(cli.input_schema, "validate", return_value=[]), \
             mock.patch.object(cli.input_schema, "to_plan_body", return_value={}), \
             mock.patch.object(cli.runner, "preview", return_value=result), \
             mock.patch.object(cli, "_print_warnings"):
            (row,) = cli._handle_csv(args)
        return row

    def test_the_monthly_estimate_is_the_second_periods(self):
        plan = _relative_two_period_plan()

        row = self.row_for(plan)

        self.assertEqual(row["monthlyEstimate"], 1791.97)
        self.assertEqual(row["weeklyTotal"], 380.0)
        self.assertEqual(row["publicFundingAmount"], 116.24)
        self.assertEqual(row["pricingFrom"], plan.plan_states[1].from_)
        self.assertEqual(len(row["periods"]), 2)

    def test_the_row_keeps_its_existing_keys(self):
        row = self.row_for(_relative_two_period_plan())

        for key in ("childId", "valid", "errors", "previewed", "monthlyEstimate", "warnings"):
            self.assertIn(key, row)
        self.assertEqual(row["childId"], CHILD)
        self.assertTrue(row["valid"])
        self.assertTrue(row["previewed"])

    def test_a_preview_with_no_plan_still_reports_a_null_estimate(self):
        row = self.row_for(None)

        self.assertIsNone(row["monthlyEstimate"])
        self.assertTrue(row["previewed"])


if __name__ == "__main__":
    unittest.main()
