"""probe-invoice-recipient: guards, caps, one send per write, the dedupe and error reporting, the
captured-request template, and an output with no values. Fakes only; nothing talks to Famly."""

import io
import json
import unittest
from pathlib import Path
from unittest import mock

from actions.onboard import bill_payer_writes as bpw
from actions.probe_bill_payers import runner as bp_probe_runner
from actions.probe_invoice_recipient import runner
from core.client import GraphQLError, GraphQLHTTPError
from tests.test_probe_bill_payers_write import schema as write_schema
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

    def __init__(self, dedupe=True, second_add_error=None, update_error=None, payer_error=None,
                 payer_lands_then_errors=False, reuse_contact=None):
        self.dedupe, self.second_add_error, self.update_error = dedupe, second_add_error, update_error
        self.payer_error, self.payer_lands_then_errors = payer_error, payer_lands_then_errors
        self.writes, self.update_inputs, self.payer_calls = [], [], []
        self.contact = None
        if reuse_contact:                       # an AAProbe contact from an earlier run
            self.contact = {"id": "22222222-3333-4444-8555-666666666666", "name": "AAProbe IR", "email": "old@example.com",
                            "phoneNumbers": [], "emergencyContact": False, "records": [], "refs": []}
            self.contact_on_child = reuse_contact == "on_child"
        else:
            self.contact_on_child = True
        self.payer = None
        self.recipients = []
        self.adds = 0

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        if operation_name == bp_probe_runner.SCHEMA_OP:
            return {"data": {"__schema": write_schema()}}
        if operation_name in (runner.OPS[d] for d in runner.WRITE_DOCS) or operation_name == bpw.CREATE_OP:
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
        if operation_name == bpw.CREATE_OP:
            self.payer_calls.append((text, variables))
            if self.payer_error and not self.payer_lands_then_errors:
                raise self.payer_error
            self.payer = "payer-1"
            if self.payer_error:
                raise self.payer_error
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
                rows = [{"billPayerId": self.payer, "children": [{"childId": CHILD}], "invoiceRecipients": [
                    {"relationId": r, "contactId": c, "billPayerId": self.payer} for r, c in self.recipients]}]
            return {"data": {"billPayers": {"listBySiteIds": {"result": rows, "next": None}}}}
        if operation_name == runner.OPS[runner.READ_CONTACTS_DOC]:
            c = self.contact
            if not self.contact_on_child:
                return {"data": {"contacts": {"list": {"result": [], "next": None}}}}
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

    def run_probe(self, gql=None, rest=None, confirm=True, child=CHILD, template=None, contact_id=None):
        gql = gql or FakeGql()
        result = runner.run_probe(child, confirm, rest or FakeRest(), gql, out=self.lines.append,
                                  sleep=lambda s: None, template=template or None, contact_id=contact_id)
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


class SanitiserTests(unittest.TestCase):
    def test_validation_text_is_kept_and_truncated(self):
        text = runner.sanitise_message(["Variable $input of type [BillPayerInput!]! was invalid"], {"x": "abc"})
        self.assertEqual(text, "Variable $input of type [BillPayerInput!]! was invalid")
        long = runner.sanitise_message(["Field 'foo' is not defined " + "x" * 400], {})
        self.assertEqual(len(long), 200)

    def test_anything_with_an_email_long_digits_or_a_variable_value_is_dropped(self):
        self.assertIsNone(runner.sanitise_message(["Variable field ada.l@example.org is invalid"], {}))
        self.assertIsNone(runner.sanitise_message(["Variable field 12345678 is invalid"], {}))
        self.assertIsNotNone(runner.sanitise_message(["Variable field 123456 is invalid"], {}))
        self.assertIsNone(runner.sanitise_message(
            ["Variable field AAProbe IR is invalid"], {"input": [{"name": "AAProbe IR"}]}))
        self.assertIsNone(runner.sanitise_message(["Something broke in the kitchen"], {}))   # not validation
        self.assertIsNone(runner.sanitise_message([], {}))

    def test_only_an_http_400_carries_a_message(self):
        body = json.dumps({"errors": [{"message": "Unknown argument on field create"}]})
        self.assertIn("message=", runner.error_info(GraphQLHTTPError("x", 400, body)))
        self.assertNotIn("message=", runner.error_info(GraphQLHTTPError("x", 500, body)))


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

    def test_step_2_uses_the_schema_derived_document_and_variables_at_share_zero(self):
        result, gql = self.run_probe()
        text, variables = gql.payer_calls[0]
        self.assertIn(f"mutation {bpw.CREATE_OP}", text)
        self.assertIn("create(billPayers: $input)", text)            # the live schema's argument name
        self.assertNotIn("ProbeIRCreatePayer", text)
        entry = variables["input"][0]
        self.assertEqual(entry["name"], "AAProbe IR")
        self.assertTrue(entry["email"].endswith("@example.com"))
        self.assertEqual(entry["siteId"], SITE)
        for absent in ("phone", "phoneNumber", "phoneNumbers", "address", "externalId"):
            self.assertNotIn(absent, entry)
        child = entry["children"][0]
        self.assertEqual(child["childId"], CHILD)
        self.assertEqual(child["share"], {"multiplier": 0.0})        # nothing bills
        self.assertIs(type(child["share"]["multiplier"]), float)
        self.assertEqual(result.writes, 5)

    def test_the_payer_document_is_never_built_if_the_schema_cannot_support_it(self):
        gql = FakeGql()
        with mock.patch.object(runner.bpw, "load_plan", side_effect=runner.bpw.NotConfigured("x")):
            with self.assertRaises(runner.ProbeRefused):
                self.run_probe(gql)
        self.assertEqual(gql.writes, [])

    def test_a_failed_step_2_reads_the_payer_count_once_and_stops(self):
        body = json.dumps({"errors": [{"message": "Variable $input of type [BillPayerInput!]! was invalid",
                                       "extensions": {"code": "BAD_USER_INPUT"}}]})
        result, gql = self.run_probe(FakeGql(payer_error=GraphQLHTTPError("x", 400, body)))
        self.assertFalse(result.rows["2_bill_payer_created"])
        self.assertEqual(result.rows["2_child_bill_payer_count_after"], 0)
        self.assertIn("message='Variable $input of type [BillPayerInput!]! was invalid'", result.rows["2_error"])
        self.assertEqual(result.writes, 2)                           # nothing after step 2 ran
        landed, _ = self.run_probe(FakeGql(payer_error=GraphQLHTTPError("x", 400, body),
                                           payer_lands_then_errors=True))
        self.assertEqual(landed.rows["2_child_bill_payer_count_after"], 1)   # it did exist

    def test_contact_id_reuses_a_contact_on_the_child_and_skips_step_1(self):
        result, gql = self.run_probe(FakeGql(reuse_contact="on_child"), contact_id="22222222-3333-4444-8555-666666666666")
        self.assertEqual(result.writes, 4)                           # no contact create
        self.assertNotIn(runner.OPS[runner.CREATE_CONTACT_DOC], gql.writes)
        self.assertTrue(result.rows["1_contact_reused_on_this_child"])
        self.assertNotIn("1_contact_created", result.rows)
        self.assertEqual(result.contact_id, "22222222-3333-4444-8555-666666666666")

    def test_contact_id_for_a_contact_not_on_the_child_is_refused_before_any_write(self):
        gql = FakeGql(reuse_contact="elsewhere")
        with self.assertRaises(runner.ProbeRefused):
            self.run_probe(gql, contact_id="22222222-3333-4444-8555-666666666666")
        self.assertEqual(gql.writes, [])
        with self.assertRaises(runner.ProbeRefused):
            self.run_probe(FakeGql(), contact_id="not-a-uuid")

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
