"""plan_estimate: the preview-only PUT and the audit's use of it. Fake ids and a fake client throughout."""

import re
import unittest
from pathlib import Path
from unittest import mock

from actions.plan_estimate import runner as pe
from actions.read_child_plans.runner import parse_plan
from tests.test_onboard_audit import AuditCase, good_plan

PLAN, CHILD, PART, SESSION = "plan-0001", "child-0001", "part-0001", "sess-0001"
TODAY = "2026-11-18"


def list_call_plan(**over):
    """A plan as the list call returns it (fields the preview body does not use are left in on purpose)."""
    node = {
        "id": PLAN, "version": 4, "childId": CHILD, "pricingGroupId": None, "ruleGroupId": "rule-1",
        "from": "2025-09-01", "to": "2027-04-01", "note": "Auto-rounded: +0.02, 2026-10-14",
        "monthlyEstimate": 869.98, "termScheduleId": "sched-1", "termTimeOnly": False, "behaviors": [{"id": "CanSavePlan"}],
        "publicFundingSettings": {"method": "ASSISTED", "hours": 24.5, "minutes": None, "grantIds": [],
                                  "fundingTypeIds": [], "maxFundedMinutes": 660},
        "planParts": [{
            "planPartId": PART, "ordering": 0, "attendanceScheduleId": "sched-1", "billingProfileId": "bp-1",
            "termTimeOnly": False, "termScheduleId": "sched-1",
            "billing": {"id": "ANNUALIZED_V2", "title": "Annualised", "weeksOfCare": 51, "invoices": 12},
            "discounts": [], "productBookings": [], "chargeRuleExemptions": [],
            "totalAdjustments": [{"pricingGroupId": "pg-1", "adjustment": -0.03}],
            "sessionBookings": [
                {"bookingId": "b1", "sessionId": SESSION, "sessionVersion": "v", "day": d, "fundable": True,
                 "start": None, "end": None, "monthlyPrices": [{"price": 440}]}
                for d in ("WEDNESDAY", "FRIDAY")],
        }],
    }
    node.update(over)
    return node


class FakeClient:
    def __init__(self, response=None):
        self.sent = []
        self.response = response if response is not None else {"plan": {"monthlyEstimate": 932.03}}

    def request(self, method, path, params=None, json_body=None):
        self.sent.append((method, path, params, json_body))
        return self.response


class BodyTests(unittest.TestCase):
    def test_body_is_the_screens_body_built_from_the_list_call(self):
        body = pe.build_preview_body(parse_plan(list_call_plan()), TODAY)
        self.assertEqual(body, {"plan": {
            "id": PLAN, "childId": CHILD, "pricingGroupId": None, "ruleGroupId": "rule-1",
            "note": "Auto-rounded: +0.02, 2026-10-14", "from": TODAY, "to": "2027-04-01",
            "planParts": [{
                "ordering": 0, "planPartId": PART, "billingProfileId": "bp-1",
                "sessionBookings": [{"sessionId": SESSION, "fundable": True, "day": d, "start": None, "end": None}
                                    for d in ("WEDNESDAY", "FRIDAY")],
                "chargeRuleExemptions": [], "productBookings": [],
                "billing": {"id": "ANNUALIZED_V2", "invoices": 12, "weeksOfCare": 51, "title": "Annualised"},
                "termTimeOnly": False, "attendanceScheduleId": "sched-1", "termScheduleId": None,
                "totalAdjustments": [{"pricingGroupId": "pg-1", "adjustment": -0.03}], "discounts": []}],
            "publicFundingSettings": {"method": "ASSISTED", "hours": 24.5, "minutes": None,
                                      "fundingTypeIds": [], "maxFundedMinutes": 660}}})

    def test_from_is_today_but_never_before_the_plans_own_start(self):
        future = parse_plan(list_call_plan(**{"from": "2027-04-01", "to": "2027-09-01"}))
        self.assertEqual(pe.build_preview_body(future, TODAY)["plan"]["from"], "2027-04-01")

    def test_a_missing_id_raises_and_nothing_is_guessed(self):
        for bad in ({"id": None}, {"childId": ""}, {"planParts": []}, {"from": None}):
            with self.subTest(bad=bad), self.assertRaises(pe.PreviewBuildError):
                pe.build_preview_body(parse_plan(list_call_plan(**bad)), TODAY)
        node = list_call_plan()
        node["planParts"][0]["billingProfileId"] = None
        with self.assertRaises(pe.PreviewBuildError):
            pe.build_preview_body(parse_plan(node), TODAY)


class ActivePlanTests(unittest.TestCase):
    def test_the_plan_covering_today_with_an_exclusive_end(self):
        plans = [parse_plan(list_call_plan(id=f"plan-{i}", **{"from": f, "to": t}))
                 for i, (f, t) in enumerate((("2023-11-02", "2025-09-01"), ("2025-09-01", "2026-11-18"),
                                             ("2026-11-18", "2027-04-01"), ("2027-04-01", "2027-09-01")))]
        self.assertEqual(pe.active_plan(plans, TODAY).id, "plan-2")      # plan-1 ends today (exclusive)
        self.assertIsNone(pe.active_plan([plans[0], plans[3]], TODAY))    # all past or all future
        open_ended = parse_plan(list_call_plan(id="plan-9", **{"from": "2026-01-01", "to": None}))
        self.assertEqual(pe.active_plan([open_ended], TODAY).id, "plan-9")


class PreviewOnlyGuardTests(unittest.TestCase):
    """The only PUT in the codebase: it can only preview."""

    def test_it_sends_one_put_with_the_fixed_preview_query_and_returns_the_estimate(self):
        client = FakeClient()
        previewer = pe.PreviewOnlyRest(client)
        value = pe.previewed_estimate(parse_plan(list_call_plan()), TODAY, previewer)
        self.assertEqual(value, 932.03)
        (method, path, params, body), = client.sent
        self.assertEqual((method, path), ("PUT", f"/v2/plans/{PLAN}"))
        self.assertEqual(params, {"preview": "true", "version": "3"})
        self.assertEqual(body["plan"]["id"], PLAN)
        self.assertEqual(previewer.calls, 1)

    def test_the_caller_cannot_choose_the_verb_path_or_query(self):
        previewer = pe.PreviewOnlyRest(FakeClient())
        self.assertEqual({n for n in dir(previewer) if not n.startswith("_")}, {"calls", "preview_put"})
        with self.assertRaises(TypeError):
            previewer.preview_put(PLAN, {"plan": {"id": PLAN}}, params={"preview": "false"})

    def test_it_refuses_a_mismatched_or_malformed_target_before_sending(self):
        client = FakeClient()
        previewer = pe.PreviewOnlyRest(client)
        for plan_id, body in ((PLAN, {"plan": {"id": "plan-other"}}), (PLAN, {}), ("a/../b", {"plan": {"id": "a/../b"}}),
                              ("plan-0001?preview=false", {"plan": {"id": "plan-0001?preview=false"}}), (None, {})):
            with self.subTest(plan_id=plan_id), self.assertRaises(pe.PreviewBuildError):
                previewer.preview_put(plan_id, body)
        self.assertEqual(client.sent, [])
        self.assertEqual(previewer.calls, 0)

    def test_a_response_without_an_estimate_raises(self):
        with self.assertRaises(pe.PreviewBuildError):
            pe.previewed_estimate(parse_plan(list_call_plan()), TODAY, pe.PreviewOnlyRest(FakeClient({"plan": {}})))

    def test_no_other_module_sends_a_put_and_the_commit_path_is_not_imported(self):
        pattern = re.compile(r"""["']PUT["']|\.put\(""")
        offenders = [str(p) for p in Path(".").rglob("*.py")
                     if not any(part in ("tests", ".git", "venv", ".venv") for part in p.parts)
                     and p.as_posix() != "actions/plan_estimate/runner.py" and pattern.search(p.read_text("utf-8"))]
        self.assertEqual(offenders, [])
        source = Path("actions/plan_estimate/runner.py").read_text("utf-8")
        for forbidden in ("commit", "approval", "plan_write", "COMMIT_ENABLED"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from actions.{forbidden}", source)
        # the audit reaches Famly REST for reads only: its wrapper still has no put/request
        from actions.onboard_audit import runner as audit
        self.assertFalse(hasattr(audit.ReadOnlyRest(mock.Mock()), "request"))


class AuditUsesThePreviewTests(AuditCase):
    def plan_codes(self, estimator=None, estimate=800):
        self.rest.plans = {"c1": [good_plan(monthlyEstimate=estimate)]}
        return self.codes(estimator=estimator)

    def test_off_by_default_the_list_estimate_is_used_and_no_new_code_appears(self):
        codes = self.plan_codes(estimate=800)
        self.assertIn("plan_fees_differ", codes)
        self.assertFalse({"plan_estimate_previewed", "plan_preview_failed"} & set(codes))

    def test_the_previewed_estimate_replaces_the_list_estimate_for_the_plan_in_force(self):
        seen = []
        codes = self.plan_codes(lambda plan, today=None: seen.append(plan.id) or 1000.4, estimate=800)
        self.assertNotIn("plan_fees_differ", codes)
        self.assertIn("plan_estimate_previewed", codes)
        self.assertIn("plan_fee_within_1_pound", codes)
        self.assertEqual(seen, ["pl1"])

    def test_a_refused_preview_falls_back_to_the_list_estimate_and_says_so(self):
        def refuse(plan, today=None):
            raise pe.PreviewBuildError("no")
        codes = self.plan_codes(refuse, estimate=800)
        self.assertIn("plan_preview_failed", codes)
        self.assertIn("plan_fees_differ", codes)                     # judged on the list figure
        self.assertNotIn("plan_estimate_previewed", codes)


if __name__ == "__main__":
    unittest.main()
