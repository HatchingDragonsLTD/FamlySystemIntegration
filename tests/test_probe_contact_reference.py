"""probe-contact-reference: guards, caps, the not-a-test-child stop, one send per write, and an
output with no values. Fakes only; nothing here talks to Famly."""

import copy
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.probe_contact_reference import runner
from core.client import GraphQLHTTPError
from core.rest_client import RestHTTPError

CHILD = "11111111-2222-4333-8444-555555555555"


class FakeRest:
    def __init__(self, test_child=True, error=False):
        self.test_child, self.error, self.paths = test_child, error, []

    def get(self, path, params=None):
        self.paths.append(path)
        if self.error:
            raise RestHTTPError("boom", 500, "")
        return {"child": {"isTestChild": self.test_child}}


class FakeGql:
    """The few operations the probe uses. `replace_records` makes a records update REPLACE the
    contact's records (instead of merging per key); `lookup_lag` is the number of lookup calls
    that see nothing yet."""

    def __init__(self, replace_records=False, lookup_lag=0, reject=None, drop_first_ref=False):
        self.contacts, self.writes, self.calls = {}, [], []
        self.replace_records, self.lookup_lag, self.reject, self.drop_first_ref = replace_records, lookup_lag, reject, drop_first_ref
        self.lookups, self.n = 0, 0

    @staticmethod
    def _node(c):
        return {"id": c["id"], "name": {"fullName": c["name"]}, "email": c["email"],
                "phoneNumbers": copy.deepcopy(c["phoneNumbers"]), "childIds": [CHILD],
                "emergencyContact": c["emergencyContact"], "roles": [],
                "externalSystemsReferences": copy.deepcopy(c["refs"]),
                "records": [{"key": k, "value": v} for k, v in c["records"].items()]}

    def execute(self, query_path, variables, operation_name):
        text = Path(query_path).read_text(encoding="utf-8")
        self.calls.append(operation_name)
        if operation_name == runner.CREATE_OP:
            self.writes.append(operation_name)
            if self.reject:
                raise GraphQLHTTPError("bad", 400, "{}")
            item = variables["contacts"][0]
            self.n += 1
            cid = f"contact-{self.n}"
            self.contacts[cid] = {"id": cid, "name": item["name"], "email": item["email"],
                                  "phoneNumbers": item["phoneNumbers"], "emergencyContact": item["emergencyContact"],
                                  "refs": [dict(r) for r in item["externalSystemsReferences"]],
                                  "records": {r["key"]: r["value"] for r in item["records"]}}
            return {"data": {"contacts": {"create": [{"id": cid}]}}}
        if operation_name == runner.UPDATE_RECORDS_OP:
            self.writes.append(operation_name)
            for entry in variables["records"]:
                contact = self.contacts[entry["contactId"]]
                new = {r["key"]: r["value"] for r in entry["record"]}
                contact["records"] = new if self.replace_records else {**contact["records"], **new}
            return {"data": {"contacts": {"update": [{"id": e["contactId"]} for e in variables["records"]]}}}
        if operation_name == runner.UPDATE_CONTACT_OP:
            self.writes.append(operation_name)
            for item in variables["contacts"]:
                contact = self.contacts[item["id"]]
                contact.update(name=item["name"], email=item["email"], phoneNumbers=item["phoneNumbers"],
                               emergencyContact=item["emergencyContact"],
                               records={r["key"]: r["value"] for r in item["records"]})
                refs = [dict(r) for r in item["externalSystemsReferences"]]
                contact["refs"] = refs[1:] if self.drop_first_ref else refs            # replace-all
            return {"data": {"contacts": {"update": [{"id": i["id"]} for i in variables["contacts"]]}}}
        if operation_name == runner.READ_OP:
            assert "mutation" not in text.lower()
            return {"data": {"contacts": {"list": {"result": [self._node(c) for c in self.contacts.values()],
                                                    "next": None}}}}
        if operation_name == runner.LOOKUP_OP:
            assert "mutation" not in text.lower()
            self.lookups += 1
            if self.lookups <= self.lookup_lag:
                return {"data": {"contacts": {"listByExternalSystemReference": {"result": [], "next": None}}}}
            hits = [{"id": c["id"]} for c in self.contacts.values()
                    if any(r["foreignId"] == variables["foreignId"] and r["system"] == variables["system"]
                           for r in c["refs"])]
            return {"data": {"contacts": {"listByExternalSystemReference": {"result": hits, "next": None}}}}
        raise AssertionError(operation_name)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(gql=None, rest=None, confirm=True, child=CHILD, clock=None):
    clock = clock or Clock()
    lines = []
    gql = gql or FakeGql()
    result = runner.run_probe(child, confirm, rest or FakeRest(), gql, out=lines.append,
                              sleep=clock.sleep, clock=clock)
    return result, gql, "\n".join(lines)


class GuardTests(unittest.TestCase):
    def test_refuses_without_confirm_or_with_a_bad_id_and_sends_nothing(self):
        for kwargs in ({"confirm": False}, {"child": "not-a-uuid"}):
            gql = FakeGql()
            with self.subTest(kwargs=kwargs), self.assertRaises(runner.ProbeRefused):
                run(gql=gql, **kwargs)
            self.assertEqual(gql.calls, [])

    def test_a_child_that_is_not_a_test_child_stops_with_only_that_message(self):
        gql, rest = FakeGql(), FakeRest(test_child=False)
        with self.assertRaises(runner.ProbeRefused) as caught:
            run(gql=gql, rest=rest)
        self.assertEqual(str(caught.exception), "not a test child")
        self.assertEqual(gql.calls, [])
        self.assertEqual(rest.paths, [f"/v2/children/{CHILD}/summary"])

    def test_an_unreadable_summary_is_a_refusal_too(self):
        gql = FakeGql()
        with self.assertRaises(runner.ProbeRefused):
            run(gql=gql, rest=FakeRest(error=True))
        self.assertEqual(gql.calls, [])

    def test_every_document_passes_the_guards_and_a_delete_does_not(self):
        runner.check_documents()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "evil.graphql")
            path.write_text("mutation Evil($c: [ContactInput!]!) { contacts { create(contacts: $c) { id } "
                            "delete(contactIds: []) } }", encoding="utf-8")
            with self.assertRaises(AssertionError):
                runner.assert_probe_mutation(path, "create")
            path.write_text("mutation Evil { children { delete(childIds: []) } }", encoding="utf-8")
            with self.assertRaises(AssertionError):
                runner.assert_probe_mutation(path, "create")
        with self.assertRaises(AssertionError):
            runner.assert_read_only(runner.CREATE_DOC)                  # a write doc is not a read


class CapTests(unittest.TestCase):
    def test_a_full_run_sends_four_writes_and_creates_two_contacts(self):
        result, gql, _text = run()
        self.assertEqual(len(gql.writes), 4)
        self.assertEqual(result.writes, 4)
        self.assertEqual(len(result.created_ids), 2)
        self.assertLessEqual(len(gql.writes), runner.MAX_WRITES)

    def test_the_contact_cap_and_the_write_cap_stop_the_run(self):
        with mock.patch.object(runner, "MAX_CONTACTS", 1):
            result, gql, text = run()
        self.assertEqual(len(result.created_ids), 1)
        self.assertEqual(result.stopped, "contact cap reached")
        with mock.patch.object(runner, "MAX_WRITES", 2):
            result, gql, _t = run()
        self.assertEqual(result.writes, 2)
        self.assertEqual(len(gql.writes), 2)
        self.assertEqual(result.stopped, "write cap reached")

    def test_a_rejected_create_is_sent_once_and_stops_the_run(self):
        result, gql, text = run(gql=FakeGql(reject=True))
        self.assertEqual(gql.writes, [runner.CREATE_OP])                 # never retried
        self.assertFalse(result.rows["1_create_accepted"])
        self.assertEqual(result.created_ids, [])
        self.assertIn("contact A was not created", text)


class ResultTests(unittest.TestCase):
    def test_merge_semantics_and_the_reference_results(self):
        result, _gql, _t = run()
        rows = result.rows
        self.assertTrue(rows["1_create_accepted"] and rows["1_reference_present_after_create"])
        self.assertEqual(rows["1_system_returned"], "HUBSPOT")
        self.assertEqual((rows["2_lookup_seconds_until_seen"], rows["2_lookup_count"]), (0.0, 1))
        self.assertTrue(rows["3_relation_present"] and rows["3_title_survived"] and rows["3_occupation_survived"])
        self.assertEqual(rows["4_reference_count_after"], 2)
        self.assertTrue(rows["4_first_reference_survived"] and rows["4_second_reference_present"])
        for key in ("4_name_unchanged", "4_phones_unchanged", "4_email_unchanged", "4_records_unchanged"):
            self.assertTrue(rows[key], key)
        self.assertTrue(rows["5_shared_id_allowed"])
        self.assertEqual(rows["5_lookup_returns_count"], 2)

    def test_replace_semantics_are_reported_as_lost_records(self):
        rows = run(gql=FakeGql(replace_records=True))[0].rows
        self.assertTrue(rows["3_relation_present"])
        self.assertFalse(rows["3_title_survived"])
        self.assertFalse(rows["3_occupation_survived"])

    def test_a_reference_update_that_drops_the_first_reference_is_reported(self):
        rows = run(gql=FakeGql(drop_first_ref=True))[0].rows
        self.assertFalse(rows["4_first_reference_survived"])
        self.assertEqual(rows["4_reference_count_after"], 1)

    def test_a_lookup_that_lags_reports_the_seconds_and_one_that_never_sees_it_says_so(self):
        rows = run(gql=FakeGql(lookup_lag=3))[0].rows
        self.assertEqual(rows["2_lookup_seconds_until_seen"], 6.0)          # 3 empty polls, 2 s apart
        rows = run(gql=FakeGql(lookup_lag=10_000))[0].rows
        self.assertEqual(rows["2_lookup_seconds_until_seen"], "not_seen_in_60s")


class NoValuesTests(unittest.TestCase):
    def test_the_output_holds_no_name_email_phone_or_reference_value(self):
        gql = FakeGql()
        _result, _gql, text = run(gql=gql)
        for value in ("AAProbe", "example.com", "+4477", "7700900123", "probe-ref", "Parent"):
            self.assertNotIn(value, text)
        foreign_ids = {r["foreignId"] for c in gql.contacts.values() for r in c["refs"]}
        for value in foreign_ids:
            self.assertNotIn(value, text)
        self.assertIsNone(re.search(r"\d{9,}", text))                      # no long numeric id at all
        self.assertIn("contact-1", text)                                   # the created ids ARE listed


if __name__ == "__main__":
    unittest.main()
