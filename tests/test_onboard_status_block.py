"""Part 5: the Gate A / dry-run status block says what is already in Famly and what will be
written. Wording comes from messages.state only; ids, counts and codes only."""

import unittest

from actions.onboard import messages, notifier, plan_status
from actions.onboard_dry_run import runner as dry_runner
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.runner import DryRun, plan_hash
from tests.test_onboard_phase2 import DEAL, PII, SITE_ID, Phase2Case

CHILD = "child-9"


def plan_of(child, contacts=(), bill_payer=None, **extra):
    plan = {"dealId": "1", "holds": [], "warnings": [], "child": child, "contacts": list(contacts), **extra}
    if bill_payer is not None:
        plan["billPayer"] = bill_payer
    return plan


FOUND = {"status": "child_found", "famlyChildId": "c", "via": "external_system_reference", "origin": "existing"}


def contact(hubspot_id, status, bill_payer=False, **extra):
    return {"hubspotContactId": hubspot_id, "isBillPayer": bill_payer, "status": status, **extra}


class RenderTests(unittest.TestCase):
    def lines(self, plan, **kwargs):
        return plan_status.block(plan, **kwargs)

    def test_every_state_sentence_formats_and_carries_no_stray_placeholder(self):
        for code in messages.state_codes():
            text = messages.state(code, id="c1", role=" (bill payer)", stage="S", why="W", fields="f",
                                  n=1, total=2, k=1, ids="x")
            self.assertNotIn("{", text, code)

    def test_child_states(self):
        self.assertEqual(self.lines(plan_of({"status": "child_would_create"}))[0],
                         messages.state("child_would_create"))
        self.assertEqual(self.lines(plan_of({"status": "child_would_create", "testChild": True}))[0],
                         messages.state("child_would_create_test"))
        for via, code in (("external_system_reference", "child_found_reference"), ("urn", "child_found_urn"),
                          ("famly_childid", "child_found_famly_childid")):
            self.assertEqual(self.lines(plan_of({**FOUND, "via": via}))[0], messages.state(code))
        mine = {**FOUND, "origin": "created_by_pipeline"}
        self.assertEqual(self.lines(plan_of(mine), this_job_child_id="c")[0],
                         messages.state("child_created_by_this_job"))
        self.assertEqual(self.lines(plan_of(mine))[0], messages.state("child_created_earlier"))

    def test_contact_states(self):
        plan = plan_of(FOUND, [
            contact("c1", "would_create", True),
            contact("c2", "present", matchedBy="reference", relation="present"),
            contact("c3", "present", matchedBy="email", relation="present"),
            contact("c4", "present", matchedBy="email", relation="would_add"),
            contact("c5", "present", matchedBy="email", relation="unreadable"),
            contact("c6", "no_email"),
        ])
        got = self.lines(plan)[1:7]
        self.assertEqual(got[0], messages.state("contact_missing_on_existing_child", n=1, total=6))
        self.assertEqual(got[1], messages.state("contact_present_reference", id="c2", role=""))
        self.assertEqual(got[2], messages.state("contact_present_email", id="c3", role=""))
        self.assertEqual(got[3], messages.state("contact_relation_will_add", id="c4", role=""))
        self.assertEqual(got[4], messages.state("contact_relation_unreadable", id="c5", role=""))
        self.assertEqual(got[5], messages.state("contact_flagged", id="c6", role="",
                                                why=messages.lookup("contact_no_email")[1]))

    def test_bill_payer_states_with_the_flag_in_words(self):
        def line(section):
            return self.lines(plan_of(FOUND, [], section))[1]
        self.assertEqual(line({"status": "would_create"}), messages.state("bill_payer_would_create"))
        self.assertEqual(line({"status": "would_attach"}), messages.state("bill_payer_would_attach"))
        self.assertEqual(line({"status": "present"}), messages.state("bill_payer_present"))
        flagged = line({"status": "flagged", "flag": "bill_payer_ambiguous"})
        self.assertEqual(flagged, messages.state(
            "bill_payer_flagged", why=messages.lookup("bill_payer_ambiguous")[1]))
        self.assertNotIn("bill_payer_ambiguous", flagged)          # words, not the code

    def test_a_child_only_stage_says_what_was_held_back(self):
        plan = plan_of(FOUND, scope="child_only", stage={"name": "Left Nursery"},
                       billPayer={"status": "skipped_stage"})
        self.assertEqual(self.lines(plan)[1], messages.state("scope_child_only", stage="Left Nursery"))

    def test_writes_sentence(self):
        settled = plan_of(FOUND, [contact("c1", "present", True, matchedBy="email", relation="present")],
                          {"status": "present"})
        self.assertEqual(plan_status.planned_writes(settled),
                         {"child": 0, "contact": 0, "relation": 0, "bill_payer": 0})
        self.assertEqual(self.lines(settled)[-1], messages.state("nothing_to_write"))
        flagged = plan_of(FOUND, [contact("c1", "no_email")])
        flagged["holds"] = [{"reason": "contact_no_email"}]
        self.assertEqual(self.lines(flagged)[-1], messages.state("no_writes_planned"))
        writing = plan_of({"status": "child_would_create"}, [contact("c1", "would_create")])
        self.assertNotIn(messages.state("nothing_to_write"), self.lines(writing))


class LivePlanTests(Phase2Case):
    def existing_child(self):
        self.famly.children[CHILD] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": "HDWEST-l",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts[CHILD] = [("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")]

    def plan(self):
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL)

    def test_a_deal_that_is_entirely_in_famly_says_so_and_shows_zero_writes(self):
        self.existing_child()
        plan = self.plan()
        lines = plan_status.block(plan)
        self.assertEqual(lines[-1], messages.state("nothing_to_write"))
        text = notifier.gate_text(plan, plan_hash(plan), "dry")
        self.assertIn("Famly writes planned: 0 child, 0 contact(s)", text)
        self.assertIn(messages.state("nothing_to_write"), text)
        result = dry_runner.run([DEAL], self.hub, FamlyReads(self.famly, site_ids=[SITE_ID]))
        self.assertEqual(result["statusBlocks"][DEAL][-1], messages.state("nothing_to_write"))

    def test_a_state_change_changes_the_plan_hash(self):
        self.existing_child()
        settled = plan_hash(self.plan())
        self.famly.relations["fc-1"] = False                         # a relation is now missing
        missing_relation = plan_hash(self.plan())
        self.assertNotEqual(missing_relation, settled)
        self.famly.contact_refs["fc-1"] = ([CHILD], [("HUBSPOT", "c1")])   # now matched by reference
        self.assertNotEqual(plan_hash(self.plan()), missing_relation)
        self.famly.children.pop(CHILD)                               # the child is gone: would create
        self.assertNotEqual(plan_hash(self.plan()), settled)

    def test_the_test_child_flag_is_in_the_plan_and_the_hash(self):
        plain = self.plan()
        self.mode("live", test=DEAL)
        marked = self.plan()
        self.assertTrue(marked["child"]["testChild"])
        self.assertNotIn("testChild", plain["child"])
        self.assertNotEqual(plan_hash(marked), plan_hash(plain))
        self.assertEqual(plan_status.block(marked)[0], messages.state("child_would_create_test"))

    def test_the_gate_message_holds_no_value_from_the_deal(self):
        self.run_to_approval()
        text = self.notify.gates[0][0]
        self.assertIn(messages.state("child_would_create"), text)
        for value in PII:
            self.assertNotIn(value, text.casefold(), value)


if __name__ == "__main__":
    unittest.main()
