"""reference-refresh: the half pair and the unmatched-session rules. They guard pricing inputs, so every rule
is tested for what it refuses AND for what a refusal leaves untouched. Fakes only."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from actions.pull_products import runner as pull_products
from actions.pull_references import runner as pull_references
from actions.reference_refresh import runner
from tests.test_reference_refresh import PULL_NAMES, RefreshCase, defaults, entries

HALVES = {"halfMealsProductId": "half-m", "halfActivitiesProductId": "half-a"}
FULL = {"mealsProductId": "m", "activitiesProductId": "a"}
NOW = datetime(2026, 10, 29, 3, 10, tzinfo=timezone.utc)


def with_addons(addon, code="HDCW", rule="rule1"):
    data = defaults(rule=rule)
    data["institutions"][code]["addonProducts"] = addon
    return data


def unmatched(code, session_id, reason="unrecognised prefix", slot=None):
    return SimpleNamespace(institution=code, session_id=session_id, title="(FX) Morning", reason=reason, slot=slot)


def outcome(sessions=(), half_failed=(), products=(), groups=()):
    results = {}
    for name in PULL_NAMES:
        results[name] = SimpleNamespace(institutions_failed={}, institutions_skipped={}, addon_products_failed={},
                                        fetch_error=None, titles_error=None, unmatched=[], half_products_failed={})
    results["sessions"].unmatched = list(sessions)
    results["products"].half_products_failed = {code: "no half title" for code in half_failed}
    results["products"].unmatched = list(products)
    results["groups"].unmatched = list(groups)
    return pull_references.ReferencePullResult(results=results)


class Untouched:
    """Asserts a refused run left everything exactly as it was."""

    def assert_nothing_written(self, known_before=None):
        self.assertEqual(self.live_bytes(), self.originals)
        self.assertFalse(list(self.dir.glob("*.bak.json")))
        self.assertFalse([p.name for p in self.dir.iterdir() if p.name.endswith((".new", ".restore"))])
        self.assertFalse((self.dir / ".staging").exists())
        known = self.dir / runner.KNOWN_FILE_NAME
        if known_before is None:
            self.assertFalse(known.exists())
        else:
            self.assertEqual(known.read_bytes(), known_before)


class HalfPairTests(RefreshCase, Untouched):
    def live_with_halves(self):
        self.write("institution_defaults", with_addons({**FULL, **HALVES}))
        self.originals["institution_defaults"] = (self.dir / "institution_defaults.json").read_bytes()

    def test_a_staged_file_that_lacks_a_half_pair_the_live_file_has_is_refused_and_nothing_is_written(self):
        self.live_with_halves()
        result = self.go({"institution_defaults": with_addons(FULL), "groups_catalogue": entries(11)},
                         outcome(half_failed=["HDCW"]))
        self.assertEqual((result["exit"], result["code"]), (1, "refused"))
        self.assertIn("half-day add-on pair removed for HDCW", self.posts[0])
        self.assert_nothing_written()                                         # not even the other, good file

    def test_one_missing_half_counts_as_a_removal(self):
        self.live_with_halves()
        result = self.go({"institution_defaults": with_addons({**FULL, "halfMealsProductId": "half-m"})})
        self.assertEqual(result["code"], "refused")

    def test_a_failed_half_pull_through_the_real_writer_keeps_the_ids_so_the_refresh_goes_ahead(self):
        self.live_with_halves()

        def fake_pull():
            path = runner.reference_files.path_for("institution_defaults")
            failed = SimpleNamespace(addon_products={"HDCW": dict(FULL)}, half_products_failed={"HDCW": "no title"},
                                     extra_backups={})
            with mock.patch.object(pull_products, "backup_path", lambda p: p.with_name("discard.bak")):
                pull_products.write_addon_products(failed, path=path)
            runner.reference_files.path_for("groups_catalogue").write_text(json.dumps(entries(11)), encoding="utf-8")
            return outcome(half_failed=["HDCW"])
        result = runner.run(post=self.slack, pull=fake_pull, now=NOW)
        self.assertEqual((result["exit"], result["code"]), (0, "replaced"))
        kept = json.loads((self.dir / "institution_defaults.json").read_text())["institutions"]["HDCW"]["addonProducts"]
        self.assertEqual(kept, {**FULL, **HALVES})                             # the working half ids survived
        self.assertEqual(result["report"]["halfPair"], {"HDCW": "failed_kept"})

    def test_a_pair_that_was_never_there_is_not_a_removal(self):
        result = self.go({"institution_defaults": with_addons(FULL), "groups_catalogue": entries(11)},
                         outcome(half_failed=["HDCW"]))
        self.assertEqual((result["exit"], result["report"]["halfPair"]), (0, {"HDCW": "failed_missing"}))

    def test_a_present_pair_and_an_absent_pair_are_reported_per_institution(self):
        data = with_addons({**FULL, **HALVES})
        data["institutions"]["HDCITY"] = {"ruleGroupId": "r", "addonProducts": dict(FULL)}
        self.write("institution_defaults", data)
        self.originals["institution_defaults"] = (self.dir / "institution_defaults.json").read_bytes()
        result = self.go(None, outcome(), dry_run=True)
        self.assertEqual(result["report"]["halfPair"], {"HDCITY": "absent", "HDCW": "present"})


class UnmatchedSessionTests(RefreshCase, Untouched):
    def known_file(self, ids):
        path = self.dir / runner.KNOWN_FILE_NAME
        path.write_text(json.dumps({"recorded": "x", "ids": sorted(ids)}, indent=2), encoding="utf-8")
        return path.read_bytes()

    def known_ids(self):
        return json.loads((self.dir / runner.KNOWN_FILE_NAME).read_text())["ids"]

    def run_with(self, sessions, edits=None, **kwargs):
        return self.go(edits, outcome(sessions=sessions), now=NOW, **kwargs)

    def test_no_unmatched_sessions_passes_with_or_without_a_known_set(self):
        self.assertEqual(self.run_with([])["exit"], 0)
        self.assertFalse((self.dir / runner.KNOWN_FILE_NAME).exists())          # nothing to record
        self.known_file([])
        self.assertEqual(self.run_with([])["exit"], 0)                          # an empty known set passes

    def test_unmatched_sessions_with_no_recorded_set_are_refused_and_nothing_is_written(self):
        result = self.run_with([unmatched("HDCW", "s1"), unmatched("HDCW", "s2"), unmatched("HDCITY", "s3")],
                               {"groups_catalogue": entries(11)})
        self.assertEqual((result["exit"], result["code"]), (1, "refused"))
        self.assertIn("3 unmatched session(s) and no baseline recorded", self.posts[0])
        self.assertIn("HDCW 2 (s1, s2)", self.posts[0])
        self.assert_nothing_written()

    def test_the_baseline_flag_records_the_current_set_and_the_next_run_passes_on_it(self):
        sessions = [unmatched("HDCW", "s1"), unmatched("HDCITY", "s3")]
        result = self.run_with(sessions, {"groups_catalogue": entries(11)}, record_baseline=True)
        self.assertEqual(result["exit"], 0)
        self.assertEqual(self.known_ids(), ["s1", "s3"])
        self.posts.clear()
        self.assertEqual(self.run_with(sessions)["code"], "no_change")           # known ids pass, nothing posted
        self.assertEqual(self.posts, [])

    def test_a_known_id_passes_and_a_new_id_is_refused_with_the_new_ids_and_counts(self):
        known = self.known_file(["s1"])
        self.assertEqual(self.run_with([unmatched("HDCW", "s1")])["exit"], 0)
        result = self.run_with([unmatched("HDCW", "s1"), unmatched("HDCW", "s9"), unmatched("HDCITY", "s8")],
                               {"groups_catalogue": entries(11)})
        self.assertEqual((result["exit"], result["code"]), (1, "refused"))
        self.assertIn("2 NEW unmatched session(s) (1 known)", self.posts[-1])
        self.assertIn("HDCW 1 (s9)", self.posts[-1])
        self.assertIn("HDCITY 1 (s8)", self.posts[-1])
        self.assertNotIn("s1,", self.posts[-1])
        self.assert_nothing_written(known_before=known)

    def test_accept_adds_a_reviewed_id_logs_it_and_a_second_run_passes(self):
        self.known_file(["s1"])
        sessions = [unmatched("HDCW", "s1"), unmatched("HDCW", "s9")]
        with mock.patch.object(runner.logger, "info") as info:
            result = self.run_with(sessions, {"groups_catalogue": entries(11)}, accept_unmatched=["s9"])
        self.assertEqual(result["exit"], 0)
        self.assertEqual(self.known_ids(), ["s1", "s9"])
        self.assertTrue(any("accepted unmatched session id=%s" in c.args[0] and c.args[1] == "s9"
                            for c in info.call_args_list))
        self.assertEqual(self.run_with(sessions)["exit"], 0)

    def test_accepting_an_id_that_is_not_unmatched_is_refused_and_writes_nothing(self):
        known = self.known_file(["s1"])
        result = self.run_with([unmatched("HDCW", "s1")], {"groups_catalogue": entries(11)},
                               accept_unmatched=["typo-id"])
        self.assertEqual((result["exit"], result["code"]), (1, "refused"))
        self.assertIn("not unmatched now: typo-id", self.posts[0])
        self.assert_nothing_written(known_before=known)

    def test_accept_does_not_replace_the_baseline_rule(self):
        result = self.run_with([unmatched("HDCW", "s1")], accept_unmatched=["s1"])
        self.assertEqual(result["code"], "refused")                              # a baseline must be recorded first
        self.assert_nothing_written()

    def test_a_refusal_for_another_reason_leaves_the_known_set_alone(self):
        known = self.known_file(["s1"])
        result = self.run_with([unmatched("HDCW", "s1")], {"groups_catalogue": {"institutions": {}}},
                               accept_unmatched=["s1"])
        self.assertEqual(result["code"], "refused")
        self.assert_nothing_written(known_before=known)

    def test_a_resolved_id_leaves_the_known_set_so_it_is_new_if_it_breaks_again(self):
        self.known_file(["s1", "s2"])
        self.run_with([unmatched("HDCW", "s1")])                                 # s2 resolved
        self.assertEqual(self.known_ids(), ["s1"])
        self.assertEqual(self.run_with([unmatched("HDCW", "s1"), unmatched("HDCW", "s2")])["code"], "refused")

    def test_ambiguous_slots_and_a_session_with_no_id_are_unmatched_ids_and_counted(self):
        sessions = [unmatched("HDCW", "a", "ambiguous_slot", "non_funded.full_day"),
                    unmatched("HDCW", "b", "ambiguous_slot", "non_funded.full_day"),
                    unmatched("HDCW", None)]
        result = self.run_with(sessions, record_baseline=True)
        self.assertEqual(result["exit"], 0)
        self.assertEqual(self.known_ids(), ["HDCW:no_id", "a", "b"])
        self.assertEqual(result["report"]["ambiguousSlots"], {"HDCW": 1})
        self.assertEqual(result["report"]["unmatchedSessions"], {"HDCW": 3})

    def test_a_duplicate_slot_is_refused_until_it_is_reviewed(self):
        sessions = [unmatched("HDCW", "a", "ambiguous_slot", "non_funded.full_day"),
                    unmatched("HDCW", "b", "ambiguous_slot", "non_funded.full_day")]
        self.known_file([])
        result = self.run_with(sessions)
        self.assertEqual(result["code"], "refused")
        self.assertIn("2 NEW unmatched session(s) (0 known): HDCW 2 (a, b)", self.posts[0])


class DryRunAndWarningTests(RefreshCase, Untouched):
    def test_dry_run_reports_the_baseline_and_writes_no_known_set_even_with_the_flag(self):
        result = self.go(None, outcome(sessions=[unmatched("HDCW", "s1", "ambiguous_slot", "non_funded.morning")]),
                         dry_run=True, record_baseline=True)
        self.assertEqual(result["exit"], 0)
        self.assertEqual(result["report"]["unmatchedSessions"], {"HDCW": 1})
        self.assertEqual(result["report"]["ambiguousSlots"], {"HDCW": 1})
        self.assertFalse(result["report"]["knownSetRecorded"])
        self.assertEqual(self.posts, [])
        self.assert_nothing_written()

    def test_dry_run_without_a_baseline_shows_the_refusal_and_posts_nothing(self):
        result = self.go(None, outcome(sessions=[unmatched("HDCW", "s1")]), dry_run=True)
        self.assertEqual((result["exit"], result["code"], self.posts), (1, "refused", []))
        self.assertEqual(result["report"]["newUnmatchedSessions"], 1)

    def test_product_and_group_unmatched_stay_warnings(self):
        with mock.patch.object(runner.logger, "warning") as warning:
            result = self.go(None, outcome(products=[object(), object()], groups=[object()]))
        self.assertEqual((result["exit"], result["code"]), (0, "no_change"))
        self.assertEqual((result["report"]["productsUnmatched"], result["report"]["groupsUnmatched"]), (2, 1))
        self.assertTrue(any("warnings only" in c.args[0] for c in warning.call_args_list))
        self.assertEqual(self.posts, [])
