"""Stage-aware scope: a deal in a child-only stage (derive.CHILD_ONLY_STAGES, "Left Nursery")
is onboarded as a CHILD RECORD ONLY. Ids, counts and codes only."""

import os
import unittest

from actions.onboard import approval, store
from actions.onboard_dry_run import derive
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.runner import DryRun, plan_hash
from tests.test_onboard_phase2 import DEAL, SITE_ID, Phase2Case

LEFT = "s-left"


class ChildOnlyCase(Phase2Case):
    def setUp(self):
        super().setUp()
        self.hub.stages[LEFT] = ("Operations", "Left Nursery")
        self.hub.stages["s-other"] = ("Operations", "Something Later")
        self.hub.deals[DEAL]["dealstage"] = LEFT
        os.environ["ONBOARD_BILL_PAYER"] = "on"
        self.addCleanup(os.environ.pop, "ONBOARD_BILL_PAYER", None)

        def no_contacts(*args, **kwargs):
            raise AssertionError("a contact was read for a child-only deal")
        self.hub.get_contact = no_contacts
        self.hub.associations = lambda f, i, t: ([] if t != "contacts" else no_contacts())

    def back_to_normal(self):
        """Stop forbidding contact reads and the payer scan (the deal leaves the child-only stage)."""
        del self.hub.get_contact
        del self.hub.associations
        os.environ["ONBOARD_BILL_PAYER"] = "off"

    def plan(self):
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL)

    def assert_no_contact_or_payer_calls(self):
        self.assertEqual(self.famly.contact_creates, [])
        for operation in self.famly.calls:
            self.assertNotIn("Contact", operation)
            self.assertNotIn("BillPayer", operation)


class PlanTests(ChildOnlyCase):
    def test_contacts_and_payer_are_skipped_and_nothing_is_read(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["contactsStatus"], "skipped_stage")
        self.assertEqual(plan["billPayer"], {"status": "skipped_stage", "recipient": "skipped"})
        self.assertEqual(plan["contacts"], [])
        self.assertEqual(plan["scope"], "child_only")
        self.assert_no_contact_or_payer_calls()

    def test_contact_holds_do_not_apply_but_child_holds_still_do(self):
        self.hub.assoc = {}                                  # no bill payer at all: bill_payer_unclear elsewhere
        self.assertEqual(self.plan()["holds"], [])
        self.hub.deals[DEAL]["child_first_name"] = ""
        self.assertEqual([h["reason"] for h in self.plan()["holds"]], ["child_name_incomplete"])

    def test_the_plan_hash_differs_between_left_nursery_and_another_stage(self):
        left = plan_hash(self.plan())
        self.hub.deals[DEAL]["dealstage"] = "s-other"
        self.back_to_normal()
        self.hub.assoc = {("deals", DEAL, "contacts"): [
            {"id": "c1", "types": [{"typeId": 91, "label": "Bill Payer"}]}]}
        self.assertNotEqual(plan_hash(self.plan()), left)

    def test_matching_ignores_case_and_spaces_but_is_exact(self):
        for label in ("left nursery", "  LEFT   Nursery ", "Left Nursery"):
            self.assertTrue(derive.is_child_only_stage(label))
        for label in ("Left Nurser", "Left Nursery 2", "", None, "Deposit Pending"):
            self.assertFalse(derive.is_child_only_stage(label))

    def test_a_stage_outside_the_list_behaves_as_before(self):
        self.hub.deals[DEAL]["dealstage"] = "s-pending"
        self.back_to_normal()
        result = self.plan()
        self.assertNotIn("scope", result)
        self.assertNotIn("contactsStatus", result)
        self.assertEqual(len(result["contacts"]), 2)


class WorkerTests(ChildOnlyCase):
    def test_a_child_only_deal_creates_the_child_and_nothing_else(self):
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(len(self.famly.child_creates), 1)
        self.assert_no_contact_or_payer_calls()
        self.assertEqual(self.written()["famly_sync_status"], "child_linked")
        self.assertEqual(self.written()["famly_childid"], "child-1")
        self.assertNotIn("contacts_linked", self.hub_status_values())
        self.assertNotIn("bill_payer_linked", self.hub_status_values())
        final = self.notify.notices[-1]
        self.assertIn("Child created in Famly. No contacts or bill payer, because the deal is in Left Nursery.",
                      final)

    def hub_status_values(self):
        return [props.get("famly_sync_status") for _deal, props in self.hub.writes]

    def test_gate_a_says_child_only_and_counts_one_write(self):
        self.run_to_approval()
        text = self.notify.gates[-1][0]
        self.assertIn("child only", text)
        self.assertIn("Famly writes planned: 1 child", text)
        self.assertNotIn("contact(s)", text)
        self.assertNotIn("bill payer", text.lower().replace("no contacts or bill payer", ""))

    def test_an_existing_child_is_not_recreated_and_is_linked_without_contacts(self):
        self.famly.children["child-9"] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": "g",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.enqueue()
        result = self.run_worker()                          # nothing to write: no Gate A
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.famly.child_creates, [])
        self.assert_no_contact_or_payer_calls()
        self.assertEqual(self.written()["famly_sync_status"], "child_linked")
        self.assertIn("Child was already in Famly. No contacts or bill payer, because the deal is in Left Nursery.",
                      self.notify.notices[-1])

    def test_a_deal_that_changes_stage_after_approval_stops_with_plan_changed(self):
        self.run_to_approval()
        self.hub.deals[DEAL]["dealstage"] = "s-pending"                 # moved on after approval
        self.back_to_normal()
        self.approve()
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)      # asked again
        self.assertEqual(self.famly.child_creates, [])
        self.assertIn("plan changed", self.notify.gates[-1][0])

    def test_a_status_the_dropdown_does_not_offer_follows_the_usual_handling(self):
        self.hub.defs["famly_sync_status"].update(fieldType="select",
                                                   options=["child_ready", "contacts_linked", "needs_attention"])
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.written()["famly_sync_status"], "child_ready")
        self.assertIn("value_not_offered:famly_sync_status:child_linked", " ".join(self.notify.notices))


class RetryAndFileTests(ChildOnlyCase):
    def test_retry_and_the_file_gate_work_for_a_child_only_deal(self):
        self.mode("live", allowed="999")
        holds = []
        self.notify.hold_notice = lambda text, job_id, nonce: holds.append((job_id, nonce)) or True
        self.hub.deals[DEAL]["child_first_name"] = ""                    # a child hold
        store.enqueue(DEAL, "live", source="file", file_hash="f" * 64)
        self.assertEqual(self.run_worker()["status"], store.HELD)
        held = self.job()
        self.hub.deals[DEAL]["child_first_name"] = "Zara"
        from integrations import slack
        value = approval.button_value(held.id, "", holds[-1][1])
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_RETRY, value, "U1"),
                         approval.MSG_RETRY_QUEUED)
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)


if __name__ == "__main__":
    unittest.main()
