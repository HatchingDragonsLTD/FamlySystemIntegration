"""probe-bill-payers: read-only, counts only. Fakes only; nothing is sent anywhere."""

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


def schema(include_deleted=True, children="childShares", external_id=True):
    bill_payer_fields = [
        {"name": "billPayerId", "args": [], "type": _nn(_t("SCALAR", "UniqueId"))},
        {"name": "email", "args": [], "type": _t("SCALAR", "String")},
    ]
    if external_id:
        bill_payer_fields.append({"name": "externalId", "args": [], "type": _t("SCALAR", "String")})
    if children:
        bill_payer_fields.append({"name": children, "args": [],
                                  "type": _nn(_list(_nn(_t("OBJECT", "ChildShare"))))})
    list_args = [
        {"name": "siteIds", "type": _nn(_list(_nn(_t("SCALAR", "SiteId"))))},
        {"name": "nextToken", "type": _t("SCALAR", "BillPayerCursor")},
    ]
    if include_deleted:
        list_args.append({"name": "includeDeleted", "type": _t("SCALAR", "Boolean"),
                          "defaultValue": "false"})
    return {
        "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
        "types": [
            {"kind": "OBJECT", "name": "Query", "fields": [
                {"name": "billPayers", "args": [], "type": _nn(_t("OBJECT", "BillPayersQuery"))}]},
            {"kind": "OBJECT", "name": "Mutation", "fields": [
                {"name": "billPayers", "args": [], "type": _t("OBJECT", "BillPayersMutation")}]},
            {"kind": "OBJECT", "name": "BillPayersQuery", "fields": [
                {"name": "listBySiteIds", "args": list_args, "type": _nn(_t("OBJECT", "BillPayerPage"))}]},
            {"kind": "OBJECT", "name": "BillPayersMutation", "fields": [
                {"name": "addChildren", "args": [
                    {"name": "billPayerId", "type": _nn(_t("SCALAR", "UniqueId"))},
                    {"name": "children", "type": _nn(_list(_nn(_t("INPUT_OBJECT", "BillPayerChildInput"))))}],
                 "type": _t("OBJECT", "BillPayer")}]},
            {"kind": "OBJECT", "name": "BillPayer", "fields": bill_payer_fields},
            {"kind": "OBJECT", "name": "ChildShare", "fields": [
                {"name": "childId", "args": [], "type": _nn(_t("SCALAR", "ChildId"))},
                {"name": "percentage", "args": [], "type": _t("SCALAR", "Float")}]},
            {"kind": "INPUT_OBJECT", "name": "BillPayerChildInput", "inputFields": [
                {"name": "childId", "type": _nn(_t("SCALAR", "ChildId"))},
                {"name": "percentage", "type": _t("SCALAR", "Float"), "defaultValue": "100"}]},
            {"kind": "INPUT_OBJECT", "name": "ChildShareInput", "inputFields": [
                {"name": "childId", "type": _nn(_t("SCALAR", "ChildId"))}]},
            {"kind": "ENUM", "name": "Unrelated", "enumValues": [{"name": "A"}]},
        ],
    }


class FakeClient:
    """Serves the schema and pages of bill payers (2 a page). Raises on a mutation.
    `data[site][deleted_flag]` is a list of nodes."""

    def __init__(self, data, schema_=None):
        self.data = data
        self.schema = schema_ or schema()
        self.calls = 0
        self.documents = []

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        self.documents.append(text)
        if re.search(r"\bmutation\b\s*[\w{(]", text.lower().replace("mutationtype", "")):
            raise AssertionError("a mutation was sent")
        if operation_name == runner.SCHEMA_OP:
            return {"data": {"__schema": self.schema}}
        self.calls += 1
        flag = variables.get("includeDeleted", False)
        rows = self.data[variables["siteIds"][0]][bool(flag)]
        start = int(variables["next"] or 0)
        nxt = str(start + 2) if start + 2 < len(rows) else None
        return {"data": {"billPayers": {"listBySiteIds": {"result": rows[start:start + 2], "next": nxt}}}}


def node(bill_payer_id, email, children=(), external=None):
    return {"billPayerId": bill_payer_id, "email": email, "externalId": external,
            "childShares": [{"childId": c} for c in children]}


class ShapeTests(unittest.TestCase):
    def shape(self, **kw):
        return runner.discover_shape({"data": {"__schema": schema(**kw)}})

    def test_the_children_field_and_include_deleted_are_discovered(self):
        shape = self.shape()
        self.assertEqual((shape["children_field"], shape["child_path"]), ("childShares", "childId"))
        self.assertTrue(shape["external_id"])
        self.assertEqual((shape["include_deleted_type"], shape["include_deleted_default"]),
                         ("Boolean", "false"))

    def test_a_missing_piece_is_reported_not_guessed(self):
        shape = self.shape(include_deleted=False, children=None, external_id=False)
        self.assertIsNone(shape["children_field"])
        self.assertIsNone(shape["include_deleted_type"])
        self.assertFalse(shape["external_id"])
        self.assertTrue(any("includeDeleted" in n for n in shape["notes"]))
        self.assertTrue(any("children" in n for n in shape["notes"]))

    def test_the_query_built_is_a_read_only_document_with_the_flag(self):
        text = runner.build_query(self.shape(), True)
        self.assertIn("includeDeleted: $includeDeleted", text)
        self.assertIn("childShares { childId }", text)
        self.assertNotIn("mutation", text.lower())
        self.assertNotIn("includeDeleted", runner.build_query(self.shape(), False))


class AuditTests(unittest.TestCase):
    EMAILS = ["a.one@example.org", "B.Two@Example.org ", "c.three@gmail.com", "c.three@googlemail.com",
              "d.four@example.org", "d.four@example.org", "d.four@example.org", "e.five@example.org",
              "e.five@example.org"]

    def data(self):
        live = [
            node("bp1", self.EMAILS[0], ["k1"], external="x"),
            node("bp2", self.EMAILS[1], []),
            node("bp3", self.EMAILS[2], ["k2", "k3"]),          # c.three: same set as bp4
            node("bp4", self.EMAILS[3], ["k3", "k2"], external="y"),
            node("bp5", self.EMAILS[4], ["k4"]),                # d.four: bp5 and bp6 differ,
            node("bp6", self.EMAILS[5], ["k5"]),                # bp7 has none
            node("bp7", self.EMAILS[6], []),
            node("bp8", self.EMAILS[7], ["k6"]),                # e.five: identical sets
            node("bp9", self.EMAILS[8], ["k6"]),
            node("bp10", None, []),
        ]
        deleted = live + [node("bp11", self.EMAILS[0], ["k7"])]  # a deleted twin of a.one
        return {"S1": {False: live, True: deleted}, "S2": {False: [node("bp20", "b.two@example.org", [])],
                                                             True: [node("bp20", "b.two@example.org", [])]}}

    def run_audit(self):
        ticks = iter(range(0, 10000, 2))
        client = FakeClient(self.data())
        return client, runner.audit(client, {"AAA": "S1", "BBB": "S2"}, clock=lambda: next(ticks))

    def test_counts_with_include_deleted_false_and_true(self):
        _client, result = self.run_audit()
        false_, true_ = (result["sites"]["AAA"]["includeDeletedFalse"],
                         result["sites"]["AAA"]["includeDeletedTrue"])
        self.assertEqual((false_["billPayers"], true_["billPayers"]), (10, 11))
        self.assertEqual((false_["withEmail"], false_["withoutEmail"]), (9, 1))
        # Under false: c.three, d.four, e.five have 2+ ids. Under true: a.one does too.
        self.assertEqual(false_["emailsWith2PlusBillPayerIds"], 3)
        self.assertEqual(true_["emailsWith2PlusBillPayerIds"], 4)
        self.assertGreater(false_["pages"], 1)
        self.assertGreater(false_["seconds"], 0)

    def test_the_children_audit_for_duplicated_emails(self):
        false_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual(false_["billPayerIdsInThoseEmails"], 7)          # 2 + 3 + 2
        self.assertEqual(false_["idsWithZeroChildren"], 1)                 # bp7
        self.assertEqual(false_["idsSharingAnIdenticalChildSet"], 4)       # bp3+bp4, bp8+bp9
        self.assertEqual(false_["idsWithADifferentChildSet"], 2)           # bp5, bp6
        self.assertEqual(false_["emailsWhereEveryNonEmptySetIsIdentical"], 2)
        self.assertEqual(false_["emailsWhereEveryNonEmptySetDiffers"], 1)
        self.assertEqual(false_["emailsMixed"], 0)

    def test_external_id_and_no_children_counts(self):
        false_ = self.run_audit()[1]["sites"]["AAA"]["includeDeletedFalse"]
        self.assertEqual(false_["withExternalId"], 2)
        self.assertEqual(false_["withNoChildren"], 3)                       # bp2, bp7, bp10

    def test_cross_site_emails_and_the_existing_scan_flag(self):
        _client, result = self.run_audit()
        self.assertEqual(result["emailsAtMoreThanOneSite"], 1)             # b.two@ at both sites
        self.assertFalse(result["existingScanPassesIncludeDeleted"])       # pull_roster's query
        self.assertEqual(result["shape"]["include_deleted_default"], "false")

    def test_the_output_has_no_email_name_or_id(self):
        _client, result = self.run_audit()
        blob = json.dumps(result).casefold()
        self.assertNotIn("@", blob)
        for fragment in ("example.org", "gmail", "bp1", "bp20", "k1", "k6", "s1\"", "b.two"):
            self.assertNotIn(fragment, blob)

    def test_without_include_deleted_the_server_default_is_scanned_once(self):
        client = FakeClient(self.data(), schema(include_deleted=False))
        result = runner.audit(client, {"AAA": "S1"})
        self.assertEqual(list(result["sites"]["AAA"]), ["serverDefault"])
        self.assertTrue(all("includeDeleted" not in d for d in client.documents
                            if "ProbeBillPayerAudit" in d))

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


class SchemaReportTests(unittest.TestCase):
    def test_the_report_shows_required_fields_defaults_and_links(self):
        text = "\n".join(runner.run_schema(FakeClient({}, schema())))
        self.assertIn("includeDeleted: Boolean = false", text)          # the declared default
        self.assertIn("childId: ChildId!", text)                         # required
        self.assertIn("percentage: Float = 100", text)                   # optional with a default
        self.assertIn("Mutation.billPayers", text)

    def test_the_requested_types_are_printed_in_full(self):
        lines = runner.run_schema(FakeClient({}, schema()), runner.DEFAULT_TYPES)
        text = "\n".join(lines[lines.index("REQUESTED types:"):])
        for name in ("OBJECT ChildShare", "INPUT_OBJECT ChildShareInput", "INPUT_OBJECT BillPayerChildInput"):
            self.assertIn(name, text)
        self.assertIn("ChildSharesUpdateInput: NOT IN THE SCHEMA", text)  # said, not skipped
        self.assertNotIn("Unrelated", text)


if __name__ == "__main__":
    unittest.main()
