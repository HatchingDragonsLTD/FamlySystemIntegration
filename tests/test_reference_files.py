"""REFERENCE_DIR: where the five generated reference files live, and that every loader re-reads them."""

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.onboard_dry_run import derive
from actions.pull_groups import runner as pull_groups
from actions.pull_products import runner as pull_products
from integrations import catalogue, display_names, institution_defaults, reference_files, session_catalogue

ROOT = Path(__file__).resolve().parent.parent
PER_FILE_VARS = tuple(reference_files.ENV_VARS.values())
# name -> the function that returns the path every reader and pull of that file uses.
PATH_FUNCTIONS = {
    "session_catalogue": [session_catalogue.catalogue_path],
    "session_titles": [display_names.session_titles_path],
    "groups_catalogue": [derive.groups_path, pull_groups.catalogue_path],
    "products_catalogue": [display_names.products_catalogue_path, pull_products.catalogue_path],
    "institution_defaults": [institution_defaults.catalogue_path],
}


def clean_env(**extra):
    """The environment with every reference variable cleared, plus `extra`."""
    env = {name: "" for name in (reference_files.REFERENCE_DIR_VAR, *PER_FILE_VARS)}
    env.update(extra)
    return mock.patch.dict(os.environ, env)


class LocationTests(unittest.TestCase):
    def test_nothing_set_resolves_to_the_repo_reference_directory(self):
        with clean_env():
            for name, functions in PATH_FUNCTIONS.items():
                for function in functions:
                    self.assertEqual(function(), ROOT / "reference" / f"{name}.json", (name, function))

    def test_reference_dir_moves_all_five_and_a_per_file_variable_still_wins(self):
        with clean_env(REFERENCE_DIR="/srv/ref"):
            for name, functions in PATH_FUNCTIONS.items():
                for function in functions:
                    self.assertEqual(function(), Path("/srv/ref") / f"{name}.json", (name, function))
        with clean_env(REFERENCE_DIR="/srv/ref", GROUPS_CATALOGUE_FILE="/elsewhere/g.json"):
            self.assertEqual(derive.groups_path(), Path("/elsewhere/g.json"))
            self.assertEqual(pull_groups.catalogue_path(), Path("/elsewhere/g.json"))
            self.assertEqual(session_catalogue.catalogue_path(), Path("/srv/ref/session_catalogue.json"))

    def test_the_hand_maintained_files_are_not_moved(self):
        with clean_env(REFERENCE_DIR="/srv/ref"):
            self.assertEqual(catalogue.catalogue_path().parent, ROOT / "reference")

    def test_no_reader_or_pull_builds_the_path_of_a_generated_file_itself(self):
        """The five file names and a `/ "reference"` path join may appear nowhere but reference_files (and the
        hand-maintained catalogue and funding_rules, which are not moved)."""
        names = {reference_files.file_name(name) for name in reference_files.NAMES}
        allowed_join = {"reference_files.py", "catalogue.py", "funding_rules.py"}
        offenders = []
        files = [*ROOT.glob("main.py"), *ROOT.glob("web_registry.py"), *ROOT.glob("server.py")]
        for folder in ("actions", "core", "integrations"):
            files += (ROOT / folder).rglob("*.py")
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in names:
                    offenders.append((path.name, node.lineno, node.value))
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                        and isinstance(node.right, ast.Constant) and node.right.value == "reference"
                        and path.name not in allowed_join):
                    offenders.append((path.name, node.lineno, "reference join"))
        self.assertEqual(offenders, [])


class ReloadTests(unittest.TestCase):
    """Every loader of the five files reads the file on every call: a change between two calls is seen."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = clean_env(REFERENCE_DIR=str(self.dir))
        env.start()
        self.addCleanup(env.stop)

    def write(self, name, institutions):
        (self.dir / f"{name}.json").write_text(json.dumps({"institutions": institutions}), encoding="utf-8")

    def test_each_loader_sees_a_change_made_between_two_calls(self):
        defaults = lambda rule: {"HDCW": {"ruleGroupId": rule, "schedules": {"all_year_round": {
            "billingProfileId": "b", "attendanceScheduleId": "a", "weeksOfCare": 52, "billingId": "i",
            "billingTitle": "t", "billingInvoices": "m"}}}}
        loaders = {
            "session_catalogue": (lambda v: {"HDCW": {"non_funded": {"morning": v}}},
                                  lambda: session_catalogue.resolve_session("HDCW", False, "morning")),
            "groups_catalogue": (lambda v: {"HDCW": {"groups": {v: "Hatchlings"}}},
                                 lambda: derive.group_id_for("HDCW", "Hatchlings")),
            "institution_defaults": (defaults, lambda: institution_defaults.resolve_defaults(
                "HDCW", institution_defaults.SCHEDULE_ALL_YEAR_ROUND)["ruleGroupId"]),
            "session_titles": (lambda v: {"HDCW": {"sessions": {"s1": v}}},
                               lambda: catalogue.load_titles()[0].get("s1")),
            "products_catalogue": (lambda v: {"HDCW": {"products": {"p1": v}}},
                                   lambda: catalogue.load_titles()[1].get("p1")),
        }
        self.assertEqual(set(loaders), set(reference_files.NAMES))
        for name, (build, load) in loaders.items():
            with self.subTest(name):
                self.write(name, build("first"))
                before = load()
                self.write(name, build("second"))
                after = load()
                self.assertIsNotNone(before)
                self.assertNotEqual(before, after)
