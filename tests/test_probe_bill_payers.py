"""probe-bill-payers: read-only, counts only. Fakes only; nothing is sent anywhere."""

import json
import tempfile
import unittest
from pathlib import Path

from actions.probe_bill_payers import cli, runner

EMAILS = ["a.one@example.org", "B.Two@Example.org ", "c.three@gmail.com", "c.three@googlemail.com"]


class FakeClient:
    """Serves pages of bill payers per site, 2 to a page. Raises on a mutation."""

    def __init__(self, by_site, schema=None):
        self.by_site = by_site        # site id -> [(id, email or None)]
        self.schema = schema
        self.calls = 0

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        if "mutation Probe" in text or "mutation " in text.lower().replace("mutationtype", ""):
            raise AssertionError("a mutation was sent")
        self.calls += 1
        if operation_name == runner.SCHEMA_OP:
            return {"data": {"__schema": self.schema}}
        rows = self.by_site[variables["siteIds"][0]]
        start = int(variables["next"] or 0)
        page = rows[start:start + 2]
        nxt = str(start + 2) if start + 2 < len(rows) else None
        return {"data": {"billPayers": {"listBySiteIds": {
            "result": [{"billPayerId": i, "email": e} for i, e in page], "next": nxt}}}}


class AuditTests(unittest.TestCase):
    def audit(self):
        data = {
            "S1": [("bp1", EMAILS[0]), ("bp2", EMAILS[1]), ("bp3", EMAILS[2]),
                   ("bp4", EMAILS[3]), ("bp5", None)],              # c.three twice, one no email
            "S2": [("bp6", "b.two@example.org"), ("bp7", "other@example.org")],
        }
        ticks = iter(range(0, 1000, 3))
        client = FakeClient(data)
        return client, runner.audit(client, {"AAA": "S1", "BBB": "S2"}, clock=lambda: next(ticks))

    def test_counts_pages_duplicates_and_cross_site_emails(self):
        client, result = self.audit()
        first, second = result["sites"]["AAA"], result["sites"]["BBB"]
        self.assertEqual((first["billPayers"], first["pages"]), (5, 3))
        self.assertEqual((first["withEmail"], first["withoutEmail"]), (4, 1))
        # googlemail.com and gmail.com are one canonical email with two bill payer ids.
        self.assertEqual((first["emailsWith2PlusBillPayerIds"], first["billPayerIdsInThoseEmails"]), (1, 2))
        self.assertEqual((second["billPayers"], second["pages"]), (2, 1))
        self.assertEqual(second["emailsWith2PlusBillPayerIds"], 0)
        self.assertEqual(result["emailsAtMoreThanOneSite"], 1)       # b.two@ is at both sites
        self.assertEqual(result["totalPages"], 4)
        self.assertEqual(client.calls, 4)
        self.assertGreater(first["seconds"], 0)

    def test_no_email_or_name_is_in_the_result(self):
        _client, result = self.audit()
        blob = json.dumps(result)
        for email in EMAILS:
            self.assertNotIn(email.strip().casefold().split("@")[0], blob.casefold())
        self.assertNotIn("@", blob)

    def test_a_runaway_cursor_ends(self):
        class Forever(FakeClient):
            def execute(self, query_path, variables, operation_name):
                return {"data": {"billPayers": {"listBySiteIds": {
                    "result": [{"billPayerId": "x", "email": None}], "next": "again"}}}}
        scan = runner.scan_site(Forever({}), "S")
        self.assertEqual(scan["pages"], runner.MAX_PAGES)


class ReadOnlyTests(unittest.TestCase):
    def test_the_documents_sent_contain_no_mutation(self):
        runner.assert_read_only(runner.SCHEMA_QUERY)
        runner.assert_read_only(runner.BILL_PAYERS_QUERY)

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


def _t(kind, name=None, of=None):
    return {"kind": kind, "name": name, "ofType": of}


SCHEMA = {
    "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
    "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "billPayers", "args": [], "type": _t("NON_NULL", None, _t("OBJECT", "BillPayersQuery"))},
            {"name": "children", "args": [], "type": _t("OBJECT", "ChildrenQuery")}]},
        {"kind": "OBJECT", "name": "Mutation", "fields": [
            {"name": "billPayers", "args": [], "type": _t("OBJECT", "BillPayersMutation")}]},
        {"kind": "OBJECT", "name": "BillPayersMutation", "fields": [
            {"name": "addChildren", "args": [
                {"name": "billPayerId", "type": _t("NON_NULL", None, _t("SCALAR", "UniqueId"))},
                {"name": "childIds", "type": _t("NON_NULL", None, _t("LIST", None, _t("NON_NULL", None, _t("SCALAR", "ChildId"))))}],
             "type": _t("OBJECT", "BillPayer")}]},
        {"kind": "INPUT_OBJECT", "name": "BillPayerInput", "inputFields": [
            {"name": "siteId", "type": _t("NON_NULL", None, _t("SCALAR", "SiteId"))},
            {"name": "name", "type": _t("NON_NULL", None, _t("SCALAR", "NameInput"))},
            {"name": "email", "type": _t("SCALAR", "String")}]},
        {"kind": "OBJECT", "name": "BillPayer", "fields": [
            {"name": "billPayerId", "args": [], "type": _t("SCALAR", "UniqueId")}]},
        {"kind": "OBJECT", "name": "Child", "fields": [
            {"name": "billPayers", "args": [], "type": _t("LIST", None, _t("OBJECT", "BillPayer"))},
            {"name": "name", "args": [], "type": _t("SCALAR", "String")}]},
    ],
}


class SchemaReportTests(unittest.TestCase):
    def test_the_report_shows_inputs_required_fields_and_links(self):
        lines = runner.run_schema(FakeClient({}, schema=SCHEMA))
        text = "\n".join(lines)
        self.assertIn("INPUT_OBJECT BillPayerInput", text)
        self.assertIn("siteId: SiteId!", text)                         # required
        self.assertIn("email: String", text)
        self.assertNotIn("email: String!", text)                       # optional
        self.assertIn("addChildren(billPayerId: UniqueId!, childIds: [ChildId!]!) -> BillPayer", text)
        self.assertIn("Mutation.billPayers", text)
        self.assertIn("Child.billPayers -> [BillPayer]", text)         # how a link is found
        self.assertNotIn("Child.name", text)


if __name__ == "__main__":
    unittest.main()
