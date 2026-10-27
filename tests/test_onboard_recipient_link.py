"""The bill payer stage links the deal's bill payer contact as an invoice recipient of the payer
(billPayers.addInvoiceRecipients). Fakes only. Ids, counts and codes only."""

import json
import os
import unittest

from actions.onboard import bill_payer_writes as bpw
from actions.onboard import messages, store
from actions.onboard_dry_run import bill_payers as bp
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.runner import DryRun, plan_hash
from core.client import GraphQLHTTPError
from tests.test_onboard_bill_payer import BPCase, payer
from tests.test_onboard_phase2 import DEAL, PII, SITE_ID

CHILD = "child-9"
ALREADY_ADDED = {"data": None, "errors": [{
    "message": "The invoice recipient has already been added to the bill payer", "errorCode": 1675864914,
    "extensions": {"payload": {"existingEntries": [{"relationId": "fc-1", "billPayerId": "p1"}]}}}]}


class LinkCase(BPCase):
    def existing_child(self):
        self.famly.children[CHILD] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": "HDWEST-l",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts[CHILD] = [("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")]

    def plan(self):
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL, {})

    def linked_payer(self, recipients, children=None):
        self.famly.payers.append(payer(self.email, children={CHILD: 1.0} if children is None else children,
                                       pid="p1", recipients=recipients))


class WriteTests(LinkCase):
    def test_a_new_payer_and_a_contact_created_in_the_same_job_are_linked_once_with_the_right_ids(self):
        self.run_to_approval()
        text = self.notify.gates[0][0]
        self.assertIn("1 bill payer contact link(s)", text)
        self.assertIn(messages.state("bill_payer_recipient_will_add"), text)
        self.approve()
        result = self.run_worker()
        self.assertEqual(result["status"], store.DONE)
        contact_id = next(i for i, e in self.famly.contacts["child-1"] if e == "ada.l@example.org")
        payer_id = self.famly.payers[0]["id"]
        self.assertEqual(self.famly.recipient_writes, [{"relationId": contact_id, "billPayerId": payer_id}])
        self.assertIn("billpayerlink:bill_payer_recipient_linked", self.job().outcomes)
        self.assertTrue(any(messages.recipient_summary() in n for n in self.notify.notices))
        blob = self.all_output()
        for value in PII:
            self.assertNotIn(value, blob, value)

    def test_an_existing_payer_that_is_attached_is_linked_too(self):
        self.existing_child()
        self.famly.payers.append(payer(self.email, pid="p1"))               # matched, child not on it
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(self.famly.ops(), [bpw.ADD_CHILDREN_OP])
        self.assertEqual(self.famly.recipient_writes, [{"relationId": "fc-1", "billPayerId": "p1"}])

    def test_a_linked_payer_whose_recipient_is_missing_is_repaired_after_approval(self):
        self.existing_child()
        self.linked_payer([])
        self.assertEqual(self.plan()["billPayer"]["recipient"], "will_add")
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertIn("Famly writes planned: 0 child, 0 contact(s), 0 bill payer, 1 bill payer contact link(s)",
                      self.notify.gates[0][0])
        self.assertEqual(self.famly.recipient_writes, [])                   # nothing before the click
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(len(self.famly.recipient_writes), 1)

    def test_an_already_linked_payer_writes_nothing_and_asks_nothing(self):
        self.existing_child()
        self.linked_payer(["fc-1"])
        plan = self.plan()
        self.assertEqual(plan["billPayer"]["recipient"], "already_linked")
        self.assertIn(messages.state("bill_payer_recipient_linked"), __import__(
            "actions.onboard.plan_status", fromlist=["block"]).block(plan))
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)           # no Gate A
        self.assertEqual(self.notify.gates, [])
        self.assertEqual(self.famly.recipient_writes, [])

    def test_the_already_added_400_is_success_not_a_failure(self):
        self.existing_child()
        self.linked_payer([])
        self.enqueue()
        self.run_worker()
        self.famly.payers[0]["recipients"] = {"fc-1": 10 ** 6}            # linked, but the read lags
        self.approve()
        result = self.run_worker()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(len(self.famly.recipient_writes), 1)                # sent once, refused, accepted as done
        self.assertIn("billpayerlink:bill_payer_recipient_present", self.job().outcomes)
        self.assertNotIn("bill_payer_recipient_unconfirmed", self.job().outcomes)

    def test_another_error_is_settled_by_reads_and_never_resent(self):
        self.existing_child()
        self.linked_payer([])
        self.famly.recipient_mode = "error_after_landing"                   # it landed; the answer was a 500
        self.famly.recipient_lag_scans = 2                                  # and the read lags a little
        self.run_to_approval()
        self.approve()
        result = self.run_worker()
        self.assertEqual(len(self.famly.recipient_writes), 1)
        self.assertEqual(result["status"], store.DONE)
        self.assertIn("billpayerlink:bill_payer_recipient_linked", self.job().outcomes)

    def test_an_error_that_never_shows_is_flagged_for_a_person_and_never_resent(self):
        self.existing_child()
        self.linked_payer([])
        self.famly.recipient_mode = "error_not_landing"
        self.run_to_approval()
        self.approve()
        result = self.run_worker()
        self.assertEqual(len(self.famly.recipient_writes), 1)                # not retried
        self.assertEqual(result["status"], store.DONE)                       # a flag, not a stop
        self.assertIn("billpayerlink:bill_payer_recipient_unconfirmed", self.job().outcomes)
        self.assertTrue(any(messages.lookup("bill_payer_recipient_unconfirmed")[1] in n
                            for n in self.notify.notices))
        self.assertGreaterEqual(len(self.sleeps), 4)                         # the bounded waits were used


class ContactNotFoundTests(LinkCase):
    def test_a_contact_that_could_not_be_created_flags_and_the_job_continues(self):
        def reject_ada(payload):
            if payload["email"] == "ada.l@example.org":
                raise GraphQLHTTPError("bad", 400, "{}")
        self.famly.contact_effect = reject_ada
        self.run_to_approval()
        self.approve()
        result = self.run_worker()
        self.assertEqual(result["status"], store.NEEDS_REVIEW)               # the contact failed, as before
        self.assertEqual(self.famly.recipient_writes, [])                    # nothing to link
        self.assertIn("billpayerlink:bill_payer_contact_not_found", self.job().outcomes)
        self.assertEqual(self.famly.ops().count(bpw.CREATE_OP), 1)           # the payer stage still ran

    def test_the_plan_flags_a_bill_payer_contact_that_cannot_be_found(self):
        self.existing_child()
        self.famly.contacts[CHILD] = [("fc-1", "ada.l@example.org"), ("fc-3", "ada.l@example.org")]  # ambiguous
        self.famly.payers.append(payer(self.email, pid="p1"))
        plan = self.plan()
        self.assertEqual(plan["billPayer"]["recipient"], "contact_not_found")
        self.assertIn("bill_payer_contact_not_found", plan["warnings"])
        from actions.onboard import plan_status
        self.assertIn(messages.state("bill_payer_recipient_contact_not_found"), plan_status.block(plan))


class PlanAndGateTests(LinkCase):
    def test_the_plan_hash_covers_the_recipient_state(self):
        self.existing_child()
        self.linked_payer([])
        missing = plan_hash(self.plan())
        self.famly.payers[0]["recipients"] = {"fc-1": 0}
        self.assertNotEqual(plan_hash(self.plan()), missing)

    def test_child_only_and_the_switch_off_skip_it(self):
        self.hub.stages["s-left"] = ("Operations", "Left Nursery")
        self.hub.deals[DEAL]["dealstage"] = "s-left"
        self.assertEqual(self.plan()["billPayer"], {"status": "skipped_stage", "recipient": "skipped"})
        self.enqueue()
        self.run_worker()
        self.assertEqual(self.famly.recipient_writes, [])
        self.hub.deals[DEAL]["dealstage"] = "s-pending"
        os.environ["ONBOARD_BILL_PAYER"] = "off"
        self.assertNotIn("billPayer", self.plan())


class UnitTests(unittest.TestCase):
    def test_the_document_is_exactly_one_add_recipients_mutation(self):
        bpw.assert_recipient_mutation(bpw.ADD_RECIPIENT_QUERY.read_text(encoding="utf-8"))
        for bad in ("mutation A($r: X) { billPayers { deleteInvoiceRecipients(invoiceRecipients: $r) { x } } }",
                    "mutation A($r: X) { billPayers { addInvoiceRecipients(invoiceRecipients: $r) { x } "
                    "create(billPayers: $r) { y } } }"):
            with self.assertRaises(AssertionError):
                bpw.assert_recipient_mutation(bad)

    def test_the_already_added_refusal_needs_the_pair_in_existing_entries(self):
        self.assertTrue(bpw.already_added(json.dumps(ALREADY_ADDED), "fc-1", "p1"))
        self.assertFalse(bpw.already_added(json.dumps(ALREADY_ADDED), "fc-2", "p1"))     # another contact
        self.assertFalse(bpw.already_added(json.dumps(ALREADY_ADDED), "fc-1", "p2"))     # another payer
        no_entries = {"errors": [{"message": "The invoice recipient has already been added", "errorCode": 1675864914}]}
        self.assertFalse(bpw.already_added(json.dumps(no_entries), "fc-1", "p1"))
        self.assertFalse(bpw.already_added(json.dumps({"errors": [{"message": "boom"}]}), "fc-1", "p1"))
        self.assertFalse(bpw.already_added("not json", "fc-1", "p1"))

    def test_the_new_codes_have_sentences(self):
        for code in (bpw.RECIPIENT_LINKED, bpw.RECIPIENT_PRESENT, bpw.RECIPIENT_UNCONFIRMED,
                     bpw.CONTACT_NOT_FOUND):
            self.assertIsNotNone(messages.lookup(code))

    def test_the_scan_reads_recipient_ids_only(self):
        payer_row = bp._payer({"billPayerId": "p", "email": "a@x.org",
                               "invoiceRecipients": [{"relationId": "r1", "contactId": "r1"}]})
        self.assertEqual(payer_row.recipients, frozenset({"r1"}))
        self.assertEqual(payer_row.details, {})                             # not a "detail" to compare


if __name__ == "__main__":
    unittest.main()
