"""The HubSpot deal id reaches the stored preview on every route (direct, queue, chain), without changing
the queue's idempotency key or any plan body. Fakes only: the create_timeline and queue harnesses."""

import hashlib
import json
import re
import unittest
from unittest import mock

from actions.create_timeline import chains, engine
from actions.plan_write import hubspot_intake, preview_store
from actions.plan_write import web as plan_write_web
from actions.queue_timeline import jobs
from tests import test_create_timeline as harness
from tests import test_queue_timeline as queue_tests

ID = "5550001"
IDS = {"dealId", "deal_id", "hs_object_id"}


def wrapped(body, **beside):
    """The body the way HubSpot may send it: the plan fields under `properties`, ids beside it."""
    return {"action": "x", "properties": body, **beside}


def old_job_key(child_id, start, payload):
    """The idempotency key exactly as it was computed before the deal id was carried."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{child_id}|{start}|{digest}".encode("utf-8")).hexdigest()


def shape(plan_body):
    """The plan body with its generated ids blanked, so two previews of the same plan compare equal."""
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", json.dumps(plan_body, sort_keys=True))


def stored_deal_ids():
    with preview_store.open_store() as connection:
        return [row[0] for row in connection.execute("SELECT deal_id FROM previews ORDER BY created_at")]


class AllowListTests(unittest.TestCase):
    def test_the_allow_list_gained_exactly_the_three_deal_id_keys(self):
        before = set(engine._PLAN_FIELDS) | set(engine._DISCOUNT_FIELDS) | set(engine.TIMELINE_FIELDS)
        self.assertEqual(engine.ALLOWED_FIELDS - before, IDS)
        self.assertFalse(IDS & set(engine.TIMELINE_FIELDS))              # so segment payloads keep it

    def test_clean_payload_keeps_an_id_only_as_digits_and_nothing_else_leaks(self):
        raw = harness.payload(dealId=" 123 ", deal_id="12a", hs_object_id=456, dealname="x", email="a@b.co",
                              contact_phone="0123")
        stored = engine.clean_payload(raw)
        self.assertEqual({k: stored[k] for k in IDS if k in stored}, {"dealId": "123", "hs_object_id": "456"})
        self.assertTrue(set(stored) <= engine.ALLOWED_FIELDS)
        for leaked in ("dealname", "email", "contact_phone", "child_name", "parent_name"):
            self.assertNotIn(leaked, stored)

    def test_parse_request_trusts_the_id_read_from_the_original_body(self):
        raw = harness.payload(hs_object_id="999")
        self.assertEqual(engine.parse_request(raw, deal_id="123").request.payload["dealId"], "123")
        self.assertFalse(IDS & set(engine.parse_request(raw, deal_id="123").request.payload) - {"dealId"})
        self.assertFalse(IDS & set(engine.parse_request(raw, deal_id=None).request.payload))    # a conflict: none


class DirectRouteTests(harness.ChainTestCase):
    def body(self):
        base = engine.clean_payload(harness.payload())
        segment = {"from": "2026-09-01", "to": "2027-07-31", "funded": False, "advanced": False}
        return engine.segment_payload(base, segment, is_last=True)

    def preview(self, body):
        data, status = plan_write_web.handle(body)
        self.assertEqual(status, 200, data)
        return preview_store.get(data["preview_id"])

    def test_the_id_is_stored_beside_or_inside_the_wrapper_and_the_plan_body_is_unchanged(self):
        plain = self.preview(self.body())
        beside = self.preview(wrapped(self.body(), dealId=ID))
        inside = self.preview({"properties": {**self.body(), "hs_object_id": ID}})
        self.assertEqual((plain.deal_id, beside.deal_id, inside.deal_id), (None, ID, ID))
        self.assertEqual(shape(beside.plan_body), shape(plain.plan_body))      # the id never reaches Famly
        self.assertEqual(shape(inside.plan_body), shape(plain.plan_body))
        self.assertNotIn(ID, json.dumps(beside.plan_body))

    def test_two_different_ids_store_none_and_post_one_notice(self):
        stored = self.preview(wrapped({**self.body(), "hs_object_id": "111"}, dealId="222"))
        self.assertIsNone(stored.deal_id)
        self.assertEqual(len([n for n in self.notices if "plan_flag_deal_id_conflict" in n]), 1)


class ChainRouteTests(harness.ChainTestCase):
    def test_every_plan_of_a_chain_stores_the_id_and_the_stored_payload_keeps_it(self):
        data, status = self.start(wrapped(harness.with_funding(), dealId=ID))
        self.assertEqual(status, 200, data)
        self.assertEqual(self.chain(data).payload["dealId"], ID)
        self.assertTrue(set(self.chain(data).payload) <= engine.ALLOWED_FIELDS)
        self.approve()                                                          # plan 2 is previewed by the advance
        self.approve()
        ids = stored_deal_ids()
        self.assertGreaterEqual(len(ids), 3)
        self.assertEqual(set(ids), {ID})

    def test_a_chain_without_an_id_stores_none(self):
        data, _ = self.start(harness.with_funding())
        self.assertFalse(IDS & set(self.chain(data).payload))
        self.assertEqual(set(stored_deal_ids()), {None})

    def chain(self, data):
        return chains.get(data["chain_id"])


class QueueRouteTests(queue_tests.QueueTestCase):
    def hold_funded(self):
        def hook(node, body):
            if body["plan"].get("publicFundingSettings"):
                node["behaviors"] = queue_tests.WARNING
        self.famly.preview_hook = hook

    def test_the_id_survives_parse_the_worker_segment_build_and_prepare_preview(self):
        self.hold_funded()
        data = self.queue(wrapped(self.body(), dealId=ID))
        self.assertEqual(self.job(data).payload["dealId"], ID)
        self.run_all()
        ids = stored_deal_ids()
        self.assertGreaterEqual(len(ids), 2)                                    # a clean plan and the held one
        self.assertEqual(set(ids), {ID})
        held = preview_store.get(self.previews_posted[-1][1])
        self.assertEqual((held.deal_id, held.job_id), (ID, data["job_id"]))

    def test_the_stored_payload_has_the_id_and_no_names(self):
        data = self.queue(wrapped(self.body(child_name="Alex Smith"), dealId=ID))
        stored = self.job(data).payload
        self.assertEqual({k for k in stored if k in IDS}, {"dealId"})
        self.assertTrue(set(stored) <= engine.ALLOWED_FIELDS)
        self.assertNotIn("child_name", stored)

    def test_two_different_ids_store_no_id_and_post_one_notice(self):
        data = self.queue(wrapped({**self.body(), "hs_object_id": "111"}, dealId="222"))
        self.assertFalse(IDS & set(self.job(data).payload))
        self.assertEqual(len([n for n in self.notices if "plan_flag_deal_id_conflict" in n]), 1)


class IdempotencyKeyTests(queue_tests.QueueTestCase):
    def test_the_key_is_exactly_what_it_was_before_the_id_was_carried(self):
        data = self.queue(wrapped(self.body(), dealId=ID))
        job = self.job(data)
        without = {k: v for k, v in job.payload.items() if k not in IDS}
        self.assertEqual(job.job_key, old_job_key(CHILD := harness.CHILD, "2026-09-01", without))
        self.assertEqual(jobs.job_key(CHILD, "2026-09-01", job.payload), jobs.job_key(CHILD, "2026-09-01", without))

    def test_job_key_is_identical_with_no_id_with_an_id_and_with_a_different_id(self):
        payload = engine.clean_payload(harness.payload())
        keys = {jobs.job_key(harness.CHILD, "2026-09-01", payload),
                jobs.job_key(harness.CHILD, "2026-09-01", {**payload, "dealId": ID}),
                jobs.job_key(harness.CHILD, "2026-09-01", {**payload, "dealId": "777", "hs_object_id": "8"})}
        self.assertEqual(keys, {old_job_key(harness.CHILD, "2026-09-01", payload)})

    def test_a_job_already_queued_or_done_is_unaffected_by_a_resend_that_carries_an_id(self):
        queued = self.queue(self.body())                                         # stored with no id, as before
        again = self.queue(wrapped(self.body(), dealId=ID))
        self.assertEqual((again["job_id"], again["already_queued"]), (queued["job_id"], True))
        self.run_all()
        self.assertEqual(self.job(queued).status, jobs.STATUS_DONE)
        after_done = self.queue(wrapped(self.body(), dealId="777"))
        self.assertEqual((after_done["job_id"], after_done["already_queued"]), (queued["job_id"], True))
        self.assertEqual(len(jobs.list_jobs()), 1)
        self.assertEqual(self.job(queued).job_key, old_job_key(harness.CHILD, "2026-09-01", self.job(queued).payload))

    def test_the_same_request_with_or_without_or_with_another_id_is_the_same_job(self):
        first = self.queue(self.body())
        second = self.queue(wrapped(self.body(), dealId=ID))
        third = self.queue(wrapped(self.body(), dealId="777"))
        self.assertEqual({second["job_id"], third["job_id"]}, {first["job_id"]})
        self.assertTrue(second["already_queued"] and third["already_queued"])
        self.assertEqual(len(jobs.list_jobs()), 1)


class DealIdOfSignatureTests(unittest.TestCase):
    def test_a_non_dict_body_has_no_id(self):
        for body in (None, "text", [1], 5):
            self.assertIsNone(hubspot_intake.deal_id_of(body))


if __name__ == "__main__":
    unittest.main()
