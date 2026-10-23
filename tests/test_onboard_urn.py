"""Child URN generation (ONBOARD_URN_GENERATION): format, allocation, the returning-child
guard, the write order and the plan. Ids, counts and codes only."""

import os
import threading
import unittest
from unittest import mock

from actions.onboard import store, urn_alloc
from actions.onboard_dry_run import urn
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.runner import DryRun, plan_hash
from core.hubspot import HubSpotError
from tests.test_onboard_phase2 import DEAL, SITE_ID, Phase2Case

FIRST = "HD0001WM"


class UrnCase(Phase2Case):
    def setUp(self):
        super().setUp()
        os.environ["ONBOARD_URN_GENERATION"] = "on"
        self.addCleanup(os.environ.pop, "ONBOARD_URN_GENERATION", None)
        self.hub.deals[DEAL]["child_urn"] = ""

    def reads(self):
        return FamlyReads(self.famly, site_ids=[SITE_ID])

    def plan(self, **options):
        return DryRun(self.hub, self.reads()).plan_deal(DEAL, urn=options or None)

    def service(self):
        return urn_alloc.UrnService(self.hub, self.reads())

    def sent_urn(self):
        return self.famly.child_creates[0].get("externalId")


class FormatTests(unittest.TestCase):
    def test_format_padding_and_every_site_code(self):
        self.assertEqual(urn.format_urn("CW", 1), "HD0001CW")
        self.assertEqual(urn.format_urn("CL", 42), "HD0042CL")
        self.assertEqual(urn.format_urn("WM", 9999), "HD9999WM")
        self.assertEqual(urn.SITE_URN_CODES, {"HDCW": "CW", "HDCITY": "CL", "HDWEST": "WM"})
        self.assertEqual([urn.site_suffix(c) for c in ("HDCW", "hdcity", "HDWEST", "HDXX")],
                         ["CW", "CL", "WM", None])
        with self.assertRaises(ValueError):
            urn.format_urn("CW", 10000)

    def test_only_the_new_format_is_parsed_and_legacy_is_ignored(self):
        self.assertEqual(urn.parse_urn("HD0007CL"), ("CL", 7))
        for legacy in ("ABC123CD", "HD007CW", "x", "", None, "HD0007CWX"):
            self.assertIsNone(urn.parse_urn(legacy))


class PlanTests(UrnCase):
    def test_a_deal_with_a_urn_in_any_format_is_untouched(self):
        for value in ("ABC123CD", "HD0099CW", "free-form 7"):
            with self.subTest(value=value):
                self.hub.deals[DEAL]["child_urn"] = value
                plan = self.plan(allocate=True, job_id="j")
                self.assertEqual(plan["urn"], {"status": "existing"})
                self.assertIsNone(store.urn_allocation_for_deal(DEAL))

    def test_switch_off_changes_nothing(self):
        os.environ["ONBOARD_URN_GENERATION"] = "off"
        plan = self.plan(allocate=True, job_id="j")
        self.assertEqual(plan["urn"], {"status": "generation_off_missing"})
        self.assertIn("child_urn_missing", plan["warnings"])
        self.assertIsNone(store.urn_allocation_for_deal(DEAL))
        self.assertEqual(self.run_through()["status"], store.DONE)
        self.assertNotIn("externalId", self.famly.child_creates[0])
        self.hub.deals[DEAL]["child_urn"] = "ABC123CD"
        self.assertNotIn("urn", self.plan())               # off + a urn: no key at all

    def test_a_dry_run_never_reserves_and_shows_the_next_number(self):
        for _ in range(2):
            plan = self.plan()
            self.assertEqual(plan["urn"], {"status": "would_allocate", "siteCode": "WM",
                                           "nextNumber": 1, "reserved": False})
        self.assertIsNone(store.urn_allocation_for_deal(DEAL))
        self.assertNotIn("child_urn_missing", plan["warnings"])

    def test_an_unknown_site_holds_and_never_guesses(self):
        with mock.patch.dict(urn.SITE_URN_CODES, clear=True):
            plan = self.plan(allocate=True, job_id="j")
        self.assertEqual(plan["urn"], {"status": "site_unknown"})
        self.assertIn("urn_site_code_unknown", [h["reason"] for h in plan["holds"]])
        self.assertIsNone(store.urn_allocation_for_deal(DEAL))

    def test_a_deal_whose_child_exists_needs_no_urn(self):
        self.famly.children["child-9"] = {
            "name": "x", "externalId": "OLD", "currentGroupId": "g",
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.assertEqual(self.plan(allocate=True, job_id="j")["urn"], {"status": "child_exists"})

    def test_a_plan_with_another_hold_reserves_nothing(self):
        self.hub.deals[DEAL]["child_first_name"] = ""
        plan = self.plan(allocate=True, job_id="j")
        self.assertEqual(plan["urn"]["status"], "would_allocate")
        self.assertIsNone(store.urn_allocation_for_deal(DEAL))


class AllocationTests(UrnCase):
    def test_concurrent_allocations_never_collide(self):
        results, errors = {}, []

        def work(i):
            try:
                results[i] = store.allocate_urn(f"deal-{i}", f"job-{i}", "WM", lambda n: n)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
        threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(n for _site, n in results.values()), list(range(1, 13)))

    def test_allocation_is_idempotent_per_deal_whatever_the_site(self):
        first = store.allocate_urn("d1", "j1", "WM", lambda n: n)
        again = store.allocate_urn("d1", "j2", "WM", lambda n: n)
        other_site = store.allocate_urn("d1", "j3", "CW", lambda n: n)
        self.assertEqual((first, again, other_site), (("WM", 1), ("WM", 1), ("WM", 1)))
        self.assertEqual(store.allocate_urn("d2", "j4", "WM", lambda n: n), ("WM", 2))

    def test_the_next_number_follows_hubspot_and_famly_and_stays_per_site(self):
        self.hub.deals["900"] = {"child_urn": "HD0004WM"}
        self.hub.deals["901"] = {"child_urn": "HD0009CW"}          # another site
        self.hub.deals["902"] = {"child_urn": "OLD12CD"}           # legacy: ignored
        self.famly.children["c-old"] = {"name": "x", "externalId": "HD0006WM", "externalSystemsReferences": []}
        self.assertEqual(self.service().allocate(DEAL, "j", "HDWEST"), "HD0007WM")

    def test_a_candidate_already_used_in_hubspot_or_famly_is_skipped(self):
        service = self.service()
        service.floor = lambda suffix: 0                            # pretend the highest-seen read missed it
        self.hub.deals["900"] = {"child_urn": "HD0001WM"}
        self.famly.children["c-old"] = {"name": "x", "externalId": "HD0002WM", "externalSystemsReferences": []}
        self.assertEqual(service.allocate(DEAL, "j", "HDWEST"), "HD0003WM")


class ReturningGuardTests(UrnCase):
    def test_a_famly_child_at_any_site_with_the_same_name_and_birthday_holds_and_lists_ids_only(self):
        self.famly.site_children = [("child-77", "2024-01-15", "Zara Quillfeather", "HD0009CW")]
        plan = self.plan(allocate=True, job_id="j")
        hold = next(h for h in plan["holds"] if h["reason"] == "child_possible_returning")
        self.assertEqual((hold["childIds"], hold["urns"], hold["dealIds"]), (["child-77"], ["HD0009CW"], []))
        self.assertEqual(plan["urn"], {"status": "candidate_returning"})   # (the same-site duplicate hold also fires)
        self.assertIsNone(store.urn_allocation_for_deal(DEAL))
        text = str(plan)
        self.assertNotIn("Zara", text)
        self.assertNotIn("Quillfeather", text)

    def test_another_hubspot_deal_with_a_urn_holds_but_the_deal_itself_is_ignored(self):
        self.hub.deals["555"] = dict(self.hub.deals[DEAL], child_urn="HD0003CL")
        plan = self.plan(allocate=True, job_id="j")
        hold = next(h for h in plan["holds"] if h["reason"] == "child_possible_returning")
        self.assertEqual((hold["dealIds"], hold["urns"]), (["555"], ["HD0003CL"]))
        del self.hub.deals["555"]
        self.hub.deals[DEAL]["child_urn"] = ""                       # only the deal itself matches now
        self.assertEqual(self.plan(allocate=True, job_id="j")["urn"]["status"], "allocated")

    def test_the_deals_own_earlier_allocation_is_not_a_candidate(self):
        first = self.plan(allocate=True, job_id="j1")
        self.assertEqual(first["urn"]["urn"], FIRST)
        self.hub.deals["555"] = dict(self.hub.deals[DEAL], child_urn=FIRST)   # carries OUR number
        again = self.plan(allocate=True, job_id="j2")
        self.assertEqual(again["urn"], {"status": "allocated", "urn": FIRST, "reserved": True})
        self.assertEqual(again["holds"], [])

    def test_new_urn_skips_the_check_for_that_one_job_only(self):
        self.hub.deals["555"] = dict(self.hub.deals[DEAL], child_urn="HD0003CL")
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.HELD)
        self.assertEqual(self.job().last_error_code, "child_possible_returning")
        flagged = store.enqueue(DEAL, "live", new_urn=True).job
        self.assertTrue(flagged.new_urn)
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertEqual(self.job().id, flagged.id)
        later = store.enqueue("2002", "live").job
        self.assertFalse(later.new_urn)


class WorkerTests(UrnCase):
    def test_the_approved_urn_is_written_to_hubspot_then_sent_to_famly(self):
        self.run_to_approval()
        text = self.notify.gates[-1][0]
        self.assertIn(FIRST, text)
        self.assertIn("NEW URN", text)
        self.assertIn("Famly writes planned: 1 child", text)
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(self.sent_urn(), FIRST)
        self.assertEqual(self.hub.deals[DEAL]["child_urn"], FIRST)
        order = [e for e in self.events if e == ("hubspot", "child_urn") or e == "child_create"]
        self.assertEqual(order, [("hubspot", "child_urn"), "child_create"])
        self.assertIn("urn_allocated", self.notify.notices[-1])

    def test_the_urn_is_part_of_the_plan_hash(self):
        dry = self.plan()
        a = self.plan(allocate=True, job_id="j")
        b = {**a, "urn": {**a["urn"], "urn": "HD0002WM"}}
        self.assertNotEqual(plan_hash(a), plan_hash(b))
        self.assertNotEqual(plan_hash(dry), plan_hash(a))                  # would_allocate vs allocated

    def test_a_hubspot_write_failure_stops_before_famly_and_a_retry_reuses_the_urn(self):
        self.hub.fail_props = {"child_urn"}
        result = self.run_through()
        self.assertEqual((result["status"], result["code"]), (store.NEEDS_REVIEW, "urn_hubspot_write_failed"))
        self.assertEqual(self.famly.child_creates, [])
        self.assertEqual(store.urn_allocation_for_deal(DEAL), ("WM", 1))
        self.hub.fail_props = set()
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.sent_urn(), FIRST)
        self.assertEqual(store.urn_max_allocated("WM"), 1)                  # one allocation, ever

    def test_a_write_that_does_not_read_back_is_a_failure(self):
        real = self.hub.write_deal_properties

        def silent(deal_id, properties):                                    # accepted, but nothing stored
            if "child_urn" not in properties:
                real(deal_id, properties)
        self.hub.write_deal_properties = silent
        result = self.run_through()
        self.assertEqual(result["code"], "urn_hubspot_write_failed")
        self.assertEqual(self.famly.child_creates, [])

    def test_a_deal_that_gains_a_different_urn_before_the_write_stops(self):
        self.run_to_approval()
        self.hub.deals[DEAL]["child_urn"] = "HD0050WM"                      # a person set one meanwhile
        self.approve()
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)         # plan changed: asked again
        self.assertEqual(self.famly.child_creates, [])

    def test_the_readback_of_the_urn_is_a_flag_not_a_stop(self):
        def hide(n, ids):
            return []
        self.famly.post_create_script = None
        with mock.patch("actions.onboard.worker._urn_readback_flags", return_value=["urn_readback_mismatch"]):
            result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertIn("could not be confirmed on the Famly child", self.written()["famly_sync_gaps"])


if __name__ == "__main__":
    unittest.main()
