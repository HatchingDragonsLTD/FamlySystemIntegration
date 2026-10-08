"""Tests for the pull-sessions maintenance command.

Run from the project root:

    python -m unittest discover -s tests -v

No real Famly, no network: `RestClient` is stubbed throughout. `FAMLY_CATALOGUE_FILE`
and `SESSION_CATALOGUE_FILE` point at temporary files for every test, so the real
reference files are never read or written.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.pull_sessions import runner as pull_sessions
from core.rest_client import RestHTTPError

logging.disable(logging.CRITICAL)

CITY_ID = "11111111-1111-1111-1111-111111111111"
CW_ID = "22222222-2222-2222-2222-222222222222"

CATALOGUE = {
    "sites": {
        "HDCITY": {"label": "City", "institutionId": CITY_ID},
        "HDCW": {"label": "Canada Water", "institutionId": CW_ID},
    }
}

MORNING_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
AFTERNOON_UUID = "bbbbbbbb-0000-0000-0000-000000000002"


def session(session_id, title):
    return {"id": session_id, "title": title}


class FakeRest:
    """Maps institutionId -> a canned sessions response or an error to raise."""

    def __init__(self, by_institution: dict):
        self._by_institution = by_institution
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        institution_id = (params or {}).get("institutionId")
        outcome = self._by_institution.get(institution_id, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class PullSessionsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.catalogue_file = self.tmp / "catalogue.json"
        self.catalogue_file.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        self.session_catalogue_file = self.tmp / "session_catalogue.json"
        self.session_titles_file = self.tmp / "session_titles.json"

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(self.catalogue_file),
                "SESSION_CATALOGUE_FILE": str(self.session_catalogue_file),
                "SESSION_TITLES_FILE": str(self.session_titles_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def write_existing_session_catalogue(self, data: dict):
        self.session_catalogue_file.write_text(json.dumps(data), encoding="utf-8")


class ParseTitleTests(unittest.TestCase):
    """The prefix table, and the non-funded (no prefix) case."""

    def test_f_prefix_is_with_meals_and_activities(self):
        parsed = pull_sessions.parse_title("(F) Morning")
        self.assertTrue(parsed.funded)
        self.assertEqual(parsed.variant, "with_meals_and_activities")
        self.assertEqual(parsed.slot, "morning")

    def test_fnm_prefix_is_no_meals(self):
        parsed = pull_sessions.parse_title("(FNM) Afternoon")
        self.assertTrue(parsed.funded)
        self.assertEqual(parsed.variant, "no_meals")
        self.assertEqual(parsed.slot, "afternoon")

    def test_fna_prefix_is_no_activities(self):
        parsed = pull_sessions.parse_title("(FNA) Full Day")
        self.assertTrue(parsed.funded)
        self.assertEqual(parsed.variant, "no_activities")
        self.assertEqual(parsed.slot, "full_day")

    def test_fne_prefix_is_neither(self):
        parsed = pull_sessions.parse_title("(FNE) Morning")
        self.assertTrue(parsed.funded)
        self.assertEqual(parsed.variant, "neither")
        self.assertEqual(parsed.slot, "morning")

    def test_no_prefix_is_non_funded(self):
        parsed = pull_sessions.parse_title("Full Day")
        self.assertFalse(parsed.funded)
        self.assertIsNone(parsed.variant)
        self.assertEqual(parsed.slot, "full_day")

    def test_slot_text_is_normalised_the_same_way_resolve_session_uses(self):
        # "Full Day" -> "full_day" via the SAME function resolve_session calls.
        from integrations import session_catalogue

        parsed = pull_sessions.parse_title("(F) Full Day")
        self.assertEqual(parsed.slot, session_catalogue.normalise_slot("Full Day"))

    def test_an_unrecognised_prefix_raises(self):
        with self.assertRaises(pull_sessions.TitleParseError) as ctx:
            pull_sessions.parse_title("(FX) Morning")
        self.assertIn("unrecognised prefix", str(ctx.exception))
        self.assertIn("(FX)", str(ctx.exception))

    def test_a_recognised_prefix_with_unmappable_text_raises(self):
        with self.assertRaises(pull_sessions.TitleParseError) as ctx:
            pull_sessions.parse_title("(F) Brunch")
        self.assertIn("Brunch", str(ctx.exception))
        self.assertIn("known slot", str(ctx.exception))

    def test_no_prefix_with_unmappable_text_raises(self):
        with self.assertRaises(pull_sessions.TitleParseError):
            pull_sessions.parse_title("Something Else Entirely")

    def test_an_empty_title_raises(self):
        with self.assertRaises(pull_sessions.TitleParseError):
            pull_sessions.parse_title("")
        with self.assertRaises(pull_sessions.TitleParseError):
            pull_sessions.parse_title(None)


class PullAllMatchingTests(PullSessionsTestCase):
    def test_matched_sessions_are_written_under_the_right_variant_and_slot(self):
        client = FakeRest(
            {
                CITY_ID: [
                    session("s-1", "(F) Morning"),
                    session("s-2", "(FNM) Afternoon"),
                    session("s-3", "Full Day"),  # non-funded
                ],
                CW_ID: [],
            }
        )

        result = pull_sessions.pull_all(client=client)

        city = result.catalogue["institutions"]["HDCITY"]
        self.assertEqual(city["funded"]["with_meals_and_activities"]["morning"], "s-1")
        self.assertEqual(city["funded"]["no_meals"]["afternoon"], "s-2")
        self.assertEqual(city["non_funded"]["full_day"], "s-3")
        self.assertEqual(result.written_counts["HDCITY"], 3)
        self.assertEqual(result.unmatched, [])

    def test_the_discontinued_flag_is_sent_as_false(self):
        client = FakeRest({CITY_ID: [], CW_ID: []})
        pull_sessions.pull_all(client=client)

        for _path, params in client.calls:
            self.assertEqual(params["includeDiscontinued"], "false")


class UnmatchedTitleTests(PullSessionsTestCase):
    def test_an_unrecognised_prefix_is_collected_and_not_written(self):
        client = FakeRest({CITY_ID: [session("s-1", "(FX) Morning")], CW_ID: []})

        result = pull_sessions.pull_all(client=client)

        self.assertEqual(result.written_counts["HDCITY"], 0)
        self.assertEqual(len(result.unmatched), 1)
        bad = result.unmatched[0]
        self.assertEqual(bad.institution, "HDCITY")
        self.assertEqual(bad.session_id, "s-1")
        self.assertEqual(bad.title, "(FX) Morning")
        self.assertIn("unrecognised prefix", bad.reason)

        # Nothing was guessed at -- no funded branch at all for this pull.
        city = result.catalogue["institutions"]["HDCITY"]
        self.assertEqual(city["funded"], {})

    def test_a_recognised_prefix_with_unmappable_slot_is_collected(self):
        client = FakeRest({CITY_ID: [session("s-2", "(F) Brunch")], CW_ID: []})

        result = pull_sessions.pull_all(client=client)

        self.assertEqual(result.written_counts["HDCITY"], 0)
        self.assertEqual(len(result.unmatched), 1)
        self.assertIn("Brunch", result.unmatched[0].title)
        self.assertIn("known slot", result.unmatched[0].reason)

    def test_a_good_and_a_bad_title_together_only_the_bad_one_is_unmatched(self):
        client = FakeRest(
            {
                CITY_ID: [session("s-1", "(F) Morning"), session("s-2", "(ZZ) Morning")],
                CW_ID: [],
            }
        )

        result = pull_sessions.pull_all(client=client)

        self.assertEqual(result.written_counts["HDCITY"], 1)
        self.assertEqual(len(result.unmatched), 1)
        self.assertEqual(result.unmatched[0].session_id, "s-2")


class MergeBehaviorTests(PullSessionsTestCase):
    """A partial run (a failed or skipped institution) must not lose data."""

    def test_an_unreachable_institution_keeps_its_existing_entries(self):
        self.write_existing_session_catalogue(
            {
                "institutions": {
                    "HDCITY": {
                        "funded": {
                            "with_meals_and_activities": {"morning": MORNING_UUID}
                        },
                        "non_funded": {"full_day": AFTERNOON_UUID},
                    }
                }
            }
        )

        client = FakeRest(
            {
                CITY_ID: RestHTTPError("boom", status_code=502, body="down"),
                CW_ID: [session("s-9", "Full Day")],
            }
        )

        result = pull_sessions.pull_all(client=client)

        self.assertIn("HDCITY", result.institutions_failed)
        # Untouched by this run -- still exactly what was on disk before.
        city = result.catalogue["institutions"]["HDCITY"]
        self.assertEqual(
            city["funded"]["with_meals_and_activities"]["morning"], MORNING_UUID
        )
        self.assertEqual(city["non_funded"]["full_day"], AFTERNOON_UUID)

        # The reachable institution still gets its own fresh data.
        cw = result.catalogue["institutions"]["HDCW"]
        self.assertEqual(cw["non_funded"]["full_day"], "s-9")

    def test_a_fresh_pull_does_not_clobber_a_different_slot_already_filled(self):
        self.write_existing_session_catalogue(
            {
                "institutions": {
                    "HDCITY": {
                        "funded": {
                            "with_meals_and_activities": {"afternoon": AFTERNOON_UUID}
                        },
                        "non_funded": {},
                    }
                }
            }
        )

        client = FakeRest(
            {CITY_ID: [session("s-1", "(F) Morning")], CW_ID: []}
        )

        result = pull_sessions.pull_all(client=client)

        city = result.catalogue["institutions"]["HDCITY"]
        variant = city["funded"]["with_meals_and_activities"]
        # The old afternoon entry survives...
        self.assertEqual(variant["afternoon"], AFTERNOON_UUID)
        # ...alongside the newly pulled morning one.
        self.assertEqual(variant["morning"], "s-1")

    def test_an_institution_no_longer_in_catalogue_json_is_left_alone(self):
        self.write_existing_session_catalogue(
            {
                "institutions": {
                    "HDOLD": {
                        "funded": {},
                        "non_funded": {"full_day": "legacy-uuid"},
                    }
                }
            }
        )

        client = FakeRest({CITY_ID: [], CW_ID: []})
        result = pull_sessions.pull_all(client=client)

        # HDOLD isn't in catalogue.json's sites any more, so it was never
        # iterated -- but it must still be there in the merged result.
        self.assertEqual(
            result.catalogue["institutions"]["HDOLD"]["non_funded"]["full_day"],
            "legacy-uuid",
        )


class WriteCatalogueTests(PullSessionsTestCase):
    def test_a_backup_is_created_before_the_existing_file_is_overwritten(self):
        original = {"institutions": {"HDCITY": {"funded": {}, "non_funded": {}}}}
        self.write_existing_session_catalogue(original)

        client = FakeRest({CITY_ID: [session("s-1", "Full Day")], CW_ID: []})
        result = pull_sessions.pull_all(client=client)

        backup = pull_sessions.write_catalogue(
            result, path=self.session_catalogue_file
        )

        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        # The backup holds exactly the PRE-write content.
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

        # And the live file now holds the new, merged content.
        written = json.loads(self.session_catalogue_file.read_text(encoding="utf-8"))
        self.assertEqual(
            written["institutions"]["HDCITY"]["non_funded"]["full_day"], "s-1"
        )

    def test_no_backup_when_there_was_no_existing_file(self):
        client = FakeRest({CITY_ID: [], CW_ID: []})
        result = pull_sessions.pull_all(client=client)

        self.assertFalse(self.session_catalogue_file.exists())
        backup = pull_sessions.write_catalogue(
            result, path=self.session_catalogue_file
        )

        self.assertIsNone(backup)
        self.assertTrue(self.session_catalogue_file.exists())

    def test_a_leading_comment_block_is_preserved(self):
        self.write_existing_session_catalogue(
            {"_comment": ["hello"], "institutions": {}}
        )

        client = FakeRest({CITY_ID: [], CW_ID: []})
        result = pull_sessions.pull_all(client=client)
        pull_sessions.write_catalogue(result, path=self.session_catalogue_file)

        written = json.loads(self.session_catalogue_file.read_text(encoding="utf-8"))
        self.assertEqual(written["_comment"], ["hello"])


class SkippedInstitutionTests(PullSessionsTestCase):
    def test_a_site_with_no_institution_id_is_skipped_not_errored(self):
        self.catalogue_file.write_text(
            json.dumps(
                {"sites": {"HDNONE": {"label": "No institution yet"}}}
            ),
            encoding="utf-8",
        )

        client = FakeRest({})
        result = pull_sessions.pull_all(client=client)

        self.assertIn("HDNONE", result.institutions_skipped)
        self.assertEqual(result.institutions_failed, {})
        self.assertEqual(client.calls, [])


class InstitutionFilterTests(PullSessionsTestCase):
    """The --institution / `institutions=` filter, at the runner level."""

    def test_filtering_to_one_institution_only_calls_that_ones_api(self):
        client = FakeRest(
            {
                CITY_ID: [session("s-1", "Full Day")],
                CW_ID: [session("s-2", "Full Day")],
            }
        )

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        called_institution_ids = {params["institutionId"] for _path, params in client.calls}
        self.assertEqual(called_institution_ids, {CITY_ID})
        self.assertEqual(result.institutions_pulled, ["HDCITY"])
        self.assertNotIn("HDCW", result.written_counts)

    def test_filtering_leaves_other_institutions_catalogue_entries_untouched(self):
        self.write_existing_session_catalogue(
            {
                "institutions": {
                    "HDCW": {
                        "funded": {},
                        "non_funded": {"full_day": AFTERNOON_UUID},
                    }
                }
            }
        )

        client = FakeRest({CITY_ID: [session("s-1", "Full Day")], CW_ID: []})
        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        # HDCW was never touched by this run, but its existing entry survives
        # the merge exactly as it was.
        self.assertEqual(
            result.catalogue["institutions"]["HDCW"]["non_funded"]["full_day"],
            AFTERNOON_UUID,
        )
        self.assertEqual(
            result.catalogue["institutions"]["HDCITY"]["non_funded"]["full_day"],
            "s-1",
        )

    def test_multiple_institutions_can_be_named(self):
        client = FakeRest(
            {
                CITY_ID: [session("s-1", "Full Day")],
                CW_ID: [session("s-2", "Full Day")],
            }
        )

        result = pull_sessions.pull_all(
            client=client, institutions=["HDCITY", "HDCW"]
        )

        self.assertEqual(set(result.institutions_pulled), {"HDCITY", "HDCW"})

    def test_matching_is_case_insensitive_and_trimmed(self):
        client = FakeRest({CITY_ID: [], CW_ID: []})
        result = pull_sessions.pull_all(client=client, institutions=["  hdcity  "])
        self.assertEqual(result.institutions_pulled, ["HDCITY"])

    def test_an_unknown_institution_code_raises_with_zero_api_calls(self):
        client = FakeRest({CITY_ID: [], CW_ID: []})

        with self.assertRaises(pull_sessions.UnknownInstitutionError) as ctx:
            pull_sessions.pull_all(client=client, institutions=["NOPE"])

        self.assertIn("NOPE", str(ctx.exception))
        self.assertEqual(client.calls, [])

    def test_one_unknown_code_among_valid_ones_still_raises_before_any_call(self):
        client = FakeRest({CITY_ID: [], CW_ID: []})

        with self.assertRaises(pull_sessions.UnknownInstitutionError):
            pull_sessions.pull_all(client=client, institutions=["HDCITY", "NOPE"])

        self.assertEqual(client.calls, [])

    def test_omitting_the_filter_behaves_like_the_unfiltered_run(self):
        client = FakeRest(
            {
                CITY_ID: [session("s-1", "Full Day")],
                CW_ID: [session("s-2", "Full Day")],
            }
        )

        without_filter = pull_sessions.pull_all(client=client)
        self.assertEqual(set(without_filter.institutions_pulled), {"HDCITY", "HDCW"})

        client_again = FakeRest(
            {
                CITY_ID: [session("s-1", "Full Day")],
                CW_ID: [session("s-2", "Full Day")],
            }
        )
        explicit_all = pull_sessions.pull_all(
            client=client_again, institutions=None
        )
        self.assertEqual(
            without_filter.written_counts, explicit_all.written_counts
        )


class SessionTitlesPullTests(PullSessionsTestCase):
    """reference/session_titles.json: the RAW Famly title of every session.

    Display-only. The readable Slack name is derived from these at read time
    (see test_display_names.py); nothing derived is ever stored here.
    """

    def titles_of(self, result, code="HDCITY"):
        return result.titles["institutions"][code]["sessions"]

    def written(self):
        return json.loads(self.session_titles_file.read_text(encoding="utf-8"))

    def test_every_returned_session_gets_its_raw_title_stored(self):
        client = FakeRest(
            {
                CITY_ID: [
                    session("s-1", "(F) Morning"),
                    session("s-2", "(FNM) Full Day"),
                    session("s-3", "Afternoon"),
                ]
            }
        )

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(
            self.titles_of(result),
            {"s-1": "(F) Morning", "s-2": "(FNM) Full Day", "s-3": "Afternoon"},
        )
        self.assertEqual(result.titles_counts, {"HDCITY": 3})

    def test_titles_are_stored_verbatim_not_in_readable_form(self):
        client = FakeRest({CITY_ID: [session("s-1", "(FNE) Full Day")]})

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(self.titles_of(result), {"s-1": "(FNE) Full Day"})

    def test_an_unclassifiable_session_still_gets_a_stored_title(self):
        client = FakeRest(
            {
                CITY_ID: [
                    session("s-ok", "(F) Morning"),
                    session("s-bad", "Weekend School"),  # no slot
                    session("s-typo", "(FX) Morning"),  # unknown prefix
                ]
            }
        )

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        # Recorded for display...
        self.assertEqual(
            self.titles_of(result),
            {
                "s-ok": "(F) Morning",
                "s-bad": "Weekend School",
                "s-typo": "(FX) Morning",
            },
        )
        # ...and STILL reported as unmatched, writing nothing to the catalogue.
        self.assertEqual(
            sorted(u.session_id for u in result.unmatched), ["s-bad", "s-typo"]
        )
        flat = json.dumps(result.catalogue)
        self.assertNotIn("s-bad", flat)
        self.assertNotIn("s-typo", flat)

    def test_a_session_with_no_id_or_no_usable_title_is_not_stored(self):
        client = FakeRest(
            {
                CITY_ID: [
                    {"title": "(F) Morning"},  # no id: nothing to key it by
                    {"id": "s-blank", "title": "   "},
                    {"id": "s-none", "title": None},
                    session("s-ok", "Afternoon"),
                ]
            }
        )

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(self.titles_of(result), {"s-ok": "Afternoon"})

    def test_existing_titles_are_merged_not_replaced(self):
        self.session_titles_file.write_text(
            json.dumps(
                {
                    "institutions": {
                        "HDCITY": {
                            "sessions": {
                                "s-old": "(F) Afternoon",  # discontinued: not returned now
                                "s-1": "stale title",
                            }
                        },
                        "HDCW": {"sessions": {"s-cw": "Full Day"}},
                    }
                }
            ),
            encoding="utf-8",
        )
        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(
            self.titles_of(result),
            {"s-old": "(F) Afternoon", "s-1": "(F) Morning"},
        )
        # An institution this run did not touch is left exactly as it was.
        self.assertEqual(self.titles_of(result, "HDCW"), {"s-cw": "Full Day"})

    def test_one_institutions_failure_leaves_its_titles_untouched(self):
        self.session_titles_file.write_text(
            json.dumps({"institutions": {"HDCW": {"sessions": {"s-cw": "Full Day"}}}}),
            encoding="utf-8",
        )
        client = FakeRest(
            {
                CITY_ID: [session("s-1", "(F) Morning")],
                CW_ID: RestHTTPError("down", 502, "nope"),
            }
        )

        result = pull_sessions.pull_all(client=client)

        self.assertIn("HDCW", result.institutions_failed)
        self.assertEqual(self.titles_of(result), {"s-1": "(F) Morning"})
        self.assertEqual(self.titles_of(result, "HDCW"), {"s-cw": "Full Day"})
        self.assertNotIn("HDCW", result.titles_counts)

    def test_the_institution_filter_applies_to_titles_too(self):
        client = FakeRest(
            {
                CITY_ID: [session("s-1", "(F) Morning")],
                CW_ID: [session("s-2", "Afternoon")],
            }
        )

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(list(result.titles["institutions"]), ["HDCITY"])

    def test_a_site_with_no_institution_id_stores_nothing(self):
        self.catalogue_file.write_text(
            json.dumps({"sites": {"HDNONE": {"label": "x"}}}), encoding="utf-8"
        )

        result = pull_sessions.pull_all(client=FakeRest({}))

        self.assertEqual(result.titles, {"institutions": {}})

    # --- writing --------------------------------------------------------- #
    def test_write_titles_writes_the_documented_shape(self):
        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})
        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        backup = pull_sessions.write_titles(result)

        self.assertIsNone(backup)  # first-ever write: nothing to back up
        written = self.written()
        self.assertEqual(
            written["institutions"], {"HDCITY": {"sessions": {"s-1": "(F) Morning"}}}
        )
        self.assertIn("_comment", written)

    def test_a_backup_is_made_before_the_titles_file_is_overwritten(self):
        original = {"institutions": {"HDCITY": {"sessions": {"s-1": "old"}}}}
        self.session_titles_file.write_text(json.dumps(original), encoding="utf-8")
        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})
        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        backup = pull_sessions.write_titles(result)

        self.assertIsNotNone(backup)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)
        self.assertEqual(
            self.written()["institutions"]["HDCITY"]["sessions"]["s-1"], "(F) Morning"
        )

    def test_an_existing_comment_is_preserved(self):
        self.session_titles_file.write_text(
            json.dumps({"_comment": ["hello"], "institutions": {}}), encoding="utf-8"
        )
        result = pull_sessions.pull_all(
            client=FakeRest({CITY_ID: [session("s-1", "Afternoon")]}),
            institutions=["HDCITY"],
        )

        pull_sessions.write_titles(result)

        self.assertEqual(self.written()["_comment"], ["hello"])

    def test_the_catalogue_file_is_not_touched_by_the_titles_write(self):
        result = pull_sessions.pull_all(
            client=FakeRest({CITY_ID: [session("s-1", "(F) Morning")]}),
            institutions=["HDCITY"],
        )

        pull_sessions.write_titles(result)

        self.assertFalse(self.session_catalogue_file.exists())

    # --- display-only: a bad titles file must not stop the catalogue ------- #
    def test_a_malformed_titles_file_is_reported_and_never_overwritten(self):
        self.session_titles_file.write_text("{not json", encoding="utf-8")
        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])

        self.assertIsNone(result.titles)
        self.assertIn("could not be read as JSON", result.titles_error)
        self.assertIsNone(pull_sessions.write_titles(result))
        self.assertEqual(self.session_titles_file.read_text(encoding="utf-8"), "{not json")

    def test_a_malformed_titles_file_does_not_stop_the_catalogue_refresh(self):
        self.session_titles_file.write_text("{not json", encoding="utf-8")
        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})

        result = pull_sessions.pull_all(client=client, institutions=["HDCITY"])
        pull_sessions.write_catalogue(result)

        self.assertEqual(result.institutions_pulled, ["HDCITY"])
        written = json.loads(self.session_catalogue_file.read_text(encoding="utf-8"))
        self.assertEqual(
            written["institutions"]["HDCITY"]["funded"]["with_meals_and_activities"],
            {"morning": "s-1"},
        )
        self.assertEqual(
            pull_sessions.summary_payload(result)["titlesError"], result.titles_error
        )

    # --- the CLI and pull-references ------------------------------------- #
    def run_cli(self, dry_run=False):
        import argparse

        from actions.pull_sessions import cli

        args = argparse.Namespace(dry_run=dry_run, institution=["HDCITY"])
        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})
        with mock.patch.object(pull_sessions, "RestClient", lambda *a, **k: client):
            return cli.handle(args)

    def test_the_cli_writes_both_files(self):
        summary = self.run_cli()

        self.assertTrue(self.session_catalogue_file.exists())
        self.assertTrue(self.session_titles_file.exists())
        self.assertEqual(summary["titlesStoredPerInstitution"], {"HDCITY": 1})
        self.assertIsNone(summary["titlesError"])

    def test_a_dry_run_writes_neither_file_but_still_reports(self):
        summary = self.run_cli(dry_run=True)

        self.assertFalse(self.session_catalogue_file.exists())
        self.assertFalse(self.session_titles_file.exists())
        self.assertTrue(summary["dryRun"])
        self.assertEqual(summary["titlesStoredPerInstitution"], {"HDCITY": 1})
        self.assertIsNone(summary["titlesBackupFile"])

    def test_pull_references_writes_the_titles_too(self):
        from actions.pull_references import runner as pull_references

        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})
        with mock.patch.object(pull_sessions, "RestClient", lambda *a, **k: client), \
             mock.patch.object(pull_references, "PULLS", {"sessions": pull_sessions}):
            outcome = pull_references.pull_all_references(institutions=["HDCITY"])
            summary = pull_references.summary_payload(outcome)

        self.assertEqual(
            self.written()["institutions"]["HDCITY"]["sessions"], {"s-1": "(F) Morning"}
        )
        self.assertEqual(summary["pulls"]["sessions"]["titlesStoredPerInstitution"], {"HDCITY": 1})

    def test_pull_references_dry_run_writes_no_titles(self):
        from actions.pull_references import runner as pull_references

        client = FakeRest({CITY_ID: [session("s-1", "(F) Morning")]})
        with mock.patch.object(pull_sessions, "RestClient", lambda *a, **k: client), \
             mock.patch.object(pull_references, "PULLS", {"sessions": pull_sessions}):
            pull_references.pull_all_references(institutions=["HDCITY"], dry_run=True)

        self.assertFalse(self.session_titles_file.exists())
        self.assertFalse(self.session_catalogue_file.exists())


if __name__ == "__main__":
    unittest.main()
