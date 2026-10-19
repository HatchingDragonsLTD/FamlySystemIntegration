"""probe-bill-payers write: caps, confirm gating, send-once, no PII. Fakes only."""

import json
import re
import unittest
from pathlib import Path

import requests

from actions.probe_bill_payers import cli, runner, write_probe
from core.client import GraphQLError, GraphQLHTTPError
from core.rest_client import RestHTTPError
from tests.test_probe_bill_payers import _list, _nn, _t

SITE = "HDCW"
TEST_EMAIL = "Probe.Owner+bp@Example.org"
CHILD = "child-test-1"


def schema(delete_with_site=False, with_delete=True, share_required=True, extra_required=False):
    delete_args = [{"name": "billPayerIds", "type": _nn(_list(_nn(_t("SCALAR", "UniqueId"))))}]
    if delete_with_site:
        delete_args.append({"name": "siteIds", "type": _nn(_list(_nn(_t("SCALAR", "SiteId"))))})
    mut_fields = [{"name": "create", "args": [
        {"name": "billPayers", "type": _nn(_list(_nn(_t("INPUT_OBJECT", "BillPayerInput"))))}],
        "type": _nn(_list(_nn(_t("OBJECT", "BillPayer"))))}]
    if with_delete:
        mut_fields.append({"name": "delete", "args": delete_args,
                           "type": _nn(_list(_nn(_t("SCALAR", "UniqueId"))))})
    input_fields = [
        {"name": "siteId", "type": _nn(_t("SCALAR", "SiteId"))},
        {"name": "name", "type": _nn(_t("SCALAR", "NameInput"))},
        {"name": "email", "type": _t("SCALAR", "String")},
        {"name": "externalId", "type": _t("SCALAR", "String")},
        {"name": "children", "type": _list(_nn(_t("INPUT_OBJECT", "BillPayerChildInput")))},
    ]
    if extra_required:
        input_fields.append({"name": "accountType", "type": _nn(_t("SCALAR", "String"))})
    share = {"name": "share", "type": _nn(_t("SCALAR", "Float")) if share_required else _t("SCALAR", "Float")}
    return {
        "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
        "types": [
            {"kind": "OBJECT", "name": "Mutation", "fields": [
                {"name": "billPayers", "args": [], "type": _nn(_t("OBJECT", "BillPayersMutation"))}]},
            {"kind": "OBJECT", "name": "BillPayersQuery", "fields": [
                {"name": "listBySiteIds", "args": [
                    {"name": "siteIds", "type": _nn(_list(_nn(_t("SCALAR", "SiteId"))))},
                    {"name": "nextToken", "type": _t("SCALAR", "BillPayerCursor")},
                    {"name": "includeDeleted", "type": _t("SCALAR", "Boolean"), "defaultValue": "false"}],
                 "type": _nn(_t("OBJECT", "BillPayerPage"))}]},
            {"kind": "OBJECT", "name": "BillPayersMutation", "fields": mut_fields},
            {"kind": "OBJECT", "name": "BillPayer", "fields": [
                {"name": "billPayerId", "args": [], "type": _nn(_t("SCALAR", "UniqueId"))},
                {"name": "email", "args": [], "type": _t("SCALAR", "String")},
                {"name": "externalId", "args": [], "type": _t("SCALAR", "String")},
                {"name": "childShares", "args": [], "type": _nn(_list(_nn(_t("OBJECT", "ChildShare"))))}]},
            {"kind": "OBJECT", "name": "ChildShare", "fields": [
                {"name": "childId", "args": [], "type": _nn(_t("SCALAR", "ChildId"))}]},
            {"kind": "INPUT_OBJECT", "name": "BillPayerInput", "inputFields": input_fields},
            {"kind": "INPUT_OBJECT", "name": "BillPayerChildInput", "inputFields": [
                {"name": "childId", "type": _nn(_t("SCALAR", "ChildId"))}, share]},
        ],
    }


class FakeFamly:
    """A tiny stateful Famly: bill payers with read-after-write lag, delete that hides
    from includeDeleted=false, a test child create/delete. Records every write."""

    def __init__(self, schema_=None, lag_scans=0):
        self.schema = schema_ or schema()
        self.payers = []                 # {"id", "email", "ext", "children", "deleted", "visible_at"}
        self.lag_scans = lag_scans
        self.scans = 0
        self.writes = []                 # (operation, variables, document text)
        self.create_error = None
        self.delete_error = None
        self.child_deleted = []
        self.next_id = 1

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        op = operation_name
        if op == runner.SCHEMA_OP:
            return {"data": {"__schema": self.schema}}
        if op == "ProbeChildCreate":
            self.writes.append((op, variables, text))
            return {"data": {"children": {"create": [{"id": CHILD, "externalId": "x"}]}}}
        if op == "ProbeChildDelete":
            self.writes.append((op, variables, text))
            self.child_deleted += variables["childIds"]
            return {"data": {"children": {"delete": variables["childIds"]}}}
        if op == write_probe.CREATE_OP:
            self.writes.append((op, variables, text))
            if self.create_error:
                raise self.create_error
            item = variables["input"][0]
            payer = {"id": f"bp-{self.next_id}", "email": item["email"], "ext": item["externalId"],
                     "children": [c["childId"] for c in item["children"]], "deleted": False,
                     "visible_at": self.scans + self.lag_scans}
            self.next_id += 1
            self.payers.append(payer)
            return {"data": {"billPayers": {"create": [{"billPayerId": payer["id"]}]}}}
        if op == write_probe.DELETE_OP:
            self.writes.append((op, variables, text))
            if self.delete_error:
                raise self.delete_error
            for payer in self.payers:
                if payer["id"] in variables["id"]:
                    payer["deleted"] = True
            return {"data": {"billPayers": {"delete": variables["id"]}}}
        # the list scan
        self.scans += 1
        flag = variables.get("includeDeleted")
        rows = [p for p in self.payers if self.scans > p["visible_at"] and (flag or not p["deleted"])]
        return {"data": {"billPayers": {"listBySiteIds": {"result": [
            {"billPayerId": p["id"], "email": p["email"], "externalId": p["ext"],
             "childShares": [{"childId": c} for c in p["children"]]} for p in rows], "next": None}}}}

    def mutations(self, operation):
        return [w for w in self.writes if w[0] == operation]


class FakeRest:
    def __init__(self, test_children=(CHILD,)):
        self.test_children = set(test_children)

    def get(self, path):
        child = path.split("/")[3]
        if child == "missing":
            raise RestHTTPError("nope", 404, "")
        return {"child": {"isTestChild": child in self.test_children}}


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def run(famly=None, rest=None, **kw):
    famly = famly or FakeFamly()
    clock = Clock()
    lines = []
    probe = write_probe.Probe(famly, rest or FakeRest(), out=lines.append, clock=clock,
                              sleep=clock.sleep)
    args = dict(site_code=SITE, test_email=TEST_EMAIL, child_id=CHILD, create_test_child=False,
                share=100, confirm=True)
    args.update(kw)
    result = probe.run(**args)
    return famly, probe, result, lines, clock


class GuardTests(unittest.TestCase):
    def refuse(self, **kw):
        famly = FakeFamly()
        with self.assertRaises(write_probe.ProbeRefused):
            run(famly=famly, **kw)
        self.assertEqual(famly.writes, [])                    # nothing was sent
        self.assertEqual(famly.scans, 0)                      # not even a read

    def test_it_refuses_without_confirm(self):
        self.refuse(confirm=False)

    def test_it_refuses_without_a_site_or_email(self):
        self.refuse(site_code="")
        self.refuse(test_email="")
        self.refuse(test_email="not-an-email")
        self.refuse(site_code="NOSUCH")

    def test_it_needs_exactly_one_child_option(self):
        self.refuse(child_id=None, create_test_child=False)
        self.refuse(child_id=CHILD, create_test_child=True)

    def test_an_existing_child_must_be_a_verified_test_child(self):
        famly = FakeFamly()
        with self.assertRaises(write_probe.ProbeRefused) as ctx:
            run(famly=famly, rest=FakeRest(test_children=()), child_id="real-child")
        self.assertIn("not a test child", str(ctx.exception))
        self.assertEqual(famly.writes, [])

    def test_the_cli_exits_3_and_prints_no_email_when_refused(self):
        import argparse
        import contextlib
        import io
        args = argparse.Namespace(report="write", env=None, sites="", site=SITE, test_email=TEST_EMAIL,
                                  child_id=CHILD, create_test_child=False, share="100", confirm=False,
                                  types="", api="public")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            cli.handle(args)
        self.assertEqual(ctx.exception.code, 3)
        self.assertNotIn("example.org", err.getvalue().lower())


class HappyPathTests(unittest.TestCase):
    def test_one_create_one_delete_and_a_lag_report(self):
        famly, _probe, result, lines, clock = run(famly=FakeFamly(lag_scans=2))
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)
        self.assertTrue(result.bill_payer_created and result.bill_payer_deleted)
        text = "\n".join(lines)
        # Read-after-write lag: the first two scans cannot see it, the third can.
        self.assertRegex(text, r"T\+ 0s scan: found by email=no \(count 0\), found by externalId=no")
        self.assertRegex(text, r"T\+10s scan: found by email=no")
        self.assertRegex(text, r"T\+60s scan: found by email=yes \(count 1\), found by externalId=yes "
                               r"\(count 1\), child attached=yes")
        self.assertEqual(clock.slept, [10, 50])                 # scans at +10 s and +60 s
        # After the delete: hidden with includeDeleted=false, still returned with true.
        self.assertIn("AFTER DELETE (includeDeleted=false): still returned = no", text)
        self.assertIn("AFTER DELETE (includeDeleted=true): still returned = yes", text)

    def test_the_input_sent_has_the_probe_values_and_the_share(self):
        famly, *_ = run(share=100)
        payload = famly.mutations(write_probe.CREATE_OP)[0][1]["input"][0]
        self.assertEqual(payload["name"], "ZZ Probe Test")
        self.assertEqual(payload["email"], TEST_EMAIL.strip())
        self.assertRegex(payload["externalId"], r"^probe-\d{8}T\d{6}Z$")
        self.assertEqual(payload["children"], [{"childId": CHILD, "share": 100}])

    def test_the_ids_are_printed_to_the_terminal_and_the_email_never_is(self):
        _famly, _probe, _result, lines, _clock = run()
        text = "\n".join(lines)
        self.assertIn("BILL PAYER id (terminal only): bp-1", text)
        self.assertIn("DONE. Ids for a manual clean-up", text)
        for form in (TEST_EMAIL, TEST_EMAIL.lower(), "probe.owner", "example.org"):
            self.assertNotIn(form.lower(), text.lower())

    def test_a_created_test_child_is_deleted_by_child_id_only(self):
        famly, _probe, result, lines, _clock = run(child_id=None, create_test_child=True)
        creates = famly.mutations("ProbeChildCreate")
        self.assertEqual(len(creates), 1)
        self.assertTrue(creates[0][1]["children"][0]["isTestChild"])
        deletes = famly.mutations("ProbeChildDelete")
        self.assertEqual(len(deletes), 1)
        self.assertEqual(set(deletes[0][1]), {"childIds"})       # never siteIds
        code = "".join(l for l in deletes[0][2].splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("siteIds", code)
        self.assertEqual(famly.child_deleted, [CHILD])
        self.assertTrue(result.test_child_created and result.test_child_deleted)
        # The bill payer is deleted BEFORE the child.
        order = [w[0] for w in famly.writes]
        self.assertLess(order.index(write_probe.DELETE_OP), order.index("ProbeChildDelete"))

    def test_an_existing_test_child_is_never_deleted(self):
        famly, *_ = run()
        self.assertEqual(famly.mutations("ProbeChildDelete"), [])


class SendOnceTests(unittest.TestCase):
    def test_a_400_transient_is_not_retried_and_nothing_else_is_sent(self):
        body = json.dumps({"errors": [{"extensions": {"display": "Transient"}, "message": TEST_EMAIL}]})
        famly = FakeFamly()
        famly.create_error = GraphQLHTTPError("bad", 400, body)
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)       # once, not retried
        self.assertEqual(famly.mutations(write_probe.DELETE_OP), [])           # nothing to delete
        self.assertFalse(result.bill_payer_created)
        text = "\n".join(lines)
        self.assertIn("REJECTED (sent once, not retried, no variants tried)", text)
        self.assertNotIn("example.org", text.lower())                          # echoed email redacted
        self.assertIn("<test-email>", text)

    def test_a_graphql_validation_error_stops_without_trying_variants(self):
        famly = FakeFamly()
        famly.create_error = GraphQLError("bad", [{"message": "share must be a fraction"}])
        famly, *_ = run(famly=famly)
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)

    def test_every_failure_type_sends_the_create_exactly_once(self):
        errors = [requests.Timeout(), requests.ConnectionError(), GraphQLHTTPError("x", 500, ""),
                  GraphQLHTTPError("x", 429, ""), GraphQLHTTPError("x", 400, "{}"),
                  GraphQLError("x", [{"extensions": {"display": "Transient"}}])]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                famly = FakeFamly()
                famly.create_error = error
                famly, *_ = run(famly=famly)
                self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)

    def test_an_unknown_outcome_is_settled_by_a_scan_never_a_resend(self):
        # The request "times out" after Famly created it: the scan finds it, it is deleted.
        class Landed(FakeFamly):
            def execute(self, query_path, variables, operation_name):
                if operation_name == write_probe.CREATE_OP:
                    super().execute(query_path, variables, operation_name)   # it landed ...
                    raise requests.Timeout()                                  # ... but we never heard
                return super().execute(query_path, variables, operation_name)
        famly, _probe, result, lines, _clock = run(famly=Landed())
        creates = [w for w in famly.writes if w[0] == write_probe.CREATE_OP]
        self.assertEqual(len(creates), 1)                                  # the probe sent one
        self.assertIn("UNKNOWN OUTCOME", "\n".join(lines))
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)   # found by scan, deleted

    def test_the_delete_is_sent_once_even_when_it_fails(self):
        famly = FakeFamly()
        famly.delete_error = GraphQLHTTPError("x", 500, "")
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)
        self.assertFalse(result.bill_payer_deleted)
        self.assertIn("DELETE THIS BILL PAYER BY HAND: bp-1", "\n".join(lines))


class CapTests(unittest.TestCase):
    def test_the_budget_refuses_a_second_create_or_delete(self):
        budget = write_probe.Budget()
        budget.spend("billpayer_create")
        budget.spend("billpayer_delete")
        for what in ("billpayer_create", "billpayer_delete"):
            with self.assertRaises(write_probe.ProbeRefused):
                budget.spend(what)

    def test_a_second_delete_is_refused_before_a_request(self):
        famly = FakeFamly()
        probe = write_probe.Probe(famly, FakeRest(), out=lambda _t: None)
        plan = write_probe.plan_from_schema({"data": {"__schema": famly.schema}})
        shape = runner.discover_shape({"data": {"__schema": famly.schema}})
        for _ in range(2):
            probe._delete_bill_payer(plan, shape, "S", "e", "x", CHILD, "bp-9")
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)


class DocumentTests(unittest.TestCase):
    def test_the_delete_names_the_bill_payer_id_only(self):
        plan = write_probe.plan_from_schema({"data": {"__schema": schema(delete_with_site=True)}})
        # A site-wide argument exists but is required: the delete is declared unusable.
        self.assertIsNone(plan.delete_arg)
        self.assertIn("siteIds", plan.delete_unusable)
        plan = write_probe.plan_from_schema({"data": {"__schema": schema()}})
        text = write_probe.delete_document(plan)
        self.assertNotIn("siteIds", text)
        write_probe.assert_probe_mutation(text, "delete")

    def test_a_delete_that_needs_a_site_is_never_sent(self):
        famly = FakeFamly(schema(delete_with_site=True))
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(famly.mutations(write_probe.DELETE_OP), [])
        self.assertIn("NO SAFE DELETE", "\n".join(lines))
        self.assertIn("DELETE THIS BILL PAYER IN THE FAMLY UI: bp-1", "\n".join(lines))

    def test_the_guard_refuses_documents_that_are_not_the_probe(self):
        for text, kind in [
            ("mutation ProbeBillPayerCreate($i: X) { billPayers { create(billPayers: $i) } "
             "other { delete(x: 1) } }", "create"),
            ("mutation ProbeBillPayerDelete($id: X, $s: Y) { billPayers { delete(ids: $id, siteIds: $s) } }", "delete"),
            ("mutation Other { billPayers { create(x: 1) } }", "create"),
            ("mutation ProbeBillPayerCreate($i: X) { billPayers { update(x: 1) } }", "create"),
        ]:
            with self.subTest(text=text[:40]):
                with self.assertRaises(write_probe.ProbeRefused):
                    write_probe.assert_probe_mutation(text, kind)

    def test_an_unfillable_required_field_stops_before_any_write(self):
        famly = FakeFamly(schema(extra_required=True))
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(famly.writes, [])
        self.assertIn("accountType", "\n".join(lines))
        self.assertEqual(result.stopped, "schema")

    def test_a_required_share_without_share_stops_before_any_write(self):
        famly, _probe, result, lines, _clock = run(share=None)
        self.assertEqual(famly.writes, [])
        self.assertEqual(result.stopped, "share")
        self.assertIn("share: Float!", "\n".join(lines))


class AbortIfItExistsTests(unittest.TestCase):
    def test_an_existing_test_email_aborts_with_no_writes(self):
        famly = FakeFamly()
        famly.payers.append({"id": "old", "email": TEST_EMAIL.upper(), "ext": "other",
                             "children": [], "deleted": False, "visible_at": 0})
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(famly.writes, [])
        self.assertEqual(result.stopped, "exists")

    def test_an_existing_deleted_twin_also_aborts(self):
        famly = FakeFamly()
        famly.payers.append({"id": "old", "email": TEST_EMAIL, "ext": "other",
                             "children": [], "deleted": True, "visible_at": 0})
        famly, _probe, result, *_ = run(famly=famly)
        self.assertEqual(famly.writes, [])

    def test_a_rejected_test_child_stops_before_the_bill_payer(self):
        class NoChild(FakeFamly):
            def execute(self, query_path, variables, operation_name):
                if operation_name == "ProbeChildCreate":
                    raise GraphQLError("no", [{"message": "no"}])
                return super().execute(query_path, variables, operation_name)
        famly, _probe, result, lines, _clock = run(famly=NoChild(), child_id=None, create_test_child=True)
        self.assertEqual(famly.mutations(write_probe.CREATE_OP), [])
        self.assertEqual(result.stopped, "child")


class OutputTests(unittest.TestCase):
    def test_nothing_printed_in_any_scenario_contains_the_email(self):
        scenarios = []
        a = FakeFamly()
        scenarios.append(a)
        b = FakeFamly()
        b.create_error = GraphQLHTTPError("bad", 400, "echo " + TEST_EMAIL)
        scenarios.append(b)
        c = FakeFamly()
        c.delete_error = GraphQLError("x", [{"message": TEST_EMAIL.upper()}])
        scenarios.append(c)
        for famly in scenarios:
            _f, _p, _r, lines, _c = run(famly=famly)
            blob = "\n".join(lines).lower()
            self.assertNotIn("example.org", blob)
            self.assertNotIn("probe.owner", blob)

    def test_the_command_result_has_booleans_only(self):
        _f, _p, result, _l, _c = run()
        blob = json.dumps(result.__dict__)
        self.assertNotIn("bp-", blob)
        self.assertNotIn(CHILD, blob)

    def test_the_probe_never_writes_a_file_or_logs_the_ids(self):
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        try:
            run()
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        blob = "\n".join(records).lower()
        for fragment in ("bp-1", CHILD, "example.org", "probe.owner"):
            self.assertNotIn(fragment, blob)


if __name__ == "__main__":
    unittest.main()
