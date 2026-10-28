"""reference-refresh: the safety rules. Fakes only: no Famly call, no Slack."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from actions.pull_references import runner as pull_references
from actions.reference_refresh import cli, runner
from integrations import reference_files

NAMES = reference_files.NAMES
ROOT = Path(__file__).resolve().parent.parent
PULL_NAMES = ("sessions", "groups", "products", "institutionDefaults")


def entries(n, prefix="id"):
    """A groups-shaped file with n filled entries."""
    return {"_comment": "x", "institutions": {"HDCW": {"groups": {f"{prefix}{i}": f"name{i}" for i in range(n)}}}}


def defaults(rule="rule1", weeks=51.5):
    return {"institutions": {"HDCW": {"ruleGroupId": rule, "schedules": {"all_year_round": {
        "billingProfileId": "b", "attendanceScheduleId": "a", "weeksOfCare": weeks, "billingId": "i",
        "billingTitle": "t", "billingInvoices": "m"}}}}}


def clean_outcome(**problems):
    """A ReferencePullResult with a fake result per pull; `problems` maps a pull name to attribute overrides."""
    results = {}
    for name in PULL_NAMES:
        attrs = {"institutions_failed": {}, "institutions_skipped": {}, "addon_products_failed": {},
                 "fetch_error": None, "titles_error": None}
        attrs.update(problems.get(name, {}))
        results[name] = SimpleNamespace(**attrs)
    return pull_references.ReferencePullResult(results=results)


class RefreshCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "famly-reference"
        self.dir.mkdir()
        env = {name: "" for name in reference_files.ENV_VARS.values()}
        env["REFERENCE_DIR"] = str(self.dir)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.posts = []
        self.seen_paths = {}
        self.originals = {}
        for name in NAMES:
            data = defaults() if name == "institution_defaults" else entries(10)
            self.write(name, data)
            self.originals[name] = (self.dir / f"{name}.json").read_bytes()

    def write(self, name, data, folder=None):
        (folder or self.dir).joinpath(f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def slack(self, text):
        self.posts.append(text)
        return True

    def pull(self, edits=None, outcome=None):
        """A fake `pull-references`: writes each edited file to the path the loaders resolve during the pull."""
        def fake():
            for name in NAMES:
                path = reference_files.path_for(name)
                self.seen_paths[name] = path
                if edits and name in edits:
                    path.write_text(json.dumps(edits[name], indent=2), encoding="utf-8")
            return outcome or clean_outcome()
        return fake

    def go(self, edits=None, outcome=None, **kwargs):
        return runner.run(post=self.slack, pull=self.pull(edits, outcome), **kwargs)

    def live_bytes(self):
        return {name: (self.dir / f"{name}.json").read_bytes() for name in NAMES}


class ValidationTests(RefreshCase):
    def test_an_empty_staged_file_refuses_everything_and_alerts_with_counts(self):
        result = self.go({"groups_catalogue": {"institutions": {}}, "session_catalogue": entries(12)})
        self.assertEqual((result["exit"], result["code"]), (1, "refused"))
        self.assertEqual(self.live_bytes(), self.originals)                   # even the good file was kept
        self.assertEqual(len(self.posts), 1)
        self.assertIn("groups_catalogue: empty (old 10 entries, new 0)", self.posts[0])
        self.assertFalse(list(self.dir.glob("*.bak.json")))

    def test_exactly_a_20_percent_drop_is_allowed_and_anything_over_is_refused(self):
        for new, allowed in ((8, True), (7, False)):
            with self.subTest(new=new):
                self.posts.clear()
                result = self.go({"groups_catalogue": entries(new)}, now=datetime(2026, 10, 29, tzinfo=timezone.utc))
                self.assertEqual(result["exit"], 0 if allowed else 1)
                if allowed:
                    self.assertEqual(result["code"], "replaced")
                else:
                    self.assertEqual(result["code"], "refused")
                    self.assertIn("old 10 entries, new 7", self.posts[0])
                    self.assertNotEqual(json.loads((self.dir / "groups_catalogue.json").read_text())["institutions"]
                                        ["HDCW"]["groups"], {})
            self.write("groups_catalogue", entries(10))                      # reset for the next case

    def test_invalid_json_is_refused(self):
        def fake():
            reference_files.path_for("groups_catalogue").write_text("{not json", encoding="utf-8")
            return clean_outcome()
        result = runner.run(post=self.slack, pull=fake)
        self.assertEqual((result["exit"], result["code"]), (1, "refused"))
        self.assertIn("not valid JSON", self.posts[0])
        self.assertEqual(self.live_bytes(), self.originals)


class PullErrorTests(RefreshCase):
    def test_a_pull_error_for_any_institution_replaces_nothing(self):
        outcome = clean_outcome(sessions={"institutions_failed": {"HDCW": "boom with a url"}})
        result = self.go({"groups_catalogue": entries(11)}, outcome)           # a change is staged, still not applied
        self.assertEqual((result["exit"], result["code"]), (1, "pulls_failed"))
        self.assertEqual(self.live_bytes(), self.originals)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("sessions: HDCW", self.posts[0])
        self.assertNotIn("boom", self.posts[0])                                 # the error text is never posted

    def test_a_whole_pull_failing_replaces_nothing(self):
        outcome = clean_outcome()
        outcome.errors["groups"] = "connection refused"
        result = self.go({"groups_catalogue": entries(11)}, outcome)
        self.assertEqual((result["exit"], result["code"]), (1, "pulls_failed"))
        self.assertEqual(self.live_bytes(), self.originals)
        self.assertIn("groups: pull_failed", self.posts[0])


class ReplaceTests(RefreshCase):
    def test_all_or_nothing_when_a_replace_fails_part_way(self):
        edits = {"groups_catalogue": entries(11), "session_catalogue": entries(12), "session_titles": entries(13)}
        calls = []

        def flaky(source, target):
            calls.append(target)
            if len(calls) == 3:
                raise OSError("disk")
            return os.replace(source, target)
        result = self.go(edits, replace=flaky)
        self.assertEqual((result["exit"], result["code"]), (1, "replace_failed"))
        self.assertEqual(self.live_bytes(), self.originals)                    # the two replaced files were put back
        self.assertFalse([p.name for p in self.dir.iterdir() if p.name.endswith((".new", ".restore"))])
        self.assertIn("replace", self.posts[0])

    def test_a_good_replace_backs_up_each_changed_file_and_leaves_the_rest(self):
        result = self.go({"groups_catalogue": entries(12)}, now=datetime(2026, 10, 29, 3, 10, tzinfo=timezone.utc))
        self.assertEqual((result["exit"], result["code"], result["posted"]), (0, "replaced", True))
        self.assertEqual(len(json.loads((self.dir / "groups_catalogue.json").read_text())["institutions"]["HDCW"]["groups"]), 12)
        self.assertEqual([p.name for p in self.dir.glob("*.bak.json")], ["groups_catalogue.20261029T031000Z.bak.json"])
        self.assertEqual((self.dir / "session_catalogue.json").read_bytes(), self.originals["session_catalogue"])
        self.assertFalse((self.dir / ".staging").exists())

    def test_no_change_posts_nothing_and_writes_no_backup(self):
        result = self.go()
        self.assertEqual((result["exit"], result["code"], self.posts), (0, "no_change", []))
        self.assertFalse(list(self.dir.glob("*.bak.json")))
        self.assertEqual(self.live_bytes(), self.originals)

    def test_a_failed_slack_post_after_a_replace_exits_non_zero_but_keeps_the_new_file(self):
        result = runner.run(post=lambda text: False, pull=self.pull({"groups_catalogue": entries(12)}))
        self.assertEqual((result["exit"], result["code"], result["posted"]), (1, "replaced_post_failed", False))
        self.assertEqual(len(json.loads((self.dir / "groups_catalogue.json").read_text())["institutions"]["HDCW"]["groups"]), 12)


class PruneTests(RefreshCase):
    def test_prune_keeps_the_newest_5_per_file_and_never_touches_the_hand_maintained_files(self):
        stamps = [f"2026100{d}T031000Z" for d in range(1, 9)]
        for stamp in stamps:
            for stem in ("groups_catalogue", "session_catalogue"):
                (self.dir / f"{stem}.{stamp}.bak.json").write_text("{}", encoding="utf-8")
        hand = ["catalogue.json", "funding_rules.json", "catalogue.20260101T000000Z.bak.json",
                "funding_rules.20260101T000000Z.bak.json", "groups_catalogue.notes.json"]
        for name in hand:
            (self.dir / name).write_text("{}", encoding="utf-8")
        for name in NAMES:
            runner.prune_backups(self.dir / f"{name}.json")
        for stem in ("groups_catalogue", "session_catalogue"):
            kept = sorted(p.name for p in self.dir.glob(f"{stem}.2*.bak.json"))
            self.assertEqual(kept, [f"{stem}.{s}.bak.json" for s in stamps[-5:]])
        for name in hand:
            self.assertTrue((self.dir / name).exists(), name)

    def test_a_run_prunes_after_a_replace(self):
        for d in range(1, 7):
            (self.dir / f"groups_catalogue.2026100{d}T031000Z.bak.json").write_text("{}", encoding="utf-8")
        result = self.go({"groups_catalogue": entries(11)}, now=datetime(2026, 10, 29, tzinfo=timezone.utc))
        self.assertEqual(result["pruned"], 2)
        self.assertEqual(len(list(self.dir.glob("groups_catalogue.*.bak.json"))), 5)


class SummaryTests(RefreshCase):
    def test_an_institution_defaults_change_posts_its_own_marked_line_with_keys_and_counts_only(self):
        result = self.go({"institution_defaults": defaults(weeks=48.0), "groups_catalogue": entries(11)})
        self.assertEqual(result["exit"], 0)
        text = self.posts[0]
        self.assertIn("pricing inputs changed (institution_defaults): 1 key(s)", text)
        self.assertIn("HDCW: 1 key(s): schedules.all_year_round.weeksOfCare", text)
        self.assertIn("groups_catalogue HDCW: 1 added, 0 removed, 0 changed", text)
        for value in ("48.0", "name0", "rule1"):
            self.assertNotIn(value, text)

    def test_a_change_to_another_file_has_no_pricing_line(self):
        self.go({"groups_catalogue": entries(11)})
        self.assertNotIn("pricing inputs changed", self.posts[0])


class SafetyTests(RefreshCase):
    def test_the_pulls_only_ever_resolve_staged_paths_and_the_environment_is_restored(self):
        repo_before = sorted(p.name for p in (ROOT / "reference").iterdir())
        env_before = dict(os.environ)
        self.go({"groups_catalogue": entries(11)})
        staging = self.dir / ".staging"
        for name, path in self.seen_paths.items():
            self.assertEqual(path, staging / f"{name}.json", name)
        self.assertEqual(sorted(p.name for p in (ROOT / "reference").iterdir()), repo_before)
        self.assertEqual(dict(os.environ), env_before)

    def test_dry_run_replaces_nothing_posts_nothing_and_reports_the_diff(self):
        result = self.go({"groups_catalogue": entries(12)}, dry_run=True)
        self.assertEqual((result["exit"], result["code"], self.posts), (0, "dry_run", []))
        self.assertEqual(result["changes"], {"groups_catalogue": {"added": 2, "removed": 0, "changed": 0}})
        self.assertIn("famlyCalls", result)
        self.assertIn("seconds", result)
        self.assertEqual(self.live_bytes(), self.originals)
        self.assertFalse(list(self.dir.glob("*.bak.json")))

    def test_without_reference_dir_it_refuses_to_run(self):
        with mock.patch.dict(os.environ, {"REFERENCE_DIR": ""}):
            result = runner.run(post=self.slack, pull=self.pull())
        self.assertEqual((result["exit"], result["code"], self.posts), (1, "reference_dir_not_set", []))

    def test_the_cli_exits_non_zero_on_a_refusal(self):
        args = mock.Mock(dry_run=False)
        with mock.patch.object(runner, "run", return_value={"exit": 1, "code": "refused"}):
            with self.assertRaises(SystemExit) as ctx:
                cli.handle(args)
        self.assertEqual(ctx.exception.code, 1)
