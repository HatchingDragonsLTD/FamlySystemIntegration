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
from tests.test_probe_bill_payers import schema as read_schema

SITE = "HDCW"
TEST_EMAIL = "Probe.Owner+bp@Example.org"
CHILD = "child-test-1"


def schema(delete_with_site=False, with_delete=True, share_required=True, extra_required=False,
           include_deleted=True, delete_children=True, delete_children_list=True):
    """The read schema (listBySiteIds with `includeDeleted: Boolean! = true`) plus the
    bill payer mutations, including addChildren and deleteChildren."""
    delete_args = [{"name": "billPayerIds", "type": _nn(_list(_nn(_t("SCALAR", "UniqueId"))))}]
    if delete_with_site:
        delete_args.append({"name": "siteIds", "type": _nn(_list(_nn(_t("SCALAR", "SiteId"))))})
    mut_fields = [{"name": "create", "args": [
        {"name": "billPayers", "type": _nn(_list(_nn(_t("INPUT_OBJECT", "BillPayerInput"))))}],
        "type": _nn(_list(_nn(_t("OBJECT", "BillPayer"))))}]
    if with_delete:
        mut_fields.append({"name": "delete", "args": delete_args,
                           "type": _nn(_list(_nn(_t("SCALAR", "UniqueId"))))})
    share_entries = _nn(_list(_nn(_t("OBJECT", "BillPayerChildShareType"))))
    mut_fields.append({"name": "addChildren", "args": [
        {"name": "shares", "type": _nn(_list(_nn(_t("INPUT_OBJECT", "BillPayerChildAddInput"))))}],
        "type": share_entries})
    if delete_children:
        delete_type = _nn(_t("INPUT_OBJECT", "ChildSharesDeleteInput"))
        mut_fields.append({"name": "deleteChildren", "args": [
            {"name": "shares", "type": _nn(_list(delete_type)) if delete_children_list else delete_type}],
            "type": share_entries})
    input_fields = [
        {"name": "siteId", "type": _nn(_t("SCALAR", "SiteId"))},
        {"name": "name", "type": _nn(_t("SCALAR", "NameInput"))},
        {"name": "email", "type": _t("SCALAR", "String")},
        {"name": "externalId", "type": _t("SCALAR", "String")},
        {"name": "children", "type": _list(_nn(_t("INPUT_OBJECT", "BillPayerChildInput")))},
    ]
    if extra_required:
        input_fields.append({"name": "accountType", "type": _nn(_t("SCALAR", "String"))})
    share_type = _nn(_t("INPUT_OBJECT", "ChildShareInput")) if share_required else _t("INPUT_OBJECT", "ChildShareInput")
    base = read_schema(include_deleted=include_deleted)
    types = [t for t in base["types"] if t["name"] not in ("BillPayersMutation", "BillPayerChildInput")]
    types += [
        {"kind": "OBJECT", "name": "BillPayersMutation", "fields": mut_fields},
        {"kind": "INPUT_OBJECT", "name": "BillPayerInput", "inputFields": input_fields},
        {"kind": "INPUT_OBJECT", "name": "BillPayerChildInput", "inputFields": [
            {"name": "childId", "type": _nn(_t("SCALAR", "ChildId"))},
            {"name": "share", "type": share_type}]},
        {"kind": "INPUT_OBJECT", "name": "BillPayerChildAddInput", "inputFields": [
            {"name": "childId", "type": _nn(_t("SCALAR", "ChildId"))},
            {"name": "billPayerId", "type": _nn(_t("SCALAR", "UniqueId"))},
            {"name": "share", "type": share_type}]},
        # What addChildren and deleteChildren RETURN: share entries, NOT a bill payer.
        {"kind": "OBJECT", "name": "BillPayerChildShareType", "fields": [
            {"name": "billPayerId", "args": [], "type": _nn(_t("SCALAR", "UniqueId"))},
            {"name": "childId", "args": [], "type": _nn(_t("SCALAR", "ChildId"))},
            {"name": "share", "args": [], "type": _nn(_t("OBJECT", "ShareValue"))}]},
        {"kind": "OBJECT", "name": "ShareValue", "fields": [
            {"name": "multiplier", "args": [], "type": _nn(_t("SCALAR", "Float"))}]},
        {"kind": "INPUT_OBJECT", "name": "ChildSharesDeleteInput", "inputFields": [
            {"name": "childId", "type": _nn(_t("SCALAR", "ChildId"))},
            {"name": "billPayerId", "type": _nn(_t("SCALAR", "UniqueId"))}]},
    ]
    return {**base, "types": types}


def check_selection(types: dict, type_name: str, selection: str) -> list[str]:
    """The names in a `{ a b { c } }` selection that are NOT fields of the type they are
    selected on (an empty list when it is valid)."""
    tokens = re.findall(r"\w+|[{}]", selection)
    bad: list[str] = []

    def walk(position: int, owner: str) -> int:
        fields = {f["name"]: f for f in (types.get(owner) or {}).get("fields") or []}
        while position < len(tokens) and tokens[position] != "}":
            name = tokens[position]
            position += 1
            if name not in fields:
                bad.append(f"{owner}.{name}")
            if position < len(tokens) and tokens[position] == "{":
                if name in fields:
                    position = walk(position + 1, runner.named_type(fields[name]["type"]) or "")
                else:                               # an unknown field: skip its block
                    depth = 0
                    while position < len(tokens):
                        depth += {"{": 1, "}": -1}.get(tokens[position], 0)
                        position += 1
                        if depth == 0:
                            break
                    continue
                position += 1                       # the closing brace
        return position

    if tokens and tokens[0] == "{":
        walk(1, type_name)
    return bad


def document_problems(schema_: dict, text: str) -> list[str]:
    """Selection problems of a probe mutation document, checked against the schema."""
    types = {t["name"]: t for t in schema_["types"]}
    match = re.search(r"\{ billPayers \{ (\w+)\([^)]*\)(.*?) \} \}\s*$", text, re.S)
    if not match:
        return ["unparseable document"]
    field_name, selection = match.group(1), match.group(2).strip()
    mutation = next(f for f in types["BillPayersMutation"]["fields"] if f["name"] == field_name)
    return check_selection(types, runner.named_type(mutation["type"]), selection) if selection else []


TRANSIENT_400 = json.dumps({"errors": [{"message": "can not archive a bill payer with shares",
                                        "extensions": {"display": "Transient"}}]})


class FakeFamly:
    """A tiny stateful Famly: bill payers with shares and read-after-write lag, a delete
    that is REFUSED while a share exists (as the live API does) and hides from
    includeDeleted=false once it works, test children with ids child-test-1, -2.
    Records every write and the includeDeleted value of every scan."""

    def __init__(self, schema_=None, lag_scans=0, child_delete_releases_share=False,
                 delete_children_noop=False):
        self.schema = schema_ or schema()
        self.delete_children_noop = delete_children_noop     # answers with entries, removes nothing
        self.validation_errors = {}                          # op -> raise "does not pass validation"
        self.validation_failures = []
        self.payers = []     # {"id","email","ext","shares": {child: multiplier},"deleted","visible_at"}
        self.lag_scans = lag_scans
        self.child_delete_releases_share = child_delete_releases_share
        self.scans = 0
        self.scan_flags = []
        self.writes = []                 # (operation, variables, document text)
        self.create_error = None
        self.create_after_error = None   # the create RUNS (the record exists), THEN this is raised
        self.delete_error = None
        self.add_error = None
        self.delete_children_error = None
        self.child_deleted = []
        self.next_id = 1
        self.next_child = 1

    def _payer(self, bill_payer_id):
        return next(p for p in self.payers if p["id"] == bill_payer_id)

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        op = operation_name
        if op == runner.SCHEMA_OP:
            return {"data": {"__schema": self.schema}}
        if op in (write_probe.CREATE_OP, write_probe.ADD_CHILDREN_OP, write_probe.DELETE_CHILDREN_OP,
                  write_probe.DELETE_OP):
            problems = document_problems(self.schema, text)
            if op in self.validation_errors:
                problems = problems or ["forced"]
            if problems:                       # like the live API: nothing is executed, nothing recorded
                self.validation_failures.append((op, problems))
                raise GraphQLError("Query does not pass validation",
                                   [{"message": "Query does not pass validation: " + ", ".join(problems)}])
        if op == "ProbeChildCreate":
            self.writes.append((op, variables, text))
            child = f"child-test-{self.next_child}"
            self.next_child += 1
            return {"data": {"children": {"create": [{"id": child, "externalId": "x"}]}}}
        if op == "ProbeChildDelete":
            self.writes.append((op, variables, text))
            self.child_deleted += variables["childIds"]
            if self.child_delete_releases_share:
                for payer in self.payers:
                    for child in variables["childIds"]:
                        payer["shares"].pop(child, None)
            return {"data": {"children": {"delete": variables["childIds"]}}}
        if op == write_probe.CREATE_OP:
            self.writes.append((op, variables, text))
            if self.create_error:
                raise self.create_error
            item = variables["input"][0]
            payer = {"id": f"bp-{self.next_id}", "email": item["email"], "ext": item["externalId"],
                     "shares": {c["childId"]: (c.get("share") or {}).get("multiplier", 1.0)
                                for c in item["children"]},
                     "deleted": False, "visible_at": self.scans + self.lag_scans}
            self.next_id += 1
            self.payers.append(payer)
            if self.create_after_error:
                raise self.create_after_error
            return {"data": {"billPayers": {"create": [{"billPayerId": payer["id"]}]}}}
        if op == write_probe.ADD_CHILDREN_OP:
            self.writes.append((op, variables, text))
            if self.add_error:
                raise self.add_error
            payer = None
            for entry in variables["input"]:
                payer = self._payer(entry["billPayerId"])
                payer["shares"][entry["childId"]] = (entry.get("share") or {}).get("multiplier", 1.0)
            return {"data": {"billPayers": {"addChildren": [
                {"billPayerId": e["billPayerId"], "childId": e["childId"],
                 "share": {"multiplier": (e.get("share") or {}).get("multiplier", 1.0)}}
                for e in variables["input"]]}}}
        if op == write_probe.DELETE_CHILDREN_OP:
            self.writes.append((op, variables, text))
            if self.delete_children_error:
                raise self.delete_children_error
            entries = variables["input"] if isinstance(variables["input"], list) else [variables["input"]]
            for entry in entries:
                payer = self._payer(entry["billPayerId"])
                if not self.delete_children_noop:
                    payer["shares"].pop(entry["childId"], None)
            # It answers with entries even for shares that never existed.
            return {"data": {"billPayers": {"deleteChildren": [
                {"billPayerId": e["billPayerId"], "childId": e["childId"], "share": {"multiplier": 1.0}}
                for e in entries]}}}
        if op == write_probe.DELETE_OP:
            self.writes.append((op, variables, text))
            if self.delete_error:
                raise self.delete_error
            ids = variables["id"] if isinstance(variables["id"], list) else [variables["id"]]
            for bill_payer_id in ids:
                payer = self._payer(bill_payer_id)
                if payer["shares"]:                           # the live behaviour
                    raise GraphQLHTTPError("bad", 400, TRANSIENT_400)
                payer["deleted"] = True
            return {"data": {"billPayers": {"delete": ids}}}
        # the list scan: includeDeleted must be passed, explicitly
        self.scans += 1
        flag = variables.get("includeDeleted", "OMITTED")
        self.scan_flags.append(flag)
        rows = [p for p in self.payers if self.scans > p["visible_at"] and (flag is True or not p["deleted"])]
        return {"data": {"billPayers": {"listBySiteIds": {"result": [
            {"billPayerId": p["id"], "email": p["email"], "externalId": p["ext"],
             "deletedAt": "2026-01-01T00:00:00Z" if p["deleted"] else None,
             "childShares": [{"childId": c, "multiplier": m} for c, m in p["shares"].items()]}
            for p in rows], "next": None}}}}

    def mutations(self, operation):
        return [w for w in self.writes if w[0] == operation]

    def order(self):
        return [w[0] for w in self.writes]


class FakeRest:
    def __init__(self, test_children=(CHILD, "child-test-2")):
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
                share=1.0, confirm=True)
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
        famly, *_ = run(share=1.0)
        payload = famly.mutations(write_probe.CREATE_OP)[0][1]["input"][0]
        self.assertEqual(payload["name"], "ZZ Probe Test")
        self.assertEqual(payload["email"], TEST_EMAIL.strip())
        self.assertRegex(payload["externalId"], r"^probe-\d{8}T\d{6}Z$")
        self.assertEqual(payload["children"], [{"childId": CHILD, "share": {"multiplier": 1.0}}])

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
        self.assertIn("create REFUSED by the API (sent once, not retried)", text)
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
        famly.payers.append({"id": "bp-9", "email": "e", "ext": "x", "shares": {}, "deleted": False,
                             "visible_at": 0})
        probe = write_probe.Probe(famly, FakeRest(), out=lambda _t: None)
        plan = write_probe.plan_from_schema({"data": {"__schema": famly.schema}})
        shape = runner.discover_shape({"data": {"__schema": famly.schema}})
        run_ = write_probe._Run(plan=plan, shape=shape, site_id="S", email_key="e", external_id="x",
                                bill_payer_id="bp-9")
        for _ in range(2):
            probe._delete_bill_payer(run_)
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)

    def test_the_budget_refuses_a_second_delete_children_and_add_children(self):
        budget = write_probe.Budget()
        for what in ("billpayer_deletechildren", "billpayer_addchildren"):
            budget.spend(what)
            with self.assertRaises(write_probe.ProbeRefused):
                budget.spend(what)
        self.assertIn("deleteChildren", write_probe.Budget().text())
        self.assertEqual(write_probe.Budget(delete_cap=2).caps["billpayer_delete"], 2)


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
        self.assertIn("share: ChildShareInput!", "\n".join(lines))


class AbortIfItExistsTests(unittest.TestCase):
    def test_an_existing_test_email_aborts_with_no_writes(self):
        famly = FakeFamly()
        famly.payers.append({"id": "old", "email": TEST_EMAIL.upper(), "ext": "other",
                             "shares": {}, "deleted": False, "visible_at": 0})
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(famly.writes, [])
        self.assertEqual(result.stopped, "exists")

    def test_an_existing_deleted_twin_also_aborts(self):
        famly = FakeFamly()
        famly.payers.append({"id": "old", "email": TEST_EMAIL, "ext": "other",
                             "shares": {}, "deleted": True, "visible_at": 0})
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


class ShareAndFlagTests(unittest.TestCase):
    def test_a_bare_number_is_wrapped_into_the_share_input(self):
        plan = write_probe.plan_from_schema({"data": {"__schema": schema()}})
        self.assertEqual(plan.share_wrap_key, "multiplier")
        self.assertEqual(write_probe.build_share(plan, 1.0), {"multiplier": 1.0})
        self.assertEqual(write_probe.build_share(plan, 2), {"multiplier": 2.0})
        self.assertEqual(write_probe.build_share(plan, {"multiplier": 0.5}), {"multiplier": 0.5})

    def test_a_bad_share_is_refused(self):
        plan = write_probe.plan_from_schema({"data": {"__schema": schema()}})
        for bad in ("1.0", [1.0], True):
            with self.subTest(bad=bad), self.assertRaises(write_probe.ProbeRefused):
                write_probe.build_share(plan, bad)

    def test_the_cli_turns_1_0_into_the_wrapped_variable_and_prints_it_without_pii(self):
        famly, _probe, _result, lines, _clock = run(share=1.0)
        sent = famly.mutations(write_probe.CREATE_OP)[0][1]["input"][0]["children"]
        self.assertEqual(sent, [{"childId": CHILD, "share": {"multiplier": 1.0}}])
        printed = [l for l in lines if l.startswith("VARIABLES TO SEND: ")]
        self.assertEqual(len(printed), 1)
        text = printed[0]
        self.assertIn('"share": {"multiplier": 1.0}', text)
        self.assertIn("<test-email>", text)                        # the email is redacted
        self.assertNotIn("example.org", text.lower())
        # It is printed BEFORE the create is sent.
        self.assertLess(lines.index(printed[0]), next(i for i, l in enumerate(lines) if l.startswith("SENDING billPayers.create")))

    def test_a_full_json_share_is_sent_as_given(self):
        famly, *_ = run(share={"multiplier": 0.5})
        sent = famly.mutations(write_probe.CREATE_OP)[0][1]["input"][0]["children"]
        self.assertEqual(sent[0]["share"], {"multiplier": 0.5})

    def test_a_bad_share_refuses_before_any_write_or_scan(self):
        famly = FakeFamly()
        with self.assertRaises(write_probe.ProbeRefused):
            run(famly=famly, share="1.0")
        self.assertEqual(famly.writes, [])
        self.assertEqual(famly.scans, 0)

    def test_every_scan_passes_include_deleted_explicitly(self):
        famly, *_ = run(famly=FakeFamly(lag_scans=1))
        self.assertTrue(famly.scan_flags)
        self.assertNotIn("OMITTED", famly.scan_flags)
        self.assertTrue(all(isinstance(flag, bool) for flag in famly.scan_flags))
        # pre-scan true; the three lag scans false; after deleteChildren false; after the
        # delete false then true.
        self.assertEqual(famly.scan_flags, [True, False, False, False, False, False, True])

    def test_it_refuses_before_any_write_when_include_deleted_is_not_detected(self):
        famly = FakeFamly(schema(include_deleted=False))
        with self.assertRaises(write_probe.ProbeRefused) as ctx:
            run(famly=famly)
        self.assertIn("server default", str(ctx.exception))
        self.assertEqual(famly.writes, [])
        self.assertEqual(famly.scans, 0)                            # no scan at the default either

    def test_probe_scan_refuses_to_omit_the_flag(self):
        shape = runner.discover_shape({"data": {"__schema": schema()}})
        with self.assertRaises(write_probe.ProbeRefused):
            write_probe.probe_scan(FakeFamly(), "S", shape, None, "e", "x", "c")

    def test_the_cli_exits_3_for_a_non_json_share_and_for_undetected_include_deleted(self):
        import argparse
        import contextlib
        import io
        from unittest import mock
        base = dict(report="write", env=None, sites="", site=SITE, test_email=TEST_EMAIL,
                    child_id=CHILD, create_test_child=False, confirm=True, types="", api="public")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            cli.handle(argparse.Namespace(share="not json", **base))
        self.assertEqual(ctx.exception.code, 3)
        famly = FakeFamly(schema(include_deleted=False))
        with mock.patch.object(cli, "_client", return_value=famly), \
                mock.patch("core.rest_client.RestClient", return_value=FakeRest()), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            cli.handle(argparse.Namespace(share="1.0", **base))
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(famly.writes, [])


def kinds_in(famly):
    return famly.order()


class CleanupOrderTests(unittest.TestCase):
    """Famly refuses billPayers.delete while a share exists, so: shares, then the bill
    payer, then the test children."""

    def test_write_mode_removes_the_share_before_the_delete_before_the_child(self):
        famly, _probe, result, lines, _clock = run(child_id=None, create_test_child=True)
        order = famly.order()
        self.assertEqual(order, ["ProbeChildCreate", write_probe.CREATE_OP,
                                 write_probe.DELETE_CHILDREN_OP, write_probe.DELETE_OP, "ProbeChildDelete"])
        self.assertTrue(result.shares_deleted and result.bill_payer_deleted and result.test_child_deleted)
        self.assertEqual(famly.payers[0]["shares"], {})
        self.assertTrue(famly.payers[0]["deleted"])

    def test_the_delete_children_variable_names_the_probes_own_share_only(self):
        famly, *_ = run()
        variables = famly.mutations(write_probe.DELETE_CHILDREN_OP)[0][1]
        self.assertEqual(variables, {"input": [{"childId": CHILD, "billPayerId": "bp-1"}]})
        code = "".join(l for l in famly.mutations(write_probe.DELETE_CHILDREN_OP)[0][2].splitlines()
                       if not l.lstrip().startswith("#"))
        self.assertNotIn("siteIds", code)

    def test_delete_children_is_sent_once_for_every_failure_type(self):
        errors = [requests.Timeout(), requests.ConnectionError(), GraphQLHTTPError("x", 500, ""),
                  GraphQLHTTPError("x", 429, ""), GraphQLHTTPError("x", 400, TRANSIENT_400),
                  GraphQLError("x", [{"extensions": {"display": "Transient"}}])]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                famly = FakeFamly()
                famly.delete_children_error = error
                famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
                self.assertEqual(len(famly.mutations(write_probe.DELETE_CHILDREN_OP)), 1)   # never retried
                # The doomed delete is not spent; the bill payer is left for a person.
                self.assertEqual(famly.mutations(write_probe.DELETE_OP), [])
                text = "\n".join(lines)
                self.assertIn("DELETE THIS BILL PAYER BY HAND (remove its shares first): bp-1", text)
                self.assertEqual(len(famly.mutations("ProbeChildDelete")), 1)         # the child still goes

    def test_a_refused_delete_prints_the_hand_clean_up_and_does_not_retry(self):
        famly = FakeFamly()
        famly.delete_error = GraphQLHTTPError("bad", 400, TRANSIENT_400)
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)
        self.assertIn("DELETE THIS BILL PAYER BY HAND: bp-1", "\n".join(lines))

    def test_the_caps_text_names_delete_children_and_add_children(self):
        _famly, _probe, _result, lines, _clock = run()
        self.assertTrue(any("deleteChildren" in l and "addChildren" in l for l in lines))

    def test_clean_up_is_impossible_without_delete_children_so_it_stops_before_any_write(self):
        famly = FakeFamly(schema(delete_children=False))
        famly, _probe, result, lines, _clock = run(famly=famly)
        self.assertEqual(famly.writes, [])
        self.assertIn("clean-up is impossible", "\n".join(lines))
        self.assertEqual(result.stopped, "schema")

    def test_a_single_entry_delete_children_argument_is_sent_per_share(self):
        famly = FakeFamly(schema(delete_children_list=False))
        famly, *_ = run(famly=famly)
        sent = famly.mutations(write_probe.DELETE_CHILDREN_OP)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], {"input": {"childId": CHILD, "billPayerId": "bp-1"}})


class MutationGuardTests(unittest.TestCase):
    def docs(self):
        plan = write_probe.plan_from_schema({"data": {"__schema": schema()}})
        return plan, {
            "create": write_probe.create_document(plan),
            "delete": write_probe.delete_document(plan),
            "addChildren": write_probe.add_children_document(plan.add_children),
            "deleteChildren": write_probe.delete_children_document(plan.delete_children),
        }

    def test_each_built_document_passes_its_own_guard_and_no_other(self):
        _plan, docs = self.docs()
        for kind, text in docs.items():
            write_probe.assert_probe_mutation(text, kind)
            for other in docs:
                if other != kind:
                    with self.assertRaises(write_probe.ProbeRefused):
                        write_probe.assert_probe_mutation(text, other)

    def test_no_document_can_carry_a_site_wide_argument_or_an_update(self):
        _plan, docs = self.docs()
        for kind, text in docs.items():
            code = "".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).lower()
            self.assertNotIn("siteids", code)
            self.assertNotIn("update(", code)
        for bad in ("mutation ProbeBillPayerAddChildren($i: X) { billPayers { addChildren(a: $i) deleteChildren(b: $i) } }",
                    "mutation ProbeBillPayerDeleteChildren($i: X, $s: Y) { billPayers { deleteChildren(a: $i, siteIds: $s) } }"):
            kind = "addChildren" if "AddChildren" in bad else "deleteChildren"
            with self.assertRaises(write_probe.ProbeRefused):
                write_probe.assert_probe_mutation(bad, kind)

    def test_the_add_children_select_reads_back_the_nested_share(self):
        plan, docs = self.docs()
        self.assertIn("addChildren(shares: $input) { billPayerId childId share { multiplier } }", docs["addChildren"])


class AttachTests(unittest.TestCase):
    def attach(self, famly=None, **kw):
        famly = famly or FakeFamly()
        clock = Clock()
        lines = []
        probe = write_probe.Probe(famly, FakeRest(), out=lines.append, clock=clock, sleep=clock.sleep)
        args = dict(site_code=SITE, test_email=TEST_EMAIL, create_test_child=True,
                    existing_bill_payer_id=None, share=1.0, second_share=None, confirm=True)
        args.update(kw)
        result = probe.run_attach(**args)
        return famly, probe, result, lines, clock

    def test_it_refuses_without_confirm_or_test_children_and_sends_nothing(self):
        for kw in (dict(confirm=False), dict(create_test_child=False), dict(site_code=""),
                   dict(test_email="nope")):
            with self.subTest(kw=kw):
                famly = FakeFamly()
                with self.assertRaises(write_probe.ProbeRefused):
                    self.attach(famly=famly, **kw)
                self.assertEqual(famly.writes, [])
                self.assertEqual(famly.scans, 0)

    def test_the_whole_sibling_path_in_the_right_order(self):
        famly, _probe, result, lines, clock = self.attach()
        self.assertEqual(famly.order(), [
            "ProbeChildCreate", write_probe.CREATE_OP, "ProbeChildCreate", write_probe.ADD_CHILDREN_OP,
            write_probe.DELETE_CHILDREN_OP, write_probe.DELETE_OP, "ProbeChildDelete", "ProbeChildDelete"])
        self.assertTrue(result.children_added and result.shares_deleted and result.bill_payer_deleted)
        self.assertTrue(result.test_child_created and result.test_child_deleted)

    def test_add_children_sends_the_second_child_with_a_wrapped_share_once(self):
        famly, *_ = self.attach()
        sent = famly.mutations(write_probe.ADD_CHILDREN_OP)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], {"input": [
            {"childId": "child-test-2", "billPayerId": "bp-1", "share": {"multiplier": 1.0}}]})

    def test_a_second_share_can_differ(self):
        famly, *_ = self.attach(second_share=0.5)
        sent = famly.mutations(write_probe.ADD_CHILDREN_OP)[0][1]["input"][0]
        self.assertEqual(sent["share"], {"multiplier": 0.5})
        create = famly.mutations(write_probe.CREATE_OP)[0][1]["input"][0]
        self.assertEqual(create["children"][0]["share"], {"multiplier": 1.0})

    def test_the_read_back_lists_both_children_with_their_multipliers(self):
        _famly, _probe, _result, lines, _clock = self.attach(second_share=0.5)
        text = "\n".join(lines)
        self.assertRegex(text, r"T\+ 0s after addChildren: the payer lists 2 child entries; "
                               r"both test children listed=yes; child 1 listed=yes multiplier=1.000; "
                               r"child 2 listed=yes multiplier=0.500")
        self.assertIn("T+10s after addChildren", text)
        self.assertIn("addChildren RETURNED 1 share entry; includes the second child: yes", text)

    def test_the_clean_up_removes_both_shares_in_one_request_then_the_payer_then_both_children(self):
        famly, *_ = self.attach()
        delete = famly.mutations(write_probe.DELETE_CHILDREN_OP)
        self.assertEqual(len(delete), 1)
        self.assertEqual(delete[0][1]["input"], [
            {"childId": "child-test-1", "billPayerId": "bp-1"},
            {"childId": "child-test-2", "billPayerId": "bp-1"}])
        for write in famly.mutations("ProbeChildDelete"):
            self.assertEqual(set(write[1]), {"childIds"})              # never siteIds
        self.assertEqual(famly.child_deleted, ["child-test-1", "child-test-2"])
        self.assertEqual(famly.payers[0]["shares"], {})

    def test_add_children_is_sent_once_for_every_failure_type(self):
        errors = [requests.Timeout(), requests.ConnectionError(), GraphQLHTTPError("x", 500, ""),
                  GraphQLHTTPError("x", 429, ""), GraphQLHTTPError("x", 400, "{}"),
                  GraphQLError("x", [{"extensions": {"display": "Transient"}}])]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                famly = FakeFamly()
                famly.add_error = error
                famly, _probe, result, lines, _clock = self.attach(famly=famly)
                self.assertEqual(len(famly.mutations(write_probe.ADD_CHILDREN_OP)), 1)   # no retry
                # The clean-up still ran, in order.
                self.assertEqual(len(famly.mutations(write_probe.DELETE_CHILDREN_OP)), 1)
                self.assertEqual(len(famly.mutations("ProbeChildDelete")), 2)

    def test_a_rejected_create_stops_but_still_cleans_up_the_first_child(self):
        famly = FakeFamly()
        famly.create_error = GraphQLHTTPError("bad", 400, TRANSIENT_400)
        famly, _probe, result, lines, _clock = self.attach(famly=famly)
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)
        self.assertEqual(famly.mutations(write_probe.ADD_CHILDREN_OP), [])
        self.assertEqual(famly.mutations(write_probe.DELETE_CHILDREN_OP), [])
        self.assertEqual(famly.child_deleted, ["child-test-1"])

    def test_an_existing_probe_bill_payer_is_reused_and_left_in_place(self):
        famly = FakeFamly()
        famly.payers.append({"id": "bp-old", "email": TEST_EMAIL, "ext": "probe-20260101T000000Z",
                             "shares": {"old-child": 1.0}, "deleted": False, "visible_at": 0})
        famly, _probe, result, lines, _clock = self.attach(famly=famly, existing_bill_payer_id="bp-old")
        self.assertEqual(famly.mutations(write_probe.CREATE_OP), [])          # no second payer
        self.assertEqual(famly.mutations(write_probe.DELETE_OP), [])          # it is not ours to delete
        self.assertEqual(len(famly.mutations("ProbeChildCreate")), 1)
        add = famly.mutations(write_probe.ADD_CHILDREN_OP)[0][1]["input"][0]
        self.assertEqual((add["billPayerId"], add["childId"]), ("bp-old", "child-test-1"))
        # Only the share this run added is removed; the old one stays.
        self.assertEqual(famly.mutations(write_probe.DELETE_CHILDREN_OP)[0][1]["input"],
                         [{"childId": "child-test-1", "billPayerId": "bp-old"}])
        self.assertEqual(famly.payers[0]["shares"], {"old-child": 1.0})
        self.assertIn("left in place: bp-old", "\n".join(lines))

    def test_an_existing_bill_payer_that_is_not_a_probe_is_refused_without_a_write(self):
        famly = FakeFamly()
        famly.payers.append({"id": "bp-real", "email": "someone.real@example.org", "ext": "crm-1",
                             "shares": {"k": 1.0}, "deleted": False, "visible_at": 0})
        famly, _probe, result, lines, _clock = self.attach(famly=famly, existing_bill_payer_id="bp-real")
        self.assertEqual(famly.writes, [])
        self.assertEqual(result.stopped, "not_probe")
        famly2 = FakeFamly()
        famly2.payers.append({"id": "bp-x", "email": "other@example.org", "ext": "probe-1",
                              "shares": {}, "deleted": False, "visible_at": 0})
        famly2, _probe, result2, *_ = self.attach(famly=famly2, existing_bill_payer_id="bp-x")
        self.assertEqual(famly2.writes, [])                  # probe externalId but not the test email

    def test_an_existing_test_email_aborts_a_new_bill_payer_with_no_writes(self):
        famly = FakeFamly()
        famly.payers.append({"id": "old", "email": TEST_EMAIL, "ext": "other", "shares": {},
                             "deleted": False, "visible_at": 0})
        famly, _probe, result, *_ = self.attach(famly=famly)
        self.assertEqual(famly.writes, [])
        self.assertEqual(result.stopped, "exists")

    def test_the_pre_scan_and_every_scan_pass_include_deleted_false_explicitly(self):
        famly, *_ = self.attach()
        self.assertNotIn("OMITTED", famly.scan_flags)
        self.assertTrue(all(isinstance(f, bool) for f in famly.scan_flags))
        self.assertEqual(famly.scan_flags[0], False)                 # the attach pre-scan

    def test_attach_refuses_before_a_write_without_add_children_or_include_deleted(self):
        for kw in (dict(schema_=schema(delete_children=False)), dict(schema_=schema(include_deleted=False))):
            famly = FakeFamly(**kw)
            with self.subTest(kw=list(kw)):
                try:
                    famly, _probe, result, lines, _clock = self.attach(famly=famly)
                except write_probe.ProbeRefused:
                    pass
                self.assertEqual(famly.writes, [])

    def test_the_variables_are_printed_without_the_email_and_no_output_has_it(self):
        _famly, _probe, _result, lines, _clock = self.attach()
        text = "\n".join(lines)
        self.assertIn("VARIABLES TO SEND (addChildren)", text)
        self.assertIn("<test-email>", text)
        for form in (TEST_EMAIL, TEST_EMAIL.lower(), "probe.owner", "example.org"):
            self.assertNotIn(form.lower(), text.lower())

    def test_the_cli_returns_booleans_only_and_the_attach_options_exist(self):
        import argparse
        parser = argparse.ArgumentParser()
        cli.add_args(parser)
        parsed = parser.parse_args(["attach", "--site", "HDCW", "--test-email", "x@y.z",
                                    "--create-test-child", "--existing-bill-payer-id", "bp-1",
                                    "--second-share", "0.5", "--share", "1.0", "--confirm"])
        self.assertEqual((parsed.report, parsed.existing_bill_payer_id, parsed.second_share),
                         ("attach", "bp-1", "0.5"))
        flag = parser.parse_args(["write", "--probe-delete-after-child-delete"])
        self.assertTrue(flag.probe_delete_after_child_delete)


class DeleteAfterChildDeleteTests(unittest.TestCase):
    def go(self, releases):
        famly = FakeFamly(child_delete_releases_share=releases)
        return run(famly=famly, child_id=None, create_test_child=True,
                   probe_delete_after_child_delete=True)

    def test_it_needs_a_test_child_the_run_created(self):
        famly = FakeFamly()
        with self.assertRaises(write_probe.ProbeRefused):
            run(famly=famly, probe_delete_after_child_delete=True)         # --child-id given
        self.assertEqual(famly.writes, [])

    def test_when_deleting_the_child_does_not_release_the_share(self):
        famly, _probe, result, lines, _clock = self.go(releases=False)
        order = famly.order()
        # The child goes FIRST, the delete is tried (and refused), then the fallback.
        self.assertEqual(order, ["ProbeChildCreate", write_probe.CREATE_OP, "ProbeChildDelete",
                                 write_probe.DELETE_OP, write_probe.DELETE_CHILDREN_OP, write_probe.DELETE_OP])
        text = "\n".join(lines)
        self.assertIn("deleting a child does NOT release its share", text)
        self.assertIn("at most 1 billPayers.create, 2 billPayers.delete", text)
        self.assertTrue(result.bill_payer_deleted)                          # the fallback worked

    def test_when_deleting_the_child_releases_the_share(self):
        famly, _probe, result, lines, _clock = self.go(releases=True)
        self.assertEqual(famly.order(), ["ProbeChildCreate", write_probe.CREATE_OP, "ProbeChildDelete",
                                         write_probe.DELETE_OP])
        self.assertEqual(famly.mutations(write_probe.DELETE_CHILDREN_OP), [])
        self.assertIn("deleting a child RELEASES its share", "\n".join(lines))

    def test_each_mutation_is_still_sent_at_most_once_per_step(self):
        famly, *_ = self.go(releases=False)
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)
        self.assertEqual(len(famly.mutations(write_probe.DELETE_CHILDREN_OP)), 1)
        self.assertLessEqual(len(famly.mutations(write_probe.DELETE_OP)), 2)

    def test_the_default_path_does_not_run_the_experiment(self):
        famly, *_ = run(child_id=None, create_test_child=True)
        self.assertEqual(famly.order()[2], write_probe.DELETE_CHILDREN_OP)   # shares first, not the child


class SelectionTests(unittest.TestCase):
    """Every selection set is built from the return type's own fields, read from the live
    schema (the live addChildren answered with BillPayerChildShareType: billPayerId,
    childId, share), never hand-assumed."""

    def plan(self, **kw):
        return write_probe.plan_from_schema({"data": {"__schema": schema(**kw)}})

    def test_add_and_delete_children_select_the_share_entry_fields(self):
        plan = self.plan()
        self.assertEqual(plan.add_children.select, " { billPayerId childId share { multiplier } }")
        self.assertEqual(plan.delete_children.select, " { billPayerId childId share { multiplier } }")
        self.assertNotIn("children", plan.add_children.select)

    def test_no_selection_names_a_field_that_is_not_on_the_return_type(self):
        s_ = schema()
        plan = write_probe.plan_from_schema({"data": {"__schema": s_}})
        docs = {"create": write_probe.create_document(plan), "delete": write_probe.delete_document(plan),
                "addChildren": write_probe.add_children_document(plan.add_children),
                "deleteChildren": write_probe.delete_children_document(plan.delete_children)}
        for kind, text in docs.items():
            with self.subTest(kind=kind):
                self.assertEqual(document_problems(s_, text), [])

    def test_the_validator_catches_the_old_hand_assumed_selection(self):
        s_ = schema()
        old = ("mutation ProbeBillPayerAddChildren($input: [BillPayerChildAddInput!]!) { billPayers "
               "{ addChildren(shares: $input) { billPayerId children { childId share { multiplier } } } } }\n")
        self.assertEqual(document_problems(s_, old), ["BillPayerChildShareType.children"])

    def test_the_selection_follows_the_type_whatever_its_fields_are(self):
        s_ = schema()
        for t in s_["types"]:
            if t["name"] == "BillPayerChildShareType":
                t["fields"] = [{"name": "billPayerId", "args": [], "type": _nn(_t("SCALAR", "UniqueId"))},
                               {"name": "childId", "args": [], "type": _nn(_t("SCALAR", "ChildId"))},
                               {"name": "addedAt", "args": [], "type": _t("SCALAR", "DateTime")},
                               {"name": "owner", "args": [], "type": _t("OBJECT", "BillPayer")}]
        plan = write_probe.plan_from_schema({"data": {"__schema": s_}})
        self.assertEqual(plan.add_children.select, " { billPayerId childId addedAt }")   # no share, no objects

    def test_create_selects_only_the_id(self):
        plan = self.plan()
        self.assertEqual(plan.create_select, " { billPayerId }")
        self.assertIn("create(billPayers: $input) { billPayerId }", write_probe.create_document(plan))

    def test_fields_that_need_extra_permission_are_never_selected(self):
        s_ = schema()
        for t in s_["types"]:
            if t["name"] == "BillPayerChildShareType":
                t["fields"] += [{"name": name, "args": [], "type": _t("SCALAR", "String")}
                                for name in ("socialSecurityNumber", "accountNumber", "sortCode")]
        plan = write_probe.plan_from_schema({"data": {"__schema": s_}})
        for select in (plan.add_children.select, plan.delete_children.select, plan.create_select):
            for name in ("socialSecurityNumber", "accountNumber", "sortCode"):
                self.assertNotIn(name, select)
        self.assertEqual(plan.add_children.select, " { billPayerId childId share { multiplier } }")

    def test_a_scalar_return_gets_no_selection(self):
        plan = self.plan()
        self.assertEqual(plan.delete_select, "")


class RejectionKindTests(unittest.TestCase):
    def test_a_validation_failure_is_reported_as_nothing_executed_and_never_retried(self):
        for op, mode in ((write_probe.ADD_CHILDREN_OP, "attach"), (write_probe.CREATE_OP, "write"),
                         (write_probe.DELETE_CHILDREN_OP, "write"), (write_probe.DELETE_OP, "write")):
            with self.subTest(op=op):
                famly = FakeFamly()
                famly.validation_errors[op] = True
                if mode == "attach":
                    famly, _p, _r, lines, _c = AttachTests().attach(famly=famly)
                else:
                    famly, _p, _r, lines, _c = run(famly=famly, child_id=None, create_test_child=True)
                text = "\n".join(lines)
                self.assertIn("rejected at validation, nothing was executed", text)
                self.assertNotIn("REFUSED by the API", text)
                self.assertEqual(len([f for f in famly.validation_failures if f[0] == op]), 1)   # once

    def test_an_api_refusal_is_reported_differently(self):
        famly = FakeFamly()
        famly.delete_error = GraphQLHTTPError("bad", 400, TRANSIENT_400)
        _f, _p, _r, lines, _c = run(famly=famly)
        text = "\n".join(lines)
        self.assertIn("delete REFUSED by the API (sent once, not retried)", text)
        self.assertNotIn("rejected at validation", text)

    def test_the_real_old_selection_would_have_been_rejected_at_validation(self):
        # The fake validates like the live API: a hand-assumed selection fails, a built one does not.
        famly = FakeFamly()
        famly, _p, result, lines, _c = AttachTests().attach(famly=famly)
        self.assertEqual(famly.validation_failures, [])
        self.assertTrue(result.children_added)


class AfterDeleteChildrenTests(unittest.TestCase):
    def test_the_child_count_is_printed_after_delete_children_and_before_the_archive(self):
        _famly, _probe, _result, lines, _clock = run(child_id=None, create_test_child=True)
        at = next(i for i, l in enumerate(lines) if l.startswith("AFTER deleteChildren"))
        sending = next(i for i, l in enumerate(lines) if l.startswith("SENDING billPayers.delete "))
        self.assertLess(at, sending)
        self.assertIn("AFTER deleteChildren: the payer lists 0 child entries", lines[at])

    def test_a_delete_children_that_removed_nothing_is_visible_in_the_count(self):
        famly = FakeFamly(delete_children_noop=True)
        famly, _probe, _result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
        text = "\n".join(lines)
        self.assertIn("AFTER deleteChildren: the payer lists 1 child entry (shares remain", text)
        self.assertEqual(len(famly.mutations(write_probe.DELETE_CHILDREN_OP)), 1)     # still once
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)              # the archive is tried once

    def test_attach_prints_the_count_after_removing_both_shares(self):
        _famly, _probe, _result, lines, _clock = AttachTests().attach()
        self.assertTrue(any(l.startswith("AFTER deleteChildren: the payer lists 0 child entries")
                            for l in lines))


def error_body_with_data(bill_payer_id="bp-1"):
    return json.dumps({
        "errors": [{"message": "not authorised to read socialSecurityNumber"}],
        "data": {"billPayers": {"create": [{"billPayerId": bill_payer_id}]}}})


class CreateRanDespiteErrorsTests(unittest.TestCase):
    """An HTTP error or an `errors` array does NOT mean the mutation did not run. The live
    create answered 403 because a selected field was not readable, but `data` held the
    created record: the probe called it a failure and left a live orphan."""

    def assert_cleaned_up(self, famly, result, lines):
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)            # never resent
        self.assertTrue(result.bill_payer_created)
        self.assertEqual(len(famly.mutations(write_probe.DELETE_CHILDREN_OP)), 1)   # the share is removed
        self.assertEqual(len(famly.mutations(write_probe.DELETE_OP)), 1)            # and the payer archived
        self.assertTrue(famly.payers[0]["deleted"])
        self.assertEqual(famly.payers[0]["shares"], {})

    def test_http_403_with_errors_and_data_is_treated_as_created_and_cleaned_up(self):
        famly = FakeFamly()
        famly.create_after_error = GraphQLHTTPError("forbidden", 403, error_body_with_data())
        famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
        self.assert_cleaned_up(famly, result, lines)
        text = "\n".join(lines)
        self.assertIn("the response carried errors AND data with a billPayerId, so the mutation RAN", text)
        self.assertIn("BILL PAYER id (terminal only): bp-1", text)
        self.assertNotEqual(result.stopped, "rejected")

    def test_a_200_with_errors_and_data_is_treated_the_same_way(self):
        famly = FakeFamly()
        body = json.loads(error_body_with_data())
        famly.create_after_error = GraphQLError("errors", body["errors"], body=body)
        famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
        self.assert_cleaned_up(famly, result, lines)

    def test_a_server_error_after_the_record_was_created_is_settled_by_a_scan(self):
        famly = FakeFamly()
        famly.create_after_error = GraphQLHTTPError("boom", 500, "")          # no data at all
        famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
        self.assert_cleaned_up(famly, result, lines)
        text = "\n".join(lines)
        self.assertIn("SETTLED: the create DID run", text)
        self.assertIn(True, famly.scan_flags[1:])               # the settle scan used includeDeleted=true

    def test_a_timeout_after_the_record_was_created_is_settled_and_cleaned_up(self):
        famly = FakeFamly()
        famly.create_after_error = requests.Timeout()
        famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
        self.assert_cleaned_up(famly, result, lines)

    def test_every_non_clean_response_settles_by_scan_and_never_resends(self):
        errors = [requests.Timeout(), requests.ConnectionError(), GraphQLHTTPError("x", 500, ""),
                  GraphQLHTTPError("x", 429, ""), GraphQLHTTPError("x", 403, "{}"),
                  GraphQLHTTPError("x", 400, TRANSIENT_400), GraphQLError("x", [{"message": "m"}])]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                famly = FakeFamly()
                famly.create_after_error = error                      # it ran, whatever came back
                famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
                self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)
                self.assertTrue(famly.payers[0]["deleted"])           # no orphan left behind
                self.assertEqual(famly.payers[0]["shares"], {})

    def test_when_the_create_really_did_not_run_nothing_is_cleaned_up(self):
        for error in (GraphQLHTTPError("x", 400, TRANSIENT_400), requests.Timeout()):
            with self.subTest(error=type(error).__name__):
                famly = FakeFamly()
                famly.create_error = error                            # raised BEFORE anything is created
                famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
                self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)
                self.assertEqual(famly.mutations(write_probe.DELETE_CHILDREN_OP), [])
                self.assertEqual(famly.mutations(write_probe.DELETE_OP), [])
                self.assertIn("SETTLED: no record exists; the create did not run", "\n".join(lines))
                self.assertFalse(result.bill_payer_created)

    def test_a_validation_failure_is_settled_too_and_finds_nothing(self):
        famly = FakeFamly()
        famly.validation_errors[write_probe.CREATE_OP] = True
        famly, _probe, result, lines, _clock = run(famly=famly, child_id=None, create_test_child=True)
        self.assertIn("rejected at validation, nothing was executed", "\n".join(lines))
        self.assertIn("SETTLED: no record exists", "\n".join(lines))
        self.assertEqual(famly.mutations(write_probe.DELETE_OP), [])

    def test_attach_carries_on_after_a_403_with_data_and_cleans_both_shares(self):
        famly = FakeFamly()
        famly.create_after_error = GraphQLHTTPError("forbidden", 403, error_body_with_data())
        famly, _probe, result, lines, _clock = AttachTests().attach(famly=famly)
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)
        self.assertEqual(len(famly.mutations(write_probe.ADD_CHILDREN_OP)), 1)       # it went on
        self.assertEqual(len(famly.mutations(write_probe.DELETE_CHILDREN_OP)), 1)
        self.assertTrue(famly.payers[0]["deleted"])

    def test_the_create_is_never_sent_twice_in_attach_either(self):
        famly = FakeFamly()
        famly.create_after_error = GraphQLHTTPError("x", 500, "")
        famly, *_ = AttachTests().attach(famly=famly)
        self.assertEqual(len(famly.mutations(write_probe.CREATE_OP)), 1)
        self.assertTrue(famly.payers[0]["deleted"])

    def test_graphql_error_carries_the_whole_body(self):
        err = GraphQLError("m", [{"message": "x"}], body={"data": {"a": 1}, "errors": [{"message": "x"}]})
        self.assertEqual(err.body["data"], {"a": 1})
        self.assertIsNone(GraphQLError("m", []).body)           # the old two-argument form still works


if __name__ == "__main__":
    unittest.main()
