"""Tests for the contact_lookup action (read-only HubSpot id linking).

Run from the project root:

    python -m unittest tests.test_contact_lookup -q

Fakes only, no network. `ROSTER_CACHE_PATH` and `FAMLY_CATALOGUE_FILE` point at
temporary files, so the real roster cache is never touched. The live-row cache
is module-level state, so every test resets it.
"""

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.contact_lookup import runner, web
from actions.pull_roster import runner as pull_roster
from actions.pull_roster import store
from core.client import GraphQLError

logging.disable(logging.CRITICAL)

EXPECTED_KEYS = {
    "contactId",
    "contactMatched",
    "billPayerId",
    "billPayerMatched",
    "source",
}


class FakeClient:
    """Serves one live roster; counts children fetches (one per live fetch)."""

    def __init__(self, contacts=(), bill_payers=(), error=None):
        self.contacts = list(contacts)  # (contact_id, email)
        self.bill_payers = list(bill_payers)  # (bill_payer_id, email)
        self.error = error
        self.fetches = 0

    def execute(self, query_path, variables, operation_name):
        if self.error is not None:
            raise self.error
        if operation_name == pull_roster.CHILDREN_OPERATION_NAME:
            self.fetches += 1
            node = {
                "id": "child-1",
                "externalId": None,
                "contacts": [{"id": i, "email": e} for i, e in self.contacts],
            }
            return {"data": {"children": {"listBySiteIds": {"result": [node], "next": None}}}}
        result = [{"billPayerId": i, "email": e} for i, e in self.bill_payers]
        return {"data": {"billPayers": {"listBySiteIds": {"result": result, "next": None}}}}


class ContactLookupTestCase(unittest.TestCase):
    def setUp(self):
        runner._reset_live_cache_for_tests()
        self.addCleanup(runner._reset_live_cache_for_tests)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "roster_cache.db"
        catalogue = Path(tmp.name) / "catalogue.json"
        catalogue.write_text(
            json.dumps({"sites": {"S1": {"institutionId": "site-1"}}}), encoding="utf-8"
        )
        env = mock.patch.dict(
            os.environ,
            {"ROSTER_CACHE_PATH": str(self.db), "FAMLY_CATALOGUE_FILE": str(catalogue)},
        )
        env.start()
        self.addCleanup(env.stop)

        self.client = FakeClient()
        patch = mock.patch.object(runner, "_live_client", side_effect=lambda: self.client)
        patch.start()
        self.addCleanup(patch.stop)

    def seed(self, contacts=(), bill_payers=()):
        store.replace_institution(
            "S1",
            contacts=[store.ContactRow(id=i, email=e) for i, e in contacts],
            bill_payers=[store.BillPayerRow(id=i, email=e) for i, e in bill_payers],
        )

    def cache_snapshot(self):
        with store._open() as connection:
            return {
                table: [tuple(r) for r in connection.execute(f"SELECT * FROM {table}")]
                for table in ("contacts", "bill_payers")
            }

    def call(self, email="a@x.com"):
        return web.handle({"action": "contact_lookup", "email": email})


class ValidationTests(ContactLookupTestCase):
    def test_bad_or_missing_email_is_422_with_no_client_call(self):
        for bad in (None, "", "   ", 42, "no-at-sign"):
            data, status = self.call(bad)
            self.assertEqual(status, 422, bad)
            self.assertIsNone(data["contactId"])
        self.assertEqual(self.client.fetches, 0)
        self.assertFalse(self.db.exists())


class CacheTests(ContactLookupTestCase):
    def test_case_and_whitespace_variants_match(self):
        self.seed(contacts=[("c-1", "Ada@Example.com")], bill_payers=[("b-1", "ada@example.com ")])
        data, status = self.call("  ADA@example.COM ")
        self.assertEqual(status, 200)
        self.assertEqual((data["contactId"], data["billPayerId"]), ("c-1", "b-1"))

    def test_both_in_cache_is_200_cache_with_no_live_call(self):
        self.seed(contacts=[("c-1", "a@x.com")], bill_payers=[("b-1", "a@x.com")])
        data, status = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "cache")
        self.assertTrue(data["contactMatched"] and data["billPayerMatched"])
        self.assertEqual(self.client.fetches, 0)

    def test_two_rows_with_the_same_id_count_as_one(self):
        self.client.contacts = [("c-1", "a@x.com")]
        # Famly's bill payer list can repeat a payer (it is not de-duplicated).
        self.client.bill_payers = [("b-1", "a@x.com"), ("b-1", "A@X.com")]
        data, status = self.call()
        self.assertEqual((status, data["contactId"], data["billPayerId"]), (200, "c-1", "b-1"))
        self.assertEqual(data["source"], "live")


class LiveFallbackTests(ContactLookupTestCase):
    def test_contact_miss_found_live_is_cache_plus_live_and_cache_unchanged(self):
        self.seed(bill_payers=[("b-1", "a@x.com")])
        before = self.cache_snapshot()
        self.client.contacts = [("c-9", "A@x.com")]

        data, status = self.call()

        self.assertEqual(status, 200)
        self.assertEqual((data["contactId"], data["billPayerId"]), ("c-9", "b-1"))
        self.assertEqual(data["source"], "cache+live")
        self.assertEqual(self.client.fetches, 1)
        self.assertEqual(self.cache_snapshot(), before)

    def test_miss_everywhere_is_200_unmatched_with_a_warning(self):
        data, status = self.call()
        self.assertEqual(status, 200)
        self.assertFalse(data["contactMatched"] or data["billPayerMatched"])
        self.assertEqual(len(data["warnings"]), 2)
        self.assertEqual(data["errors"], [])

    def test_contact_without_bill_payer_is_200_with_a_warning(self):
        self.seed(contacts=[("c-1", "a@x.com")])
        data, status = self.call()
        self.assertEqual(status, 200)
        self.assertEqual((data["contactId"], data["billPayerId"]), ("c-1", None))
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("bill payer", data["warnings"][0])

    def test_live_failure_is_an_error_never_not_found(self):
        self.client.error = GraphQLError("boom", [])
        data, status = self.call()
        self.assertEqual(status, 503)
        self.assertFalse(data["contactMatched"] or data["billPayerMatched"])
        self.assertTrue(data["errors"])
        self.assertEqual(data["warnings"], [])

    def test_two_misses_within_the_ttl_make_one_live_fetch(self):
        self.call("a@x.com")
        self.call("b@x.com")
        self.assertEqual(self.client.fetches, 1)


class AmbiguityTests(ContactLookupTestCase):
    def assert_ambiguous(self, data, status, count_key):
        self.assertEqual(status, 409)
        self.assertIsNone(data["contactId"])
        self.assertIsNone(data["billPayerId"])
        self.assertFalse(data["contactMatched"] or data["billPayerMatched"])
        self.assertIn(count_key, data["errors"][0])

    def test_two_distinct_contact_ids_give_409_even_if_bill_payer_is_unique(self):
        self.seed(
            contacts=[("c-1", "a@x.com"), ("c-2", "a@x.com")],
            bill_payers=[("b-1", "a@x.com")],
        )
        data, status = self.call()
        self.assert_ambiguous(data, status, "contactMatchCount=2")
        self.assertIn("c-1", data["errors"][0])
        self.assertIn("c-2", data["errors"][0])

    def test_two_distinct_bill_payer_ids_give_409_even_if_contact_is_unique(self):
        self.seed(
            contacts=[("c-1", "a@x.com")],
            bill_payers=[("b-1", "a@x.com"), ("b-2", "a@x.com")],
        )
        data, status = self.call()
        self.assert_ambiguous(data, status, "billPayerMatchCount=2")


class ShapeTests(ContactLookupTestCase):
    def test_response_has_no_keys_other_than_the_documented_ones(self):
        self.seed(contacts=[("c-1", "a@x.com")], bill_payers=[("b-1", "a@x.com")])
        for email in ("a@x.com", "nobody@x.com", "bad"):
            data, _ = self.call(email)
            self.assertEqual(set(data) - {"warnings", "errors"}, EXPECTED_KEYS)


if __name__ == "__main__":
    unittest.main()
