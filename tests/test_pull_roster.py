"""Tests for the pull-roster maintenance command (children/contacts/bill payers).

Run from the project root:

    python -m unittest discover -s tests -v

No real Famly, no network: `GraphQLClient` is stubbed throughout via a fake
whose `.execute()` returns canned pages. `FAMLY_CATALOGUE_FILE` and
`ROSTER_CACHE_PATH` point at temporary files for every test, so the real
reference file and the real (gitignored) roster cache are never touched.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.pull_roster import runner as pull_roster
from actions.pull_roster import store
from core.client import GraphQLError, GraphQLHTTPError
from tests import fast_sqlite  # noqa: F401  (SQLite without fsync: see the module)

logging.disable(logging.CRITICAL)

CITY_ID = "11111111-1111-1111-1111-111111111111"
CW_ID = "22222222-2222-2222-2222-222222222222"

CATALOGUE = {
    "sites": {
        "HDCITY": {"label": "City", "institutionId": CITY_ID},
        "HDCW": {"label": "Canada Water", "institutionId": CW_ID},
    }
}


def child_node(child_id, external_id, contacts=None):
    return {
        "id": child_id,
        "externalId": external_id,
        "contacts": contacts or [],
    }


def bill_payer_node(bill_payer_id, email):
    return {"billPayerId": bill_payer_id, "email": email}


class FakeGraphQLClient:
    """Serves canned pages per operation name, recording every call.

    `children_pages`/`bill_payer_pages` are queues consumed in order, one per
    call to that operation -- lets a test script exactly what each page in a
    paginated sequence returns.
    """

    def __init__(self, children_pages=None, bill_payer_pages=None, error=None):
        self._children_pages = list(children_pages or [])
        self._bill_payer_pages = list(bill_payer_pages or [])
        self._error = error
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append((operation_name, dict(variables or {})))
        if self._error is not None:
            raise self._error

        if operation_name == pull_roster.CHILDREN_OPERATION_NAME:
            page = self._children_pages.pop(0)
            return {"data": {"children": {"listBySiteIds": page}}}

        if operation_name == pull_roster.BILL_PAYERS_OPERATION_NAME:
            page = self._bill_payer_pages.pop(0)
            return {"data": {"billPayers": {"listBySiteIds": page}}}

        raise AssertionError(f"unexpected operation {operation_name!r}")


class PullRosterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.catalogue_file = self.tmp / "catalogue.json"
        self.catalogue_file.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        self.store_file = self.tmp / "roster_cache.db"

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(self.catalogue_file),
                "ROSTER_CACHE_PATH": str(self.store_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)


# --------------------------------------------------------------------------- #
# Fetch + pagination
# --------------------------------------------------------------------------- #
class FetchChildrenAndContactsTests(PullRosterTestCase):
    def test_a_single_page_needs_only_one_call(self):
        client = FakeGraphQLClient(
            children_pages=[
                {
                    "result": [
                        child_node("c-1", "urn-1", [{"id": "ct-1", "email": "a@x.com"}])
                    ],
                    "next": None,
                }
            ]
        )

        children, contacts = pull_roster.fetch_children_and_contacts([CITY_ID], client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual([c.id for c in children], ["c-1"])
        self.assertEqual([c.email for c in contacts], ["a@x.com"])

    def test_pagination_is_followed_correctly(self):
        client = FakeGraphQLClient(
            children_pages=[
                {"result": [child_node("c-1", "urn-1")], "next": "page-2"},
                {"result": [child_node("c-2", "urn-2")], "next": "page-3"},
                {"result": [child_node("c-3", "urn-3")], "next": None},
            ]
        )

        children, _ = pull_roster.fetch_children_and_contacts([CITY_ID], client)

        cursors = [variables["next"] for _op, variables in client.calls]
        self.assertEqual(cursors, [None, "page-2", "page-3"])
        self.assertEqual([c.id for c in children], ["c-1", "c-2", "c-3"])

    def test_contacts_shared_across_children_are_deduplicated(self):
        shared_contact = {"id": "ct-shared", "email": "parent@x.com"}
        client = FakeGraphQLClient(
            children_pages=[
                {
                    "result": [
                        child_node("c-1", "urn-1", [shared_contact]),
                        child_node("c-2", "urn-2", [shared_contact]),
                    ],
                    "next": None,
                }
            ]
        )

        _, contacts = pull_roster.fetch_children_and_contacts([CITY_ID], client)

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].id, "ct-shared")

    def test_a_graphql_error_is_surfaced_not_swallowed(self):
        client = FakeGraphQLClient(error=GraphQLError("boom", errors=[{"message": "boom"}]))

        with self.assertRaises(GraphQLError):
            pull_roster.fetch_children_and_contacts([CITY_ID], client)


class FetchBillPayersTests(PullRosterTestCase):
    def test_pagination_is_followed_correctly(self):
        client = FakeGraphQLClient(
            bill_payer_pages=[
                {"result": [bill_payer_node("bp-1", "a@x.com")], "next": "page-2"},
                {"result": [bill_payer_node("bp-2", "b@x.com")], "next": None},
            ]
        )

        rows = pull_roster.fetch_bill_payers([CITY_ID], client)

        cursors = [variables["next"] for _op, variables in client.calls]
        self.assertEqual(cursors, [None, "page-2"])
        self.assertEqual([r.id for r in rows], ["bp-1", "bp-2"])

    def test_a_bill_payer_with_no_email_is_still_kept(self):
        # email is nullable on the public schema -- absence is not an error.
        client = FakeGraphQLClient(
            bill_payer_pages=[{"result": [bill_payer_node("bp-1", None)], "next": None}]
        )

        rows = pull_roster.fetch_bill_payers([CITY_ID], client)

        self.assertEqual(rows[0].id, "bp-1")
        self.assertIsNone(rows[0].email)

    def test_an_http_error_is_surfaced_not_swallowed(self):
        client = FakeGraphQLClient(error=GraphQLHTTPError("down", 502, "nope"))

        with self.assertRaises(GraphQLHTTPError):
            pull_roster.fetch_bill_payers([CITY_ID], client)


# --------------------------------------------------------------------------- #
# pull_all: per-institution isolation, --institution filter
# --------------------------------------------------------------------------- #
def _one_page_client(children=None, bill_payers=None):
    return FakeGraphQLClient(
        children_pages=[{"result": children or [], "next": None}] * 2,
        bill_payer_pages=[{"result": bill_payers or [], "next": None}] * 2,
    )


class PullAllTests(PullRosterTestCase):
    def test_every_institution_is_pulled_by_default(self):
        client = _one_page_client(
            children=[child_node("c-1", "urn-1")], bill_payers=[bill_payer_node("bp-1", "a@x.com")]
        )

        result = pull_roster.pull_all(client=client)

        self.assertEqual(sorted(result.institutions_pulled), ["HDCITY", "HDCW"])
        self.assertEqual(len(result.rosters["HDCITY"].children), 1)
        self.assertEqual(result.institutions_failed, {})

    def test_an_unknown_institution_code_raises_with_zero_api_calls(self):
        client = _one_page_client()

        with self.assertRaises(pull_roster.UnknownInstitutionError) as ctx:
            pull_roster.pull_all(client=client, institutions=["NOPE"])

        self.assertIn("NOPE", str(ctx.exception))
        self.assertEqual(client.calls, [])

    def test_filtering_restricts_which_institutions_are_pulled(self):
        client = _one_page_client(children=[child_node("c-1", "urn-1")])

        result = pull_roster.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(result.institutions_pulled, ["HDCITY"])
        self.assertNotIn("HDCW", result.rosters)

    def test_a_site_with_no_institution_id_is_skipped_with_zero_api_calls(self):
        self.catalogue_file.write_text(
            json.dumps({"sites": {"HDNONE": {"label": "No institution yet"}}}),
            encoding="utf-8",
        )

        client = _one_page_client()
        result = pull_roster.pull_all(client=client)

        self.assertIn("HDNONE", result.institutions_skipped)
        self.assertEqual(client.calls, [])

    def test_one_institution_failing_does_not_stop_the_others(self):
        class SelectiveFailure:
            def __init__(self):
                self.calls = []

            def execute(self, query_path, variables, operation_name):
                self.calls.append((operation_name, dict(variables)))
                if variables.get("siteIds") == [CITY_ID]:
                    raise GraphQLError("boom", errors=[{"message": "boom"}])
                if operation_name == pull_roster.CHILDREN_OPERATION_NAME:
                    return {"data": {"children": {"listBySiteIds": {"result": [], "next": None}}}}
                return {"data": {"billPayers": {"listBySiteIds": {"result": [], "next": None}}}}

        result = pull_roster.pull_all(client=SelectiveFailure())

        self.assertIn("HDCITY", result.institutions_failed)
        self.assertEqual(result.institutions_pulled, ["HDCW"])


# --------------------------------------------------------------------------- #
# write_store: backup, per-institution replace-not-wipe
# --------------------------------------------------------------------------- #
class WriteStoreTests(PullRosterTestCase):
    def test_a_backup_is_created_before_the_existing_file_is_overwritten(self):
        store.replace_institution(
            "HDCITY", children=[store.ChildRow(id="old-1", external_id="old-urn")]
        )
        self.assertTrue(self.store_file.exists())

        client = _one_page_client(children=[child_node("c-new", "urn-new")])
        result = pull_roster.pull_all(client=client, institutions=["HDCITY"])
        backup = pull_roster.write_store(result, path=self.store_file)

        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())

        import sqlite3

        old_conn = sqlite3.connect(backup)
        old_rows = old_conn.execute("SELECT famly_id FROM children").fetchall()
        old_conn.close()
        self.assertEqual([r[0] for r in old_rows], ["old-1"])

    def test_no_backup_when_there_was_no_existing_file(self):
        client = _one_page_client()
        result = pull_roster.pull_all(client=client)

        self.assertFalse(self.store_file.exists())
        backup = pull_roster.write_store(result, path=self.store_file)

        self.assertIsNone(backup)
        self.assertTrue(self.store_file.exists())

    def test_refreshing_one_institution_leaves_another_untouched(self):
        store.replace_institution(
            "HDCW", children=[store.ChildRow(id="cw-1", external_id="cw-urn")]
        )

        client = _one_page_client(children=[child_node("c-1", "urn-1")])
        result = pull_roster.pull_all(client=client, institutions=["HDCITY"])
        pull_roster.write_store(result, path=self.store_file)

        self.assertEqual(store.find_child_by_external_id("cw-urn")[0]["famly_id"], "cw-1")
        self.assertEqual(store.find_child_by_external_id("urn-1")[0]["famly_id"], "c-1")

    def test_dry_run_never_writes_anything(self):
        client = _one_page_client(children=[child_node("c-1", "urn-1")])
        result = pull_roster.pull_all(client=client)

        # Simulating --dry-run: the CLI simply never calls write_store.
        self.assertFalse(self.store_file.exists())
        self.assertEqual(store.find_child_by_external_id("urn-1"), [])


# --------------------------------------------------------------------------- #
# store.py: schema, replace semantics, normalized lookups
# --------------------------------------------------------------------------- #
class StoreLookupTests(PullRosterTestCase):
    def test_find_child_by_external_id_normalizes_case_and_whitespace(self):
        store.replace_institution(
            "HDCITY", children=[store.ChildRow(id="c-1", external_id="  URN-Abc123  ")]
        )

        matches = store.find_child_by_external_id(" urn-abc123 ")
        self.assertEqual([m["famly_id"] for m in matches], ["c-1"])

    def test_find_contact_by_email_normalizes_case_and_whitespace(self):
        store.replace_institution(
            "HDCITY", contacts=[store.ContactRow(id="ct-1", email="Parent@Example.com")]
        )

        matches = store.find_contact_by_email("  parent@example.com  ")
        self.assertEqual([m["famly_id"] for m in matches], ["ct-1"])

    def test_find_bill_payer_by_email_normalizes_case_and_whitespace(self):
        store.replace_institution(
            "HDCITY", bill_payers=[store.BillPayerRow(id="bp-1", email="Payer@Example.com")]
        )

        matches = store.find_bill_payer_by_email("PAYER@EXAMPLE.COM")
        self.assertEqual([m["famly_id"] for m in matches], ["bp-1"])

    def test_no_match_returns_an_empty_list(self):
        self.assertEqual(store.find_child_by_external_id("does-not-exist"), [])
        self.assertEqual(store.find_contact_by_email("nobody@example.com"), [])
        self.assertEqual(store.find_bill_payer_by_email("nobody@example.com"), [])

    def test_an_empty_or_none_query_matches_nothing(self):
        store.replace_institution(
            "HDCITY", children=[store.ChildRow(id="c-1", external_id="")]
        )
        self.assertEqual(store.find_child_by_external_id(""), [])
        self.assertEqual(store.find_child_by_external_id(None), [])

    def test_replace_institution_deletes_that_institutions_stale_rows(self):
        store.replace_institution(
            "HDCITY", children=[store.ChildRow(id="c-old", external_id="old")]
        )
        store.replace_institution(
            "HDCITY", children=[store.ChildRow(id="c-new", external_id="new")]
        )

        self.assertEqual(store.find_child_by_external_id("old"), [])
        self.assertEqual(store.find_child_by_external_id("new")[0]["famly_id"], "c-new")

    def test_a_row_with_no_id_is_skipped(self):
        store.replace_institution(
            "HDCITY",
            children=[store.ChildRow(id=None, external_id="urn-1")],
            contacts=[store.ContactRow(id=None, email="a@x.com")],
            bill_payers=[store.BillPayerRow(id=None, email="b@x.com")],
        )

        self.assertEqual(store.find_child_by_external_id("urn-1"), [])
        self.assertEqual(store.find_contact_by_email("a@x.com"), [])
        self.assertEqual(store.find_bill_payer_by_email("b@x.com"), [])


# --------------------------------------------------------------------------- #
# The hard rule: this cache must never back a pre-creation existence check.
# --------------------------------------------------------------------------- #
class ExistenceCheckGuardTests(unittest.TestCase):
    def test_the_module_documents_the_rule_prominently(self):
        # A documentation-presence check: the warning must not be silently
        # deleted later, since there is no creation pipeline yet to test
        # against directly.
        doc = (store.__doc__ or "").lower()
        self.assertIn("must never be used", doc)
        self.assertIn("existence check", doc)
        self.assertIn("live famly call", doc)

    @unittest.skip(
        "TODO: once actions/<contact-or-child-creation-pipeline> exists, assert "
        "here that its existence check calls Famly directly and does NOT import "
        "or call actions.pull_roster.store.find_child_by_external_id / "
        "find_contact_by_email / find_bill_payer_by_email."
    )
    def test_the_creation_pipelines_existence_check_does_not_use_this_cache(self):
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# The roster must include PAST and FUTURE children.
#
# Famly's `children.listBySiteIds` returns only children enrolled TODAY unless
# it is given a `timeRange` (found 2026-10-14: 168 children by default, 292 with
# a range). The fake below behaves like the real API in that respect, so these
# tests fail against the old code rather than just checking a variable.
# --------------------------------------------------------------------------- #
class TimeAwareClient:
    """Like Famly: current children only, unless a timeRange is supplied."""

    def __init__(self, current, extra=()):
        self.current = list(current)
        self.extra = list(extra)
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append((operation_name, dict(variables or {})))
        if operation_name == pull_roster.BILL_PAYERS_OPERATION_NAME:
            return {"data": {"billPayers": {"listBySiteIds": {"result": [], "next": None}}}}

        rows = list(self.current)
        if (variables or {}).get("timeRange"):
            rows += self.extra
        return {"data": {"children": {"listBySiteIds": {"result": rows, "next": None}}}}


class TimeRangeTests(PullRosterTestCase):
    def test_a_child_who_has_left_or_not_started_is_pulled(self):
        client = TimeAwareClient(
            current=[child_node("c-now", "urn-now")],
            extra=[child_node("c-past", "urn-past"), child_node("c-future", "urn-future")],
        )

        children, _ = pull_roster.fetch_children_and_contacts([CITY_ID], client)

        self.assertEqual({c.id for c in children}, {"c-now", "c-past", "c-future"})

    def test_every_page_asks_for_the_time_range(self):
        client = FakeGraphQLClient(
            children_pages=[
                {"result": [child_node("c-1", "urn-1")], "next": "page-2"},
                {"result": [child_node("c-2", "urn-2")], "next": None},
            ]
        )

        pull_roster.fetch_children_and_contacts([CITY_ID], client)

        for _op, variables in client.calls:
            self.assertEqual(variables["timeRange"], pull_roster.ROSTER_TIME_RANGE)

    def test_the_range_is_wide_enough_and_well_formed(self):
        from datetime import date

        start = date.fromisoformat(pull_roster.ROSTER_TIME_RANGE["from"])
        end = date.fromisoformat(pull_roster.ROSTER_TIME_RANGE["to"])

        self.assertLess(start, date(2010, 1, 1))  # every child ever enrolled
        self.assertGreater(end, date(2060, 1, 1))  # every child yet to start
        self.assertLess(start, end)

    def test_the_query_declares_and_passes_the_time_range(self):
        # The bug was the ABSENCE of this argument. Fails if it is ever removed.
        text = pull_roster.CHILDREN_QUERY_PATH.read_text(encoding="utf-8")

        self.assertIn("$timeRange: NonEmptyLocalDateRangeInput", text)
        self.assertIn("timeRange: $timeRange", text)

    def test_pull_all_includes_them_for_every_institution(self):
        client = TimeAwareClient(
            current=[child_node("c-now", "urn-now")],
            extra=[child_node("c-past", "urn-past")],
        )

        result = pull_roster.pull_all(client)

        for code in ("HDCITY", "HDCW"):
            self.assertEqual(
                {c.id for c in result.rosters[code].children}, {"c-now", "c-past"}
            )

    def test_one_institution_failing_still_does_not_stop_the_others(self):
        # Failure isolation is unchanged by the range.
        class FailsForCity(TimeAwareClient):
            def execute(self, query_path, variables, operation_name):
                if CITY_ID in (variables or {}).get("siteIds", []):
                    raise GraphQLError("boom", errors=[{"message": "boom"}])
                return super().execute(query_path, variables, operation_name)

        client = FailsForCity(current=[child_node("c-1", "urn-1")], extra=[child_node("c-2", "urn-2")])

        result = pull_roster.pull_all(client)

        self.assertIn("HDCITY", result.institutions_failed)
        self.assertEqual(result.institutions_pulled, ["HDCW"])
        self.assertEqual({c.id for c in result.rosters["HDCW"].children}, {"c-1", "c-2"})

    def test_past_and_future_children_reach_the_store_and_match(self):
        client = TimeAwareClient(
            current=[child_node("c-now", "urn-now")],
            extra=[child_node("c-past", "URN-Past ")],
        )

        pull_roster.write_store(pull_roster.pull_all(client), self.store_file)

        found = store.find_child_by_external_id("urn-past", self.store_file)
        self.assertEqual([row["famly_id"] for row in found], ["c-past"])


class MergeByIdTests(PullRosterTestCase):
    def test_a_child_repeated_across_pages_is_one_row(self):
        client = FakeGraphQLClient(
            children_pages=[
                {"result": [child_node("c-1", "urn-1")], "next": "page-2"},
                {"result": [child_node("c-1", "urn-1"), child_node("c-2", "urn-2")], "next": None},
            ]
        )

        children, _ = pull_roster.fetch_children_and_contacts([CITY_ID], client)

        self.assertEqual([c.id for c in children], ["c-1", "c-2"])

    def test_a_child_returned_for_two_site_ids_is_one_row(self):
        # Two sites asked for together, one child related to both.
        client = FakeGraphQLClient(
            children_pages=[
                {
                    "result": [child_node("c-1", "urn-1"), child_node("c-1", "urn-1")],
                    "next": None,
                }
            ]
        )

        children, _ = pull_roster.fetch_children_and_contacts([CITY_ID, CW_ID], client)

        self.assertEqual([c.id for c in children], ["c-1"])

    def test_different_children_sharing_a_urn_are_not_merged(self):
        client = FakeGraphQLClient(
            children_pages=[
                {
                    "result": [child_node("c-1", "urn-same"), child_node("c-2", "urn-same")],
                    "next": None,
                }
            ]
        )

        children, _ = pull_roster.fetch_children_and_contacts([CITY_ID], client)

        self.assertEqual([c.id for c in children], ["c-1", "c-2"])

    def test_a_duplicate_fills_a_blank_but_never_overwrites(self):
        merged = pull_roster.merge_by_id(
            [
                store.ChildRow(id="c-1", external_id=None),
                store.ChildRow(id="c-1", external_id="urn-1"),
                store.ChildRow(id="c-2", external_id="urn-2"),
                store.ChildRow(id="c-2", external_id="urn-OTHER"),
            ]
        )

        self.assertEqual([(c.id, c.external_id) for c in merged], [("c-1", "urn-1"), ("c-2", "urn-2")])

    def test_a_child_with_no_id_is_still_dropped_from_the_fetch(self):
        # Unchanged behaviour: a row the store cannot key is not kept.
        client = FakeGraphQLClient(
            children_pages=[{"result": [child_node(None, "urn-x"), child_node("c-1", "urn-1")], "next": None}]
        )

        children, _ = pull_roster.fetch_children_and_contacts([CITY_ID], client)

        self.assertEqual([c.id for c in children], ["c-1"])

    def test_a_child_in_two_institutions_is_one_row_at_rest(self):
        # Pins the store's existing guarantee, which the wider range makes
        # more likely to matter: a child who left one site and joined another
        # is returned by both, and must not be stored (or matched) twice.
        client = TimeAwareClient(current=[child_node("c-1", "urn-1")])

        pull_roster.write_store(pull_roster.pull_all(client), self.store_file)

        self.assertEqual(
            len(store.find_child_by_external_id("urn-1", self.store_file)), 1
        )


if __name__ == "__main__":
    unittest.main()
