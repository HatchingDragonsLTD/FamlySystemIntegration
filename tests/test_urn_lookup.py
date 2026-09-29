"""Tests for the urn_lookup TEMPORARY backfill action.

Run from the project root:

    python -m unittest discover -s tests -v

No real Famly, no network: `GraphQLClient` is stubbed throughout via a fake
whose `.execute()` returns canned pages.

The roster cache is MODULE-LEVEL, process-global state (see runner.py), so
every test resets it in setUp -- otherwise one test's fetch would leak into
the next and make results order-dependent.
"""

import logging
import os
import unittest
from unittest import mock

from actions.urn_lookup import runner, web
from core.client import GraphQLError

logging.disable(logging.CRITICAL)


def child_node(child_id, external_id, full_name):
    return {
        "id": child_id,
        "externalId": external_id,
        "name": {"fullName": full_name},
    }


def page_body(children, next_cursor=None):
    return {
        "data": {
            "children": {
                "list": {
                    "result": children,
                    "next": next_cursor,
                }
            }
        }
    }


class FakeGraphQLClient:
    """Returns one canned page per call, in order. Records every call's variables."""

    def __init__(self, pages=None, error=None):
        self._pages = list(pages or [])
        self._error = error
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append(dict(variables or {}))
        if self._error is not None:
            raise self._error
        if not self._pages:
            return page_body([])
        return self._pages.pop(0)


class UrnLookupTestCase(unittest.TestCase):
    def setUp(self):
        runner._reset_cache_for_tests()
        self.addCleanup(runner._reset_cache_for_tests)


# --------------------------------------------------------------------------- #
# Matching: exact, normalized, zero, and multiple matches.
# --------------------------------------------------------------------------- #
class FindMatchesTests(UrnLookupTestCase):
    def test_an_exact_match_is_found(self):
        roster = [runner.ChildRow(id="c-1", external_id="urn:deal:123", full_name="Ada")]
        matches = runner.find_matches("urn:deal:123", roster)
        self.assertEqual([m.id for m in matches], ["c-1"])

    def test_mismatched_case_and_whitespace_still_match(self):
        roster = [runner.ChildRow(id="c-1", external_id="  URN:Deal:123  ", full_name="Ada")]
        matches = runner.find_matches(" urn:deal:123 ", roster)
        self.assertEqual([m.id for m in matches], ["c-1"])

    def test_zero_matches_returns_an_empty_list(self):
        roster = [runner.ChildRow(id="c-1", external_id="urn:deal:999", full_name="Ada")]
        self.assertEqual(runner.find_matches("urn:deal:123", roster), [])

    def test_multiple_matches_are_all_returned(self):
        roster = [
            runner.ChildRow(id="c-1", external_id="urn:deal:123", full_name="Ada"),
            runner.ChildRow(id="c-2", external_id="URN:DEAL:123", full_name="Bea"),
        ]
        matches = runner.find_matches("urn:deal:123", roster)
        self.assertEqual(sorted(m.id for m in matches), ["c-1", "c-2"])

    def test_children_with_no_external_id_are_skipped(self):
        roster = [
            runner.ChildRow(id="c-1", external_id=None, full_name="Ada"),
            runner.ChildRow(id="c-2", external_id="", full_name="Bea"),
        ]
        self.assertEqual(runner.find_matches("", roster), [])
        self.assertEqual(runner.find_matches("urn:deal:123", roster), [])


# --------------------------------------------------------------------------- #
# fetch_roster: pagination and error propagation.
# --------------------------------------------------------------------------- #
class FetchRosterTests(UrnLookupTestCase):
    def test_a_single_page_needs_only_one_call(self):
        client = FakeGraphQLClient(
            pages=[page_body([child_node("c-1", "urn:1", "Ada")])]
        )

        rows = runner.fetch_roster(client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual([r.id for r in rows], ["c-1"])

    def test_pagination_across_multiple_pages_is_followed_correctly(self):
        client = FakeGraphQLClient(
            pages=[
                page_body([child_node("c-1", "urn:1", "Ada")], next_cursor="page-2"),
                page_body([child_node("c-2", "urn:2", "Bea")], next_cursor="page-3"),
                page_body([child_node("c-3", "urn:3", "Cy")], next_cursor=None),
            ]
        )

        rows = runner.fetch_roster(client=client)

        self.assertEqual(len(client.calls), 3)
        # The first call carries no cursor; each later call carries the PREVIOUS
        # page's cursor.
        self.assertIsNone(client.calls[0]["next"])
        self.assertEqual(client.calls[1]["next"], "page-2")
        self.assertEqual(client.calls[2]["next"], "page-3")
        self.assertEqual([r.id for r in rows], ["c-1", "c-2", "c-3"])

    def test_an_empty_next_cursor_stops_pagination(self):
        client = FakeGraphQLClient(
            pages=[page_body([child_node("c-1", "urn:1", "Ada")], next_cursor="")]
        )

        rows = runner.fetch_roster(client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual([r.id for r in rows], ["c-1"])

    def test_a_graphql_error_is_surfaced_not_swallowed(self):
        error = GraphQLError(
            "GraphQL request 'Children' returned errors: "
            "Variable '$something' of required type ... was not provided",
            errors=[{"message": "required variable missing"}],
        )
        client = FakeGraphQLClient(error=error)

        with self.assertRaises(GraphQLError) as ctx:
            runner.fetch_roster(client=client)

        self.assertIs(ctx.exception, error)
        self.assertIn("required", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Caching: reused within the TTL, refetched once it expires. Parametrized via
# the env var rather than tied to whatever DEFAULT_CACHE_TTL_SECONDS happens
# to be -- these tests only ever set/force staleness relative to whatever TTL
# is configured, never a hardcoded number of seconds.
# --------------------------------------------------------------------------- #
class CacheTests(UrnLookupTestCase):
    def test_the_cache_is_reused_within_the_ttl(self):
        with mock.patch.dict("os.environ", {"URN_LOOKUP_CACHE_TTL_SECONDS": "60"}):
            client = FakeGraphQLClient(
                pages=[page_body([child_node("c-1", "urn:1", "Ada")])]
            )

            first = runner.get_roster(client=client)
            second = runner.get_roster(client=client)

        self.assertEqual(len(client.calls), 1)  # only ONE fetch across two lookups
        self.assertEqual(first, second)

    def test_the_cache_refetches_once_the_ttl_expires(self):
        with mock.patch.dict("os.environ", {"URN_LOOKUP_CACHE_TTL_SECONDS": "60"}):
            client = FakeGraphQLClient(
                pages=[
                    page_body([child_node("c-1", "urn:1", "Ada")]),
                    page_body([child_node("c-1", "urn:1", "Ada")]),
                ]
            )

            runner.get_roster(client=client)
            # Force staleness directly, relative to whatever TTL is
            # configured, rather than sleeping or mocking time.monotonic()
            # call-by-call.
            runner._cache_fetched_at -= runner.cache_ttl_seconds() + 1
            runner.get_roster(client=client)

        self.assertEqual(len(client.calls), 2)

    def test_force_refresh_bypasses_a_fresh_cache(self):
        client = FakeGraphQLClient(
            pages=[
                page_body([child_node("c-1", "urn:1", "Ada")]),
                page_body([child_node("c-1", "urn:1", "Ada")]),
            ]
        )

        runner.get_roster(client=client)
        runner.get_roster(client=client, force_refresh=True)

        self.assertEqual(len(client.calls), 2)


class CacheTtlConfigTests(unittest.TestCase):
    """The TTL itself: default, env override, and a malformed value."""

    def test_the_default_is_900_seconds(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop("URN_LOOKUP_CACHE_TTL_SECONDS", None)
            self.assertEqual(runner.cache_ttl_seconds(), 900)
            self.assertEqual(runner.DEFAULT_CACHE_TTL_SECONDS, 900)

    def test_the_environment_variable_overrides_the_default(self):
        with mock.patch.dict("os.environ", {"URN_LOOKUP_CACHE_TTL_SECONDS": "1800"}):
            self.assertEqual(runner.cache_ttl_seconds(), 1800)

    def test_a_non_numeric_value_falls_back_to_the_default(self):
        with mock.patch.dict("os.environ", {"URN_LOOKUP_CACHE_TTL_SECONDS": "soon"}):
            self.assertEqual(
                runner.cache_ttl_seconds(), runner.DEFAULT_CACHE_TTL_SECONDS
            )


# --------------------------------------------------------------------------- #
# web.handle: the standard envelope shapes.
# --------------------------------------------------------------------------- #
class HandleTests(UrnLookupTestCase):
    def _patch_roster(self, roster):
        patcher = mock.patch.object(runner, "get_roster", lambda: roster)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exactly_one_match(self):
        self._patch_roster(
            [runner.ChildRow(id="c-1", external_id="urn:1", full_name="Ada Lovelace")]
        )

        data, status = web.handle({"action": "urn_lookup", "urn": "urn:1"})

        self.assertEqual(status, 200)
        self.assertEqual(data["childId"], "c-1")
        self.assertTrue(data["matched"])
        self.assertEqual(data["childName"], "Ada Lovelace")
        self.assertEqual(data["errors"], [])

    def test_zero_matches_is_ok_not_an_error(self):
        self._patch_roster(
            [runner.ChildRow(id="c-1", external_id="urn:other", full_name="Ada")]
        )

        data, status = web.handle({"action": "urn_lookup", "urn": "urn:1"})

        self.assertEqual(status, 200)
        self.assertIsNone(data["childId"])
        self.assertFalse(data["matched"])
        self.assertEqual(data["errors"], [])
        self.assertNotIn("childName", data)

    def test_two_or_more_matches_is_an_error_with_no_child_id(self):
        self._patch_roster(
            [
                runner.ChildRow(id="c-1", external_id="urn:1", full_name="Ada"),
                runner.ChildRow(id="c-2", external_id="URN:1", full_name="Bea"),
            ]
        )

        data, status = web.handle({"action": "urn_lookup", "urn": "urn:1"})

        self.assertEqual(status, 409)
        self.assertIsNone(data["childId"])
        self.assertFalse(data["matched"])
        self.assertEqual(data["matchCount"], 2)
        self.assertEqual(len(data["errors"]), 1)
        self.assertIn("c-1", data["errors"][0])
        self.assertIn("c-2", data["errors"][0])
        self.assertIn("urn:1", data["errors"][0])

    def test_a_missing_urn_is_a_client_error(self):
        data, status = web.handle({"action": "urn_lookup"})

        self.assertEqual(status, 422)
        self.assertIn("missing 'urn'", data["errors"])

    def test_a_blank_urn_is_a_client_error(self):
        data, status = web.handle({"action": "urn_lookup", "urn": "   "})
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
