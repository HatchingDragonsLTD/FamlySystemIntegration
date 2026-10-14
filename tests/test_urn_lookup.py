"""Tests for the urn_lookup TEMPORARY backfill action.

Run from the project root:

    python -m unittest discover -s tests -v

No real Famly, no network: `GraphQLClient` is stubbed throughout via a fake
whose `.execute()` returns canned pages.

The roster cache is MODULE-LEVEL, process-global state (see runner.py), so
every test resets it in setUp -- otherwise one test's fetch would leak into
the next and make results order-dependent.
"""

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
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
                "listBySiteIds": {
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
    """Every test gets a temp catalogue with one site (so `fetch_roster`'s
    required `siteIds` always has something to send), plus the usual
    module-level cache reset."""

    def setUp(self):
        runner._reset_cache_for_tests()
        self.addCleanup(runner._reset_cache_for_tests)

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        catalogue_path = Path(tmpdir.name) / "catalogue.json"
        catalogue_path.write_text(
            json.dumps({"sites": {"TESTSITE": {"institutionId": "site-1"}}}),
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            os.environ, {"FAMLY_CATALOGUE_FILE": str(catalogue_path)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)


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

    def test_every_configured_institution_id_is_sent_in_one_call(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        catalogue_path = Path(tmpdir.name) / "catalogue.json"
        catalogue_path.write_text(
            json.dumps(
                {
                    "sites": {
                        "A": {"institutionId": "site-a"},
                        "B": {"institutionId": "site-b"},
                        "C": {},  # no institutionId -- must be skipped, not sent as null
                    }
                }
            ),
            encoding="utf-8",
        )
        client = FakeGraphQLClient(pages=[page_body([])])

        with mock.patch.dict(os.environ, {"FAMLY_CATALOGUE_FILE": str(catalogue_path)}):
            runner.fetch_roster(client=client)

        self.assertEqual(sorted(client.calls[0]["siteIds"]), ["site-a", "site-b"])

    def test_no_configured_institution_ids_raises_a_clear_error(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        catalogue_path = Path(tmpdir.name) / "catalogue.json"
        catalogue_path.write_text(json.dumps({"sites": {"A": {}}}), encoding="utf-8")
        client = FakeGraphQLClient(pages=[page_body([])])

        with mock.patch.dict(os.environ, {"FAMLY_CATALOGUE_FILE": str(catalogue_path)}):
            with self.assertRaises(ValueError) as ctx:
                runner.fetch_roster(client=client)

        self.assertIn("institutionId", str(ctx.exception))
        self.assertEqual(client.calls, [])  # never even attempted the call

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
            "GraphQL request 'UrnLookupChildren' returned errors: "
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


# --------------------------------------------------------------------------- #
# The roster must include PAST and FUTURE children.
#
# Famly's `children.listBySiteIds` returns only children enrolled TODAY unless
# it is given a `timeRange`. Nine children who exist in Famly with their urn in
# `externalId` could not be resolved because all nine were leavers or future
# starters. The fake below behaves like the real API in that respect, so these
# tests fail against the old code instead of merely checking a variable.
# --------------------------------------------------------------------------- #
class TimeAwareClient:
    """Like Famly: current children only, unless a timeRange is supplied."""

    def __init__(self, current, past=(), future=()):
        self.current = list(current)
        self.extra = list(past) + list(future)
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append(dict(variables or {}))
        rows = list(self.current)
        if (variables or {}).get("timeRange"):
            rows += self.extra
        return page_body(rows)


class TimeRangeTests(UrnLookupTestCase):
    def roster(self):
        client = TimeAwareClient(
            current=[child_node("c-now", "urn:now", "Now")],
            past=[child_node("c-past", "urn:past", "Past")],
            future=[child_node("c-future", "urn:future", "Future")],
        )
        return runner.fetch_roster(client=client), client

    def test_a_child_who_has_left_is_in_the_roster(self):
        rows, _ = self.roster()
        matches = runner.find_matches("urn:past", rows)

        self.assertEqual([m.id for m in matches], ["c-past"])

    def test_a_child_who_has_not_started_is_in_the_roster(self):
        rows, _ = self.roster()
        matches = runner.find_matches("urn:future", rows)

        self.assertEqual([m.id for m in matches], ["c-future"])

    def test_a_current_child_is_still_found(self):
        rows, _ = self.roster()

        self.assertEqual([m.id for m in runner.find_matches("urn:now", rows)], ["c-now"])

    def test_every_page_asks_for_the_time_range(self):
        client = FakeGraphQLClient(
            pages=[
                page_body([child_node("c-1", "urn:1", "Ada")], next_cursor="page-2"),
                page_body([child_node("c-2", "urn:2", "Bea")], next_cursor=None),
            ]
        )

        runner.fetch_roster(client=client)

        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertEqual(call["timeRange"], runner.ROSTER_TIME_RANGE)

    def test_the_range_is_shared_with_pull_roster(self):
        # One definition of "the whole roster", so the two actions cannot
        # drift apart again.
        from actions.pull_roster import runner as pull_roster

        self.assertIs(runner.ROSTER_TIME_RANGE, pull_roster.ROSTER_TIME_RANGE)

    def test_a_caller_cannot_change_the_range_by_mutating_a_request(self):
        client = FakeGraphQLClient(pages=[page_body([])])
        before = dict(runner.ROSTER_TIME_RANGE)

        runner.fetch_roster(client=client)
        client.calls[0]["timeRange"]["from"] = "1999-01-01"

        self.assertEqual(runner.ROSTER_TIME_RANGE, before)

    def test_the_query_declares_and_passes_the_time_range(self):
        # The bug was the ABSENCE of this argument. Fails if it is ever removed.
        text = runner.QUERY_PATH.read_text(encoding="utf-8")

        self.assertIn("$timeRange: NonEmptyLocalDateRangeInput", text)
        self.assertIn("timeRange: $timeRange", text)

    def test_the_web_handler_resolves_a_leaver_exactly_once(self):
        client = TimeAwareClient(
            current=[child_node("c-now", "urn:now", "Now")],
            past=[child_node("c-past", "urn:past", "Past")],
        )

        # The real handler and the real fetch, with only the HTTP client faked.
        with mock.patch.object(runner, "_public_client", lambda: client):
            data, status = web.handle({"action": "urn_lookup", "urn": "urn:past"})

        self.assertEqual(status, 200)
        self.assertTrue(data["matched"])
        self.assertEqual(data["childId"], "c-past")


# --------------------------------------------------------------------------- #
# Merge by child id
# --------------------------------------------------------------------------- #
class MergeByIdTests(UrnLookupTestCase):
    def test_a_child_returned_on_two_pages_is_one_row(self):
        client = FakeGraphQLClient(
            pages=[
                page_body([child_node("c-1", "urn:1", "Ada")], next_cursor="page-2"),
                # A page boundary that repeats the last row.
                page_body([child_node("c-1", "urn:1", "Ada"), child_node("c-2", "urn:2", "Bea")]),
            ]
        )

        rows = runner.fetch_roster(client=client)

        self.assertEqual([r.id for r in rows], ["c-1", "c-2"])

    def test_a_duplicate_does_not_turn_one_match_into_two(self):
        # Without the merge this reads as a "duplicate externalId" data
        # problem (HTTP 409) when it is only the same child twice.
        client = FakeGraphQLClient(
            pages=[
                page_body(
                    [child_node("c-1", "urn:1", "Ada"), child_node("c-1", "urn:1", "Ada")]
                )
            ]
        )

        rows = runner.fetch_roster(client=client)

        self.assertEqual(len(runner.find_matches("urn:1", rows)), 1)

    def test_two_different_children_with_the_same_urn_are_NOT_merged(self):
        # The merge is by CHILD id. Two children sharing an externalId are a
        # real data problem and must still be reported, never collapsed.
        client = FakeGraphQLClient(
            pages=[
                page_body(
                    [child_node("c-1", "urn:same", "Ada"), child_node("c-2", "urn:same", "Bea")]
                )
            ]
        )

        rows = runner.fetch_roster(client=client)

        self.assertEqual([r.id for r in rows], ["c-1", "c-2"])
        self.assertEqual(len(runner.find_matches("urn:same", rows)), 2)

    def test_first_seen_order_is_kept(self):
        client = FakeGraphQLClient(
            pages=[
                page_body(
                    [
                        child_node("c-3", "u3", "C"),
                        child_node("c-1", "u1", "A"),
                        child_node("c-3", "u3", "C"),
                        child_node("c-2", "u2", "B"),
                    ]
                )
            ]
        )

        self.assertEqual([r.id for r in runner.fetch_roster(client=client)], ["c-3", "c-1", "c-2"])

    def test_a_duplicate_fills_a_blank_but_never_overwrites(self):
        merged = runner.merge_by_id(
            [
                runner.ChildRow(id="c-1", external_id=None, full_name="Ada"),
                runner.ChildRow(id="c-1", external_id="urn:1", full_name="Someone Else"),
            ]
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].external_id, "urn:1")  # blank filled
        self.assertEqual(merged[0].full_name, "Ada")  # known value kept

    def test_a_row_with_no_id_is_kept_rather_than_guessed_at(self):
        merged = runner.merge_by_id(
            [
                runner.ChildRow(id=None, external_id="u-a"),
                runner.ChildRow(id=None, external_id="u-b"),
                runner.ChildRow(id="c-1", external_id="u-1"),
            ]
        )

        self.assertEqual(len(merged), 3)


if __name__ == "__main__":
    unittest.main()
