"""Integration tests for automatic rounding of a plan's monthly totals.

Run from the project root:

    python -m unittest tests.test_auto_round -v

Reuses the create_timeline harness (a stateful fake Famly, recorded Slack
posts, fixture catalogues) with a fake that prices a plan in TWO periods under
two pricing groups, so the per-group adjustments, the shared-group refusal, the
verification re-preview and the read-back can all be exercised. NOTHING here
touches Famly or Slack.
"""

import json
import logging
import unittest
from datetime import date, timedelta
from unittest import mock

try:  # `python -m unittest tests.test_auto_round`
    from tests import test_create_timeline as harness
except ImportError:  # `python -m unittest discover -s tests`
    import test_create_timeline as harness

from actions.additional_plan import web as additional_plan_web
from actions.create_timeline import chains
from actions.plan_write import approval, auto_round, preview_store, web as plan_write_web
from core.rest_client import RestHTTPError
from tests import fast_sqlite  # noqa: F401  (SQLite without fsync: see the module)

logging.disable(logging.CRITICAL)

CHILD = harness.CHILD
TODAY = date.today().isoformat()
NOTE = f"Auto-rounded: +0.97 / -0.30, {TODAY}"


class PricingFamly(harness.FakeFamly):
    """A fake Famly that prices every plan in two periods, one per group."""

    def __init__(self):
        super().__init__()
        self.estimates = [("pg-1", 269.03), ("pg-2", 1503.30)]
        self.on_adjusted_preview = None  # callable(node) for a body with adjustments
        self.drop_adjustments_on_save = False

    def states(self, plan):
        start = date.fromisoformat(plan["from"])
        starts = [start, start + timedelta(days=120)]
        return [
            {"from": s.isoformat(), "to": None, "pricingGroupId": g, "monthlyEstimate": e}
            for s, (g, e) in zip(starts, self.estimates)
        ]

    def post(self, path, params=None, json_body=None):
        node = super().post(path, params, json_body)
        plan = json_body["plan"]
        node["planStates"] = self.states(plan)
        adjustments = (plan["planParts"][0].get("totalAdjustments") or [])

        if (params or {}).get("preview") == "true":
            if adjustments and self.on_adjusted_preview:
                self.on_adjusted_preview(node)
            return node

        saved = self.plans[-1]
        saved["note"] = plan.get("note")
        saved["planStates"] = node["planStates"]
        saved["planParts"] = [
            {"totalAdjustments": [] if self.drop_adjustments_on_save else adjustments}
        ]
        return node


def a_warning():
    return [
        {
            "id": "ShowPlanWarnings",
            "payload": {
                "warnings": [
                    {"title": "Odd", "message": "x", "severity": "warning", "error": "SOMETHING"}
                ]
            },
        }
    ]


class AutoRoundTestCase(harness.ChainTestCase):
    def setUp(self):
        super().setUp()
        self.famly = PricingFamly()
        for name in ("AUTO_ROUND_DEFAULT",):
            env = mock.patch.dict("os.environ", {name: ""})
            env.start()
            self.addCleanup(env.stop)

    def flat(self, **overrides):
        body = harness.payload(**{"from": "2026-09-01", "auto_round": "true"})
        body.update(overrides)
        return body

    def preview(self, **overrides):
        return plan_write_web.handle(self.flat(**overrides))

    def stored(self):
        return preview_store.get(self.last_preview_id())

    def adjustments(self, body):
        return body["plan"]["planParts"][0].get("totalAdjustments") or []


class TwoPeriodTests(AutoRoundTestCase):
    def test_two_periods_give_two_adjustments_in_the_stored_body(self):
        data, status = self.preview()

        self.assertEqual(status, 200)
        self.assertEqual(
            self.adjustments(self.stored().plan_body),
            [
                {"pricingGroupId": "pg-1", "adjustment": 0.97},
                {"pricingGroupId": "pg-2", "adjustment": -0.30},
            ],
        )

    def test_the_note_says_what_was_done(self):
        self.preview(note="Starter")

        self.assertEqual(self.stored().plan_body["plan"]["note"], f"Starter\n{NOTE}")

    def test_the_note_alone_when_there_was_no_note(self):
        self.preview(note="")

        self.assertEqual(self.stored().plan_body["plan"]["note"], NOTE)

    def test_the_slack_summary_shows_each_period(self):
        self.preview()

        text = self.last_text()
        self.assertIn("Rounded: 269.03 -> 270.00 (from 2026-09-01)", text)
        second = (date(2026, 9, 1) + timedelta(days=120)).isoformat()
        self.assertIn(f"Rounded: 1503.30 -> 1503.00 (from {second})", text)

    def test_the_approved_plan_is_committed_with_the_adjustments(self):
        self.preview()
        self.approve()

        committed = self.famly.commits[0]["body"]
        self.assertEqual(
            [(e["pricingGroupId"], e["adjustment"]) for e in self.adjustments(committed)],
            [("pg-1", 0.97), ("pg-2", -0.30)],
        )
        self.assertIn(NOTE, committed["plan"]["note"])

    def test_it_re_previews_the_adjusted_body_once(self):
        self.preview()

        self.assertEqual(len(self.famly.previews), 2)
        self.assertFalse(self.adjustments(self.famly.previews[0]["body"]))
        self.assertTrue(self.adjustments(self.famly.previews[1]["body"]))

    def test_an_estimate_that_is_already_a_target_adds_no_entry_and_no_note(self):
        self.famly.estimates = [("pg-1", 270.00), ("pg-2", 1500.00)]

        self.preview()

        self.assertEqual(self.adjustments(self.stored().plan_body), [])
        self.assertNotIn("Auto-rounded", self.stored().plan_body["plan"].get("note", ""))
        self.assertEqual(len(self.famly.previews), 1)  # nothing to verify
        self.assertNotIn("Rounded:", self.last_text())

    def test_only_the_group_that_needs_it_gets_an_entry(self):
        self.famly.estimates = [("pg-1", 270.00), ("pg-2", 1503.30)]

        self.preview()

        self.assertEqual(
            self.adjustments(self.stored().plan_body),
            [{"pricingGroupId": "pg-2", "adjustment": -0.30}],
        )
        self.assertIn("Auto-rounded: -0.30", self.stored().plan_body["plan"]["note"])

    def test_the_adjustment_is_never_more_than_a_pound(self):
        self.famly.estimates = [("pg-1", 1501.00), ("pg-2", 1503.49)]

        self.preview()

        for entry in self.adjustments(self.stored().plan_body):
            self.assertLessEqual(abs(entry["adjustment"]), 1.00)


class SwitchTests(AutoRoundTestCase):
    def rounded(self):
        return bool(self.adjustments(self.stored().plan_body))

    def test_off_by_default(self):
        self.preview(auto_round="")
        self.assertFalse(self.rounded())
        self.assertEqual(len(self.famly.previews), 1)

    def test_the_payload_turns_it_on(self):
        self.preview(auto_round="true")
        self.assertTrue(self.rounded())

    def test_the_env_default_turns_it_on(self):
        with mock.patch.dict("os.environ", {"AUTO_ROUND_DEFAULT": "true"}):
            self.preview(auto_round="")
        self.assertTrue(self.rounded())

    def test_the_payload_wins_over_the_env_default(self):
        with mock.patch.dict("os.environ", {"AUTO_ROUND_DEFAULT": "true"}):
            self.preview(auto_round="false")
        self.assertFalse(self.rounded())

        with mock.patch.dict("os.environ", {"AUTO_ROUND_DEFAULT": "false"}):
            self.preview(auto_round="true")
        self.assertTrue(self.rounded())

    def test_a_wrapped_payload_is_read_too(self):
        plan_write_web.handle({"properties": self.flat(auto_round="true")})
        self.assertTrue(self.rounded())

    def test_enabled_directly(self):
        self.assertTrue(auto_round.enabled({"auto_round": True}))
        self.assertTrue(auto_round.enabled({"auto_round": "yes"}))
        self.assertFalse(auto_round.enabled({"auto_round": "no"}))
        self.assertFalse(auto_round.enabled({}))
        self.assertFalse(auto_round.enabled(None))


class SharedGroupTests(AutoRoundTestCase):
    def setUp(self):
        super().setUp()
        self.famly.estimates = [("pg-1", 269.03), ("pg-1", 275.10)]

    def test_a_shared_group_with_different_estimates_is_not_rounded(self):
        data, status = self.preview()

        self.assertEqual(status, 200)  # the plan carries on, unrounded
        self.assertEqual(self.adjustments(self.stored().plan_body), [])
        self.assertNotIn("Auto-rounded", self.stored().plan_body["plan"].get("note", ""))
        self.assertEqual(len(self.famly.previews), 1)

    def test_the_reason_is_recorded_where_the_approver_reads_it(self):
        data, _ = self.preview()

        self.assertTrue(
            any("Not auto-rounded" in json.dumps(w) and "pg-1" in json.dumps(w) for w in data["warnings"]),
            data["warnings"],
        )
        self.assertIn("Not auto-rounded: pricing group pg-1", self.last_text())

    def test_a_shared_group_with_the_same_estimate_is_still_rounded(self):
        self.famly.estimates = [("pg-1", 269.03), ("pg-1", 269.03)]

        self.preview()

        self.assertEqual(
            self.adjustments(self.stored().plan_body),
            [{"pricingGroupId": "pg-1", "adjustment": 0.97}],
        )


class VerificationTests(AutoRoundTestCase):
    def assert_unrounded_with_a_notice(self, fragment):
        data, status = self.preview()

        self.assertEqual(status, 200)
        self.assertEqual(self.adjustments(self.stored().plan_body), [])
        self.assertNotIn("Auto-rounded", self.stored().plan_body["plan"].get("note", ""))
        self.assertNotIn("Rounded:", self.last_text())
        self.assertTrue(
            any("discarded" in n and fragment in n for n in self.notices), self.notices
        )
        self.assertEqual(len(self.famly.previews), 2)  # preview + one verification

    def test_a_moved_estimate_discards_the_rounding(self):
        def move(node):
            node["planStates"][0]["monthlyEstimate"] += 5

        self.famly.on_adjusted_preview = move

        self.assert_unrounded_with_a_notice("moved")

    def test_a_drift_of_a_penny_is_tolerated(self):
        def nudge(node):
            node["planStates"][0]["monthlyEstimate"] += 0.01

        self.famly.on_adjusted_preview = nudge

        self.preview()
        self.assertTrue(self.adjustments(self.stored().plan_body))

    def test_a_new_warning_discards_the_rounding(self):
        def warn(node):
            node["behaviors"] = a_warning()

        self.famly.on_adjusted_preview = warn

        self.assert_unrounded_with_a_notice("new warning")

    def test_a_different_number_of_periods_discards_the_rounding(self):
        def lose_one(node):
            node["planStates"] = node["planStates"][:1]

        self.famly.on_adjusted_preview = lose_one

        self.assert_unrounded_with_a_notice("period")

    def test_a_failed_verification_discards_the_rounding(self):
        def fail(node):
            raise RestHTTPError("Famly down", 503, "")

        self.famly.on_adjusted_preview = fail

        self.assert_unrounded_with_a_notice("verification preview failed")

    def test_a_warning_the_first_preview_already_had_is_not_new(self):
        real_post = self.famly.post

        def post(path, params=None, json_body=None):
            node = real_post(path, params, json_body)
            node["behaviors"] = a_warning()
            return node

        with mock.patch.object(self.famly, "post", post):
            self.preview()

        self.assertTrue(self.adjustments(self.stored().plan_body))

    def test_the_unrounded_plan_still_commits_normally(self):
        self.famly.on_adjusted_preview = lambda node: node["planStates"][0].update(monthlyEstimate=1)

        self.preview()
        text = self.approve()

        self.assertIn("committed to Famly", text)
        self.assertEqual(self.adjustments(self.famly.commits[0]["body"]), [])


class ManualAdjustmentTests(AutoRoundTestCase):
    """A manual Adjust & Approve takes over from the automatic entry."""

    def setUp(self):
        super().setUp()
        # Today falls in the FIRST period, so the manual adjustment is for pg-1.
        self.start_date = (date.today() - timedelta(days=30)).isoformat()

    def adjust(self, value="0.50"):
        self.preview(**{"from": self.start_date})
        original = self.last_preview_id()
        approval.handle_adjust_submission(original, value, "drew", spawn=lambda w: w())
        return preview_store.get(self.adjusted_posted[-1][1])

    def test_it_replaces_the_auto_entry_for_that_group_only(self):
        adjusted = self.adjust("0.50")

        entries = self.adjustments(adjusted.plan_body)
        self.assertEqual(
            sorted((e["pricingGroupId"], e["adjustment"]) for e in entries),
            [("pg-1", 0.50), ("pg-2", -0.30)],
        )

    def test_a_group_never_has_two_entries(self):
        entries = self.adjustments(self.adjust("0.50").plan_body)

        groups = [e["pricingGroupId"] for e in entries]
        self.assertEqual(len(groups), len(set(groups)))

    def test_the_note_is_rewritten(self):
        note = self.adjust("0.50").plan_body["plan"]["note"]

        self.assertIn(f"Auto-rounded: -0.30, {TODAY}", note)
        self.assertNotIn("+0.97", note)
        self.assertIn("Adjustment: £+0.50", note)

    def test_when_every_group_is_taken_over_the_auto_line_goes(self):
        self.famly.estimates = [("pg-1", 269.03), ("pg-2", 300.00)]  # only pg-1 rounds

        note = self.adjust("0.50").plan_body["plan"]["note"]

        self.assertNotIn("Auto-rounded", note)
        self.assertIn("Adjustment: £+0.50", note)

    def test_the_adjusted_plan_commits_with_the_manual_entry(self):
        adjusted_id = self.adjust("0.50").preview_id
        self.approve(adjusted_id)

        entries = self.adjustments(self.famly.commits[0]["body"])
        self.assertEqual(sorted(e["adjustment"] for e in entries), [-0.30, 0.50])

    def test_a_plain_manual_adjustment_is_unchanged(self):
        self.preview(**{"from": self.start_date, "auto_round": "false"})
        approval.handle_adjust_submission(
            self.last_preview_id(), "0.25", "drew", spawn=lambda w: w()
        )

        adjusted = preview_store.get(self.adjusted_posted[-1][1])
        self.assertEqual(
            self.adjustments(adjusted.plan_body),
            [{"pricingGroupId": "pg-1", "adjustment": 0.25}],
        )
        self.assertEqual(adjusted.plan_body["plan"]["note"].count("Adjustment:"), 1)


class AdditionalPlanTests(AutoRoundTestCase):
    def test_it_applies_to_additional_plan_with_the_old_plan_id(self):
        self.famly.plans.append(
            {"id": "existing-1", "childId": CHILD, "from": "2025-09-01", "to": None}
        )

        data, status = additional_plan_web.handle(self.flat(), sleeper=lambda s: None)

        self.assertEqual(status, 200)
        self.assertEqual(len(self.adjustments(self.stored().plan_body)), 2)
        self.assertEqual(self.stored().old_plan_id, "existing-1")
        # the verification preview names the old plan too
        self.assertEqual(
            [c["params"].get("oldPlanId") for c in self.famly.previews],
            ["existing-1", "existing-1"],
        )


class TimelineTests(AutoRoundTestCase):
    def chain_body(self):
        return harness.with_funding(auto_round="true")

    def test_every_segment_is_rounded(self):
        self.run_all(self.chain_body())

        self.assertEqual(len(self.famly.commits), 3)
        for call in self.famly.commits:
            self.assertEqual(len(self.adjustments(call["body"])), 2)
            self.assertIn("Auto-rounded", call["body"]["plan"]["note"])

    def test_the_headings_and_old_plan_ids_are_unchanged(self):
        self.start(self.chain_body())
        self.assertEqual(self.heading(), "*Plan 1 of 3 (base, 2026-09-01 to 2026-10-31)*")
        self.approve()
        self.assertEqual(self.heading(), "*Plan 2 of 3 (funded, 2026-11-01 to 2026-12-31)*")
        self.assertEqual(self.famly.previews[-1]["params"].get("oldPlanId"), "plan-1")

    def test_the_read_back_passes_quietly_when_the_plan_saved_as_intended(self):
        self.run_all(self.chain_body())

        self.assertFalse(any("does not add up" in n for n in self.notices))
        self.assertTrue(any("All 3 plans created" in n for n in self.notices))

    def test_a_plan_saved_without_its_adjustments_warns_but_does_not_stop_the_chain(self):
        self.famly.drop_adjustments_on_save = True

        data = self.run_all(self.chain_body())

        warnings = [n for n in self.notices if "does not add up" in n]
        self.assertEqual(len(warnings), 3)  # one per plan, each naming the periods
        self.assertIn("expected 270.00", warnings[0])
        self.assertEqual(self.chain(data).status, chains.STATUS_COMPLETE)
        self.assertTrue(any("All 3 plans created" in n for n in self.notices))

    def test_a_chain_without_auto_round_is_untouched(self):
        self.run_all(harness.with_funding())

        for call in self.famly.commits:
            self.assertEqual(self.adjustments(call["body"]), [])
        self.assertEqual(len(self.famly.previews), 6)  # two per plan: preview + commit's

    def test_auto_round_survives_the_stored_payload_for_later_segments(self):
        data, _ = self.start(self.chain_body())

        self.assertEqual(self.chain(data).payload["auto_round"], "true")
        self.assertIn("auto_round", harness.engine.ALLOWED_FIELDS)

    def test_a_shared_group_in_one_segment_continues_that_plan_unrounded(self):
        self.famly.estimates = [("pg-1", 269.03), ("pg-1", 275.10)]

        self.run_all(self.chain_body())

        self.assertEqual(len(self.famly.commits), 3)
        for call in self.famly.commits:
            self.assertEqual(self.adjustments(call["body"]), [])


if __name__ == "__main__":
    unittest.main()
