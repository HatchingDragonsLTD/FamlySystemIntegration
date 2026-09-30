"""Tests for the pull-institution-defaults maintenance command.

Run from the project root:

    python -m unittest discover -s tests -v

No real Famly, no network: `GraphQLClient` is stubbed throughout.
`FAMLY_CATALOGUE_FILE` and `INSTITUTION_DEFAULTS_FILE` point at temporary
files for every test, so the real reference files are never read or written.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.pull_institution_defaults import runner as pull_defaults
from core.client import GraphQLError, GraphQLHTTPError
from integrations.institution_defaults import SCHEDULE_ALL_YEAR_ROUND, SCHEDULE_TERM_ONLY

logging.disable(logging.CRITICAL)

CITY_ID = "11111111-1111-1111-1111-111111111111"
CW_ID = "22222222-2222-2222-2222-222222222222"

CATALOGUE = {
    "sites": {
        "HDCITY": {"label": "City", "institutionId": CITY_ID},
        "HDCW": {"label": "Canada Water", "institutionId": CW_ID},
    }
}


def rule_group_body(entries: dict) -> dict:
    """entries: institution_id -> list of ruleGroupId strings (0, 1, or 2+)."""
    return {
        "data": {
            "institutions": [
                {
                    "institutionId": institution_id,
                    "ruleGroups": [{"ruleGroupId": rid} for rid in rule_group_ids],
                }
                for institution_id, rule_group_ids in entries.items()
            ]
        }
    }


def profile_node(
    billing_profile_id,
    full_year,
    *,
    scheme_id="ANNUALIZED_V2",
    scheme_title="Annualised",
    weeks_of_care=51,
    invoices=12,
    attendance_schedule_id="sched-1",
    is_deleted=False,
):
    return {
        "billingProfileId": billing_profile_id,
        "isDeleted": is_deleted,
        "billingScheme": {"id": scheme_id, "title": scheme_title},
        "annualization": {"weeksOfCare": weeks_of_care, "invoices": invoices},
        "attendanceSchedule": {"id": attendance_schedule_id, "fullYear": full_year},
    }


def billing_profiles_body(profiles: list) -> dict:
    return {"data": {"finance": {"billingProfiles": {"list": profiles}}}}


def _default_profiles():
    return [
        profile_node("bp-full", True, attendance_schedule_id="sched-full"),
        profile_node("bp-term", False, attendance_schedule_id="sched-term"),
    ]


class FakeGraphQLClient:
    """Serves a canned rule-group body and per-institution billing-profile
    bodies, keyed by operation name (and, for billing profiles, by
    institutionSetId). Records every call as (operation_name, variables).
    """

    def __init__(
        self,
        rule_group_body: dict | None = None,
        rule_group_error: Exception | None = None,
        billing_bodies: dict | None = None,
        billing_errors: dict | None = None,
    ):
        self._rule_group_body = (
            rule_group_body if rule_group_body is not None else {"data": {"institutions": []}}
        )
        self._rule_group_error = rule_group_error
        self._billing_bodies = billing_bodies or {}
        self._billing_errors = billing_errors or {}
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        self.calls.append((operation_name, dict(variables or {})))

        if operation_name == pull_defaults.RULE_GROUP_OPERATION_NAME:
            if self._rule_group_error is not None:
                raise self._rule_group_error
            return self._rule_group_body

        if operation_name == pull_defaults.BILLING_PROFILES_OPERATION_NAME:
            site_id = variables["institutionSetId"]
            if site_id in self._billing_errors:
                raise self._billing_errors[site_id]
            return self._billing_bodies.get(site_id, billing_profiles_body([]))

        raise AssertionError(f"unexpected operation {operation_name!r}")


class PullInstitutionDefaultsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.catalogue_file = self.tmp / "catalogue.json"
        self.catalogue_file.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        self.defaults_file = self.tmp / "institution_defaults.json"

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(self.catalogue_file),
                "INSTITUTION_DEFAULTS_FILE": str(self.defaults_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def write_existing_defaults(self, data: dict):
        self.defaults_file.write_text(json.dumps(data), encoding="utf-8")

    def happy_client(self):
        """Both institutions have a clean rule group + two matching profiles."""
        return FakeGraphQLClient(
            rule_group_body=rule_group_body(
                {CITY_ID: ["rg-city"], CW_ID: ["rg-cw"]}
            ),
            billing_bodies={
                CITY_ID: billing_profiles_body(_default_profiles()),
                CW_ID: billing_profiles_body(_default_profiles()),
            },
        )


class FetchRuleGroupsTests(PullInstitutionDefaultsTestCase):
    def test_exactly_one_rule_group_resolves_cleanly(self):
        client = FakeGraphQLClient(
            rule_group_body=rule_group_body({CITY_ID: ["rg-city"]})
        )
        ok, failed = pull_defaults.fetch_rule_groups([CITY_ID], client=client)

        self.assertEqual(ok, {CITY_ID: "rg-city"})
        self.assertEqual(failed, {})

    def test_zero_rule_groups_is_a_failure_naming_the_count(self):
        client = FakeGraphQLClient(rule_group_body=rule_group_body({CITY_ID: []}))
        ok, failed = pull_defaults.fetch_rule_groups([CITY_ID], client=client)

        self.assertEqual(ok, {})
        self.assertIn("found 0", failed[CITY_ID])

    def test_two_rule_groups_is_a_failure_naming_the_count(self):
        client = FakeGraphQLClient(
            rule_group_body=rule_group_body({CITY_ID: ["rg-1", "rg-2"]})
        )
        ok, failed = pull_defaults.fetch_rule_groups([CITY_ID], client=client)

        self.assertEqual(ok, {})
        self.assertIn("found 2", failed[CITY_ID])

    def test_an_institution_missing_from_the_response_is_a_failure(self):
        client = FakeGraphQLClient(rule_group_body=rule_group_body({}))
        ok, failed = pull_defaults.fetch_rule_groups([CITY_ID], client=client)

        self.assertEqual(ok, {})
        self.assertIn("no institution", failed[CITY_ID])

    def test_one_call_covers_every_requested_institution(self):
        client = self.happy_client()
        pull_defaults.fetch_rule_groups([CITY_ID, CW_ID], client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]["institutionIds"], [CITY_ID, CW_ID])

    def test_a_graphql_error_is_surfaced_not_swallowed(self):
        error = GraphQLError("boom", errors=[{"message": "boom"}])
        client = FakeGraphQLClient(rule_group_error=error)

        with self.assertRaises(GraphQLError):
            pull_defaults.fetch_rule_groups([CITY_ID], client=client)


class FetchBillingDefaultsTests(PullInstitutionDefaultsTestCase):
    def test_profiles_are_bucketed_by_full_year(self):
        client = FakeGraphQLClient(
            billing_bodies={CITY_ID: billing_profiles_body(_default_profiles())}
        )
        result = pull_defaults.fetch_billing_defaults(CITY_ID, client=client)

        self.assertEqual(result[SCHEDULE_ALL_YEAR_ROUND].billing_profile_id, "bp-full")
        self.assertEqual(result[SCHEDULE_TERM_ONLY].billing_profile_id, "bp-term")

    def test_a_deleted_duplicate_is_filtered_out_before_bucketing(self):
        profiles = _default_profiles() + [
            profile_node("bp-full-deleted", True, is_deleted=True)
        ]
        client = FakeGraphQLClient(
            billing_bodies={CITY_ID: billing_profiles_body(profiles)}
        )

        result = pull_defaults.fetch_billing_defaults(CITY_ID, client=client)
        self.assertEqual(result[SCHEDULE_ALL_YEAR_ROUND].billing_profile_id, "bp-full")

    def test_zero_profiles_in_a_bucket_is_an_anomaly(self):
        client = FakeGraphQLClient(
            billing_bodies={
                CITY_ID: billing_profiles_body(
                    [profile_node("bp-term", False)]  # no all_year_round profile
                )
            }
        )

        with self.assertRaises(pull_defaults.InstitutionDefaultsAnomaly) as ctx:
            pull_defaults.fetch_billing_defaults(CITY_ID, client=client)
        self.assertIn("all_year_round", str(ctx.exception))
        self.assertIn("found 0", str(ctx.exception))

    def test_two_profiles_in_a_bucket_is_an_anomaly(self):
        client = FakeGraphQLClient(
            billing_bodies={
                CITY_ID: billing_profiles_body(
                    [
                        profile_node("bp-full-1", True),
                        profile_node("bp-full-2", True),
                        profile_node("bp-term", False),
                    ]
                )
            }
        )

        with self.assertRaises(pull_defaults.InstitutionDefaultsAnomaly) as ctx:
            pull_defaults.fetch_billing_defaults(CITY_ID, client=client)
        self.assertIn("all_year_round", str(ctx.exception))
        self.assertIn("found 2", str(ctx.exception))

    def test_a_profile_with_no_attendance_schedule_is_an_anomaly(self):
        profiles = _default_profiles()
        profiles[0]["attendanceSchedule"] = None
        client = FakeGraphQLClient(
            billing_bodies={CITY_ID: billing_profiles_body(profiles)}
        )

        with self.assertRaises(pull_defaults.InstitutionDefaultsAnomaly) as ctx:
            pull_defaults.fetch_billing_defaults(CITY_ID, client=client)
        self.assertIn("attendanceSchedule", str(ctx.exception))

    def test_mismatched_billing_schemes_across_buckets_is_an_anomaly(self):
        profiles = [
            profile_node("bp-full", True, scheme_id="ANNUALIZED_V2", scheme_title="Annualised"),
            profile_node("bp-term", False, scheme_id="FLAT_RATE", scheme_title="Flat"),
        ]
        client = FakeGraphQLClient(
            billing_bodies={CITY_ID: billing_profiles_body(profiles)}
        )

        with self.assertRaises(pull_defaults.InstitutionDefaultsAnomaly) as ctx:
            pull_defaults.fetch_billing_defaults(CITY_ID, client=client)

        message = str(ctx.exception)
        self.assertIn("DIFFERENT", message)
        self.assertIn("ANNUALIZED_V2", message)
        self.assertIn("FLAT_RATE", message)

    def test_matching_billing_schemes_across_buckets_is_fine(self):
        client = FakeGraphQLClient(
            billing_bodies={CITY_ID: billing_profiles_body(_default_profiles())}
        )
        # Must not raise.
        pull_defaults.fetch_billing_defaults(CITY_ID, client=client)

    def test_a_graphql_error_is_surfaced_not_swallowed(self):
        error = GraphQLHTTPError("down", 502, "nope")
        client = FakeGraphQLClient(billing_errors={CITY_ID: error})

        with self.assertRaises(GraphQLHTTPError):
            pull_defaults.fetch_billing_defaults(CITY_ID, client=client)

    def test_one_call_per_institution_carries_that_institutions_id(self):
        client = FakeGraphQLClient(
            billing_bodies={CITY_ID: billing_profiles_body(_default_profiles())}
        )
        pull_defaults.fetch_billing_defaults(CITY_ID, client=client)

        self.assertEqual(client.calls[0][1]["institutionSetId"], CITY_ID)


class PullAllHappyPathTests(PullInstitutionDefaultsTestCase):
    def test_both_institutions_get_a_complete_entry(self):
        result = pull_defaults.pull_all(client=self.happy_client())

        self.assertEqual(sorted(result.institutions_pulled), ["HDCITY", "HDCW"])
        self.assertEqual(result.institutions_failed, {})

        city = result.catalogue["institutions"]["HDCITY"]
        self.assertEqual(city["ruleGroupId"], "rg-city")
        self.assertEqual(
            city["schedules"]["all_year_round"]["billingProfileId"], "bp-full"
        )
        self.assertEqual(
            city["schedules"]["term_only"]["billingProfileId"], "bp-term"
        )

    def test_the_institution_filter_restricts_which_ids_are_asked_about(self):
        client = self.happy_client()
        result = pull_defaults.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(result.institutions_pulled, ["HDCITY"])
        rule_group_call = next(
            v for name, v in client.calls if name == pull_defaults.RULE_GROUP_OPERATION_NAME
        )
        self.assertEqual(rule_group_call["institutionIds"], [CITY_ID])

    def test_an_unknown_institution_code_raises_with_zero_api_calls(self):
        client = self.happy_client()

        with self.assertRaises(pull_defaults.UnknownInstitutionError) as ctx:
            pull_defaults.pull_all(client=client, institutions=["NOPE"])

        self.assertIn("NOPE", str(ctx.exception))
        self.assertEqual(client.calls, [])

    def test_a_site_with_no_institution_id_is_skipped_with_zero_api_calls(self):
        self.catalogue_file.write_text(
            json.dumps({"sites": {"HDNONE": {"label": "No institution yet"}}}),
            encoding="utf-8",
        )
        client = self.happy_client()

        result = pull_defaults.pull_all(client=client)

        self.assertIn("HDNONE", result.institutions_skipped)
        self.assertEqual(client.calls, [])


class PullAllFailureIsolationTests(PullInstitutionDefaultsTestCase):
    def test_one_institutions_rule_group_anomaly_does_not_block_the_other(self):
        client = FakeGraphQLClient(
            rule_group_body=rule_group_body({CITY_ID: [], CW_ID: ["rg-cw"]}),
            billing_bodies={CW_ID: billing_profiles_body(_default_profiles())},
        )

        result = pull_defaults.pull_all(client=client)

        self.assertIn("HDCITY", result.institutions_failed)
        self.assertEqual(result.institutions_pulled, ["HDCW"])
        self.assertNotIn("HDCITY", result.catalogue["institutions"])

    def test_one_institutions_billing_anomaly_does_not_block_the_other(self):
        client = FakeGraphQLClient(
            rule_group_body=rule_group_body({CITY_ID: ["rg-city"], CW_ID: ["rg-cw"]}),
            billing_bodies={
                CITY_ID: billing_profiles_body([profile_node("bp-term", False)]),
                CW_ID: billing_profiles_body(_default_profiles()),
            },
        )

        result = pull_defaults.pull_all(client=client)

        self.assertIn("HDCITY", result.institutions_failed)
        self.assertEqual(result.institutions_pulled, ["HDCW"])

    def test_a_failed_institution_leaves_its_previous_entry_untouched(self):
        self.write_existing_defaults(
            {
                "institutions": {
                    "HDCITY": {
                        "ruleGroupId": "rg-old",
                        "schedules": {
                            "all_year_round": {"billingProfileId": "old-full"},
                            "term_only": {"billingProfileId": "old-term"},
                        },
                    }
                }
            }
        )
        client = FakeGraphQLClient(
            rule_group_body=rule_group_body({CITY_ID: [], CW_ID: ["rg-cw"]}),
            billing_bodies={CW_ID: billing_profiles_body(_default_profiles())},
        )

        result = pull_defaults.pull_all(client=client)

        self.assertEqual(result.catalogue["institutions"]["HDCITY"]["ruleGroupId"], "rg-old")

    def test_the_batched_rule_group_calls_own_failure_fails_every_institution(self):
        client = FakeGraphQLClient(rule_group_error=GraphQLHTTPError("down", 502, "nope"))

        result = pull_defaults.pull_all(client=client)

        self.assertEqual(sorted(result.institutions_failed), ["HDCITY", "HDCW"])
        self.assertEqual(result.institutions_pulled, [])


class WriteCatalogueTests(PullInstitutionDefaultsTestCase):
    def test_a_backup_is_created_before_the_existing_file_is_overwritten(self):
        original = {"institutions": {"HDCITY": {"ruleGroupId": "rg-old", "schedules": {}}}}
        self.write_existing_defaults(original)

        result = pull_defaults.pull_all(client=self.happy_client(), institutions=["HDCITY"])
        backup = pull_defaults.write_catalogue(result, path=self.defaults_file)

        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

        written = json.loads(self.defaults_file.read_text(encoding="utf-8"))
        self.assertEqual(written["institutions"]["HDCITY"]["ruleGroupId"], "rg-city")

    def test_no_backup_when_there_was_no_existing_file(self):
        result = pull_defaults.pull_all(client=self.happy_client())

        self.assertFalse(self.defaults_file.exists())
        backup = pull_defaults.write_catalogue(result, path=self.defaults_file)

        self.assertIsNone(backup)
        self.assertTrue(self.defaults_file.exists())

    def test_a_leading_comment_block_is_preserved(self):
        self.write_existing_defaults({"_comment": ["hello"], "institutions": {}})

        result = pull_defaults.pull_all(client=self.happy_client())
        pull_defaults.write_catalogue(result, path=self.defaults_file)

        written = json.loads(self.defaults_file.read_text(encoding="utf-8"))
        self.assertEqual(written["_comment"], ["hello"])


if __name__ == "__main__":
    unittest.main()
