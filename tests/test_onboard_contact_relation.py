"""Part 2: RELATION = Parent on a contact that is present on the child and has no RELATION record.

Fakes only; the update goes through the single-field `records` argument. Ids, counts and codes
only."""

import unittest

from actions.onboard import famly_writes, messages, store
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.runner import DryRun
from core.client import GraphQLHTTPError
from tests.test_onboard_phase2 import DEAL, PII, SITE_ID, Phase2Case

CHILD = "child-9"


class RelationCase(Phase2Case):
    def existing_child(self, missing=("fc-1",)):
        self.famly.children[CHILD] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": "HDWEST-l",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts[CHILD] = [("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")]
        for contact_id in missing:
            self.famly.relations[contact_id] = False

    def plan(self):
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL)


class AddTests(RelationCase):
    def test_a_present_contact_without_relation_gets_parent_and_gate_a_counts_it(self):
        self.existing_child()
        self.assertEqual([c.get("relation") for c in self.plan()["contacts"]], ["would_add", "present"])
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        text = self.notify.gates[0][0]
        self.assertIn("Famly writes planned: 0 child, 0 contact(s), 1 contact relation(s)", text)
        self.approve()
        result = self.run_worker()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.famly.relation_updates,
                         [("fc-1", [{"contactId": "fc-1", "record": [{"key": "RELATION", "value": "Parent"}]}])])
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:present", "relation:c1:relation_added"])
        self.assertTrue(any(messages.relation_summary(1) in n for n in self.notify.notices))
        self.assertEqual(self.famly.contact_creates, [])

    def test_an_existing_relation_of_any_value_is_left_alone(self):
        self.existing_child(missing=())
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)        # nothing to write: no Gate A
        self.assertEqual(self.famly.relation_updates, [])

    def test_a_relation_set_after_approval_is_not_overwritten(self):
        self.existing_child()
        self.run_to_approval()
        self.approve()
        self.famly.relations["fc-1"] = True                              # a person set it meanwhile
        self.run_worker()                                                # the plan changed: asks again
        self.assertEqual(self.famly.relation_updates, [])

    def test_a_refused_update_is_flagged_and_the_job_continues(self):
        self.existing_child(missing=("fc-1", "fc-2"))

        def refuse(variables):
            raise GraphQLHTTPError("bad", 400, "{}")
        self.famly.relation_effect = refuse
        self.run_to_approval()
        self.approve()
        result = self.run_worker()
        self.assertEqual(result["status"], store.DONE)                   # a flag, not a stop
        self.assertEqual(len(self.famly.relation_updates), 2)            # each contact still tried once
        self.assertIn("relation:c1:relation_not_added", self.job().outcomes)
        self.assertTrue(any(messages.lookup("relation_not_added")[1] in n for n in self.notify.notices))

    def test_one_write_per_contact_and_an_unknown_outcome_is_settled_by_a_read_never_resent(self):
        self.existing_child(missing=("fc-1", "fc-2"))
        self.famly.relation_land_then_raise = GraphQLHTTPError("boom", 500, "")
        self.run_to_approval()
        self.approve()
        result = self.run_worker()
        self.assertEqual(sorted(c for c, _ in self.famly.relation_updates), ["fc-1", "fc-2"])
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.job().outcomes[2:], ["relation:c1:relation_added", "relation:c2:relation_added"])

    def test_an_accepted_update_that_does_not_show_afterwards_is_flagged_unconfirmed(self):
        self.existing_child()
        self.famly.relation_lands = False
        self.run_to_approval()
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(len(self.famly.relation_updates), 1)
        self.assertIn("relation:c1:relation_unconfirmed", self.job().outcomes)

    def test_the_update_document_is_the_single_field_records_update_only(self):
        famly_writes._assert_relation_update_only(famly_writes.UPDATE_RELATION_QUERY)
        text = famly_writes.UPDATE_RELATION_QUERY.read_text(encoding="utf-8")
        self.assertNotIn("update(contacts", text)

    def test_no_value_reaches_any_output(self):
        self.existing_child()
        self.run_to_approval()
        self.approve()
        self.run_worker()
        blob = self.all_output()
        for value in PII:
            self.assertNotIn(value, blob, value)


class BlankRelationTests(RelationCase):
    """Famly returns a RELATION record for every contact, with an empty value when unset."""

    def would_add(self):
        return [c.get("relation") for c in self.plan()["contacts"]]

    def test_an_empty_value_an_absent_record_and_a_blank_value_all_mean_not_set(self):
        self.existing_child(missing=("fc-1",))                         # the fake returns an EMPTY record
        self.assertEqual(self.would_add(), ["would_add", "present"])
        self.famly.relation_record_absent = True                       # and no record at all
        self.assertEqual(self.would_add(), ["would_add", "present"])
        self.famly.relation_record_absent = False
        self.famly.relations["fc-1"] = True
        self.famly.relation_values["fc-1"] = "   "                      # whitespace only
        self.assertEqual(self.would_add(), ["would_add", "present"])

    def test_any_non_empty_value_is_set_and_left_alone(self):
        self.existing_child(missing=())
        for word in ("Parent", "Grandparent", "Mother", "x"):
            self.famly.relation_values["fc-1"] = word
            self.assertEqual(self.would_add(), ["present", "present"], word)

    def test_an_empty_value_is_filled_in_with_parent_after_approval_and_confirmed(self):
        self.existing_child(missing=("fc-1",))
        self.run_to_approval()
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual([c for c, _ in self.famly.relation_updates], ["fc-1"])
        self.assertIn("relation:c1:relation_added", self.job().outcomes)

    def test_a_value_set_after_approval_is_never_overwritten(self):
        self.existing_child(missing=("fc-1",))
        self.run_to_approval()
        self.approve()
        self.famly.relations["fc-1"] = True
        self.famly.relation_values["fc-1"] = "Grandparent"              # a person filled it in meanwhile
        self.run_worker()
        self.assertEqual(self.famly.relation_updates, [])

    def test_the_read_keeps_only_whether_a_value_is_set(self):
        self.existing_child(missing=("fc-1",))
        self.famly.relation_values["fc-2"] = "Mother"
        reads = FamlyReads(self.famly, site_ids=[SITE_ID])
        self.assertEqual(reads.child_contact_relations(CHILD), {"fc-1": False, "fc-2": True})


class ChildOnlyTests(RelationCase):
    def test_a_child_only_stage_never_reads_or_writes_relations(self):
        self.hub.stages["s-left"] = ("Operations", "Left Nursery")
        self.hub.deals[DEAL]["dealstage"] = "s-left"
        self.existing_child()
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(self.famly.relation_updates, [])
        self.assertNotIn("OnboardChildContactRelations", self.famly.calls)


if __name__ == "__main__":
    unittest.main()
