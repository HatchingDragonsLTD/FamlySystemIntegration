"""Tests for the probe-child-create ONE-OFF probe: the guards.

Run from the project root:

    python -m unittest tests.test_probe_child_create -q

Fakes only. The probe is never run against live Famly from a test: the fake
clients record every call, and each guard test asserts that no write request
(create or delete) was made when a guard should have stopped it.
"""

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.probe_child_create import cli, runner
from core.client import GraphQLError
from core.rest_client import RestHTTPError

SITE_ID = "site-cw-id"
GROUP_ID = "hatchlings-cw-id"


def gql_error(message):
    return GraphQLError("rejected", [{"message": message}])


class FakePublic:
    """The public API: introspection, create, delete, lookup by reference."""

    def __init__(self, has_delete=True):
        self.has_delete = has_delete
        self.calls = []          # operation names, in order
        self.creates = []        # payloads
        self.delete_vars = []    # variables of each delete request
        self.refs = {}           # child id -> [(system, foreignId)]
        self.deleted = set()
        self.create_rule = lambda payload: None
        self.lookup_error = None

    def execute(self, query_path, variables, operation_name):
        self.calls.append(operation_name)
        if operation_name == runner.INTROSPECT_OP:
            fields = [{"name": "create", "description": "creates", "args": [], "type": {"name": "Child"}}]
            if self.has_delete:
                fields.append({"name": "delete", "description": "Soft-deletes the given children, "
                               "or every child in the given sites", "args": [], "type": {"name": "ChildId"}})
            return {"data": {"mutation": {"fields": fields}, "input": {"inputFields": []},
                             "siteRelation": {"inputFields": []}, "reference": {"inputFields": []}}}
        if operation_name == runner.CREATE_OP:
            payload = variables["children"][0]
            self.creates.append(payload)
            self.create_rule(payload)
            child_id = f"child-{len(self.creates)}"
            self.refs[child_id] = [(r["system"], r["foreignId"]) for r in payload["externalSystemsReferences"]]
            return {"data": {"children": {"create": [{"id": child_id,
                                                      "externalId": payload["externalId"]}]}}}
        if operation_name == runner.DELETE_OP:
            self.delete_vars.append(dict(variables))
            self.deleted.update(variables["childIds"])
            return {"data": {"children": {"delete": variables["childIds"]}}}
        if operation_name == runner.LOOKUP_OP:
            if self.lookup_error:
                raise self.lookup_error
            wanted = (variables["system"].strip().upper(), variables["foreignId"].strip().casefold())
            ids = [cid for cid, refs in self.refs.items() if cid not in self.deleted
                   and any((s.upper(), f.casefold()) == wanted for s, f in refs)]
            return {"data": {"children": {"listByExternalSystemReference": {
                "result": [{"id": i} for i in ids]}}}}
        raise AssertionError(operation_name)

    @property
    def writes(self):
        return [c for c in self.calls if c in (runner.CREATE_OP, runner.DELETE_OP)]


class FakeInternal:
    """The internal API's childrenList. Lists every created, undeleted child plus
    an unrelated child whose id must never be printed."""

    def __init__(self, public):
        self.public = public
        self.calls = []

    def execute(self, query_path, variables, operation_name):
        assert operation_name == runner.LIST_OP
        self.calls.append(variables["filter"]["enrollmentStatuses"])
        rows = [{"id": "someone-elses-child", "status": "CURRENT", "startDate": "2020-01-01",
                 "endDate": None, "isTestChild": False}]
        for child_id in self.public.refs:
            if child_id not in self.public.deleted:
                rows.append({"id": child_id, "status": "UPCOMING", "startDate": "2027-01-04",
                             "endDate": "2027-12-31", "isTestChild": True})
        return {"data": {"childrenList": {"children": rows, "next": None}}}


class FakeRest:
    def __init__(self):
        self.gets = []
        self.not_test = set()    # child ids whose summary says isTestChild false
        self.flip_on_second_read = set()  # ids that stop looking like test children
        self.reads = {}
        self.error = None

    def get(self, path, params=None):
        self.gets.append(path)
        if self.error:
            raise self.error
        child_id = path.split("/")[3]
        self.reads[child_id] = self.reads.get(child_id, 0) + 1
        if child_id in self.flip_on_second_read and self.reads[child_id] >= 2:
            self.not_test.add(child_id)
        return {"child": {"id": child_id, "isTestChild": child_id not in self.not_test,
                          "group": "Hatchlings"}, "enrollment": {"status": "UPCOMING"}}


class ProbeTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "catalogue.json").write_text(json.dumps(
            {"sites": {"HDCW": {"institutionId": SITE_ID}}}), encoding="utf-8")
        (root / "groups.json").write_text(json.dumps({"institutions": {"HDCW": {"groups": {
            GROUP_ID: "Hatchlings", "other-id": "Voyagers"}}}}), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"FAMLY_CATALOGUE_FILE": str(root / "catalogue.json"),
                                           "GROUPS_CATALOGUE_FILE": str(root / "groups.json")})
        env.start()
        self.addCleanup(env.stop)
        self.public = FakePublic()
        self.internal = FakeInternal(self.public)
        self.rest = FakeRest()
        self.lines = []

    def run_probe(self, confirm=True):
        return runner.run_probe(confirm, self.rest, self.public, self.internal,
                                out=self.lines.append)

    @property
    def text(self):
        return "\n".join(self.lines)


class GuardTests(ProbeTestCase):
    def test_it_refuses_without_confirm_and_touches_nothing(self):
        with self.assertRaises(runner.ProbeRefused):
            self.run_probe(confirm=False)
        self.assertEqual((self.public.calls, self.internal.calls, self.rest.gets), ([], [], []))

    def test_a_payload_without_is_test_child_true_is_never_built_or_sent(self):
        session = runner.ProbeSession(self.rest, self.public, self.internal, self.lines.append)
        good = runner.make_child_input(runner.CHILD_A, SITE_ID, GROUP_ID, "%Y-%m-%d")
        self.assertIs(good["isTestChild"], True)
        for bad in (dict(good, isTestChild=False), dict(good, isTestChild="true"),
                    {k: v for k, v in good.items() if k != "isTestChild"},
                    dict(good, name="Real Child"), dict(good, externalId="URN-REAL")):
            with self.subTest(bad=bad.get("isTestChild", "missing")):
                with self.assertRaises(runner.ProbeRefused):
                    session.create_child(bad, "X")
        self.assertEqual(self.public.calls, [])

    def test_the_caps_are_printed_and_the_child_cap_is_enforced(self):
        with mock.patch.object(runner, "MAX_CHILDREN", 1):
            result = self.run_probe()
        self.assertEqual(len(self.public.creates), 1)
        self.assertEqual(len(result.created), 1)
        self.assertIn("CAPS: at most 1 children created, at most 8 write requests", self.text)
        self.assertIn("child cap (1) reached", self.text)

    def test_the_write_cap_counts_rejected_creates_and_deletes_alike(self):
        self.public.create_rule = lambda payload: (_ for _ in ()).throw(gql_error("bad birthday"))
        with mock.patch.object(runner, "MAX_WRITES", 3):
            result = self.run_probe()
        self.assertEqual(len(self.public.writes), 3)
        self.assertEqual(result.created, [])
        self.assertIn("write cap (3) reached", self.text)
        # With the real caps a full clean run stays at or under 8.
        self.public, self.lines = FakePublic(), []
        self.internal = FakeInternal(self.public)
        result = self.run_probe()
        self.assertLessEqual(len(self.public.writes), runner.MAX_WRITES)
        self.assertEqual(runner.MAX_WRITES, 8)

    def test_a_missing_hdcw_site_or_group_refuses_before_any_write(self):
        Path(os.environ["GROUPS_CATALOGUE_FILE"]).write_text(json.dumps({"institutions": {
            "HDCW": {"groups": {"x": "Voyagers"}}}}), encoding="utf-8")
        with self.assertRaises(runner.ProbeRefused):
            self.run_probe()
        self.assertEqual(self.public.calls, [])


class DeleteGuardTests(ProbeTestCase):
    def test_it_never_deletes_a_child_this_run_did_not_create(self):
        session = runner.ProbeSession(self.rest, self.public, self.internal, self.lines.append)
        with self.assertRaises(runner.ProbeRefused):
            session.delete_child("some-real-childs-id")
        self.assertNotIn(runner.DELETE_OP, self.public.calls)
        self.assertEqual(self.rest.gets, [])

    def test_it_re_reads_the_summary_and_will_not_delete_a_non_test_child(self):
        self.rest.flip_on_second_read.add("child-1")  # fine when inspected, not at delete time
        result = self.run_probe()
        self.assertNotIn("child-1", self.public.deleted)
        self.assertIn("child-1", result.undeleted)
        self.assertIn("NOT DELETING child-1", self.text)
        self.assertIn("DELETE THESE TEST CHILDREN BY HAND", self.text)

    def test_a_delete_request_names_exactly_one_child_and_never_site_ids(self):
        self.run_probe()
        self.assertEqual(len(self.public.delete_vars), 2)
        for variables in self.public.delete_vars:
            self.assertEqual(list(variables), ["childIds"])
            self.assertEqual(len(variables["childIds"]), 1)
        lines = runner.DELETE_QUERY.read_text(encoding="utf-8").splitlines()
        delete_doc = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        self.assertNotIn("siteIds", delete_doc)  # not declared, not passed

    def test_with_no_delete_mutation_the_ids_are_printed_instead(self):
        self.public = FakePublic(has_delete=False)
        self.internal = FakeInternal(self.public)
        result = self.run_probe()
        self.assertNotIn(runner.DELETE_OP, self.public.calls)
        self.assertEqual(result.undeleted, ["child-1", "child-2"])
        self.assertIn("DELETE THESE TEST CHILDREN BY HAND", self.text)
        self.assertIn("  child-1", self.text)


class BehaviourTests(ProbeTestCase):
    def test_payloads_match_the_brief(self):
        self.run_probe()
        a, b = self.public.creates
        for payload in (a, b):
            self.assertIs(payload["isTestChild"], True)
            self.assertNotIn("gender", payload)
            self.assertEqual((payload["records"], payload["sensitiveRecords"]), ([], []))
            self.assertEqual(payload["currentGroupId"], GROUP_ID)
        self.assertEqual((a["name"], a["birthday"], a["externalId"]),
                         ("AAProbe ChildOne", "2025-06-01", "AAPROBE-URN-001"))
        self.assertEqual(a["externalSystemsReferences"], [
            {"foreignId": "AAPROBE-DEAL-001", "system": "HUBSPOT"},
            {"foreignId": "AAPROBE-URN-001", "system": "URN"}])
        self.assertEqual(a["sitesRelation"], [{"siteId": SITE_ID, "firstDay": "2027-01-04",
                                               "lastDay": "2027-12-31"}])
        self.assertEqual((b["name"], b["externalId"]), ("AAProbe ChildTwo", "AAPROBE-URN-002"))
        self.assertEqual(b["sitesRelation"][0]["firstDay"], "2024-09-02")
        self.assertEqual(b["sitesRelation"][0]["lastDay"], "2025-07-31")

    def test_a_rejected_birthday_format_prints_the_error_and_tries_the_next(self):
        self.public.create_rule = lambda p: (_ for _ in ()).throw(
            gql_error("LocalDate parse error")) if p["birthday"] == "2025-06-01" else None
        result = self.run_probe()
        self.assertIn("REJECTED", self.text)
        self.assertIn("LocalDate parse error", self.text)
        self.assertEqual(result.birthday_format, "%Y-%m-%dT00:00:00Z")
        self.assertTrue(self.public.creates[-1]["birthday"].endswith("T00:00:00Z"))  # B too

    def test_when_no_birthday_format_works_nothing_is_created_or_deleted(self):
        self.public.create_rule = lambda p: (_ for _ in ()).throw(gql_error("nope"))
        result = self.run_probe()
        self.assertEqual((result.created, self.public.delete_vars), ([], []))
        self.assertIn("NO BIRTHDAY FORMAT WAS ACCEPTED", self.text)

    def test_the_inspection_looks_up_every_variant_and_prints_only_test_child_presence(self):
        result = self.run_probe()
        self.assertEqual(self.public.calls.count(runner.LOOKUP_OP), 2 * 5 + 2)  # 5 each + 2 final
        for variant in ("'AAPROBE-DEAL-001'", "'aaprobe-deal-001'", "'  AAPROBE-DEAL-001  '"):
            self.assertIn(variant, self.text)
        self.assertIn("system=URN", self.text)
        self.assertIn("child A present = True", self.text)
        self.assertIn("child B present = True", self.text)
        self.assertNotIn("someone-elses-child", self.text)
        self.assertEqual(self.internal.calls.count(["UPCOMING"]), 2)
        self.assertIn("FULL INTERNAL SUMMARY", self.text)
        # Cleaned up, and the final lookups confirm it.
        self.assertEqual(result.deleted, ["child-1", "child-2"])
        self.assertIn("still returned: False", self.text)
        self.assertNotIn("still returned: True", self.text)

    def test_a_failing_read_never_skips_the_delete_step(self):
        self.public.lookup_error = gql_error("lookup down")
        result = self.run_probe()
        self.assertIn("FAILED", self.text)
        self.assertEqual(result.deleted, ["child-1", "child-2"])


class CliTests(unittest.TestCase):
    def test_confirm_is_off_by_default(self):
        parser = argparse.ArgumentParser()
        cli.add_args(parser)
        self.assertFalse(parser.parse_args([]).confirm)
        self.assertTrue(parser.parse_args(["--confirm"]).confirm)

    def test_a_refusal_exits_non_zero_and_builds_no_write_call(self):
        public = FakePublic()
        with mock.patch.object(cli, "RestClient", return_value=FakeRest()), \
                mock.patch.object(cli, "_public_client", return_value=public), \
                mock.patch.object(cli, "GraphQLClient", return_value=object()):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                cli.handle(argparse.Namespace(confirm=False, no_delete=False))
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(public.calls, [])


# --------------------------------------------------------------------------- #
# --no-delete and probe-child-delete
# --------------------------------------------------------------------------- #
ID_ONE = "11111111-1111-1111-1111-111111111111"
ID_TWO = "22222222-2222-2222-2222-222222222222"
ID_THREE = "33333333-3333-3333-3333-333333333333"


class NoDeleteTests(ProbeTestCase):
    def run_no_delete(self):
        return runner.run_probe(True, self.rest, self.public, self.internal,
                                out=self.lines.append, no_delete=True)

    def test_no_delete_skips_the_delete_step_and_says_the_ids_must_be_deleted(self):
        result = self.run_no_delete()
        self.assertNotIn(runner.DELETE_OP, self.public.calls)
        self.assertEqual(self.public.delete_vars, [])
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.undeleted, ["child-1", "child-2"])
        self.assertIn("--no-delete: the delete step was SKIPPED", self.text)
        self.assertIn("MUST BE DELETED", self.text)
        self.assertIn("  child-1", self.text)
        self.assertIn("  child-2", self.text)
        self.assertIn("probe-child-delete --child-ids child-1,child-2 --confirm", self.text)
        # Nothing was deleted, so the "no longer returned" lookups are not run.
        self.assertNotIn("FINAL LOOKUPS", self.text)

    def test_no_delete_still_creates_and_inspects_within_the_caps(self):
        self.run_no_delete()
        self.assertEqual(len(self.public.creates), 2)
        self.assertIn("FULL INTERNAL SUMMARY", self.text)
        self.assertLessEqual(len(self.public.writes), runner.MAX_WRITES)

    def test_the_flag_is_off_by_default_and_passed_through(self):
        parser = argparse.ArgumentParser()
        cli.add_args(parser)
        self.assertFalse(parser.parse_args(["--confirm"]).no_delete)
        self.assertTrue(parser.parse_args(["--confirm", "--no-delete"]).no_delete)
        public = FakePublic()
        with mock.patch.object(cli, "RestClient", return_value=FakeRest()), \
                mock.patch.object(cli, "_public_client", return_value=public), \
                mock.patch.object(cli, "GraphQLClient", return_value=FakeInternal(public)), \
                mock.patch.object(runner, "resolve_site_and_group", return_value=(SITE_ID, GROUP_ID)), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.handle(argparse.Namespace(confirm=True, no_delete=True))
        self.assertNotIn(runner.DELETE_OP, public.calls)


class DeleteCommandTests(unittest.TestCase):
    def setUp(self):
        self.public = FakePublic()
        self.rest = FakeRest()
        self.lines = []

    def delete(self, ids, confirm=True):
        return runner.run_delete(ids, confirm, self.rest, self.public, out=self.lines.append)

    @property
    def text(self):
        return "\n".join(self.lines)

    def assert_nothing_happened(self):
        self.assertEqual(self.public.calls, [])
        self.assertEqual(self.public.delete_vars, [])

    def test_it_refuses_without_confirm_and_touches_nothing(self):
        with self.assertRaises(runner.ProbeRefused):
            self.delete([ID_ONE], confirm=False)
        self.assert_nothing_happened()
        self.assertEqual(self.rest.gets, [])

    def test_ids_are_limited_to_two_distinct_uuids(self):
        for raw in ("", " , ", f"{ID_ONE},{ID_TWO},{ID_THREE}", "not-a-uuid", f"{ID_ONE},oops"):
            with self.subTest(raw=raw):
                with self.assertRaises(runner.ProbeRefused):
                    runner.parse_child_ids(raw)
        self.assertEqual(runner.parse_child_ids(f" {ID_ONE} , {ID_ONE},{ID_TWO} "), [ID_ONE, ID_TWO])
        with self.assertRaises(runner.ProbeRefused):   # the run itself also enforces the cap
            self.delete([ID_ONE, ID_TWO, ID_THREE])
        self.assert_nothing_happened()

    def test_every_id_must_be_a_test_child_or_nothing_is_deleted(self):
        self.rest.not_test.add(ID_TWO)   # the SECOND id is a real child
        with self.assertRaises(runner.ProbeRefused) as ctx:
            self.delete([ID_ONE, ID_TWO])
        self.assertIn(ID_TWO, str(ctx.exception))
        self.assert_nothing_happened()   # not even the first one, which was fine

    def test_an_unreadable_summary_refuses_everything(self):
        self.rest.error = RestHTTPError("boom", 500, "body")
        with self.assertRaises(runner.ProbeRefused):
            self.delete([ID_ONE])
        self.assert_nothing_happened()

    def test_it_sends_one_child_per_request_and_never_site_ids(self):
        result = self.delete([ID_ONE, ID_TWO])
        self.assertEqual(self.public.delete_vars, [{"childIds": [ID_ONE]}, {"childIds": [ID_TWO]}])
        self.assertEqual(result.deleted, [ID_ONE, ID_TWO])
        self.assertEqual(result.writes, 2)
        for variables in self.public.delete_vars:
            self.assertEqual(list(variables), ["childIds"])
            self.assertNotIn("siteIds", variables)
        # Each id was read once up front and once again right before its delete.
        self.assertEqual(self.rest.reads, {ID_ONE: 2, ID_TWO: 2})

    def test_a_child_that_stops_being_a_test_child_before_its_delete_is_not_deleted(self):
        self.rest.flip_on_second_read.add(ID_ONE)
        result = self.delete([ID_ONE, ID_TWO])
        self.assertEqual(self.public.delete_vars, [{"childIds": [ID_TWO]}])
        self.assertEqual(result.undeleted, [ID_ONE])
        self.assertIn("NOT DELETING " + ID_ONE, self.text)
        self.assertIn("DELETE THESE TEST CHILDREN BY HAND", self.text)

    def test_with_no_delete_mutation_the_ids_are_printed_instead(self):
        self.public = FakePublic(has_delete=False)
        result = self.delete([ID_ONE])
        self.assertNotIn(runner.DELETE_OP, self.public.calls)
        self.assertEqual(result.undeleted, [ID_ONE])
        self.assertIn(f"  {ID_ONE}", self.text)

    def test_the_cli_refuses_without_confirm_with_a_non_zero_exit_and_no_request(self):
        from actions.probe_child_create import cli_delete
        parser = argparse.ArgumentParser()
        cli_delete.add_args(parser)
        self.assertFalse(parser.parse_args(["--child-ids", ID_ONE]).confirm)
        with mock.patch.object(cli_delete, "RestClient", return_value=self.rest), \
                mock.patch.object(cli_delete, "_public_client", return_value=self.public):
            for argv in (["--child-ids", ID_ONE], ["--child-ids", f"{ID_ONE},{ID_TWO},{ID_THREE}", "--confirm"]):
                with self.subTest(argv=argv):
                    with self.assertRaises(SystemExit) as ctx, \
                            contextlib.redirect_stderr(io.StringIO()):
                        cli_delete.handle(parser.parse_args(argv))
                    self.assertEqual(ctx.exception.code, 3)
        self.assert_nothing_happened()


if __name__ == "__main__":
    unittest.main()
