"""Part 1: new contacts are stamped with the HubSpot contact id, and an existing contact is
matched by that reference (on this child) before falling back to canonical email.

Fakes only. Ids, counts and codes only: no value from the deal reaches any output."""

import unittest

from actions.onboard import store
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.runner import DryRun, plan_hash
from core.client import GraphQLHTTPError
from tests.test_onboard_phase2 import DEAL, PII, SITE_ID, Phase2Case

CHILD = "child-9"


class ReferenceCase(Phase2Case):
    def existing_child(self, contacts, refs=None):
        self.famly.children[CHILD] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": "HDWEST-l",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts[CHILD] = list(contacts)
        for contact_id, (child_ids, foreign_id) in (refs or {}).items():
            self.famly.contact_refs[contact_id] = (child_ids, [("HUBSPOT", foreign_id)])

    def plan(self):
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL)

    def entry(self, plan, hubspot_id):
        return next(c for c in plan["contacts"] if c["hubspotContactId"] == hubspot_id)


class StampTests(ReferenceCase):
    def test_a_created_contact_carries_its_hubspot_contact_id_as_a_reference(self):
        self.assertEqual(self.run_through()["status"], store.DONE)
        sent = {p["email"]: p["externalSystemsReferences"] for p in self.famly.contact_creates}
        self.assertEqual(sorted(map(str, sent.values())), sorted([
            str([{"foreignId": "c1", "system": "HUBSPOT"}]),
            str([{"foreignId": "c2", "system": "HUBSPOT"}])]))
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:created"])

    def test_the_dry_run_shows_create_for_a_child_that_does_not_exist_yet(self):
        plan = self.plan()
        self.assertEqual([c["matchedBy"] for c in plan["contacts"]], ["create", "create"])


class MatchingTests(ReferenceCase):
    def test_a_reference_match_counts_as_present_even_when_the_email_changed(self):
        self.existing_child([("fc-1", "changed.address@example.net"), ("fc-2", "ben.b@example.org")],
                            refs={"fc-1": ([CHILD], "c1")})
        plan = self.plan()
        self.assertEqual(plan["holds"], [])
        first = self.entry(plan, "c1")
        self.assertEqual((first["status"], first["matchedBy"], first["famlyContactId"]),
                         ("present", "reference", "fc-1"))
        self.enqueue()
        result = self.run_worker()                       # nothing to write: no Gate A, no create
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.famly.contact_creates, [])
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:present"])

    def test_without_a_reference_it_falls_back_to_the_email_on_the_child(self):
        self.existing_child([("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")])
        plan = self.plan()
        self.assertEqual([(c["status"], c["matchedBy"]) for c in plan["contacts"]],
                         [("present", "email"), ("present", "email")])

    def test_siblings_sharing_one_hubspot_id_are_told_apart_by_child(self):
        # One HubSpot contact (c1), two Famly contacts on two children. This child's contact has
        # a different email, so only the reference can find it, and only the one on THIS child.
        self.existing_child([("fc-1", "other.address@example.net"), ("fc-2", "ben.b@example.org")],
                            refs={"fc-1": ([CHILD], "c1"), "fc-sib": (["child-sibling"], "c1")})
        first = self.entry(self.plan(), "c1")
        self.assertEqual((first["status"], first["famlyContactId"]), ("present", "fc-1"))
        # A reference that exists only on the sibling's child does not match this child.
        self.famly.contact_refs.pop("fc-1")
        plan = self.plan()
        self.assertEqual(self.entry(plan, "c1")["status"], "would_create")    # no match: a repair

    def test_an_unknown_create_outcome_is_settled_by_the_reference_lookup_too(self):
        # The create lands but answers HTTP 500, and the child's contact list lags: only the
        # reference lookup shows the parent. Found once = created; the create is not resent.
        self.famly.contact_land_then_raise = lambda payload: GraphQLHTTPError("boom", 500, "")
        self.famly.contact_list_script = lambda n, rows: []
        self.run_through()
        self.assertEqual(len(self.famly.contact_creates), 2)           # one each, never resent
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:created"])

    def test_a_failed_reference_read_never_leads_to_a_create(self):
        self.existing_child([("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")])
        self.famly.contact_ref_error = GraphQLHTTPError("bad", 500, "{}")
        self.assertEqual(self.plan()["status"], "error")

    def test_the_plan_hash_changes_when_the_way_a_contact_is_matched_changes(self):
        self.existing_child([("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")])
        by_email = plan_hash(self.plan())
        self.famly.contact_refs["fc-1"] = ([CHILD], [("HUBSPOT", "c1")])
        self.assertNotEqual(plan_hash(self.plan()), by_email)

    def test_no_value_from_the_deal_reaches_any_output(self):
        self.existing_child([("fc-1", "changed.address@example.net"), ("fc-2", "ben.b@example.org")],
                            refs={"fc-1": ([CHILD], "c1")})
        self.enqueue()
        self.run_worker()
        blob = self.all_output()
        for value in PII:
            self.assertNotIn(value, blob, value)


if __name__ == "__main__":
    unittest.main()
