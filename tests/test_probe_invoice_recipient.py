"""probe-invoice-recipient: guards, caps, one send per write, the dedupe and error reporting, the
captured-request template, and an output with no values. Fakes only; nothing talks to Famly."""

import io
import json
import unittest
from pathlib import Path
from unittest import mock

from actions.probe_invoice_recipient import runner
from core.client import GraphQLError, GraphQLHTTPError
from core.rest_client import RestHTTPError

CHILD = "11111111-2222-4333-8444-555555555555"
SITE = "site-1"


class FakeRest:
    def __init__(self, test_child=True, error=False):
        self.test_child, self.error, self.paths = test_child, error, []

    def get(self, path, params=None):
        self.paths.append(path)
        if self.error:
            raise RestHTTPError("boom", 500, "")
        return {"child": {"isTestChild": self.test_child}}


class FakeGql:
    """Reads and the four writes. `dedupe` makes a repeated add a no-op; `add_error` raises on the
    second add (after nothing changes); `update_error` refuses the full contact update."""

    def __init__(self, dedupe=True, second_add_error=None, update_error=None):
        self.dedupe, self.second_add_error, self.update_error = dedupe, second_add_error, update_error
        self.writes, self.update_inputs = [], []
        self.contact = None
        self.payer = None
        self.recipients = []
        self.adds = 0

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        if operation_name in (runner.OPS[d] for d in runner.WRITE_DOCS):
            self.writes.append(operation_name)
        else:
            assert "mutation" not in text.lower()
        if operation_name == runner.OPS[runner.SITE_CHILDREN_DOC]:
            ids = [CHILD] if variables["siteIds"] == [SITE] else []
            return {"data": {"children": {"listBySiteIds": {"result": [{"id": i} for i in ids], "next": None}}}}
        if operation_name == runner.OPS[runner.CREATE_CONTACT_DOC]:
            item = variables["contacts"][0]
            assert "externalSystemsReferences" not in item
            self.contact = {"id": "contact-1", "name": item["name"], "email": item["email"],
                            "phoneNumbers": item["phoneNumbers"], "emergencyContact": False,
                            "records": item["records"], "refs": []}
            return {"data": {"contacts": {"create": [{"id": "contact-1"}]}}}
        if operation_name == runner.OPS[runner.CREATE_PAYER_DOC]:
            entry = variables["billPayers"][0]
            assert entry["children"][0]["share"] == {"multiplier": 0.0}
            self.payer = "payer-1"
            return {"data": {"billPayers": {"create": [{"billPayerId": "payer-1"}]}}}
        if operation_name == runner.OPS[runner.ADD_DOC]:
            self.adds += 1
            entry = variables["invoiceRecipients"][0]
            if self.adds == 2 and self.second_add_error:
                raise self.second_add_error
            if not (self.adds == 2 and self.dedupe):
                self.recipients.append((entry["relationId"], entry["relationId"]))
            return {"data": {"billPayers": {"addInvoiceRecipients": [
                {"relationId": entry["relationId"], "contactId": entry["relationId"],
                 "billPayerId": entry["billPayerId"]}]}}}
        if operation_name == runner.OPS[runner.READ_PAYERS_DOC]:
            rows = []
            if self.payer and variables["siteIds"] == [SITE]:
                rows = [{"billPayerId": self.payer, "invoiceRecipients": [
                    {"relationId": r, "contactId": c, "billPayerId": self.payer} for r, c in self.recipients]}]
            return {"data": {"billPayers": {"listBySiteIds": {"result": rows, "next": None}}}}
        if operation_name == runner.OPS[runner.READ_CONTACTS_DOC]:
            c = self.contact
            node = {"id": c["id"], "name": {"fullName": c["name"]}, "email": c["email"],
                    "phoneNumbers": c["phoneNumbers"], "emergencyContact": False, "roles": [],
                    "externalSystemsReferences": c["refs"], "records": c["records"]}
            return {"data": {"contacts": {"list": {"result": [node], "next": None}}}}
        if operation_name == runner.OPS[runner.UPDATE_CONTACT_DOC]:
            item = variables["contacts"][0]
            self.update_inputs.append(item)
            if self.update_error:
                raise self.update_error
            self.contact["refs"] = [dict(r) for r in item["externalSystemsReferences"]]
            return {"data": {"contacts": {"update": [{"id": item["id"]}]}}}
        raise AssertionError(f"unexpected operation {operation_name}")


class ProbeCase(unittest.TestCase):
    def setUp(self):
        sites = mock.patch.object(runner.catalogue, "load_sites",
                                  return_value={"HDX": {"institutionId": SITE}})
        sites.start()
        self.addCleanup(sites.stop)
        self.lines = []

    def run_probe(self, gql=None, rest=None, confirm=True, child=CHILD, template=None):
        gql = gql or FakeGql()
        result = runner.run_probe(child, confirm, rest or FakeRest(), gql, out=self.lines.append,
                                  sleep=lambda s: None, template=template or None)
        return result, gql


class GuardTests(ProbeCase):
    def refused(self, **kwargs):
        gql = kwargs.setdefault("gql", FakeGql())
        with self.assertRaises(runner.ProbeRefused):
            self.run_probe(**kwargs)
        self.assertEqual(gql.writes, [])

    def test_no_confirm_a_bad_id_and_a_non_test_child_write_nothing(self):
        self.refused(confirm=False)
        self.refused(child="not-a-uuid")
        self.refused(rest=FakeRest(test_child=False))
        self.refused(rest=FakeRest(error=True))

    def test_every_write_document_is_one_mutation_and_never_a_delete(self):
        runner.check_documents()
        for path, (top, call) in runner.WRITE_DOCS.items():
            self.assertEqual(sum(1 for d in runner.WRITE_DOCS if d == path), 1)
        with self.assertRaises(AssertionError):
            runner.assert_one_mutation(Path(runner.ADD_DOC), "billPayers", "deleteInvoiceRecipients")


class FlowTests(ProbeCase):
    def test_a_full_run_sends_exactly_five_writes_and_reports_booleans_and_counts(self):
        result, gql = self.run_probe()
        self.assertEqual((result.writes, len(gql.writes)), (5, 5))
        rows = result.rows
        self.assertTrue(rows["1_contact_created"] and rows["2_bill_payer_created"])
        self.assertEqual((rows["3_contact_present"], rows["3_recipient_count"]), (True, 1))
        self.assertTrue(rows["3_relationId_equals_contactId"])
        self.assertEqual((rows["4_deduplicated"], rows["4_duplicated"], rows["4_recipient_count_after"]),
                         (True, False, 1))
        self.assertTrue(rows["5_update_accepted"] and rows["5_reference_present"])
        self.assertEqual(rows["5_reference_count_after"], 1)
        self.assertTrue(rows["5_name_unchanged"] and rows["5_phones_unchanged"] and rows["5_records_unchanged"])
        self.assertEqual((result.contact_id, result.payer_id), ("contact-1", "payer-1"))
        self.assertEqual(len(gql.update_inputs[0]["externalSystemsReferences"]), 1)

    def test_a_repeated_add_that_duplicates_is_reported_as_duplicated(self):
        result, _ = self.run_probe(FakeGql(dedupe=False))
        self.assertEqual((result.rows["4_deduplicated"], result.rows["4_duplicated"],
                          result.rows["4_recipient_count_after"]), (False, True, 2))

    def test_an_error_on_the_second_add_is_class_and_code_only_and_the_probe_goes_on(self):
        body = json.dumps({"errors": [{"message": "ada.l@example.org already linked",
                                       "extensions": {"code": "ALREADY_EXISTS"}}]})
        result, gql = self.run_probe(FakeGql(second_add_error=GraphQLHTTPError("x", 400, body)))
        self.assertFalse(result.rows["4_second_add_accepted"])
        self.assertEqual(result.rows["4_error"], "GraphQLHTTPError status=400 codes=['ALREADY_EXISTS']")
        self.assertEqual(result.rows["4_recipient_count_after"], 1)
        self.assertEqual(result.writes, 5)
        self.assertNotIn("ada.l", "\n".join(self.lines))
        self.assertEqual(gql.adds, 2)                       # sent twice in all, never a third time

    def test_a_refused_contact_update_reports_the_class_and_changes_nothing_else(self):
        result, gql = self.run_probe(FakeGql(update_error=GraphQLError("secret detail", [{"message": "secret", "extensions": {"code": "BAD_USER_INPUT"}}])))
        self.assertFalse(result.rows["5_update_accepted"])
        self.assertEqual(result.rows["5_error"], "GraphQLError status=None codes=['BAD_USER_INPUT']")
        self.assertFalse(result.rows["5_reference_present"])
        self.assertEqual(len(gql.update_inputs), 1)          # sent once, never retried
        self.assertNotIn("secret", "\n".join(self.lines))

    def test_the_captured_request_is_used_when_present(self):
        template = {"id": "$ID", "name": "$ECHO", "phoneNumbers": "$ECHO", "records": "$ECHO", "roles": [],
                    "externalSystemsReferences": [{"foreignId": "$FOREIGN_ID", "system": "HUBSPOT"}]}
        _result, gql = self.run_probe(template=template)
        sent = gql.update_inputs[0]
        self.assertEqual(sent["id"], "contact-1")
        self.assertNotIn("email", sent)                      # exactly the captured shape
        self.assertEqual(sent["externalSystemsReferences"][0]["system"], "HUBSPOT")
        self.assertTrue(sent["externalSystemsReferences"][0]["foreignId"].isdigit())

    def test_the_template_is_read_from_the_docs_section_only_when_it_has_a_json_block(self):
        none = "## Captured contact update\n\n```\n[[ place ]]\n```\n\n## Next\n"
        self.assertIsNone(runner.captured_update_template(none))
        self.assertIsNone(runner.captured_update_template("## Other\n```json\n{}\n```"))
        good = '## Captured contact update\n\n```json\n{"id": "$ID"}\n```\n\n## Next\n```json\n{"x": 1}\n```'
        self.assertEqual(runner.captured_update_template(good), {"id": "$ID"})

    def test_no_value_is_printed(self):
        self.run_probe()
        text = "\n".join(self.lines).casefold()
        for word in ("aaprobe", "example.com", "+4477", "@"):
            self.assertNotIn(word, text)
        self.assertIn("contact id: contact-1", text)


if __name__ == "__main__":
    unittest.main()
