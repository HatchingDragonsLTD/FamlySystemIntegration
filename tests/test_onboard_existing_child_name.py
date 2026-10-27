"""A missing child name holds a deal only when the child is about to be CREATED. For a child that
already exists (deal reference, URN or the deal's famly_childid) it holds nothing and blocks no
contact work. Ids and codes only."""

import unittest

from actions.onboard import store
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.runner import DryRun
from tests.test_onboard_phase2 import DEAL, GROUP_ID, SITE_ID, Phase2Case
from tests import fast_sqlite  # noqa: F401  (SQLite without fsync: see the module)

NAME_DEPENDENT = ("child_name_incomplete", "child_possible_duplicate")
NAME_WARNINGS = ("duplicate_check_skipped", "child_name_unavailable_for_duplicate_check")


class ExistingChildNameCase(Phase2Case):
    def existing_child(self, via):
        """A child already in Famly, found by `via`, with BOTH parents already on it."""
        self.famly.children["child-9"] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": GROUP_ID,
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": ([{"foreignId": DEAL, "system": "HUBSPOT"}] if via == "reference" else [])}
        self.famly.contacts["child-9"] = [("k1", "ada.l@example.org"), ("k2", "ben.b@example.org")]
        self.hub.deals[DEAL]["child_urn"] = "URN-1" if via == "urn" else ""
        self.hub.deals[DEAL]["famly_childid"] = "child-9" if via == "recorded" else ""

    def plan(self):
        return DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL)


class ExistingChildTests(ExistingChildNameCase):
    ROUTES = ("reference", "urn", "recorded")

    def test_an_incomplete_name_holds_nothing_for_an_existing_child_by_any_route(self):
        for route in self.ROUTES:
            for blank in ("child_first_name", "child_last_name"):
                with self.subTest(route=route, blank=blank):
                    self.existing_child(route)
                    self.hub.deals[DEAL][blank] = ""
                    plan = self.plan()
                    self.assertEqual(plan["child"]["status"], "child_found")
                    self.assertEqual(plan["holds"], [])
                    self.assertEqual(plan["status"], "ready")
                    for code in NAME_WARNINGS:
                        self.assertNotIn(code, plan["warnings"])
                    self.hub.deals[DEAL][blank] = "Zara"

    def test_the_job_proceeds_to_the_contacts_and_creates_no_child(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                store._reset_for_tests()
                self.notify.notices.clear()
                self.existing_child(route)
                self.hub.deals[DEAL]["child_first_name"] = ""
                self.hub.deals[DEAL]["child_last_name"] = ""
                self.enqueue()
                result = self.run_worker()                       # nothing to write: no Gate A
                self.assertEqual(result["status"], store.DONE)
                self.assertEqual(self.job().outcomes, ["c1:present", "c2:present"])   # contact work ran
                self.assertEqual(self.famly.child_creates, [])
                self.assertEqual(self.job().child_id, "child-9")

    def test_a_missing_contact_on_the_pipelines_own_child_is_still_created_with_no_child_name(self):
        self.existing_child("reference")
        self.hub.deals[DEAL]["child_first_name"] = ""
        self.famly.contacts["child-9"] = [("k1", "ada.l@example.org")]          # ben is missing
        job = store.enqueue(DEAL, "live").job
        store.update(job.id, child_id="child-9")
        store.mark_child_create_sent(job.id)                                    # an earlier job made it
        store.set_status(job.id, store.NEEDS_REVIEW)
        self.run_through()
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:created"])
        self.assertEqual(self.famly.child_creates, [])


class NewChildTests(ExistingChildNameCase):
    def test_a_new_child_with_an_incomplete_name_still_holds(self):
        for blank in ("child_first_name", "child_last_name"):
            with self.subTest(blank=blank):
                self.hub.deals[DEAL]["child_urn"] = ""
                self.hub.deals[DEAL][blank] = ""
                plan = self.plan()
                self.assertEqual(plan["child"]["status"], "child_would_create")
                self.assertIn("child_name_incomplete", [h["reason"] for h in plan["holds"]])
                self.assertEqual(plan["status"], "held")
                self.hub.deals[DEAL][blank] = "Zara"

    def test_the_worker_holds_a_new_child_and_creates_nothing(self):
        self.hub.deals[DEAL]["child_first_name"] = ""
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.HELD, "child_name_incomplete"))
        self.assertEqual(self.famly.child_creates, [])

    def test_the_other_child_holds_are_unchanged_for_an_existing_child(self):
        self.existing_child("reference")
        self.hub.deals[DEAL]["location_of_interest"] = "Shoreditch"         # a site hold
        self.assertIn("site_unmapped", [h["reason"] for h in self.plan()["holds"]])


if __name__ == "__main__":
    unittest.main()
