"""Tests for the probe-contact-create ONE-OFF probe: the guards only.

Run from the project root:

    python -m unittest tests.test_probe_contact_create -q

Fakes only. The probe is never run against live Famly from a test: the fake
GraphQL client records every call, and each guard test asserts that NO create
request was made.
"""

import contextlib
import io
import json
import unittest
from unittest import mock

from actions.probe_contact_create import cli, runner
from core.client import GraphQLError
from core.rest_client import RestHTTPError

CHILD = "26263e6b-265f-45a5-a7df-e35158aaa68d"


class FakeRest:
    def __init__(self, summary=None, error=None):
        self.summary = {"child": {"id": CHILD, "isTestChild": True}} if summary is None else summary
        self.error = error
        self.gets = []

    def get(self, path, params=None):
        self.gets.append(path)
        if self.error:
            raise self.error
        return self.summary

    def post(self, *a, **k):
        raise AssertionError("the probe must not POST to the internal API")

    request = post


class FakeGql:
    """Records every call. `rule(contact)` returns normally to accept or raises
    GraphQLError to reject."""

    def __init__(self, rule=None):
        self.rule = rule or (lambda contact: None)
        self.calls = []
        self.creates = []
        self.stored = {}

    def execute(self, query_path, variables, operation_name):
        self.calls.append(operation_name)
        if operation_name == runner.INTROSPECT_OP:
            return {"data": {"mutation": {"fields": [{"name": "create", "args": [
                {"name": "contacts", "type": {"kind": "NON_NULL", "name": None, "ofType": {
                    "kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "ContactInput"}}}}],
                "type": {"kind": "OBJECT", "name": "Contact", "ofType": None}}]},
                "input": {"inputFields": []}}}
        if operation_name == runner.CREATE_OP:
            contact = variables["contacts"][0]
            self.creates.append(contact)
            self.rule(contact)
            new_id = f"contact-{len(self.creates)}"
            self.stored[new_id] = {"id": new_id, "name": {"firstName": "AAProbe",
                                   "lastName": "ContactOne", "fullName": "AAProbe ContactOne"}}
            return {"data": {"contacts": {"create": [{"id": new_id}]}}}
        if operation_name == runner.READ_OP:
            return {"data": {"contacts": {"list": {"result": list(self.stored.values()),
                                                   "next": None}}}}
        raise AssertionError(operation_name)


def reject(message):
    def rule(contact):
        raise GraphQLError("rejected", [{"message": message}])
    return rule


class ProbeTestCase(unittest.TestCase):
    def run_probe(self, rest=None, gql=None, child=CHILD, confirm=True):
        self.rest = rest or FakeRest()
        self.gql = gql or FakeGql()
        self.lines = []
        return runner.run_probe(child, confirm, self.rest, self.gql, out=self.lines.append)

    def assert_refused_without_any_write(self, message=None, **kwargs):
        with self.assertRaises(runner.ProbeRefused) as ctx:
            self.run_probe(**kwargs)
        self.assertEqual(self.gql.creates, [])
        self.assertNotIn(runner.CREATE_OP, self.gql.calls)
        if message is not None:
            self.assertEqual(str(ctx.exception), message)
        return ctx.exception


class GuardTests(ProbeTestCase):
    def test_it_refuses_without_confirm_and_touches_nothing(self):
        self.assert_refused_without_any_write(confirm=False)
        self.assertEqual(self.rest.gets, [])
        self.assertEqual(self.gql.calls, [])

    def test_it_refuses_a_child_that_is_not_a_test_child(self):
        for summary in (
            {"child": {"isTestChild": False}},
            {"child": {"name": "no flag here"}},
            {"child": {"isTestChild": "true"}},   # a string is not true
            {"child": {"isTestChild": None}},
            {"child": {"isTestChild": True}, "other": {"isTestChild": False}},  # any false
            {},
        ):
            with self.subTest(summary=summary):
                self.assert_refused_without_any_write(
                    "not a test child", rest=FakeRest(summary), gql=FakeGql())
                self.assertEqual(self.gql.calls, [])  # not even the introspection

    def test_an_unreadable_summary_refuses_rather_than_assuming(self):
        err = RestHTTPError("boom", 500, "body")
        exc = self.assert_refused_without_any_write(rest=FakeRest(error=err))
        self.assertIn("no write made", str(exc))

    def test_a_non_uuid_child_id_is_refused_before_any_call(self):
        self.assert_refused_without_any_write(child="not-a-uuid")
        self.assertEqual(self.rest.gets, [])

    def test_the_contact_cap_is_enforced_and_printed(self):
        with mock.patch.object(runner, "MAX_CONTACTS", 2):
            result = self.run_probe()
        self.assertEqual(len(self.gql.creates), 2)
        self.assertEqual(len(result.created), 2)
        self.assertTrue(any("CAP: at most 2 contacts" in line for line in self.lines))
        self.assertTrue(any("contact cap (2) reached" in line for line in self.lines))

    def test_the_default_cap_is_eight_and_a_clean_run_stays_under_it(self):
        self.assertEqual(runner.MAX_CONTACTS, 8)
        result = self.run_probe()
        self.assertLessEqual(len(result.created), 8)
        self.assertTrue(any("CAP: at most 8 contacts" in line for line in self.lines))

    def test_rejections_cannot_loop_past_the_attempt_cap(self):
        with mock.patch.object(runner, "MAX_ATTEMPTS", 3):
            result = self.run_probe(gql=FakeGql(reject("phone is invalid, country too")))
        self.assertEqual(len(self.gql.creates), 3)
        self.assertEqual(result.created, [])

    def test_a_child_that_stops_being_a_test_child_stops_the_run(self):
        rest = FakeRest()
        original = rest.get
        def get(path, params=None):
            body = original(path, params)
            if len(rest.gets) >= 2:  # the re-check after the first create
                return {"child": {"isTestChild": False}}
            return body
        rest.get = get
        with self.assertRaises(runner.ProbeRefused):
            self.run_probe(rest=rest)
        self.assertEqual(len(self.gql.creates), 1)  # nothing after the failed re-check


class AttemptOrderTests(ProbeTestCase):
    def test_name_styles_are_tried_in_order_and_every_error_is_printed(self):
        def rule(contact):
            if isinstance(contact["name"], dict):
                raise GraphQLError("x", [{"message": "NameInput: bad format %d" % len(contact["name"])}])
        result = self.run_probe(gql=FakeGql(rule))
        names = [c["name"] for c in self.gql.creates[:3]]
        self.assertEqual(names[0], {"firstName": "AAProbe", "lastName": "ContactOne"})
        self.assertEqual(names[1], {"firstName": "AAProbe", "lastName": "ContactOne",
                                    "fullName": "AAProbe ContactOne"})
        self.assertEqual(names[2], "AAProbe ContactOne")
        self.assertEqual(len(result.rejected), 2)
        text = "\n".join(self.lines)
        self.assertIn("NameInput: bad format 2", text)
        self.assertIn("NameInput: bad format 3", text)
        self.assertEqual(result.accepted["name"], ["plain_string"])

    def test_every_create_is_roles_empty_on_the_given_child_with_no_state(self):
        self.run_probe()
        self.assertTrue(self.gql.creates)
        for contact in self.gql.creates:
            self.assertEqual(contact["roles"], [])
            self.assertEqual(contact["childIds"], [CHILD])
            self.assertIs(contact["emergencyContact"], False)
            self.assertNotIn("state", contact["address"])
            self.assertEqual(contact["records"], [{"key": "RELATION", "value": "Parent"}])
        emails = [c["email"] for c in self.gql.creates]
        self.assertEqual(len(emails), len(set(emails)))  # a different email each
        self.assertTrue(all(e.endswith("@example.com") for e in emails))

    def test_country_then_phone_fallbacks_and_the_accepted_formats_are_recorded(self):
        def rule(contact):
            if contact["address"]["country"] == "United Kingdom":
                raise GraphQLError("x", [{"message": "invalid country"}])
            if contact["phoneNumbers"][0]["value"] == "+447700900123":
                raise GraphQLError("x", [{"message": "invalid phone number"}])
        result = self.run_probe(gql=FakeGql(rule))
        self.assertEqual(result.accepted["country"], ["GB"])
        self.assertEqual(result.accepted["phone"][0], "07700 900123")
        self.assertIn("+44 7700 900123", result.accepted["phone"])
        self.assertTrue(any("invalid country" in r["error"] for r in result.rejected))
        ids = [c["id"] for c in result.created]
        self.assertTrue(all(ids))
        printed = "\n".join(self.lines).split("CREATED CONTACT IDS")[1]
        for contact_id in ids:
            self.assertIn(contact_id, printed)  # listed so they can be deleted in Famly


class CliTests(unittest.TestCase):
    def parse(self, argv):
        import argparse
        parser = argparse.ArgumentParser()
        cli.add_args(parser)
        return parser.parse_args(argv)

    def test_confirm_is_off_by_default_and_the_child_id_is_required(self):
        self.assertFalse(self.parse(["--child-id", CHILD]).confirm)
        self.assertTrue(self.parse(["--child-id", CHILD, "--confirm"]).confirm)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.parse([])

    def test_a_refusal_exits_non_zero_without_building_a_write_client_call(self):
        args = self.parse(["--child-id", CHILD])  # no --confirm
        gql = FakeGql()
        with mock.patch.object(cli, "RestClient", return_value=FakeRest()), \
                mock.patch.object(cli, "_public_client", return_value=gql):
            with self.assertRaises(SystemExit) as ctx,                     contextlib.redirect_stderr(io.StringIO()):
                cli.handle(args)
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(gql.calls, [])

    def test_the_test_child_id_is_not_hard_coded_anywhere_in_the_package(self):
        from pathlib import Path
        for path in Path(runner.__file__).parent.iterdir():
            if path.suffix in (".py", ".graphql"):
                self.assertNotIn(CHILD, path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
