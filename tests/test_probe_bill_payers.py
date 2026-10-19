"""probe-bill-payers (read-only parts): counts only. Fakes only; nothing is sent anywhere."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from actions.probe_bill_payers import cli, runner


def _t(kind, name=None, of=None):
    return {"kind": kind, "name": name, "ofType": of}


def _nn(inner):
    return _t("NON_NULL", None, inner)


def _list(inner):
    return _t("LIST", None, inner)


def schema(include_deleted=True, children="childShares", external_id=True, deleted_at=True,
           query_type="BillPayerQueries", root_link=True, nested=False):
    """The public schema's shape: `listBySiteIds(siteIds, nextToken,
    includeDeleted: Boolean! = true)`, on an object whose name does NOT begin with
    "BillPayers" (the old detector looked for that and so found nothing)."""
    bill_payer_fields = [
        {"name": "billPayerId", "args": [], "type": _nn(_t("SCALAR", "UniqueId"))},
        {"name": "email", "args": [], "type": _t("SCALAR", "String")},
    ]
    if external_id:
        bill_payer_fields.append({"name": "externalId", "args": [], "type": _t("SCALAR", "String")})
    if deleted_at:
        bill_payer_fields.append({"name": "deletedAt", "args": [], "type": _t("SCALAR", "DateTime")})
    if nested:
        # The live nesting: BillPayer.children -> BillPayerChild.share -> ChildShare.multiplier
        bill_payer_fields.append({"name": "children", "args": [],
                                  "type": _nn(_list(_nn(_t("OBJECT", "BillPayerChild"))))})
    elif children:
        bill_payer_fields.append({"name": children, "args": [],
                                  "type": _nn(_list(_nn(_t("OBJECT", "ChildShare"))))})
    list_args = [
        {"name": "siteIds", "type": _nn(_list(_nn(_t("SCALAR", "SiteId"))))},
        {"name": "nextToken", "type": _t("SCALAR", "BillPayerCursor")},
    ]
    if include_deleted:
        list_args.append({"name": "includeDeleted", "type": _nn(_t("SCALAR", "Boolean")),
                          "defaultValue": "true"})
    return {
        "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
        "types": [
            {"kind": "OBJECT", "name": "Query", "fields": (
                [{"name": "billPayers", "args": [], "type": _nn(_t("OBJECT", query_type))}]
                if root_link else [])},
            {"kind": "OBJECT", "name": "Mutation", "fields": [
                {"name": "billPayers", "args": [], "type": _t("OBJECT", "BillPayersMutation")}]},
            {"kind": "OBJECT", "name": query_type, "fields": [
                {"name": "listBySiteIds", "args": list_args, "type": _nn(_t("OBJECT", "BillPayerPage"))}]},
            {"kind": "OBJECT", "name": "BillPayersMutation", "fields": [
                {"name": "addChildren", "args": [
                    {"name": "billPayerId", "type": _nn(_t("SCALAR", "UniqueId"))},
                    {"name": "children", "type": _nn(_list(_nn(_t("INPUT_OBJECT", "BillPayerChildInput"))))}],
                 "type": _t("OBJECT", "BillPayer")}]},
            {"kind": "OBJECT", "name": "BillPayer", "fields": bill_payer_fields},
            {"kind": "OBJECT", "name": "ChildShare", "fields": (
                [{"name": "multiplier", "args": [], "type": _nn(_t("SCALAR", "Float"))}] if nested else
                [{"name": "childId", "args": [], "type": _nn(_t("SCALAR", "ChildId"))},
                 {"name": "multiplier", "args": [], "type": _nn(_t("SCALAR", "Float"))}])},
            {"kind": "OBJECT", "name": "BillPayerChild", "fields": [
                {"name": "childId", "args": [], "type": _nn(_t("SCALAR", "ChildId"))},
                {"name": "share", "args": [], "type": _nn(_t("OBJECT", "ChildShare"))}]},
            {"kind": "INPUT_OBJECT", "name": "BillPayerChildInput", "inputFields": [
                {"name": "childId", "type": _nn(_t("SCALAR", "ChildId"))},
                {"name": "share", "type": _nn(_t("INPUT_OBJECT", "ChildShareInput"))}]},
            {"kind": "INPUT_OBJECT", "name": "ChildShareInput", "inputFields": [
                {"name": "multiplier", "type": _nn(_t("SCALAR", "Float"))}]},
            {"kind": "ENUM", "name": "Unrelated", "enumValues": [{"name": "A"}]},
        ],
    }


class FakeClient:
    """Serves the schema and pages of bill payers (2 a page). Raises on a mutation.
    `data[site][flag]` is the list of nodes the server returns for includeDeleted=flag."""

    def __init__(self, data, schema_=None):
        self.data = data
        self.schema = schema_ or schema()
        self.calls = 0
        self.documents = []
        self.flags = []

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        self.documents.append(text)
        if re.search(r"\bmutation\b\s*[\w{(]", text.lower().replace("mutationtype", "")):
            raise AssertionError("a mutation was sent")
        if operation_name == runner.SCHEMA_OP:
            return {"data": {"__schema": self.schema}}
        self.calls += 1
        self.flags.append(variables.get("includeDeleted", "OMITTED"))
        rows = self.data[variables["siteIds"][0]][bool(variables.get("includeDeleted", True))]
        start = int(variables["next"] or 0)
        nxt = str(start + 2) if start + 2 < len(rows) else None
        return {"data": {"billPayers": {"listBySiteIds": {"result": rows[start:start + 2], "next": nxt}}}}


def node(bill_payer_id, email, children=(), external=None, deleted=False, multipliers=None):
    multipliers = multipliers or [1.0] * len(children)
    return {"billPayerId": bill_payer_id, "email": email, "externalId": external,
            "deletedAt": "2026-01-01T00:00:00Z" if deleted else None,
            "childShares": [{"childId": c, "multiplier": m} for c, m in zip(children, multipliers)]}


class ShapeTests(unittest.TestCase):
    def shape(self, **kw):
        return runner.discover_shape({"data": {"__schema": schema(**kw)}})

    def test_include_deleted_is_detected_in_the_exact_public_shape(self):
        # listBySiteIds(siteIds, nextToken, includeDeleted: Boolean! = true) on an
        # object called BillPayerQueries (not "BillPayers...").
        shape = self.shape()
        self.assertEqual(shape["include_deleted_type"], "Boolean!")
        self.assertEqual(shape["include_deleted_default"], "true")
        self.assertFalse([n for n in shape["notes"] if "includeDeleted" in n])
        text = runner.build_query(shape, True)
        self.assertIn("$includeDeleted: Boolean!", text)
        self.assertIn("includeDeleted: $includeDeleted", text)

    def test_detection_does_not_depend_on_the_object_type_name(self):
        for name in ("BillPayersQuery", "BillPayerQueries", "BillPayersQueries", "Whatever"):
            with self.subTest(name=name):
                self.assertEqual(self.shape(query_type=name)["include_deleted_type"], "Boolean!")

    def test_detection_falls_back_to_a_listBySiteIds_that_returns_a_bill_payer_page(self):
        shape = self.shape(query_type="BillPayerQueries", root_link=False)
        self.assertEqual(shape["include_deleted_type"], "Boolean!")

    def test_a_missing_argument_is_reported_not_guessed(self):
        shape = self.shape(include_deleted=False, children=None, external_id=False, deleted_at=False)
        self.assertIsNone(shape["include_deleted_type"])
        self.assertTrue(any("includeDeleted" in n for n in shape["notes"]))
        self.assertTrue(any("children" in n for n in shape["notes"]))
        self.assertFalse(shape["deleted_at"])

    def test_the_children_deleted_at_and_multiplier_are_discovered(self):
        shape = self.shape()
        self.assertEqual((shape["children_field"], shape["child_path"], shape["multiplier_path"]),
                         ("childShares", "childId", ["multiplier"]))
        self.assertEqual(shape["multiplier_chain"], "ChildShare.multiplier")
        self.assertTrue(shape["deleted_at"] and shape["external_id"])
        text = runner.build_query(shape, True)
        self.assertIn("childShares { childId multiplier }", text)
        self.assertIn("deletedAt", text)
        self.assertNotIn("mutation", text.lower())
        self.assertNotIn("includeDeleted", runner.build_query(shape, False))


def to_nested(n: dict) -> dict:
    """A flat test node as the live schema nests it: children[{childId, share{multiplier}}]."""
    out = {k: v for k, v in n.items() if k != "childShares"}
    out["children"] = [{"childId": c["childId"], "share": {"multiplier": c["multiplier"]}}
                       for c in n["childShares"]]
    return out


class NestedMultiplierTests(unittest.TestCase):
    """The multiplier is nested: BillPayerChild.share -> ChildShare.multiplier (Float!)."""

    def shape(self):
        return runner.discover_shape({"data": {"__schema": schema(nested=True)}})

    def test_discovery_follows_the_share_field_one_level(self):
        shape = self.shape()
        self.assertEqual(shape["children_field"], "children")
        self.assertEqual(shape["child_path"], "childId")
        self.assertEqual(shape["multiplier_path"], ["share", "multiplier"])
        self.assertEqual(shape["multiplier_chain"], "BillPayerChild.share -> ChildShare.multiplier")
        self.assertFalse([n for n in shape["notes"] if "multiplier" in n])
        self.assertIn("children { childId share { multiplier } }", runner.build_query(shape, True))

    def test_the_path_chosen_is_printed_in_the_shape_section(self):
        data = {"S": {False: [], True: []}}
        client = FakeClient(data, schema(nested=True))
        result = runner.audit(client, {"AAA": "S"})
        self.assertEqual(result["shape"]["multiplier_path"], ["share", "multiplier"])
        self.assertEqual(result["shape"]["multiplier_chain"], "BillPayerChild.share -> ChildShare.multiplier")

    def test_multiplier_entries_fill_in_from_the_nested_field(self):
        flat = AuditTests().live()
        data = {"S1": {False: [to_nested(n) for n in flat], True: [to_nested(n) for n in flat]}}
        client = FakeClient(data, schema(nested=True))
        result = runner.audit(client, {"AAA": "S1"})
        false_ = result["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual(false_["multiplierEntries"], {"0.333": 3, "0.500": 4, "1.000": 6})
        self.assertNotIn("@", json.dumps(result))

    def test_a_missing_nested_multiplier_is_reported_not_guessed(self):
        base = schema(nested=True)
        for t in base["types"]:
            if t["name"] == "ChildShare":
                t["fields"] = [{"name": "other", "args": [], "type": _t("SCALAR", "String")}]
        shape = runner.discover_shape({"data": {"__schema": base}})
        self.assertIsNone(shape["multiplier_path"])
        self.assertTrue(any("no multiplier" in n for n in shape["notes"]))


class EqualMultiplierTests(unittest.TestCase):
    def counts(self, nodes):
        data = {"S": {False: nodes, True: nodes}}
        client = FakeClient(data, schema(nested=True))
        return runner.audit(client, {"AAA": "S"})["sites"]["AAA"]["includeDeletedFalse"]

    def test_live_payers_with_two_or_more_children_by_equal_multipliers(self):
        nodes = [to_nested(n) for n in [
            node("a", "a@x.org", ["k1", "k2"], multipliers=[0.5, 0.5]),            # equal
            node("b", "b@x.org", ["k3", "k4", "k5"], multipliers=[0.3333, 0.3333, 0.3334]),  # equal at 3 dp
            node("c", "c@x.org", ["k6", "k7"], multipliers=[0.25, 0.75]),            # unequal
            node("d", "d@x.org", ["k8"], multipliers=[1.0]),                          # one child: not counted
            node("e", "e@x.org", ["k9", "k10"], multipliers=[0.5, 0.5], deleted=True),  # deleted: not counted
        ]]
        counts = self.counts(nodes)
        self.assertEqual(counts["liveWith2PlusChildren"], 3)
        self.assertEqual(counts["liveWith2PlusAllEqualMultipliers"], 2)
        self.assertEqual(counts["liveWith2PlusUnequalMultipliers"], 1)
        self.assertEqual(counts["liveWith2PlusMultiplierUnavailable"], 0)

    def test_an_entry_without_a_readable_multiplier_is_counted_as_unavailable(self):
        broken = to_nested(node("a", "a@x.org", ["k1", "k2"], multipliers=[0.5, 0.5]))
        broken["children"][1]["share"] = None
        counts = self.counts([broken])
        self.assertEqual(counts["liveWith2PlusMultiplierUnavailable"], 1)
        self.assertEqual(counts["liveWith2PlusAllEqualMultipliers"], 0)

    def test_the_definitions_explain_the_new_counts(self):
        data = {"S": {False: [], True: []}}
        result = runner.audit(FakeClient(data, schema(nested=True)), {"AAA": "S"})
        for key in ("liveWith2PlusAllEqualMultipliers", "liveWith2PlusUnequalMultipliers",
                    "liveWith2PlusMultiplierUnavailable"):
            self.assertIn(key, result["definitions"])


class AuditTests(unittest.TestCase):
    EMAILS = {"a": "a.one@example.org", "b": "B.Two@Example.org ", "c3": "c.three@gmail.com",
              "c4": "c.three@googlemail.com", "d": "d.four@example.org", "e": "e.five@example.org",
              "f": "f.six@example.org", "g": "g.seven@example.org", "h": "h.eight@example.org"}

    def live(self):
        E = self.EMAILS
        return [
            node("bp1", E["a"], ["k1"], external="x"),
            node("bp2", E["b"], []),
            node("bp3", E["c3"], ["k2", "k3"], multipliers=[0.5, 0.5]),     # c.three: identical sets
            node("bp4", E["c4"], ["k3", "k2"], external="y", multipliers=[0.5, 0.5]),
            node("bp5", E["d"], ["k4"]),                                    # d.four: two differ, one empty
            node("bp6", E["d"], ["k5"]),
            node("bp7", E["d"], []),
            node("bp8", E["e"], ["k6"]),                                    # e.five: identical
            node("bp9", E["e"], ["k6"]),
            node("bp12", E["f"], ["k8"]),                                   # f.six: only one has children
            node("bp13", E["f"], []),
            node("bp14", E["g"], []),                                       # g.seven: none have children
            node("bp15", E["g"], []),
            node("bp16", E["h"], ["k9"], multipliers=[0.3333]),             # h.eight: mixed
            node("bp17", E["h"], ["k9"], multipliers=[0.3333]),
            node("bp18", E["h"], ["k10"], multipliers=[0.3333]),
            node("bp10", None, []),
        ]

    def data(self):
        live = self.live()
        deleted = live + [node("bp11", self.EMAILS["a"], ["k7"], deleted=True)]   # a deleted twin of a.one
        return {"S1": {False: live, True: deleted},
                "S2": {False: [node("bp20", "b.two@example.org", [])],
                       True: [node("bp20", "b.two@example.org", [])]}}

    def run_audit(self, data=None, schema_=None):
        ticks = iter(range(0, 10000, 2))
        client = FakeClient(data or self.data(), schema_)
        return client, runner.audit(client, {"AAA": "S1", "BBB": "S2"}, clock=lambda: next(ticks))

    def test_the_false_and_true_comparison_runs_and_both_flags_are_passed(self):
        client, result = self.run_audit()
        false_, true_ = (result["sites"]["AAA"]["includeDeletedFalse"],
                         result["sites"]["AAA"]["includeDeletedTrue"])
        self.assertEqual((false_["billPayers"], true_["billPayers"]), (17, 18))
        self.assertEqual((false_["withDeletedAtSet"], true_["withDeletedAtSet"]), (0, 1))
        self.assertEqual(false_["emailsWith2PlusBillPayerIds"], 6)
        self.assertEqual(true_["emailsWith2PlusBillPayerIds"], 7)          # a.one gains a deleted twin
        self.assertNotIn("OMITTED", client.flags)                          # includeDeleted always explicit
        self.assertEqual({True, False}, set(client.flags))

    def test_a_server_that_returns_deleted_rows_under_false_is_counted(self):
        data = self.data()
        data["S1"][False] = data["S1"][True]                                # the filter is ignored
        false_ = self.run_audit(data)[1]["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual(false_["withDeletedAtSet"], 1)
        self.assertEqual(false_["idsDeleted"], 1)                           # bp11, in a.one's group
        self.assertEqual(false_["emailsWith2PlusBillPayerIds"], 7)

    def test_duplicate_groups_split_into_deleted_live_with_children_and_live_without(self):
        false_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual(false_["idsInGroups"], 14)                         # c(2) d(3) e(2) f(2) g(2) h(3)
        self.assertEqual(false_["idsDeleted"], 0)
        self.assertEqual(false_["idsLiveWithChildren"] + false_["idsLiveWithNoChildren"], false_["idsInGroups"])
        self.assertEqual(false_["idsLiveWithNoChildren"], 4)                # bp7, bp13, bp14, bp15
        true_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedTrue"]
        self.assertEqual(true_["idsDeleted"], 1)

    def test_every_email_group_falls_into_exactly_one_documented_category(self):
        false_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual(false_["emailsNoneHaveChildren"], 1)               # g.seven
        self.assertEqual(false_["emailsOneHasChildren"], 1)                 # f.six
        self.assertEqual(false_["emailsAllIdentical"], 2)                   # c.three, e.five
        self.assertEqual(false_["emailsAllDifferent"], 1)                   # d.four
        self.assertEqual(false_["emailsMixed"], 1)                          # h.eight
        total = sum(false_[k] for k in ("emailsNoneHaveChildren", "emailsOneHasChildren",
                    "emailsAllIdentical", "emailsAllDifferent", "emailsMixed"))
        self.assertEqual(total, false_["emailsWith2PlusBillPayerIds"])

    def test_emails_mixed_is_defined_in_the_output(self):
        result = self.run_audit()[1]
        self.assertIn("emailsMixed", result["definitions"])
        self.assertIn("some share a child set and others differ", result["definitions"]["emailsMixed"])
        for key in ("emailsAllIdentical", "emailsAllDifferent", "emailsOneHasChildren",
                    "emailsNoneHaveChildren", "multiplierEntries", "liveWith2PlusChildren"):
            self.assertIn(key, result["definitions"])

    def test_the_multiplier_distribution_and_live_with_two_or_more_children(self):
        false_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual(false_["multiplierEntries"], {"0.333": 3, "0.500": 4, "1.000": 6})
        self.assertEqual(false_["liveWith2PlusChildren"], 2)                # bp3, bp4
        true_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedTrue"]
        self.assertEqual(true_["multiplierEntries"]["1.000"], 7)            # + the deleted twin's entry
        self.assertEqual(true_["liveWith2PlusChildren"], 2)                 # deleted ones do not count

    def test_the_old_counts_still_work(self):
        false_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual((false_["withEmail"], false_["withoutEmail"]), (16, 1))
        self.assertEqual(false_["withExternalId"], 2)
        self.assertGreater(false_["pages"], 1)
        self.assertEqual(self.run_audit()[1]["emailsAtMoreThanOneSite"], 1)   # b.two@ at both sites

    def test_the_audit_refuses_when_include_deleted_is_not_detected(self):
        client = FakeClient(self.data(), schema(include_deleted=False))
        with self.assertRaises(runner.AuditRefused) as ctx:
            runner.audit(client, {"AAA": "S1"})
        self.assertIn("server default", str(ctx.exception))
        self.assertEqual(client.calls, 0)                                   # nothing was scanned

    def test_the_cli_exits_3_when_it_refuses(self):
        import argparse
        import contextlib
        import io
        from unittest import mock
        client = FakeClient(self.data(), schema(include_deleted=False))
        args = argparse.Namespace(report="audit", env=None, sites="HDCW", types="", api="public")
        err = io.StringIO()
        with mock.patch.object(cli, "_client", return_value=client), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            cli.handle(args)
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("includeDeleted", err.getvalue())

    def test_the_output_has_no_email_name_or_id(self):
        _client, result = self.run_audit()
        blob = json.dumps(result).casefold()
        self.assertNotIn("@", blob)
        for fragment in ("example.org", "c.three", "d.four", "bp1", "bp20", "k1", "k6", "s1\"", "b.two"):
            self.assertNotIn(fragment, blob)

    def test_a_runaway_cursor_ends(self):
        class Forever(FakeClient):
            def execute(self, query_path, variables, operation_name):
                return {"data": {"billPayers": {"listBySiteIds": {
                    "result": [node("x", None)], "next": "again"}}}}
        shape = runner.discover_shape({"data": {"__schema": schema()}})
        scan = runner.scan_site(Forever({}), "S", shape, False)
        self.assertEqual(scan["pages"], runner.MAX_PAGES)


class ReadOnlyTests(unittest.TestCase):
    def test_the_documents_sent_contain_no_mutation(self):
        runner.assert_read_only(runner.SCHEMA_QUERY)
        runner.assert_read_only(runner.BILL_PAYERS_QUERY)

    def test_every_document_the_audit_sends_is_read_only(self):
        client = FakeClient(AuditTests().data())
        runner.audit(client, {"AAA": "S1"})
        self.assertTrue(client.documents)
        for text in client.documents:
            self.assertNotRegex(text.lower().replace("mutationtype", ""), r"\bmutation\b\s*[\w{(]")

    def test_a_mutation_document_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.graphql"
            bad.write_text("mutation M { billPayers { create(billPayers: []) { billPayerId } } }",
                           encoding="utf-8")
            with self.assertRaises(AssertionError):
                runner.assert_read_only(bad)

    def test_the_cli_is_registered_and_exposes_the_standard_interface(self):
        import main
        self.assertIn(cli, main.ACTIONS)
        for attr in ("NAME", "HELP", "add_args", "handle"):
            self.assertTrue(hasattr(cli, attr))

    def test_pull_roster_still_omits_include_deleted(self):
        # It is NOT changed: its scan omits the argument and so gets the server default
        # (true), which returns deleted bill payers. Noted in docs/status.md.
        self.assertFalse("includedeleted" in runner.BILL_PAYERS_QUERY.read_text(encoding="utf-8").lower())


class SchemaReportTests(unittest.TestCase):
    def test_the_report_shows_required_fields_defaults_and_links(self):
        text = "\n".join(runner.run_schema(FakeClient({}, schema())))
        self.assertIn("includeDeleted: Boolean! = true", text)             # the declared default
        self.assertIn("childId: ChildId!", text)                            # required
        self.assertIn("Mutation.billPayers", text)

    def test_the_requested_types_are_printed_in_full(self):
        lines = runner.run_schema(FakeClient({}, schema()), runner.DEFAULT_TYPES)
        text = "\n".join(lines[lines.index("REQUESTED types:"):])
        for name in ("OBJECT ChildShare", "INPUT_OBJECT ChildShareInput", "INPUT_OBJECT BillPayerChildInput"):
            self.assertIn(name, text)
        self.assertIn("multiplier: Float!", text)
        self.assertIn("ChildSharesUpdateInput: NOT IN THE SCHEMA", text)    # said, not skipped
        self.assertNotIn("Unrelated", text)


if __name__ == "__main__":
    unittest.main()
