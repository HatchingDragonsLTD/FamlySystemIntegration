"""onboard-audit: codes, found_via order, child-only stages, no values in any output, and the
read-only guards. All data is built here from fake values."""

import copy
import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from actions.onboard_audit import runner
from core.hubspot import HubSpotClient
from tests.test_onboard_phase2 import DEAL, GROUP_ID, SITE_ID, Phase2Case

SECOND = "2002"
URN = "URN-1"


def famly_child(child_id="c1", **over):
    child = {
        "id": child_id, "externalId": URN, "birthday": "2024-01-15",
        "name": {"firstName": "Zara", "lastName": "Quillfeather", "fullName": "Zara Quillfeather"},
        "currentGroup": {"id": GROUP_ID},
        "sitesRelation": [{"firstDay": "2026-01-15", "lastDay": "2028-08-31", "site": {"siteId": SITE_ID}}],
        "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"},
                                      {"foreignId": URN, "system": "URN"}],
        "contacts": [
            {"id": "k1", "email": "ada.l@example.org", "emergencyContact": True,
             "records": [{"key": "RELATION", "value": "Parent"}]},
            {"id": "k2", "email": "ben.b@example.org", "emergencyContact": False,
             "records": [{"key": "RELATION", "value": "Parent"}]},
        ],
    }
    child.update(over)
    return child


def payer(payer_id="p1", email="ada.l@example.org", children=None, recipients=("k1",)):
    """A live payer; by default the bill payer's Famly contact (k1) is an invoice recipient of it."""
    return {"billPayerId": payer_id, "email": email, "deletedAt": None,
            "children": [{"childId": c, "share": {"multiplier": m}} for c, m in (children if children is not None else {"c1": 1.0}).items()],
            "invoiceRecipients": [{"relationId": r, "contactId": r} for r in recipients]}


class FakeGql:
    def __init__(self):
        self.children = [famly_child()]
        self.payers = [payer()]
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        if "mutation" in text.lower():
            raise AssertionError("a mutation reached the fake")
        self.calls.append(operation_name)
        if operation_name == "AuditSiteChildren":
            return {"data": {"children": {"listBySiteIds": {"result": copy.deepcopy(self.children), "next": None}}}}
        if operation_name == "OnboardBillPayers":
            return {"data": {"billPayers": {"listBySiteIds": {"result": copy.deepcopy(self.payers), "next": None}}}}
        raise AssertionError(f"unexpected operation {operation_name}")


def good_plan(**over):
    plan = {"id": "pl1", "from": "2026-01-15", "to": None, "monthlyEstimate": 1050,
            "planParts": [{"sessionBookings": [{"sessionId": "s-m", "day": "MONDAY"},
                                               {"sessionId": "s-f", "day": "TUESDAY"}]}]}
    plan.update(over)
    return plan


class FakeRest:
    def __init__(self):
        self.plans = {"c1": [good_plan()]}
        self.paths = []

    def get(self, path, params=None):
        self.paths.append(path)
        return {"plans": copy.deepcopy(self.plans.get(params["childId"], []))}


class AuditCase(Phase2Case):
    def setUp(self):
        super().setUp()
        self.hub.stages["s-left"] = ("Operations", "Left Nursery")
        self.gql, self.rest = FakeGql(), FakeRest()
        import os
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name, "sessions.json")
        path.write_text(json.dumps({"institutions": {"HDWEST": {
            "non_funded": {"morning": "s-m", "afternoon": "s-a", "full_day": "s-f"},
            "funded": {"neither": {"morning": "f-m", "afternoon": "f-a", "full_day": "f-f"}}}}}), encoding="utf-8")
        os.environ["SESSION_CATALOGUE_FILE"] = str(path)
        self.addCleanup(os.environ.pop, "SESSION_CATALOGUE_FILE", None)
        self.hub.deals[DEAL].update(monday="morning", tuesday="Full Day", billable_fees="1000")
        self.hub.calls_made = 0

    def audit(self, **kwargs):
        kwargs.setdefault("deal_ids", [DEAL])
        return runner.run(self.hub, self.gql, self.rest, site_ids=[SITE_ID], **kwargs)

    def codes(self, **kwargs):
        _summary, rows = self.audit(**kwargs)
        return rows[0].codes

    def with_child(self, **over):
        self.gql.children = [famly_child(**over)]


class RecipientAndRelationCodeTests(AuditCase):
    def test_a_recipient_contact_is_present_and_it_costs_no_new_famly_call(self):
        codes = self.codes()
        self.assertIn("bill_payer_recipient_present", codes)
        self.assertNotIn("bill_payer_recipient_missing", codes)
        self.assertEqual(set(self.gql.calls), {"AuditSiteChildren", "OnboardBillPayers"})   # the scan already reads it

    def test_a_payer_without_the_contact_as_recipient_is_missing(self):
        self.gql.payers = [payer(recipients=())]
        self.assertIn("bill_payer_recipient_missing", self.codes())
        self.gql.payers = [payer(recipients=("k2",))]                 # another parent is a recipient, not the payer's
        self.assertIn("bill_payer_recipient_missing", self.codes())

    def test_no_recipient_code_without_a_payer_or_when_the_payer_does_not_list_the_child(self):
        for payers in ([], [payer(children={})]):
            self.gql.payers = payers
            codes = self.codes()
            self.assertFalse([c for c in codes if c.startswith("bill_payer_recipient_")], payers)
        self.gql.payers = []
        self.assertIn("bill_payer_missing", self.codes())             # the payer codes already say it

    def test_a_blank_relation_gets_the_empty_sub_code_and_an_absent_one_does_not(self):
        blank = famly_child()
        blank["contacts"][1]["records"] = [{"key": "RELATION", "value": ""}]
        self.gql.children = [blank]
        codes = self.codes()
        self.assertEqual((codes.count("contact_relation_missing"), codes.count("contact_relation_empty")), (1, 1))
        absent = famly_child()
        absent["contacts"][1]["records"] = []
        self.gql.children = [absent]
        codes = self.codes()
        self.assertIn("contact_relation_missing", codes)
        self.assertNotIn("contact_relation_empty", codes)
        self.gql.children = [famly_child()]
        self.assertNotIn("contact_relation_missing", self.codes())

    def test_the_new_codes_are_ids_and_codes_only_and_the_quality_check_tiers_them(self):
        from actions.onboard_quality import config
        self.assertEqual(config.tier("bill_payer_recipient_missing"), config.ALERT)
        self.assertEqual(config.tier("bill_payer_recipient_present"), config.IGNORE)
        self.assertEqual(config.tier("contact_relation_empty"), config.IGNORE)
        self.assertEqual(config.tier("contact_relation_missing"), config.ALERT)


class CodeTests(AuditCase):
    def test_a_matching_child_has_no_codes(self):
        summary, rows = self.audit()
        self.assertEqual(rows[0].codes, ["bill_payer_recipient_present", "plan_matches", "plan_fee_under_10_percent"])
        self.assertEqual((rows[0].found_via, rows[0].child_id, rows[0].site_code, rows[0].stage),
                         ("deal_reference", "c1", "HDWEST", "Deposit Pending"))
        self.assertEqual(summary["childrenFoundBy"], {"deal_reference": 1})

    def test_each_child_code(self):
        deal = self.hub.deals[DEAL]
        cases = {
            "child_name_differs": lambda: self.with_child(name={"firstName": "A", "lastName": "B", "fullName": "A B"}),
            "child_dob_differs": lambda: self.with_child(birthday="2023-02-02"),
            "child_group_differs": lambda: self.with_child(currentGroup={"id": "other"}),
            "child_start_differs": lambda: self.with_child(sitesRelation=[
                {"firstDay": "2026-02-02", "lastDay": "2028-08-31", "site": {"siteId": SITE_ID}}]),
            "child_end_differs": lambda: self.with_child(sitesRelation=[
                {"firstDay": "2026-01-15", "lastDay": None, "site": {"siteId": SITE_ID}}]),
            "child_urn_differs": lambda: self.with_child(externalId="OTHER-9"),
            "child_urn_missing": lambda: self.with_child(externalId=""),
            "child_wrong_site": lambda: self.with_child(sitesRelation=[
                {"firstDay": "2026-01-15", "lastDay": None, "site": {"siteId": "elsewhere"}}]),
        }
        for code, setup in cases.items():
            with self.subTest(code=code):
                self.gql.children = [famly_child()]
                setup()
                self.assertIn(code, self.codes())
        self.with_child()
        deal["child_urn"] = ""
        self.assertIn("deal_urn_missing", self.codes())
        deal["child_urn"] = URN
        deal["child_first_name"] = deal["child_last_name"] = ""
        self.assertIn("child_name_not_comparable", self.codes())

    def test_no_hubspot_reference_when_found_by_the_recorded_child_id(self):
        self.with_child(externalSystemsReferences=[{"foreignId": URN, "system": "URN"}])
        self.hub.deals[DEAL]["famly_childid"] = "c1"
        _s, rows = self.audit()
        self.assertEqual(rows[0].found_via, "famly_childid")
        self.assertIn("no_hubspot_reference", rows[0].codes)

    def test_each_contact_code(self):
        self.with_child(contacts=[famly_child()["contacts"][0]])                       # ben is missing
        self.assertIn("contact_missing", self.codes())
        self.with_child(contacts=[])                                                    # the bill payer too
        self.assertIn("bill_payer_contact_missing", self.codes())
        self.with_child(contacts=[
            {"id": "k1", "email": "ada.l@example.org", "emergencyContact": False, "records": []},
            famly_child()["contacts"][1]])
        codes = self.codes()
        self.assertIn("contact_relation_missing", codes)
        self.assertIn("bill_payer_not_emergency", codes)
        self.with_child()
        self.hub.contacts["c2"]["email"] = ""
        self.assertIn("contact_no_email", self.codes())

    def test_each_bill_payer_code(self):
        self.with_child()
        cases = {
            "bill_payer_missing": [],
            "bill_payer_not_linked": [payer(children={})],
            "bill_payer_share_not_full": [payer(children={"c1": 0.5})],
            "bill_payer_ambiguous": [payer("p1"), payer("p2")],
            "double_billing_risk": [payer("p9", email="someone.else@example.org")],
        }
        for code, payers in cases.items():
            with self.subTest(code=code):
                self.gql.payers = payers
                self.assertIn(code, self.codes())

    def test_plan_missing_and_overlap_come_from_the_existing_plans(self):
        self.rest.plans = {"c1": []}
        self.assertEqual(self.codes(), ["bill_payer_recipient_present", "plan_missing"])
        self.rest.plans = {"c1": [good_plan(to="2026-09-01"), good_plan(id="b", **{"from": "2026-06-01"})]}
        self.assertIn("plan_overlap_or_duplicate", self.codes())
        self.rest.plans = {"c1": [good_plan(to="2026-09-01"), good_plan(id="b", **{"from": "2026-09-01"})]}
        self.assertNotIn("plan_overlap_or_duplicate", self.codes())      # `to` is exclusive

    def test_a_plan_that_matches_the_deal(self):
        self.assertEqual(self.codes(), ["bill_payer_recipient_present", "plan_matches", "plan_fee_under_10_percent"])

    def test_each_plan_difference(self):
        cases = {
            "plan_dates_differ": good_plan(**{"from": "2026-02-02"}),
            "plan_sessions_differ": good_plan(planParts=[{"sessionBookings": [
                {"sessionId": "s-a", "day": "MONDAY"}, {"sessionId": "s-f", "day": "TUESDAY"}]}]),
            "plan_fees_differ": good_plan(monthlyEstimate=2000),
        }
        for code, plan in cases.items():
            with self.subTest(code=code):
                self.rest.plans = {"c1": [plan]}
                codes = self.codes()
                self.assertIn("plan_differs", codes)
                self.assertIn(code, codes)
        self.rest.plans = {"c1": [good_plan(to="2026-12-31")]}             # the deal has no end date
        self.assertIn("plan_dates_differ", self.codes())
        self.hub.deals[DEAL]["end_date"] = "2026-12-31"
        self.rest.plans = {"c1": [good_plan(to="2027-01-01")]}
        self.assertNotIn("plan_dates_differ", self.codes())
        self.rest.plans = {"c1": [good_plan(monthlyEstimate=1090)]}        # within 10% of billable_fees
        self.assertNotIn("plan_fees_differ", self.codes())

    def test_not_derivable_names_the_missing_input_class(self):
        deal = self.hub.deals[DEAL]
        for key, code in (("monday", None), ("billable_fees", "plan_input_fees_missing"),
                          ("actual_start_date_1", "plan_input_dates_missing")):
            with self.subTest(missing=key):
                before = deal[key]
                deal[key] = ""
                if key == "monday":
                    deal["tuesday"] = ""
                    code = "plan_input_sessions_missing"
                codes = self.codes()
                self.assertIn(code, codes)
                self.assertTrue("plan_not_derivable" in codes or "plan_differs" in codes)
                deal[key] = before
                deal["tuesday"] = "Full Day"
        deal["monday"] = "whenever"
        self.assertIn("plan_input_sessions_missing", self.codes())

    def test_a_site_missing_from_the_session_catalogue_is_a_gap_not_a_difference(self):
        import os
        Path(os.environ["SESSION_CATALOGUE_FILE"]).write_text('{"institutions": {}}', encoding="utf-8")
        self.assertIn("plan_session_catalogue_gap", self.codes())

    def test_a_deal_with_no_child_is_counted_and_not_audited_further(self):
        self.gql.children = []
        summary, rows = self.audit()
        self.assertEqual(rows[0].codes, ["child_missing"])
        self.assertEqual((summary["childMissing"], rows[0].found_via), (1, "not_found"))
        self.assertEqual(self.rest.paths, [])                                           # no plan read

    def test_two_children_with_the_deal_reference_are_ambiguous(self):
        self.gql.children = [famly_child("c1"), famly_child("c2")]
        self.assertEqual(self.codes(), ["child_ambiguous"])


class SubCodeTests(AuditCase):
    """HOW a value differs, as codes. The values themselves never leave the comparison."""

    def child_with(self, **over):
        self.gql.children = [famly_child(**over)]

    def test_dob_sub_codes(self):
        deal = self.hub.deals[DEAL]
        deal["child_date_of_birth"] = "2024-03-07"
        cases = {"2024-07-03": "dob_day_month_swapped", "2023-03-07": "dob_year_differs",
                 "2024-04-07": "dob_month_differs", "2024-03-09": "dob_day_differs",
                 "2022-05-01": "dob_other"}
        for famly, code in cases.items():
            with self.subTest(code=code):
                self.child_with(birthday=famly)
                codes = self.codes()
                self.assertIn("child_dob_differs", codes)
                self.assertIn(code, codes)

    def test_name_sub_codes(self):
        deal = self.hub.deals[DEAL]
        cases = [("Zara", "Quillfeather", "ZaraQuillfeather", "name_case_or_spacing_only"),
                 ("Zara", "Quill-feather", "Zara Quillfeather", "name_punctuation_only"),
                 ("Zara", "Quillfeather", "Zara Mae Quillfeather", "name_one_contains_the_other"),
                 ("Zara", "Quillfeather", "Someone Else", "name_different")]
        for first, last, famly, code in cases:
            with self.subTest(code=code):
                deal["child_first_name"], deal["child_last_name"] = first, last
                self.child_with(name={"firstName": "x", "lastName": "y", "fullName": famly})
                codes = self.codes()
                self.assertIn("child_name_differs", codes)
                self.assertIn(code, codes)

    def test_start_and_end_gap_buckets_and_direction(self):
        def rel(first, last):
            return [{"firstDay": first, "lastDay": last, "site": {"siteId": SITE_ID}}]
        cases = [("2026-01-20", "2028-08-31", "child_start_famly_later_1_to_7_days"),
                 ("2026-01-01", "2028-08-31", "child_start_famly_earlier_8_to_31_days"),
                 ("2025-01-01", "2028-08-31", "child_start_famly_earlier_over_31_days"),
                 ("2026-01-15", "2028-09-02", "child_end_famly_later_1_to_7_days"),
                 ("2026-01-15", "2028-08-01", "child_end_famly_earlier_8_to_31_days"),
                 ("2026-01-15", "2029-12-31", "child_end_famly_later_over_31_days"),
                 ("2026-01-15", None, "child_end_famly_open_ended")]
        for first, last, code in cases:
            with self.subTest(code=code):
                self.child_with(sitesRelation=rel(first, last))
                self.assertIn(code, self.codes())

    def test_contact_missing_sub_codes(self):
        self.child_with(contacts=[])
        self.assertIn("child_has_no_contacts", self.codes())
        self.child_with(contacts=[{"id": "k9", "email": "someone.else@example.org", "emergencyContact": False,
                                   "records": []}])
        codes = self.codes()
        self.assertIn("child_has_contacts_but_email_not_matched", codes)
        self.assertNotIn("email_matches_a_contact_on_a_different_child_of_the_same_family", codes)
        sibling = famly_child("c2", externalSystemsReferences=[], externalId="X-2")
        self.gql.children = [famly_child(contacts=[]), sibling]            # ben and ada are on the sibling
        self.assertIn("email_matches_a_contact_on_a_different_child_of_the_same_family", self.codes())

    def test_bill_payer_sub_codes(self):
        self.child_with()
        cases = {
            "bill_payer_share_zero": [payer(children={"c1": 0.0})],
            "bill_payer_share_partial": [payer(children={"c1": 0.5})],
            "bill_payer_share_row_missing": [payer(children={})],
            "bill_payer_child_has_no_live_payer": [payer(children={"c1": 0.0})],
            "bill_payer_child_has_multiple_live_payers": [payer("p1"), payer("p2", email="other@example.org",
                                                                             children={"c1": 0.5})],
            "bill_payer_other_payer_billing": [payer("p9", email="someone.else@example.org")],
        }
        for code, payers in cases.items():
            with self.subTest(code=code):
                self.gql.payers = payers
                self.assertIn(code, self.codes())

    def test_double_billing_split(self):
        self.child_with()
        cases = {
            "other_payer_full_and_expected_zero": [payer("p9", email="someone.else@example.org")],
            "both_full": [payer("p1"), payer("p2", email="other@example.org")],
            "other": [payer("p1"), payer("p2", email="other@example.org", children={"c1": 0.5})],
        }
        for code, payers in cases.items():
            with self.subTest(code=code):
                self.gql.payers = payers
                codes = self.codes()
                self.assertIn("double_billing_risk", codes)
                self.assertIn(code, codes)
                self.assertEqual(len([c for c in codes if c in cases]), 1)


class PlanBucketTests(AuditCase):
    def test_fee_buckets_are_counted_next_to_the_tolerance(self):
        for estimate, bucket in ((1000.5, "within_1_pound"), (1015, "under_2_percent"),
                                 (1080, "under_10_percent"), (1200, "over_10_percent")):
            with self.subTest(bucket=bucket):
                self.rest.plans = {"c1": [good_plan(monthlyEstimate=estimate)]}
                summary, rows = self.audit()
                self.assertIn(f"plan_fee_{bucket}", rows[0].codes)
                self.assertEqual(summary["feeBuckets"], {bucket: 1})
        self.assertEqual(runner.FEE_TOLERANCE, 0.10)
        self.assertIn("plan_fees_differ", rows[0].codes)              # only the over 10 percent one


class LeftNurseryPlanTests(AuditCase):
    def setUp(self):
        super().setUp()
        self.hub.deals[DEAL]["dealstage"] = "s-left"

    def test_plans_of_a_child_only_stage_are_skipped_unless_include_left(self):
        self.rest.plans = {"c1": []}
        _s, rows = self.audit()
        self.assertEqual([c for c in rows[0].codes if c.startswith("plan_")], [])
        self.assertEqual(self.rest.paths, [])                           # not even read
        _s, rows = self.audit(include_left=True)
        self.assertIn("plan_missing", rows[0].codes)
        self.assertEqual(len(self.rest.paths), 1)

    def test_a_normal_stage_is_unchanged_by_the_flag(self):
        self.hub.deals[DEAL]["dealstage"] = "s-pending"
        self.rest.plans = {"c1": []}
        self.assertIn("plan_missing", self.codes())


class SummaryAndCsvTests(AuditCase):
    def two_deals(self):
        from tests.test_onboard_dry_run import make_deal
        self.hub.deals[SECOND] = make_deal(dealstage="s-left", child_urn="URN-2")
        self.hub.assoc[("deals", SECOND, "contacts")] = []
        self.gql.children = [famly_child(birthday="2023-02-02")]
        self.rest.plans = {"c1": []}

    def test_codes_by_stage_in_the_summary(self):
        self.two_deals()
        summary, _rows = self.audit(deal_ids=[DEAL, SECOND])
        self.assertEqual(summary["byCodeByStage"]["child_dob_differs"], {"Deposit Pending": 1})
        self.assertEqual(summary["byCodeByStage"]["child_missing"], {"Left Nursery": 1})
        self.assertEqual(summary["byCode"]["plan_missing"], 1)

    def test_codes_restricts_the_csv_to_rows_with_any_of_them(self):
        self.two_deals()
        _s, rows = self.audit(deal_ids=[DEAL, SECOND])
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "o.csv"))
            self.assertEqual(runner.write_csv(path, rows, only_codes=["dob_other", "nonsense"]), 1)
            with open(path, encoding="utf-8") as handle:
                body = list(csv.reader(handle))
            self.assertEqual([r[0] for r in body[1:]], [DEAL])
            self.assertEqual(runner.write_csv(path, rows, only_codes=None), 2)
            self.assertEqual(runner.write_csv(path, rows, only_codes=["child_missing"]), 1)


class FoundViaTests(AuditCase):
    def test_deal_reference_then_recorded_child_id_then_urn(self):
        by_ref = famly_child("c1")
        by_id = famly_child("c2", externalSystemsReferences=[], externalId="X-2")
        by_urn = famly_child("c3", externalSystemsReferences=[], externalId=URN)
        self.hub.deals[DEAL]["famly_childid"] = "c2"
        self.gql.children = [by_urn, by_id, by_ref]
        self.assertEqual(self.audit()[1][0].found_via, "deal_reference")
        self.assertEqual(self.audit()[1][0].child_id, "c1")
        self.gql.children = [by_urn, by_id]
        _s, rows = self.audit()
        self.assertEqual((rows[0].found_via, rows[0].child_id), ("famly_childid", "c2"))
        self.hub.deals[DEAL]["famly_childid"] = ""
        _s, rows = self.audit()
        self.assertEqual((rows[0].found_via, rows[0].child_id), ("urn", "c3"))


class ChildOnlyTests(AuditCase):
    def test_a_child_only_stage_skips_contacts_and_the_bill_payer(self):
        self.hub.deals[DEAL]["dealstage"] = "s-left"
        self.with_child(contacts=[])
        self.gql.payers = []

        def no_contacts(*a, **k):
            raise AssertionError("a contact was read")
        self.hub.get_contact = no_contacts
        codes = self.codes()
        self.assertFalse([c for c in codes if c.startswith(("contact_", "bill_payer"))], codes)
        self.assertNotIn("OnboardBillPayers", self.gql.calls)


class OperationsTests(AuditCase):
    def test_operations_filters_by_stage_and_makes_one_search(self):
        from tests.test_onboard_dry_run import make_deal
        self.hub.deals[SECOND] = make_deal(dealstage="s-left")
        self.gql.children = []
        summary, rows = self.audit(deal_ids=None, operations=True)
        self.assertEqual(sorted(r.deal_id for r in rows), sorted([DEAL, SECOND]))
        _s, rows = self.audit(deal_ids=None, operations=True, stage_names=["left nursery"])
        self.assertEqual([r.deal_id for r in rows], [SECOND])
        self.assertEqual(rows[0].stage, "Left Nursery")


class NoValuesTests(AuditCase):
    def test_the_summary_and_the_csv_hold_ids_and_codes_only(self):
        self.with_child(name={"firstName": "A", "lastName": "B", "fullName": "A B"},
                        birthday="2023-02-02", contacts=[])
        summary, rows = self.audit()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "out.csv"))
            runner.write_csv(path, rows)
            text = Path(path).read_text(encoding="utf-8")
            parsed = list(csv.reader(io.StringIO(text)))
        blob = json.dumps(summary) + text
        for value in ("Zara", "Quillfeather", "example.org", "2024-01-15", "2023-02-02", "ada", "Mill Lane"):
            self.assertNotIn(value, blob)
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", blob))                  # no date of any kind
        self.assertIsNone(re.search(r"@|\d{6,}", blob))                            # no email, no phone-like run
        self.assertEqual(parsed[0], ["dealId", "siteCode", "stage", "childId", "found_via", "codes"])
        self.assertEqual(parsed[1][:5], [DEAL, "HDWEST", "Deposit Pending", "c1", "deal_reference"])
        self.assertIn("child_name_differs", parsed[1][5])


class ReadOnlyGuardTests(unittest.TestCase):
    def test_hubspot_refuses_anything_but_get_and_the_deal_search(self):
        client = runner.ReadOnlyHubSpot("tok")
        for method, path in (("PATCH", "/crm/v3/objects/deals/1"), ("POST", "/crm/v3/objects/contacts"),
                             ("PUT", "/x"), ("DELETE", "/x"), ("POST", "/crm/v3/objects/deals")):
            with self.subTest(method=method, path=path), self.assertRaises(runner.NotReadOnly):
                client._call(method, path, json_body={})
        self.assertEqual(client.calls, 0)
        self.assertFalse(hasattr(HubSpotClient, "write_deal_properties"))

    def test_hubspot_allows_get_and_the_search_post(self):
        class Session:
            def __init__(self):
                self.sent = []

            def _response(self):
                class R:
                    status_code = 200
                    headers = {}

                    @staticmethod
                    def json():
                        return {"results": []}
                return R()

            def get(self, url, **kw):
                self.sent.append(("GET", url))
                return self._response()

            def post(self, url, **kw):
                self.sent.append(("POST", url))
                return self._response()
        session = Session()
        client = runner.ReadOnlyHubSpot("tok", session=session)
        client.get_deal("1")
        client.search_deals([{"propertyName": "dealstage", "operator": "IN", "values": ["a"]}], ["dealname"])
        self.assertEqual([m for m, _ in session.sent], ["GET", "POST"])
        self.assertEqual(client.calls, 2)

    def test_graphql_refuses_a_mutation_document_and_rest_has_only_get(self):
        class Inner:
            def execute(self, **kw):
                raise AssertionError("must not be reached")
        guard = runner.ReadOnlyGraphQL(Inner())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "m.graphql")
            path.write_text("mutation Evil { children { delete(childIds: []) } }", encoding="utf-8")
            with self.assertRaises(runner.NotReadOnly):
                guard.execute(path, {}, "Evil")
        self.assertEqual(guard.calls, 0)
        rest = runner.ReadOnlyRest(object())
        for name in ("post", "put", "patch", "delete", "request"):
            self.assertFalse(hasattr(rest, name))


if __name__ == "__main__":
    unittest.main()
