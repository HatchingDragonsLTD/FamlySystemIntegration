"""hubspot-set-plan-uploaded: a one-off that sets famly_plan_uploaded on a list of deals.

Fakes only. Counts, ids and codes only: nothing personal is planted or printed."""

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.hubspot_plan_uploaded import cli, runner
from core.hubspot import (
    PLAN_UPLOADED_PROPERTY, WRITABLE_DEAL_PROPERTIES, HubSpotDealWriter, HubSpotError)

BOOL = {"type": "bool", "fieldType": "booleancheckbox", "options": []}


class FakeWriter:
    def __init__(self, values, definition=BOOL):
        self.values = dict(values)          # deal id -> "true" | "false" | None; absent = not found
        self.definition = definition
        self.reads, self.writes = [], []
        self.reject = set()

    def deal_property_definitions(self):
        return {} if self.definition is None else {PLAN_UPLOADED_PROPERTY: self.definition}

    def read_plan_uploaded(self, deal_id):
        self.reads.append(deal_id)
        if deal_id not in self.values:
            raise HubSpotError("GET failed with HTTP 404", 404)
        return self.values[deal_id]

    def write_plan_uploaded(self, deal_id, value):
        self.writes.append((deal_id, value))
        if deal_id in self.reject:
            raise HubSpotError("PATCH failed with HTTP 400", 400)
        self.values[deal_id] = value


class RunTests(unittest.TestCase):
    def setUp(self):
        self.writer = FakeWriter({"1": "true", "2": "false", "3": None, "4": "false"})

    def test_a_dry_run_reads_every_deal_and_writes_nothing(self):
        result = runner.run(self.writer, ["1", "2", "3", "4", "9"], "false", confirm=False)
        self.assertEqual(self.writer.writes, [])
        self.assertEqual(len(self.writer.reads), 5)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["currently"], {"true": 1, "false": 2, "empty": 1, "not_found": 1})
        self.assertEqual(result["results"], {"would_change": 2, "unchanged": 2, "not_found": 1})

    def test_only_deals_whose_value_differs_are_written_once_each(self):
        result = runner.run(self.writer, ["1", "2", "3", "4", "9"], "false", confirm=True)
        self.assertEqual(sorted(self.writer.writes), [("1", "false"), ("3", "false")])
        self.assertEqual(result["perDeal"], {"1": "changed", "2": "unchanged", "3": "changed",
                                             "4": "unchanged", "9": "not_found"})
        self.assertEqual(self.writer.reads, ["1", "2", "3", "4", "9"])      # no re-read after a write

    def test_a_rejected_deal_is_reported_and_the_rest_still_go(self):
        self.writer.reject = {"1"}
        result = runner.run(self.writer, ["1", "3"], "false", confirm=True)
        self.assertEqual(result["perDeal"], {"1": "rejected", "3": "changed"})
        self.assertEqual(len(self.writer.writes), 2)                        # sent once each

    def test_the_property_missing_or_not_a_checkbox_stops_with_a_code_and_no_request(self):
        for definition, code in ((None, "property_missing"),
                                 ({"type": "string", "fieldType": "text", "options": []}, "property_not_boolean")):
            writer = FakeWriter({"1": "true"}, definition)
            result = runner.run(writer, ["1"], "false", confirm=True)
            self.assertEqual(result["stopped"], code)
            self.assertEqual((writer.reads, writer.writes), ([], []))

    def test_the_cap_refuses_more_than_60_deals_before_any_request(self):
        ids = [str(n) for n in range(1, 62)]
        with self.assertRaises(runner.FileRefused) as ctx:
            runner.parse_deal_ids("\n".join(ids))
        self.assertEqual(ctx.exception.code, "too_many_deals")
        with self.assertRaises(runner.FileRefused):
            runner.run(self.writer, ids, "false", confirm=True)
        self.assertEqual((self.writer.reads, self.writer.writes), ([], []))
        self.assertEqual(len(runner.parse_deal_ids("\n".join(ids[:60]))), 60)

    def test_an_invalid_value_is_refused(self):
        with self.assertRaises(runner.FileRefused):
            runner.run(self.writer, ["1"], "yes", confirm=True)
        self.assertEqual(self.writer.writes, [])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser = argparse.ArgumentParser()
            cli.add_args(parser)
            parser.parse_args(["--file", "x", "--value", "maybe"])

    def test_both_file_formats_are_read(self):
        self.assertEqual(runner.parse_deal_ids("# a note\n10\n\n11  # trailing\n10\n"), ["10", "11"])
        csv_text = "dealId,siteCode,stage,childId,found_via,codes\n20,HDCW,S,c1,urn,a;b\n21,HDWEST,S,,x,\n"
        self.assertEqual(runner.parse_deal_ids(csv_text), ["20", "21"])
        for bad in ("12x\n", "", "dealId,codes\nabc,x\n"):
            with self.assertRaises(runner.FileRefused):
                runner.parse_deal_ids(bad)

    def test_the_output_has_ids_counts_and_codes_only(self):
        result = runner.run(self.writer, ["1", "3"], "false", confirm=True)
        text = json.dumps(result)
        for word in ("@", "name", "phone", "address"):
            self.assertNotIn(word, text)


class CliTests(unittest.TestCase):
    def run_cli(self, ids_text, writer, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ids.txt"
            path.write_text(ids_text, encoding="utf-8")
            parser = argparse.ArgumentParser()
            cli.add_args(parser)
            args = parser.parse_args(["--file", str(path), "--value", "false", *extra])
            with mock.patch.object(cli, "HubSpotDealWriter", return_value=writer), \
                    contextlib.redirect_stdout(io.StringIO()):
                return cli.handle(args)

    def test_without_confirm_nothing_is_written(self):
        writer = FakeWriter({"1": "true"})
        self.run_cli("1\n", writer)
        self.assertEqual(writer.writes, [])

    def test_a_rejected_deal_makes_the_exit_non_zero(self):
        writer = FakeWriter({"1": "true"})
        writer.reject = {"1"}
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("1\n", writer, "--confirm")
        self.assertEqual(ctx.exception.code, 1)


class OnlyThisCommandMayWriteTheProperty(unittest.TestCase):
    def writer(self):
        return HubSpotDealWriter(token="test-token")

    def test_the_general_writer_refuses_the_property_and_sends_nothing(self):
        self.assertNotIn(PLAN_UPLOADED_PROPERTY, WRITABLE_DEAL_PROPERTIES)
        writer = self.writer()
        with mock.patch.object(writer, "_call") as call:
            with self.assertRaises(ValueError):
                writer.write_deal_properties("1", {PLAN_UPLOADED_PROPERTY: "true"})
            with self.assertRaises(ValueError):                  # also not mixed in with allowed ones
                writer.write_deal_properties("1", {"famly_childid": "c", PLAN_UPLOADED_PROPERTY: "true"})
            call.assert_not_called()

    def test_the_onboarding_syncer_cannot_write_it(self):
        from actions.onboard import hubspot_sync
        writer = self.writer()
        syncer = hubspot_sync.Syncer(writer, "1")
        with mock.patch.object(writer, "_call") as call:
            try:
                syncer.write(PLAN_UPLOADED_PROPERTY, "true")
            except Exception:  # noqa: BLE001 - refused one way or another
                pass
            call.assert_not_called()

    def test_the_dedicated_method_sends_one_request_with_one_attempt_and_only_true_or_false(self):
        writer = self.writer()
        with mock.patch.object(writer, "_call") as call:
            writer.write_plan_uploaded("5", "false")
            call.assert_called_once_with(
                "PATCH", "/crm/v3/objects/deals/5",
                json_body={"properties": {PLAN_UPLOADED_PROPERTY: "false"}}, attempts=1)
            with self.assertRaises(ValueError):
                writer.write_plan_uploaded("5", "True")
            self.assertEqual(call.call_count, 1)

    def test_nothing_else_in_the_repo_calls_the_dedicated_method(self):
        root = Path(__file__).resolve().parent.parent
        callers = []
        for path in root.rglob("*.py"):
            if any(part in ("tests", "__pycache__", ".git") for part in path.parts):
                continue
            if "write_plan_uploaded(" in path.read_text(encoding="utf-8", errors="ignore"):
                callers.append(path.relative_to(root).as_posix())
        self.assertEqual(sorted(callers), ["actions/hubspot_plan_uploaded/runner.py",
                                           "actions/plan_write/plan_flag.py", "core/hubspot.py"])


if __name__ == "__main__":
    unittest.main()
