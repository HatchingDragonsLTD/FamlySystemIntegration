"""Tests for the site_code metadata field.

Run from the project root:

    python -m unittest discover -s tests -v

No Famly, no network: the REST client is stubbed throughout.

site_code is METADATA. These tests pin two things in particular:

  * it never blocks a preview -- missing or unrecognised only warns; and
  * it never reaches the Famly-bound plan body, and never changes a single
    UUID in it. Those stay determined by what HubSpot sends.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.plan_write import hubspot_flatten as flatten
from actions.plan_write import hubspot_intake, input_schema, preview_store, runner
from integrations import catalogue, slack

logging.disable(logging.CRITICAL)

CHILD = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000005"
SCHEDULE = "00000000-0000-0000-0000-000000000003"

CITY_INSTITUTION = "11111111-1111-1111-1111-111111111111"

CATALOGUE = {
    "sites": {
        "HDCITY": {
            "label": "Hatching Dragons - City",
            "institutionId": CITY_INSTITUTION,
        },
        "HDCW": {"label": "", "institutionId": ""},
    },
    "sessions": {SESSION: "Full Day"},
    "products": {},
}


def flat_payload(**overrides) -> dict:
    payload = {
        "childId": CHILD,
        "from": "2026-09-01",
        "attendanceScheduleId": SCHEDULE,
        "weeksOfCare": 51,
        "billingId": "ANNUALIZED_V2",
        "billingTitle": "Monthly",
        "billingInvoices": "ADVANCE",
        "monday_session": SESSION,
        "funded": "false",
    }
    payload.update(overrides)
    return payload


class SiteTestCase(unittest.TestCase):
    """A temporary catalogue and preview store; Famly always stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)

        catalogue_file = tmp / "catalogue.json"
        catalogue_file.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(catalogue_file),
                "PREVIEW_STORE_PATH": str(tmp / "store.sqlite3"),
                "FAMLY_ACCESS_TOKEN": "test-token",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        self.posted = []
        outer = self

        class FakeRest:
            def post(self, path, params=None, json_body=None):
                outer.posted.append(json_body)
                return {
                    "id": "plan-9",
                    "childId": CHILD,
                    "monthlyEstimate": 500.0,
                    "planParts": [],
                    "behaviors": [],
                }

        patcher = mock.patch.object(runner, "RestClient", lambda *a, **k: FakeRest())
        patcher.start()
        self.addCleanup(patcher.stop)

    def intake(self, **overrides):
        return hubspot_intake.handle_intake(flat_payload(**overrides), version=3)


class ResolveSiteTests(SiteTestCase):
    def test_a_known_code_resolves(self):
        site = catalogue.resolve_site("HDCITY")

        self.assertEqual(site["site_code"], "HDCITY")
        self.assertEqual(site["label"], "Hatching Dragons - City")
        self.assertEqual(site["institution_id"], CITY_INSTITUTION)

    def test_matching_is_case_insensitive_and_trimmed(self):
        for variant in ("hdcity", "HdCity", "  HDCITY  "):
            self.assertEqual(catalogue.resolve_site(variant)["site_code"], "HDCITY")

    def test_an_unfilled_entry_still_resolves(self):
        # Known code, blank label and institutionId: usable, not a failure.
        site = catalogue.resolve_site("HDCW")

        self.assertEqual(site["site_code"], "HDCW")
        self.assertEqual(site["label"], "HDCW")  # falls back to the code
        self.assertIsNone(site["institution_id"])

    def test_unknown_and_empty_codes_return_none(self):
        for value in ("NOPE", "", "   ", None, 123):
            self.assertIsNone(catalogue.resolve_site(value))

    def test_a_missing_catalogue_file_resolves_nothing(self):
        with mock.patch.dict(
            "os.environ", {"FAMLY_CATALOGUE_FILE": "/nope/missing.json"}
        ):
            self.assertEqual(catalogue.load_sites(), {})
            self.assertIsNone(catalogue.resolve_site("HDCITY"))


class FlattenSiteTests(SiteTestCase):
    def test_a_known_code_becomes_metadata(self):
        nested = flatten.flatten_to_nested(flat_payload(site_code="HDCITY"))
        meta = nested[flatten.METADATA_KEY]

        self.assertEqual(meta["site_code"], "HDCITY")
        self.assertEqual(meta["label"], "Hatching Dragons - City")
        self.assertEqual(meta["institution_id"], CITY_INSTITUTION)
        self.assertIsNone(meta["warning"])

    def test_metadata_is_a_sibling_of_the_plan_not_part_of_it(self):
        nested = flatten.flatten_to_nested(flat_payload(site_code="HDCITY"))

        self.assertNotIn(flatten.METADATA_KEY, nested["planParts"][0])
        self.assertNotIn("institutionId", json.dumps(nested["planParts"]))

    def test_an_unknown_code_warns_and_carries_no_institution(self):
        nested = flatten.flatten_to_nested(flat_payload(site_code="HDMARS"))
        meta = nested[flatten.METADATA_KEY]

        self.assertEqual(meta["site_code"], "HDMARS")
        self.assertIsNone(meta["institution_id"])
        self.assertIn("Unrecognised site_code: HDMARS", meta["warning"])

    def test_a_missing_code_behaves_like_an_unknown_one(self):
        nested = flatten.flatten_to_nested(flat_payload())
        meta = nested[flatten.METADATA_KEY]

        self.assertIsNone(meta["site_code"])
        self.assertIsNone(meta["institution_id"])
        self.assertIsNotNone(meta["warning"])


class SiteDoesNotReachFamlyTests(SiteTestCase):
    """The plan body must be byte-identical with and without a site."""

    @staticmethod
    def _normalised(body):
        """The body with planPartId blanked.

        Each build generates a fresh uuid4 for the part, so two bodies never
        compare equal verbatim. Everything else must match.
        """
        body = json.loads(json.dumps(body))
        for part in body["plan"]["planParts"]:
            part["planPartId"] = "<generated>"
        return body

    def test_the_body_is_unchanged_by_site_code(self):
        without = self._normalised(self.intake().plan_body)
        with_site = self._normalised(self.intake(site_code="HDCITY").plan_body)

        self.assertEqual(without, with_site)

    def test_no_site_field_is_posted_to_famly(self):
        self.intake(site_code="HDCITY")

        sent = json.dumps(self.posted[-1])
        self.assertNotIn("HDCITY", sent)
        self.assertNotIn(CITY_INSTITUTION, sent)
        self.assertNotIn("institution", sent.lower())
        self.assertNotIn(flatten.METADATA_KEY, sent)

    def test_the_booked_session_uuid_is_untouched(self):
        body = self.intake(site_code="HDCITY").plan_body
        bookings = body["plan"]["planParts"][0]["sessionBookings"]

        # Still exactly what HubSpot sent -- site context decides nothing.
        self.assertEqual([b["sessionId"] for b in bookings], [SESSION])


class SitePreviewTests(SiteTestCase):
    def test_a_known_code_previews_cleanly(self):
        result = self.intake(site_code="HDCITY")

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.plan_input.metadata["institution_id"], CITY_INSTITUTION)
        self.assertNotIn("site_unresolved", [w.key for w in result.warnings])

    def test_an_unknown_code_warns_but_the_preview_succeeds(self):
        result = self.intake(site_code="HDMARS")

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(self.posted), 1)  # Famly was still called

        keys = [w.key for w in result.warnings]
        self.assertIn("site_unresolved", keys)
        titles = [w.warning.title for w in result.warnings]
        self.assertTrue(any("HDMARS" in t for t in titles))

    def test_a_missing_code_behaves_the_same_as_unknown(self):
        result = self.intake()

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])
        self.assertIn("site_unresolved", [w.key for w in result.warnings])

    def test_an_unresolved_site_is_a_known_warning_not_an_escalation(self):
        result = self.intake(site_code="HDMARS")

        for warning in result.warnings:
            if warning.key == "site_unresolved":
                self.assertTrue(warning.is_known)
                break
        else:
            self.fail("no site_unresolved warning")

    def test_validate_never_rejects_on_site(self):
        plan_input = input_schema.from_dict(
            flatten.flatten_to_nested(flat_payload(site_code="HDMARS"))
        )
        self.assertEqual(input_schema.validate(plan_input), [])


class SiteStoreTests(SiteTestCase):
    def test_the_store_records_the_site(self):
        preview_store.save(
            "pv-1",
            {"plan": {"childId": CHILD}},
            CHILD,
            3,
            site_code="HDCITY",
            institution_id=CITY_INSTITUTION,
        )
        stored = preview_store.get("pv-1")

        self.assertEqual(stored.site_code, "HDCITY")
        self.assertEqual(stored.institution_id, CITY_INSTITUTION)

    def test_an_unresolved_site_stores_as_null(self):
        preview_store.save("pv-2", {"plan": {}}, CHILD, 3)
        stored = preview_store.get("pv-2")

        self.assertIsNone(stored.site_code)
        self.assertIsNone(stored.institution_id)

    def test_a_store_written_before_these_columns_still_opens(self):
        import sqlite3
        from datetime import datetime, timezone

        old_db = Path(self._tmp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(old_db)
        connection.execute(
            "CREATE TABLE previews (preview_id TEXT PRIMARY KEY, plan_body TEXT "
            "NOT NULL, child_id TEXT, version INTEGER, created_at TEXT NOT NULL, "
            "status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO previews VALUES (?,?,?,?,?,?)",
            (
                "legacy-1",
                '{"plan":{}}',
                CHILD,
                3,
                datetime.now(timezone.utc).isoformat(),
                "pending",
            ),
        )
        connection.commit()
        connection.close()

        with mock.patch.dict("os.environ", {"PREVIEW_STORE_PATH": str(old_db)}):
            stored = preview_store.get("legacy-1")

        # Migrated in place: the pending preview survives, site reads as null.
        self.assertEqual(stored.preview_id, "legacy-1")
        self.assertEqual(stored.status, preview_store.STATUS_PENDING)
        self.assertIsNone(stored.site_code)


class SiteSummaryTests(SiteTestCase):
    def _summary(self, site):
        from actions.read_child_plans.runner import parse_plan

        plan = parse_plan({"childId": CHILD, "from": "2026-09-01", "planParts": []})
        return slack.build_summary(plan, [], site=site)

    def test_the_label_is_shown(self):
        text = self._summary(
            {"label": "Hatching Dragons - City", "site_code": "HDCITY"}
        )
        self.assertIn("• Site: Hatching Dragons - City", text)

    def test_unknown_when_unresolved_or_absent(self):
        for site in ({"label": None}, {}, None):
            self.assertIn("• Site: unknown", self._summary(site))

    def test_the_site_line_sits_near_the_top(self):
        text = self._summary({"label": "Hatching Dragons - City"})
        lines = text.splitlines()

        self.assertTrue(lines[1].startswith("• Site:"))
        self.assertTrue(lines[2].startswith("• Child:"))


if __name__ == "__main__":
    unittest.main()
