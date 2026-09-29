"""Tests for the pull-groups maintenance command.

Run from the project root:

    python -m unittest discover -s tests -v

No real Famly, no network: `GraphQLClient` is stubbed throughout. `FAMLY_CATALOGUE_FILE`
and `GROUPS_CATALOGUE_FILE` point at temporary files for every test, so the real
reference files are never read or written.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.pull_groups import runner as pull_groups
from core.client import GraphQLError, GraphQLHTTPError

logging.disable(logging.CRITICAL)

CITY_ID = "11111111-1111-1111-1111-111111111111"
CW_ID = "22222222-2222-2222-2222-222222222222"

CATALOGUE = {
    "sites": {
        "HDCITY": {"label": "City", "institutionId": CITY_ID},
        "HDCW": {"label": "Canada Water", "institutionId": CW_ID},
    }
}


def group_node(group_id, title, institution_id):
    return {"id": group_id, "title": title, "institutionId": institution_id}


class FakeGraphQLClient:
    """Returns one canned body (or raises), recording every call's variables."""

    def __init__(self, body=None, error=None):
        self._body = body if body is not None else {"data": {"groups": []}}
        self._error = error
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append(dict(variables or {}))
        if self._error is not None:
            raise self._error
        return self._body


class PullGroupsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.catalogue_file = self.tmp / "catalogue.json"
        self.catalogue_file.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        self.groups_catalogue_file = self.tmp / "groups_catalogue.json"

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(self.catalogue_file),
                "GROUPS_CATALOGUE_FILE": str(self.groups_catalogue_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def write_existing_groups_catalogue(self, data: dict):
        self.groups_catalogue_file.write_text(json.dumps(data), encoding="utf-8")


class FetchGroupsTests(PullGroupsTestCase):
    def test_the_query_carries_every_requested_institution_id(self):
        client = FakeGraphQLClient()
        pull_groups.fetch_groups([CITY_ID, CW_ID], client=client)

        self.assertEqual(client.calls[0]["institutionIds"], [CITY_ID, CW_ID])

    def test_a_graphql_error_is_surfaced_not_swallowed(self):
        error = GraphQLError("boom", errors=[{"message": "boom"}])
        client = FakeGraphQLClient(error=error)

        with self.assertRaises(GraphQLError):
            pull_groups.fetch_groups([CITY_ID], client=client)


class PullAllMatchingTests(PullGroupsTestCase):
    def test_matched_groups_are_written_under_the_right_institution(self):
        client = FakeGraphQLClient(
            body={
                "data": {
                    "groups": [
                        group_node("g-1", "Hatchlings", CITY_ID),
                        group_node("g-2", "Voyagers", CITY_ID),
                        group_node("g-3", "Office", CW_ID),
                    ]
                }
            }
        )

        result = pull_groups.pull_all(client=client)

        city = result.catalogue["institutions"]["HDCITY"]
        self.assertEqual(city["groups"], {"g-1": "Hatchlings", "g-2": "Voyagers"})
        self.assertEqual(
            result.catalogue["institutions"]["HDCW"]["groups"], {"g-3": "Office"}
        )
        self.assertEqual(result.written_counts, {"HDCITY": 2, "HDCW": 1})
        self.assertEqual(sorted(result.institutions_pulled), ["HDCITY", "HDCW"])
        self.assertEqual(result.unmatched, [])
        self.assertIsNone(result.fetch_error)

    def test_a_group_missing_id_or_title_is_unmatched_not_written(self):
        client = FakeGraphQLClient(
            body={
                "data": {
                    "groups": [
                        group_node("g-1", "Hatchlings", CITY_ID),
                        group_node(None, "No id", CITY_ID),
                        group_node("g-2", None, CITY_ID),
                    ]
                }
            }
        )

        result = pull_groups.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(result.catalogue["institutions"]["HDCITY"]["groups"], {"g-1": "Hatchlings"})
        self.assertEqual(len(result.unmatched), 2)
        self.assertTrue(all(u.reason == "missing id or title" for u in result.unmatched))


class InstitutionFilterTests(PullGroupsTestCase):
    def test_filtering_to_one_institution_only_requests_that_ones_id(self):
        client = FakeGraphQLClient(
            body={"data": {"groups": [group_node("g-1", "Hatchlings", CITY_ID)]}}
        )

        result = pull_groups.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(client.calls[0]["institutionIds"], [CITY_ID])
        self.assertEqual(result.institutions_pulled, ["HDCITY"])
        self.assertNotIn("HDCW", result.written_counts)

    def test_filtering_leaves_other_institutions_catalogue_entries_untouched(self):
        self.write_existing_groups_catalogue(
            {"institutions": {"HDCW": {"groups": {"g-old": "Old Room"}}}}
        )

        client = FakeGraphQLClient(
            body={"data": {"groups": [group_node("g-1", "Hatchlings", CITY_ID)]}}
        )

        result = pull_groups.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(
            result.catalogue["institutions"]["HDCW"]["groups"], {"g-old": "Old Room"}
        )
        self.assertEqual(
            result.catalogue["institutions"]["HDCITY"]["groups"], {"g-1": "Hatchlings"}
        )

    def test_an_unknown_institution_code_raises_with_zero_api_calls(self):
        client = FakeGraphQLClient()

        with self.assertRaises(pull_groups.UnknownInstitutionError) as ctx:
            pull_groups.pull_all(client=client, institutions=["NOPE"])

        self.assertIn("NOPE", str(ctx.exception))
        self.assertEqual(client.calls, [])

    def test_omitting_the_filter_requests_every_configured_institution(self):
        client = FakeGraphQLClient()
        pull_groups.pull_all(client=client)

        self.assertEqual(sorted(client.calls[0]["institutionIds"]), sorted([CITY_ID, CW_ID]))


class NoInstitutionIdConfiguredTests(PullGroupsTestCase):
    def test_a_site_with_no_institution_id_is_skipped_with_zero_api_calls(self):
        self.catalogue_file.write_text(
            json.dumps({"sites": {"HDNONE": {"label": "No institution yet"}}}),
            encoding="utf-8",
        )

        client = FakeGraphQLClient()
        result = pull_groups.pull_all(client=client)

        self.assertIn("HDNONE", result.institutions_skipped)
        self.assertEqual(client.calls, [])
        self.assertIsNone(result.fetch_error)


class FetchFailureTests(PullGroupsTestCase):
    def test_a_total_fetch_failure_leaves_existing_entries_untouched(self):
        self.write_existing_groups_catalogue(
            {"institutions": {"HDCITY": {"groups": {"g-old": "Old Room"}}}}
        )

        client = FakeGraphQLClient(error=GraphQLHTTPError("down", 502, "nope"))
        result = pull_groups.pull_all(client=client)

        self.assertIsNotNone(result.fetch_error)
        self.assertIn("down", result.fetch_error)
        self.assertEqual(
            result.catalogue["institutions"]["HDCITY"]["groups"], {"g-old": "Old Room"}
        )
        self.assertEqual(result.written_counts, {})


class WriteCatalogueTests(PullGroupsTestCase):
    def test_a_backup_is_created_before_the_existing_file_is_overwritten(self):
        original = {"institutions": {"HDCITY": {"groups": {}}}}
        self.write_existing_groups_catalogue(original)

        client = FakeGraphQLClient(
            body={"data": {"groups": [group_node("g-1", "Hatchlings", CITY_ID)]}}
        )
        result = pull_groups.pull_all(client=client, institutions=["HDCITY"])

        backup = pull_groups.write_catalogue(result, path=self.groups_catalogue_file)

        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

        written = json.loads(self.groups_catalogue_file.read_text(encoding="utf-8"))
        self.assertEqual(
            written["institutions"]["HDCITY"]["groups"], {"g-1": "Hatchlings"}
        )

    def test_no_backup_when_there_was_no_existing_file(self):
        client = FakeGraphQLClient()
        result = pull_groups.pull_all(client=client)

        self.assertFalse(self.groups_catalogue_file.exists())
        backup = pull_groups.write_catalogue(result, path=self.groups_catalogue_file)

        self.assertIsNone(backup)
        self.assertTrue(self.groups_catalogue_file.exists())

    def test_a_leading_comment_block_is_preserved(self):
        self.write_existing_groups_catalogue({"_comment": ["hello"], "institutions": {}})

        client = FakeGraphQLClient()
        result = pull_groups.pull_all(client=client)
        pull_groups.write_catalogue(result, path=self.groups_catalogue_file)

        written = json.loads(self.groups_catalogue_file.read_text(encoding="utf-8"))
        self.assertEqual(written["_comment"], ["hello"])


if __name__ == "__main__":
    unittest.main()
