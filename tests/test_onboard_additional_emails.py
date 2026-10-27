"""A parent whose Famly email is a SECONDARY address on the HubSpot contact (`hs_additional_emails`) is matched,
not duplicated. Fake values only (example addresses); nothing here is live."""

import unittest

from actions.onboard import store
from actions.onboard_audit import runner as audit
from actions.onboard_dry_run import bill_payers as bp
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.links import DealContact, email_set
from actions.onboard_dry_run.runner import DryRun
from tests.test_onboard_bill_payer import BPCase, payer
from tests.test_onboard_phase2 import DEAL, SITE_ID, Phase2Case

CHILD = "child-9"
SECOND = "ada.second@example.net"                   # a secondary address of HubSpot contact c1


class EmailCase(Phase2Case):
    def setUp(self):
        super().setUp()
        self.hub.contacts["c1"]["hs_additional_emails"] = f" {SECOND.upper()} ; other@example.com;; "

    def existing_child(self, contacts):
        self.famly.children[CHILD] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": "HDWEST-l",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts[CHILD] = list(contacts)

    def plan(self):
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL)

    def entry(self, plan, hubspot_id):
        return next(c for c in plan["contacts"] if c["hubspotContactId"] == hubspot_id)


class EmailSetTests(unittest.TestCase):
    def test_primary_plus_additional_canonical_deduplicated_and_blank_free(self):
        self.assertEqual(email_set(" A@Example.org ", "b@example.org; A@EXAMPLE.org ;; c@example.org;"),
                         frozenset({"a@example.org", "b@example.org", "c@example.org"}))
        self.assertEqual(email_set("", None), frozenset())

    def test_the_contact_keeps_its_primary_and_gets_the_set_from_its_properties(self):
        contact = DealContact("c1", "Ada@Example.org", True, props={"hs_additional_emails": "x@example.net"})
        self.assertEqual(contact.email, "Ada@Example.org")                      # primary unchanged
        self.assertEqual(contact.emails, frozenset({"ada@example.org", "x@example.net"}))
        self.assertNotIn("example", repr(contact))                              # never in a log line
        self.assertEqual(DealContact("c1", "a@example.org", True).emails, frozenset({"a@example.org"}))


class MatchingTests(EmailCase):
    def test_a_secondary_email_matches_an_existing_child_contact(self):
        self.existing_child([("fc-1", SECOND), ("fc-2", "ben.b@example.org")])
        plan = self.plan()
        first = self.entry(plan, "c1")
        self.assertEqual((first["status"], first["famlyContactId"], first["matchedBy"]),
                         ("present", "fc-1", "additional_email"))
        self.assertEqual(self.entry(plan, "c2")["matchedBy"], "email")
        self.assertEqual(plan["holds"], [])
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)              # nothing to write
        self.assertEqual(self.famly.contact_creates, [])                         # no duplicate
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:present"])

    def test_a_secondary_match_gets_relation_where_missing_and_nothing_else_changes(self):
        self.existing_child([("fc-1", SECOND), ("fc-2", "ben.b@example.org")])
        self.famly.relations["fc-1"] = False
        self.run_to_approval()
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual([c for c, _ in self.famly.relation_updates], ["fc-1"])      # RELATION, one write
        self.assertEqual(self.famly.contact_creates, [])
        self.assertEqual(self.famly.contacts[CHILD][0], ("fc-1", SECOND))        # the Famly email is untouched

    def test_a_create_still_sends_the_primary_email_only(self):
        self.run_through()                                                       # a new child: both parents created
        sent = {p["email"] for p in self.famly.contact_creates}
        self.assertEqual(sent, {"ada.l@example.org", "ben.b@example.org"})
        for payload in self.famly.contact_creates:
            self.assertNotIn("ada.second", str(payload))
            self.assertNotIn("other@example.com", str(payload))

    def test_one_deal_contact_matching_two_famly_contacts_holds(self):
        self.existing_child([("fc-1", "ada.l@example.org"), ("fc-3", SECOND), ("fc-2", "ben.b@example.org")])
        plan = self.plan()
        self.assertEqual(self.entry(plan, "c1")["status"], "ambiguous_on_child")
        self.assertEqual([h["reason"] for h in plan["holds"]], ["contact_ambiguous_on_child"])
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.HELD)
        self.assertEqual(self.famly.contact_creates, [])

    def test_one_famly_contact_matching_two_deal_contacts_holds_with_a_code(self):
        shared = "shared@example.org"
        self.hub.contacts["c1"]["hs_additional_emails"] = shared
        self.hub.contacts["c2"]["hs_additional_emails"] = shared
        self.existing_child([("fc-1", shared)])
        plan = self.plan()
        self.assertEqual([self.entry(plan, c)["status"] for c in ("c1", "c2")],
                         ["famly_contact_matches_two_deal_contacts"] * 2)
        self.assertEqual(plan["holds"], [{"reason": "famly_contact_matches_two_deal_contacts",
                                          "contactIds": ["fc-1"], "hubspotContactIds": ["c1", "c2"]}])
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]),
                         (store.HELD, "famly_contact_matches_two_deal_contacts"))
        self.assertEqual((self.famly.contact_creates, self.famly.relation_updates), ([], []))
        self.assertNotIn(shared, self.all_output())                              # codes and ids only

    def test_the_plan_says_how_it_matched_without_any_address(self):
        self.existing_child([("fc-1", SECOND), ("fc-2", "ben.b@example.org")])
        plan = self.plan()
        from actions.onboard import messages, plan_status
        lines = plan_status.block(plan)
        self.assertIn(messages.state("contact_present_additional_email", id="c1", role=" (bill payer)"), lines)
        text = "".join(lines) + str(plan)
        for value in (SECOND, "other@example.com", "example.net"):
            self.assertNotIn(value, text)


class PayerScanTests(unittest.TestCase):
    def contact(self):
        return DealContact("c1", "ada.l@example.org", True, props={"hs_additional_emails": SECOND,
                                                                    "firstname": "A", "lastname": "B"},
                           has_first_and_last_name=True)

    def live(self, email, pid):
        return bp.Payer(pid, bp.normalise_email(email), {})

    def test_zero_one_and_two_matches_through_the_email_set(self):
        contact = self.contact()
        self.assertEqual(bp.decide([contact], [self.live("other@example.org", "o")]).status, bp.WOULD_CREATE)
        one = bp.decide([contact], [self.live(SECOND, "s")])
        self.assertEqual((one.status, one.bill_payer_id, one.matches), (bp.WOULD_ATTACH, "s", 1))
        two = bp.decide([contact], [self.live(SECOND, "s"), self.live("ada.l@example.org", "p")])
        self.assertEqual((two.status, two.flag, two.matches), (bp.FLAGGED, bp.AMBIGUOUS, 2))

    def test_the_double_billing_guard_is_unchanged(self):
        contact = self.contact()
        elsewhere = bp.Payer("e", "someone@example.org", {"child-1": 1.0})
        decision = bp.decide([contact], [self.live(SECOND, "s"), elsewhere], "child-1")
        self.assertEqual((decision.status, decision.flag), (bp.FLAGGED, bp.CHILD_LINKED_ELSEWHERE))


class PayerStageTests(BPCase):
    def test_a_payer_on_a_secondary_email_is_attached_not_duplicated(self):
        self.hub.contacts["c1"]["hs_additional_emails"] = SECOND
        self.famly.payers.append(payer(SECOND, pid="p-second"))
        self.run_to_approval()
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(self.famly.ops(), ["OnboardBillPayerAddChildren"])        # attach: no second payer
        self.assertEqual(len(self.famly.payers), 1)


class AuditTests(unittest.TestCase):
    def codes(self, famly_emails, **props):
        contact = DealContact("c1", "ada.l@example.org", False, props=props)
        child = audit.FamlyChild("k1", contacts=[audit.FamlyContact(email=e, relation=True) for e in famly_emails])
        return audit.contact_codes([contact], child)

    def test_a_secondary_match_is_rescued_and_counted_by_an_info_code(self):
        self.assertEqual(self.codes([SECOND], hs_additional_emails=SECOND), ["contact_matched_via_additional_email"])

    def test_a_primary_match_has_no_info_code_and_no_match_is_still_missing(self):
        self.assertEqual(self.codes(["ada.l@example.org"], hs_additional_emails=SECOND), [])
        self.assertIn("contact_missing", self.codes(["nobody@example.org"], hs_additional_emails=SECOND))


if __name__ == "__main__":
    unittest.main()
