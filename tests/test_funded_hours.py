"""Tests for clamping funded hours to the hours Famly can actually fund.

Run from the project root:

    python -m unittest tests.test_funded_hours -v

Reuses the create_timeline harness and the pricing fake from the auto-round
tests, extended so Famly reports `publicFunding` per period and raises a
shortfall warning when the hours asked for exceed the scheduled hours.
NOTHING here touches Famly or Slack.
"""

import json
import logging
import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

try:  # `python -m unittest tests.test_funded_hours`
    from tests import test_auto_round as auto
    from tests import test_create_timeline as harness
    from tests import test_queue_timeline as queue_tests
except ImportError:  # `python -m unittest discover -s tests`
    import test_auto_round as auto
    import test_create_timeline as harness
    import test_queue_timeline as queue_tests

from actions.additional_plan import web as additional_plan_web
from actions.create_timeline import chains
from actions.plan_write import approval, funded_hours, preview_store, rounding
from actions.plan_write import web as plan_write_web
from actions.queue_timeline import jobs, worker
from actions.queue_timeline import web as queue_web
from integrations import slack
from tests import fast_sqlite  # noqa: F401  (SQLite without fsync: see the module)

logging.disable(logging.CRITICAL)

CHILD = harness.CHILD
TODAY = date.today().isoformat()
RATE = 0.2  # pounds per funded minute, in the fake

SHORTFALL = {
    "title": "Funding shortfall",
    "message": "Scheduled hours are fewer than the funded hours",
    "severity": "warning",
    "error": "FundingShortfall",
}
LIMIT_NOTE = {
    "title": "Funding limit reached",
    "message": "fyi",
    "severity": "info",
    "error": "FundingLimit",
}


class FundingFamly(auto.PricingFamly):
    """Prices periods, reports `publicFunding`, and warns about a shortfall.

    `scheduled` is the minutes of funded time the child's sessions allow. Famly
    applies the smaller of that and the minutes asked for, and the funded AMOUNT
    follows the applied minutes -- so clamping to the applied minutes changes
    nothing. `amount_follows_request` breaks that on purpose.
    """

    def __init__(self):
        super().__init__()
        self.scheduled = 1650  # 27.5 hours
        self.amount_follows_request = False
        self.warn_on_shortfall = True
        self.extra_warnings: list = []
        self.estimates = [("pg-1", 500.0), ("pg-1", 500.0)]

    def post(self, path, params=None, json_body=None):
        node = super().post(path, params, json_body)
        settings = json_body["plan"].get("publicFundingSettings")
        if settings is None or (params or {}).get("preview") != "true":
            return node

        asked = funded_hours.requested_minutes(settings["hours"])
        applied = max(0, min(asked, self.scheduled))
        hours, minutes = divmod(applied, 60)
        amount = round((asked if self.amount_follows_request else applied) * RATE, 2)
        for state in node["planStates"]:
            state["publicFunding"] = {"amount": amount, "hours": hours, "minutes": minutes}

        warnings = list(self.extra_warnings)
        if asked > self.scheduled and self.warn_on_shortfall:
            warnings.append(SHORTFALL)
        if warnings:
            node["behaviors"] = [
                {"id": "ShowPlanWarnings", "payload": {"warnings": warnings}}
            ]
        return node


class FundedTestCase(harness.ChainTestCase):
    def setUp(self):
        super().setUp()
        self.famly = FundingFamly()
        env = mock.patch.dict("os.environ", {"AUTO_ROUND_DEFAULT": ""})
        env.start()
        self.addCleanup(env.stop)

    def flat(self, **overrides):
        body = harness.payload(
            **{"from": "2026-09-01", "funded": "true", "fundingHours": 33, "note": "", "site_code": "HDCITY"}
        )
        body.update(overrides)
        return body

    def preview(self, **overrides):
        return plan_write_web.handle(self.flat(**overrides))

    def stored(self):
        return preview_store.get(self.last_preview_id())

    def hours_in(self, body):
        return body["plan"]["publicFundingSettings"]["hours"]

    def titles(self, data):
        return [json.dumps(w) for w in data["warnings"]]


class ClampTests(FundedTestCase):
    def test_a_shortfall_clamps_the_hours_to_what_famly_applies(self):
        data, status = self.preview()

        self.assertEqual(status, 200)
        self.assertEqual(self.hours_in(self.stored().plan_body), 27.5)
        self.assertEqual(len(self.famly.previews), 2)  # the preview and one verification

    def test_the_amounts_are_unchanged(self):
        nodes = []
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            if (params or {}).get("preview") == "true":
                nodes.append(node)
            return node

        with mock.patch.object(self.famly, "post", post):
            self.preview()

        original, clamped = nodes
        self.assertEqual(len(nodes), 2)
        for before, after in zip(original["planStates"], clamped["planStates"]):
            self.assertEqual(before["publicFunding"]["amount"], after["publicFunding"]["amount"])
            self.assertEqual(before["monthlyEstimate"], after["monthlyEstimate"])
        self.assertEqual(original["planStates"][0]["publicFunding"]["amount"], 330.0)

    def test_the_amount_in_the_summary_is_the_applied_amount(self):
        self.preview()

        self.assertIn("Public funding: 330.00 (27h 30m)", self.last_text())

    def test_max_funded_minutes_is_unchanged(self):
        self.preview(maxFundedMinutes=660)

        settings = self.stored().plan_body["plan"]["publicFundingSettings"]
        self.assertEqual(settings["maxFundedMinutes"], 660)

    def test_the_shortfall_warning_is_gone_and_the_summary_says_so(self):
        data, _ = self.preview()

        self.assertFalse(any("Funding shortfall" in t for t in self.titles(data)))
        text = self.last_text()
        self.assertNotIn("Funding shortfall", text)
        self.assertIn("Funding set to scheduled hours: 27.5h (entitlement 33h)", text)
        self.assertIn("No warnings returned", text)

    def test_the_note_text(self):
        self.preview()

        self.assertEqual(
            self.stored().plan_body["plan"]["note"],
            f"Funded hours set to scheduled hours: 27.5h (entitlement 33h), {TODAY}",
        )

    def test_the_note_is_appended_to_an_existing_one(self):
        self.preview(note="Starter")

        self.assertEqual(
            self.stored().plan_body["plan"]["note"],
            f"Starter\nFunded hours set to scheduled hours: 27.5h (entitlement 33h), {TODAY}",
        )

    def test_hours_that_are_not_whole_minutes_are_shown_to_two_places(self):
        self.famly.scheduled = 1475  # 24h 35m = 24.58333333h

        self.preview()

        self.assertEqual(round(self.hours_in(self.stored().plan_body) * 60), 1475)
        self.assertIn("Funded hours set to scheduled hours: 24.58h (entitlement 33h)", self.stored().plan_body["plan"]["note"])

    def test_a_clamped_plan_commits_with_the_clamped_hours(self):
        self.preview()
        self.approve()

        self.assertEqual(self.hours_in(self.famly.commits[0]["body"]), 27.5)

    def test_the_servers_funding_rules_are_never_written_to(self):
        before = self.funding_file.read_bytes()
        self.preview()

        self.assertEqual(self.funding_file.read_bytes(), before)

    def test_additional_plan_clamps_too(self):
        self.famly.plans.append({"id": "existing", "childId": CHILD, "from": "2025-09-01", "to": None})

        additional_plan_web.handle(self.flat(), sleeper=lambda s: None)

        self.assertEqual(self.hours_in(self.stored().plan_body), 27.5)
        self.assertEqual(self.stored().old_plan_id, "existing")
        self.assertEqual(
            [c["params"].get("oldPlanId") for c in self.famly.previews], ["existing", "existing"]
        )

    def test_a_plan_with_no_funding_is_untouched(self):
        self.preview(funded="false")

        self.assertEqual(len(self.famly.previews), 1)
        self.assertNotIn("publicFundingSettings", self.stored().plan_body["plan"])
        self.assertNotIn("Funding set", self.last_text())


class NoClampTests(FundedTestCase):
    def assert_unclamped(self, hours=33):
        self.assertEqual(len(self.famly.previews), 1)  # nothing to verify
        self.assertEqual(self.hours_in(self.stored().plan_body), hours)
        self.assertNotIn("Funded hours set", self.stored().plan_body["plan"].get("note", ""))
        self.assertNotIn("Funding set to scheduled hours", self.last_text())

    def test_a_rounding_sized_difference_does_nothing(self):
        for scheduled in (1980, 1979):  # requested 1980: a difference of 0 and of 1 minute
            with self.subTest(scheduled=scheduled):
                self.famly.posts.clear()
                self.famly.scheduled = scheduled
                self.preview()

                self.assert_unclamped()

    def test_two_minutes_short_is_a_shortfall(self):
        self.famly.scheduled = 1978

        self.preview()

        self.assertEqual(round(self.hours_in(self.stored().plan_body) * 60), 1978)

    def test_the_threshold_is_more_than_one_minute(self):
        self.assertEqual(funded_hours.ROUNDING_MINUTES, 1)

    def test_nothing_short_means_nothing_to_do(self):
        self.famly.scheduled = 2400

        self.preview()

        self.assert_unclamped()

    def test_famly_not_reporting_funded_time_means_no_clamp(self):
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            for state in node["planStates"]:
                state.pop("publicFunding", None)
            return node

        with mock.patch.object(self.famly, "post", post):
            self.preview()

        self.assert_unclamped()

    def test_an_applied_time_of_zero_is_not_clamped_and_famlys_warning_stays(self):
        self.famly.scheduled = 0

        data, _ = self.preview()

        self.assert_unclamped()
        self.assertTrue(any("Funding shortfall" in t for t in self.titles(data)))
        self.assertIn("Funding shortfall", self.last_text())


class DiscardedClampTests(FundedTestCase):
    def test_a_clamp_that_changes_the_money_is_discarded(self):
        self.famly.amount_follows_request = True

        data, _ = self.preview()

        body = self.stored().plan_body
        self.assertEqual(self.hours_in(body), 33)  # the original body
        self.assertNotIn("Funded hours set", body["plan"].get("note", ""))
        self.assertEqual(len(self.famly.previews), 2)

    def test_the_original_warning_is_kept_and_the_reason_is_recorded(self):
        self.famly.amount_follows_request = True

        data, _ = self.preview()

        titles = self.titles(data)
        self.assertTrue(any("Funding shortfall" in t for t in titles), titles)
        self.assertTrue(any("Funded hours not clamped" in t and "funded amount" in t for t in titles), titles)
        self.assertNotIn("Funding set to scheduled hours", self.last_text())

    def test_a_clamp_that_moves_the_monthly_estimate_is_discarded(self):
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            settings = json_body["plan"].get("publicFundingSettings")
            if (params or {}).get("preview") == "true" and settings and settings["hours"] < 33:
                node["planStates"][0]["monthlyEstimate"] += 5
            return node

        with mock.patch.object(self.famly, "post", post):
            data, _ = self.preview()

        self.assertEqual(self.hours_in(self.stored().plan_body), 33)
        self.assertTrue(any("monthly estimate" in t for t in self.titles(data)))

    def test_a_clamp_that_leaves_the_shortfall_warning_is_discarded(self):
        self.famly.scheduled = 1650
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            settings = json_body["plan"].get("publicFundingSettings")
            if (params or {}).get("preview") == "true" and settings and settings["hours"] < 33:
                node["behaviors"] = [{"id": "ShowPlanWarnings", "payload": {"warnings": [SHORTFALL]}}]
            return node

        with mock.patch.object(self.famly, "post", post):
            data, _ = self.preview()

        self.assertEqual(self.hours_in(self.stored().plan_body), 33)
        self.assertTrue(any("still there" in t for t in self.titles(data)))

    def test_a_clamp_that_raises_a_new_warning_is_discarded(self):
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            settings = json_body["plan"].get("publicFundingSettings")
            if (params or {}).get("preview") == "true" and settings and settings["hours"] < 33:
                other = {"title": "Something else", "message": "x", "severity": "warning", "error": "ELSE"}
                node["behaviors"] = [{"id": "ShowPlanWarnings", "payload": {"warnings": [other]}}]
            return node

        with mock.patch.object(self.famly, "post", post):
            data, _ = self.preview()

        self.assertEqual(self.hours_in(self.stored().plan_body), 33)
        self.assertTrue(any("new warning" in t for t in self.titles(data)))

    def test_a_failed_verification_preview_discards_the_clamp(self):
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            settings = json_body["plan"].get("publicFundingSettings")
            if (params or {}).get("preview") == "true" and settings and settings["hours"] < 33:
                raise harness.RestHTTPError("Famly down", 503, "")
            return real_post(path, params, json_body)

        with mock.patch.object(self.famly, "post", post):
            data, status = self.preview()

        self.assertEqual(status, 200)
        self.assertEqual(self.hours_in(self.stored().plan_body), 33)

    def test_an_unclamped_plan_still_commits_as_it_was_previewed(self):
        self.famly.amount_follows_request = True
        self.preview()
        self.approve()

        self.assertEqual(self.hours_in(self.famly.commits[0]["body"]), 33)


class HoursAreNeverRaisedTests(unittest.TestCase):
    def preview_with(self, applied_hours, applied_minutes):
        state = SimpleNamespace(
            from_="2026-09-01",
            monthly_estimate=500.0,
            public_funding=SimpleNamespace(
                amount=1.0, hours=applied_hours, minutes=applied_minutes, seconds=None
            ),
        )
        return SimpleNamespace(plan=SimpleNamespace(plan_states=[state]), warnings=[])

    def body(self, hours):
        return {"plan": {"note": "", "publicFundingSettings": {"hours": hours, "maxFundedMinutes": 660}}}

    def run_apply(self, hours, applied_hours, applied_minutes):
        reprice = mock.Mock(return_value=SimpleNamespace(
            plan=self.preview_with(applied_hours, applied_minutes).plan, warnings=[]
        ))
        with mock.patch.object(funded_hours.runner, "preview", reprice):
            return funded_hours.apply(self.body(hours), self.preview_with(applied_hours, applied_minutes), 3), reprice

    def test_applied_above_requested_leaves_the_hours_alone(self):
        outcome, reprice = self.run_apply(20, 30, 0)

        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.body["plan"]["publicFundingSettings"]["hours"], 20)
        reprice.assert_not_called()

    def test_applied_equal_to_requested_leaves_the_hours_alone(self):
        outcome, _ = self.run_apply(27.5, 27, 30)

        self.assertFalse(outcome.applied)

    def test_the_new_hours_are_always_below_the_old(self):
        for asked, h, m in ((33, 27, 30), (24.58823529, 20, 0), (15, 14, 0), (30, 29, 58), (16.5, 8, 15)):
            with self.subTest(asked=asked, applied=(h, m)):
                outcome, _ = self.run_apply(asked, h, m)
                if outcome.applied:
                    self.assertLess(outcome.hours, Decimal(str(asked)))
                    self.assertEqual(round(outcome.hours * 60), h * 60 + m)
                else:
                    self.assertEqual(outcome.body["plan"]["publicFundingSettings"]["hours"], asked)

    def test_the_new_hours_are_decimal_minutes_over_sixty(self):
        outcome, _ = self.run_apply(33, 27, 30)

        self.assertEqual(outcome.hours, Decimal("27.5"))
        self.assertEqual(outcome.entitlement_hours, Decimal("33"))


class AppliedMinutesTests(unittest.TestCase):
    def funding(self, **parts):
        return SimpleNamespace(**{"hours": None, "minutes": None, "seconds": None, **parts})

    def test_hours_and_minutes_are_components_of_one_duration_cases(self):
        """Folded cases: hours_and_minutes_are_components_of_one_duration; a_minutes_figure_of_sixty_or_more_is_read_as_the_total."""
        for _i, (_p0,) in enumerate([
            (30,),
            (1650,),
        ]):
            with self.subTest(case=_i):
                self.assertEqual(funded_hours.applied_minutes(self.funding(hours=27, minutes=_p0)), 1650)

    def test_seconds_are_added(self):
        self.assertEqual(funded_hours.applied_minutes(self.funding(hours=1, minutes=1, seconds=30)), Decimal("61.5"))

    def test_hours_alone_fall_back_to_hours_times_sixty(self):
        self.assertEqual(funded_hours.applied_minutes(self.funding(hours=27.5)), 1650)
        self.assertEqual(funded_hours.applied_minutes(self.funding(hours=15, minutes=0)), 900)

    def test_minutes_alone(self):
        self.assertEqual(funded_hours.applied_minutes(self.funding(minutes=45)), 45)

    def test_nothing_reported_is_none_and_zero_is_zero(self):
        self.assertIsNone(funded_hours.applied_minutes(None))
        self.assertIsNone(funded_hours.applied_minutes(self.funding()))
        self.assertEqual(funded_hours.applied_minutes(self.funding(hours=0, minutes=0)), 0)

    def test_the_smallest_period_wins_and_unreported_periods_are_skipped(self):
        plan = SimpleNamespace(
            plan_states=[
                SimpleNamespace(public_funding=self.funding(hours=27, minutes=30)),
                SimpleNamespace(public_funding=None),
                SimpleNamespace(public_funding=self.funding(hours=20)),
            ]
        )

        self.assertEqual(funded_hours.smallest_applied(plan), 1200)
        self.assertIsNone(funded_hours.smallest_applied(SimpleNamespace(plan_states=[])))

    def test_requested_minutes_round_half_up(self):
        self.assertEqual(funded_hours.requested_minutes(33), 1980)
        self.assertEqual(funded_hours.requested_minutes(24.58823529), 1475)
        self.assertEqual(funded_hours.requested_minutes(0.00833334), 1)
        self.assertEqual(funded_hours.requested_minutes("27.5"), 1650)
        self.assertIsNone(funded_hours.requested_minutes(None))
        self.assertIsNone(funded_hours.requested_minutes("abc"))

    def test_format_hours(self):
        self.assertEqual(funded_hours.format_hours(Decimal("27.5")), "27.5")
        self.assertEqual(funded_hours.format_hours(Decimal("33")), "33")
        self.assertEqual(funded_hours.format_hours(Decimal("24.58333333")), "24.58")
        self.assertEqual(funded_hours.format_hours(Decimal("0.5")), "0.5")


class WarningClassificationTests(unittest.TestCase):
    def warning(self, title, message="", local=False):
        from actions.plan_write import warnings as plan_warnings

        if local:
            return plan_warnings.local_warning(title, message)
        return plan_warnings.classify(
            plan_warnings.PlanWarning(title=title, message=message, error="X", raw={})
        )

    def test_a_famly_funding_warning_is_a_shortfall(self):
        self.assertTrue(funded_hours.is_shortfall(self.warning("Funding shortfall")))
        self.assertTrue(funded_hours.is_shortfall(self.warning("Plan", "funded hours exceed the schedule")))

    def test_the_funding_limit_note_is_informational_not_a_shortfall(self):
        note = self.warning("Funding limit reached")
        self.assertTrue(funded_hours.is_informational(note))
        self.assertFalse(funded_hours.is_shortfall(note))

    def test_a_warning_this_codebase_raised_is_never_a_shortfall(self):
        self.assertFalse(funded_hours.is_shortfall(self.warning("Funding thing", local=True)))

    def test_an_unrelated_warning_is_not_a_shortfall(self):
        self.assertFalse(funded_hours.is_shortfall(self.warning("Discount excluded")))

    def test_the_queue_uses_the_same_informational_definition(self):
        self.assertIs(worker.INFORMATIONAL_TITLES, funded_hours.INFORMATIONAL_TITLE_FRAGMENTS)


class ManualAdjustmentTests(FundedTestCase):
    def test_a_manual_adjustment_keeps_the_clamped_hours(self):
        self.preview(**{"from": "2026-08-01"})  # today falls in the first period
        original = self.last_preview_id()

        approval.handle_adjust_submission(original, "0.50", "drew", spawn=lambda w: w())
        adjusted = preview_store.get(self.adjusted_posted[-1][1])

        self.assertEqual(self.hours_in(adjusted.plan_body), 27.5)
        self.assertIn("Funded hours set to scheduled hours: 27.5h", adjusted.plan_body["plan"]["note"])
        self.assertIn("Adjustment: £+0.50", adjusted.plan_body["plan"]["note"])

    def test_the_adjusted_plan_commits_with_the_clamped_hours(self):
        self.preview(**{"from": "2026-08-01"})
        approval.handle_adjust_submission(self.last_preview_id(), "0.50", "drew", spawn=lambda w: w())

        self.approve(self.adjusted_posted[-1][1])

        self.assertEqual(self.hours_in(self.famly.commits[0]["body"]), 27.5)

    def test_the_adjustment_re_preview_is_sent_with_the_clamped_hours(self):
        self.preview(**{"from": "2026-08-01"})
        approval.handle_adjust_submission(self.last_preview_id(), "0.50", "drew", spawn=lambda w: w())

        self.assertEqual(self.hours_in(self.famly.previews[-1]["body"]), 27.5)


class ClampThenRoundTests(FundedTestCase):
    def setUp(self):
        super().setUp()
        self.famly.estimates = [("pg-1", 269.03), ("pg-2", 1503.30)]

    def test_the_clamp_comes_first_and_the_rounding_works_on_the_clamped_body(self):
        self.preview(auto_round="true")

        hours = [self.hours_in(c["body"]) for c in self.famly.previews]
        adjustments = [
            c["body"]["plan"]["planParts"][0].get("totalAdjustments") or [] for c in self.famly.previews
        ]
        # 1. the plain preview, 2. the clamp's re-preview, 3. the rounding's.
        self.assertEqual(len(hours), 3)
        self.assertEqual(hours, [33, 27.5, 27.5])
        self.assertEqual([len(a) for a in adjustments], [0, 0, 2])

    def test_the_stored_body_carries_both_and_the_note_has_both_lines_in_order(self):
        self.preview(auto_round="true")
        body = self.stored().plan_body

        self.assertEqual(self.hours_in(body), 27.5)
        self.assertEqual(len(body["plan"]["planParts"][0]["totalAdjustments"]), 2)
        lines = body["plan"]["note"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("Funded hours set to scheduled hours"))
        self.assertTrue(lines[1].startswith("Auto-rounded:"))

    def test_the_summary_shows_both(self):
        self.preview(auto_round="true")

        text = self.last_text()
        self.assertIn("Funding set to scheduled hours: 27.5h (entitlement 33h)", text)
        self.assertIn("Rounded: 269.03 -> 270.00", text)

    def test_a_discarded_clamp_still_lets_the_rounding_run_on_the_original_body(self):
        self.famly.amount_follows_request = True
        self.preview(auto_round="true")
        body = self.stored().plan_body

        self.assertEqual(self.hours_in(body), 33)
        self.assertEqual(len(body["plan"]["planParts"][0]["totalAdjustments"]), 2)

    def test_the_committed_body_has_both(self):
        self.preview(auto_round="true")
        self.approve()

        committed = self.famly.commits[0]["body"]
        self.assertEqual(self.hours_in(committed), 27.5)
        self.assertEqual(len(committed["plan"]["planParts"][0]["totalAdjustments"]), 2)


class TimelineTests(FundedTestCase):
    def test_every_funded_segment_is_clamped_and_the_others_are_not(self):
        self.run_all(harness.with_funding())

        first, funded, last = self.famly.commits
        self.assertNotIn("publicFundingSettings", first["body"]["plan"])
        self.assertNotIn("publicFundingSettings", last["body"]["plan"])
        self.assertLessEqual(funded_hours.requested_minutes(self.hours_in(funded["body"])), 1650)

    def test_the_chain_completes_with_the_clamped_funded_plan(self):
        data = self.run_all(harness.with_funding())

        self.assertEqual(self.chain(data).status, chains.STATUS_COMPLETE)
        self.assertTrue(any("All 3 plans created" in n for n in self.notices))

    def test_the_funded_segment_message_says_so(self):
        self.famly.scheduled = 1000
        self.start(harness.with_funding())
        self.approve()

        text = self.last_text()
        self.assertIn("Funding set to scheduled hours: 16.67h (entitlement 24.5h)", text)
        self.assertEqual(self.heading(), "*Plan 2 of 3 (funded, 2026-11-01 to 2026-12-31)*")

    def test_the_clamp_does_not_touch_the_stored_chain_payload_or_the_rules(self):
        before = self.funding_file.read_bytes()
        data = self.run_all(harness.with_funding())

        self.assertEqual(self.funding_file.read_bytes(), before)
        self.assertEqual(self.chain(data).payload["fundingHours"], 15)  # HubSpot's, as sent


class QueueTests(FundedTestCase, queue_tests.QueueTestCase):
    """The queue worker previews through the same path, so it clamps too."""

    def setUp(self):
        queue_tests.QueueTestCase.setUp(self)
        self.famly = FundingFamly()

    def run_job(self, **overrides):
        data = self.queue(self.body(**overrides))
        self.run_all()
        return self.job(data)

    def test_a_clamped_plan_is_clean_and_commits_unattended(self):
        self.famly.scheduled = 1000  # the funded plan asks for 24.5h: a shortfall
        job = self.run_job()

        self.assertEqual(job.status, jobs.STATUS_DONE)
        self.assertEqual(len(self.famly.commits), 3)
        self.assertLessEqual(funded_hours.requested_minutes(self.hours_in(self.famly.commits[1]["body"])), 1650)

    def test_a_clamp_that_could_not_be_verified_holds_the_job(self):
        self.famly.scheduled = 1000  # the funded plan asks for 24.5h: a shortfall
        self.famly.amount_follows_request = True

        job = self.run_job()

        self.assertEqual(job.status, jobs.STATUS_HELD)
        self.assertIn("warning:", job.detail)
        self.assertEqual(len(self.famly.commits), 1)  # plan 1 only

    def test_an_applied_time_of_zero_holds_the_job(self):
        self.famly.scheduled = 0

        job = self.run_job()

        self.assertEqual(job.status, jobs.STATUS_HELD)
        self.assertIn("Funding shortfall", job.detail)
        self.assertEqual(len(self.famly.commits), 1)

    def test_the_held_message_keeps_famlys_warning(self):
        self.famly.scheduled = 0
        self.run_job()

        self.assertIn("Funding shortfall", self.previews_posted[-1][0])

    def test_the_informational_funding_limit_note_does_not_hold(self):
        self.famly.scheduled = 5000  # nothing short
        self.famly.extra_warnings = [LIMIT_NOTE]

        job = self.run_job()

        self.assertEqual(job.status, jobs.STATUS_DONE)

    def test_the_dry_run_applies_the_clamp_too(self):
        with mock.patch.dict("os.environ", {"QUEUE_MODE": "dry_run"}):
            job = self.run_job()

        self.assertTrue(all(s["dry_run"]["clean"] for s in job.segments))
        self.assertEqual(self.famly.commits, [])


if __name__ == "__main__":
    unittest.main()
