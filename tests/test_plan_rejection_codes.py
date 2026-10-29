"""Plan rejection sub-codes: every validation rule has a stable code, the note is codes only, one Slack line per
rejection, and plan-check reports the same codes read-only. Fakes only."""

import json
import os
import re
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from actions.create_timeline import engine
from actions.plan_check import runner as plan_check
from actions.plan_write import hubspot_flatten, hubspot_intake, input_schema, status_codes, status_sync, timeline
from actions.plan_write import web as plan_write_web
from core import hubspot
from core.hubspot import HubSpotError
from tests import test_create_timeline as harness
from tests import test_queue_timeline as queue_tests
from tests.test_plan_status_sync import StatusWriter

UUID = "11111111-1111-4111-8111-111111111111"
INST = harness.INSTITUTION
ID = "5550001"
SECRETS = ("Alex Smith", "pat.smith@example.com", "07700 900123", "12 Acorn Road", "'none'", "2026-02-30")


def nested(**over):
    """A valid nested plan; `over` replaces top-level keys."""
    part = {"attendanceScheduleId": UUID, "termTimeOnly": False,
            "billing": {"id": "ANNUALIZED_V2", "title": "Monthly", "weeksOfCare": 51, "invoices": "ADVANCE"},
            "sessionBookings": [{"sessionId": UUID, "day": "MONDAY", "fundable": False}],
            "productBookings": [], "discounts": []}
    plan = {"childId": UUID, "from": "2026-09-01", "to": None, "planParts": [part]}
    plan.update(over)
    return plan


def part_with(**over):
    plan = nested()
    plan["planParts"][0].update(over)
    return plan


def schema_errors(data):
    return input_schema.validate(input_schema.from_dict(data))


class SubCodeTriggerTests(harness.ChainTestCase):
    """Each sub-code is triggered by the REAL validator or function that produces its error text."""

    def reset_files(self):
        for env, content in (("SESSION_CATALOGUE_FILE", harness.SESSION_CATALOGUE),
                             ("INSTITUTION_DEFAULTS_FILE", harness.INSTITUTION_DEFAULTS),
                             ("FUNDING_RULES_FILE", harness.FUNDING_RULES)):
            Path(os.environ[env]).write_text(json.dumps(content), encoding="utf-8")

    def edit(self, env, change):
        path = Path(os.environ[env])
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def segments(self, funding=None, advanced=None):
        return timeline.build_segments("2026-09-01", "2027-07-31", funding, advanced)[0]

    def bookings(self, **over):
        return hubspot_flatten.build_session_bookings({"institution": INST, "monday": "morning", **over})[1]

    def timeline_error(self, *args, **kwargs):
        try:
            timeline.build_segments(*args, **kwargs)
        except timeline.TimelineError as exc:
            return [str(exc)]
        raise AssertionError("no TimelineError")

    def validate(self, funded=False, **over):
        base = engine.clean_payload(harness.with_funding(**over) if funded else harness.payload(**over))
        return engine.validate_segments(base, self.segments((("2026-11-01", "2026-12-31")) if funded else None))

    def advanced(self, **over):
        payload = harness.payload(has_advanced_fee="true", advanced_start="2026-10-01", advanced_end="2026-10-31",
                                  advanced_discount="0.05", **over)
        return engine.validate_segments(engine.clean_payload(payload),
                                        self.segments(advanced=("2026-10-01", "2026-10-31")))

    def triggers(self):
        t = {}
        # -- the request and its dates (engine.parse_request, timeline.build_segments) ------------------------
        t["child_id_missing"] = lambda: schema_errors(nested(childId=None))
        t["child_id_invalid"] = lambda: schema_errors(nested(childId="not-a-uuid"))
        t["start_date_missing"] = lambda: engine.parse_request(harness.payload(start_date="")).errors
        t["start_date_invalid"] = lambda: schema_errors(nested(**{"from": "01/09/2026"}))
        t["end_date_invalid"] = lambda: schema_errors(nested(to="next summer"))
        t["funding_dates_missing"] = lambda: engine.parse_request(harness.payload(has_funding="true")).errors
        t["advanced_fee_missing"] = lambda: engine.parse_request(harness.payload(has_advanced_fee="true")).errors
        t["date_not_iso"] = lambda: self.timeline_error("2026-09-01", None, ("2026-11-01", "11/12/2026"))
        t["date_not_real"] = lambda: self.timeline_error("2026-09-01", None, ("2026-02-30", "2026-03-01"))
        t["period_not_pair"] = lambda: self.timeline_error("2026-09-01", None, ("2026-11-01",))
        t["period_one_date"] = lambda: self.timeline_error("2026-09-01", None, ("2026-11-01", None))
        t["period_reversed"] = lambda: self.timeline_error("2026-09-01", None, ("2026-12-01", "2026-11-01"))
        t["leave_before_start"] = lambda: self.timeline_error("2026-09-01", "2026-08-01")
        # -- input_schema.validate ---------------------------------------------------------------------------
        t["rule_group_invalid"] = lambda: schema_errors(nested(ruleGroupId=""))
        t["no_plan_parts"] = lambda: schema_errors(nested(planParts=[]))
        t["billing_profile_invalid"] = lambda: schema_errors(part_with(billingProfileId="x"))
        t["attendance_schedule_invalid"] = lambda: schema_errors(part_with(attendanceScheduleId=None))
        t["term_schedule_invalid"] = lambda: schema_errors(part_with(termScheduleId="x"))
        t["term_time_flag_invalid"] = lambda: schema_errors(part_with(termTimeOnly="yes"))
        t["billing_scheme_unknown"] = lambda: schema_errors(part_with(billing={"id": "BRAND_NEW", "title": "t"}))
        t["billing_missing"] = lambda: schema_errors(part_with(billing=None))
        t["discount_invalid"] = lambda: schema_errors(part_with(discounts=["not an object"]))
        t["no_bookings"] = lambda: schema_errors(part_with(sessionBookings=[], productBookings=[]))
        t["session_id_invalid"] = lambda: schema_errors(part_with(
            sessionBookings=[{"sessionId": "x", "day": "MONDAY", "fundable": False}]))
        t["booking_day_invalid"] = lambda: schema_errors(part_with(
            sessionBookings=[{"sessionId": UUID, "day": "FUNDAY", "fundable": False}]))
        t["fundable_invalid"] = lambda: schema_errors(part_with(
            sessionBookings=[{"sessionId": UUID, "day": "MONDAY", "fundable": "yes"}]))
        t["product_id_invalid"] = lambda: schema_errors(part_with(
            productBookings=[{"productId": "x", "day": "MONDAY", "amount": 1}]))
        t["product_amount_invalid"] = lambda: schema_errors(part_with(
            productBookings=[{"productId": UUID, "day": "MONDAY", "amount": 0}]))
        t["funding_type_invalid"] = lambda: schema_errors(nested(publicFundingSettings={"fundingTypeIds": ["x"]}))
        # -- the flatten errors ------------------------------------------------------------------------------
        t["institution_missing"] = lambda: hubspot_flatten.build_session_bookings({"monday": "morning"})[1]
        t["day_slot_invalid"] = lambda: self.bookings(monday="none")
        t["session_institution_unknown"] = lambda: self.bookings(institution="NOSUCH")
        t["session_not_in_catalogue"] = lambda: (
            self.edit("SESSION_CATALOGUE_FILE", lambda d: d["institutions"][INST].update(non_funded={})),
            self.bookings())[1]
        t["session_catalogue_unreadable"] = lambda: (
            Path(os.environ["SESSION_CATALOGUE_FILE"]).unlink(), self.bookings())[1]
        t["defaults_institution_unknown"] = lambda: self.defaults_error("NOSUCH", "all_year_round")
        t["defaults_rule_group_missing"] = lambda: (
            self.edit("INSTITUTION_DEFAULTS_FILE", lambda d: d["institutions"][INST].pop("ruleGroupId")),
            self.defaults_error(INST, "all_year_round"))[1]
        t["defaults_schedule_invalid"] = lambda: self.defaults_error(INST, "weekly")
        t["defaults_schedule_missing"] = lambda: (
            self.edit("INSTITUTION_DEFAULTS_FILE", lambda d: d["institutions"][INST].pop("schedules")),
            self.defaults_error(INST, "all_year_round"))[1]
        t["defaults_incomplete"] = lambda: (
            self.edit("INSTITUTION_DEFAULTS_FILE",
                      lambda d: d["institutions"][INST]["schedules"]["all_year_round"].pop("billingId")),
            self.defaults_error(INST, "all_year_round"))[1]
        t["addon_products_missing"] = lambda: (
            self.edit("INSTITUTION_DEFAULTS_FILE", lambda d: d["institutions"][INST].pop("addonProducts")),
            hubspot_flatten.build_product_bookings({"institution": INST, "addon_quantity": "1"}, ["MONDAY"])[1])[1]
        t["defaults_unreadable"] = lambda: (
            Path(os.environ["INSTITUTION_DEFAULTS_FILE"]).unlink(), self.defaults_error(INST, "all_year_round"))[1]
        t["addon_not_a_number"] = lambda: self.addon("abc")
        t["addon_negative"] = lambda: self.addon("-1")
        t["addon_exceeds_days"] = lambda: self.addon("3", ["MONDAY"])
        t["addon_half_missing"] = lambda: self.addon("0.5")
        # -- engine.validate_segments -------------------------------------------------------------------------
        t["funding_invalid"] = lambda: self.validate(funded=True, institution="NOSUCH")
        t["discount_slot_in_use"] = lambda: self.advanced(discount_10_name="x", discount_10_amount="1")
        t["advanced_discount_out_of_range"] = lambda: engine.validate_segments(
            engine.clean_payload(harness.payload(has_advanced_fee="true", advanced_start="2026-10-01",
                                                 advanced_end="2026-10-31", advanced_discount="5")),
            self.segments(advanced=("2026-10-01", "2026-10-31")))
        t["advanced_fee_invalid"] = lambda: engine.validate_segments(
            engine.clean_payload(harness.payload(has_advanced_fee="true", advanced_start="2026-10-01",
                                                 advanced_end="2026-10-31", advanced_discount="abc")),
            self.segments(advanced=("2026-10-01", "2026-10-31")))
        return t

    def defaults_error(self, institution, schedule):
        from integrations import institution_defaults
        try:
            institution_defaults.resolve_defaults(institution, schedule)
        except institution_defaults.InstitutionDefaultsError as exc:
            return [str(exc)]
        raise AssertionError("no error")

    def addon(self, quantity, booked=("MONDAY", "TUESDAY")):
        return hubspot_flatten.build_product_bookings({"institution": INST, "addon_quantity": quantity},
                                                      list(booked))[1]

    def test_every_rule_has_a_sample_and_the_real_validator_triggers_its_code(self):
        triggers = self.triggers()
        rules = {code for code, _pattern in status_codes.SUBCODE_RULES}
        self.assertEqual(set(triggers), rules, "every rule needs a trigger and every trigger a rule")
        for code, trigger in triggers.items():
            with self.subTest(code=code):
                self.reset_files()
                errors = trigger()
                self.assertTrue(errors, f"{code}: the real code produced no error")
                found = status_codes.subcodes_of(errors)
                self.assertIn(code, found, (code, found))
                self.assertNotIn(status_codes.OTHER, found, (code, found))

    def test_an_error_no_rule_recognises_is_other_and_the_text_never_comes_out(self):
        self.assertEqual(status_codes.subcodes_of(["something brand new about Alex Smith"]), ["other"])
        self.assertEqual(status_codes.plan_rejected(["something brand new"]), "plan_rejected:input_invalid:other")

    def test_the_plan_check_only_codes_are_listed_and_the_table_has_no_duplicates(self):
        codes = [code for code, _p in status_codes.SUBCODE_RULES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(set(status_codes.PLAN_CHECK_ONLY), {"fees_missing", "fees_unparseable", "site_unknown"})
        self.assertEqual(len(status_codes.ALL_SUBCODES), len(set(status_codes.ALL_SUBCODES)))


class NoteShapeTests(unittest.TestCase):
    def test_a_note_holds_at_most_three_subcodes_then_plus_n(self):
        subs = ["a_one", "b_two", "c_three", "d_four", "e_five"]
        self.assertEqual(status_codes.with_subcodes("plan_rejected:input_invalid", subs[:3]),
                         "plan_rejected:input_invalid:a_one,b_two,c_three")
        self.assertEqual(status_codes.with_subcodes("plan_rejected:input_invalid", subs),
                         "plan_rejected:input_invalid:a_one,b_two,c_three,+2")
        self.assertEqual(status_codes.with_subcodes("plan_rejected:input_invalid", subs[:4]),
                         "plan_rejected:input_invalid:a_one,b_two,c_three,+1")
        self.assertEqual(status_codes.with_subcodes("plan_rejected:input_invalid", ["a", "a", "b"]),
                         "plan_rejected:input_invalid:a,b")                      # de-duplicated
        self.assertEqual(status_codes.with_subcodes("plan_rejected:input_invalid", []), "plan_rejected:input_invalid")

    def test_a_note_never_exceeds_100_characters_or_leaves_the_codes_only_alphabet(self):
        longest = sorted(status_codes.ALL_SUBCODES, key=len)[-4:]
        for top in ("plan_rejected:input_invalid", "plan_rejected:segments_invalid", "plan_rejected:institution_defaults"):
            code = status_codes.with_subcodes(top, longest)
            self.assertLessEqual(len(code), status_codes.NOTE_MAX)
            self.assertTrue(hubspot.PLAN_NOTE_PATTERN.match(code), code)
            self.assertIn(",+", code)
        every = status_codes.with_subcodes("plan_rejected:input_invalid", list(status_codes.ALL_SUBCODES))
        self.assertTrue(hubspot.PLAN_NOTE_PATTERN.match(every) and len(every) <= 100)
        self.assertEqual(status_codes.NOTE_MAX, 100)

    def test_the_note_never_holds_text_names_emails_or_values_whatever_the_errors_say(self):
        dirty = [f"childId: {SECRETS[0]} {SECRETS[1]} is not a UUID (labels must be resolved upstream)",
                 f"session_catalogue: {SECRETS[4]} is not a valid slot (expected one of morning)",
                 f"to: {SECRETS[5]!r} is not an ISO date (YYYY-MM-DD)", f"unrecognised: {SECRETS[2]} {SECRETS[3]}",
                 f"Plan 2 of 3: has_funding is true but funding_start is missing {SECRETS[0]}"]
        for note in (status_codes.plan_rejected(dirty), status_codes.request_invalid("segments_invalid", dirty),
                     status_codes.request_invalid("input_invalid", dirty), status_codes.request_invalid("timeline_invalid", dirty)):
            self.assertTrue(hubspot.PLAN_NOTE_PATTERN.match(note), note)
            self.assertLessEqual(len(note), 100)
            for secret in SECRETS:
                self.assertNotIn(secret.lower(), note)
            self.assertNotRegex(note, r"[\s@A-Z]")

    def test_no_bookings_is_left_out_when_a_failed_session_already_explains_it(self):
        failed = "session_catalogue: 'none' is not a valid slot (expected one of morning, afternoon, full_day)"
        empty = "planParts[0]: has neither session nor product bookings"
        self.assertEqual(status_codes.subcodes_of([failed, empty]), ["day_slot_invalid"])
        self.assertEqual(status_codes.subcodes_of([empty]), ["no_bookings"])                  # on its own it stays
        self.assertEqual(status_codes.subcodes_of(["institution: required, but missing or empty", empty]),
                         ["institution_missing"])

    def test_the_writer_still_refuses_free_text_and_accepts_a_plus_count(self):
        writer = hubspot.HubSpotDealWriter.__new__(hubspot.HubSpotDealWriter)
        with mock.patch.object(hubspot.HubSpotDealWriter, "_call") as call:
            for bad in ("Alex Smith", "pat@example.com", "plan rejected", "A"):
                with self.assertRaises(ValueError):
                    writer.write_plan_note(ID, bad)
            writer.write_plan_note(ID, "plan_rejected:input_invalid:a,b,c,+2")
        self.assertEqual(call.call_count, 1)


class SlackLineTests(harness.ChainTestCase):
    def setUp(self):
        super().setUp()
        self.writer = StatusWriter()
        for target, name, fake in ((status_sync, "HubSpotDealWriter", lambda: self.writer),
                                   (status_sync, "_spawn", lambda work: work())):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def bad_payload(self, **over):
        base = engine.segment_payload(engine.clean_payload(harness.payload()),
                                      {"from": "2026-09-01", "to": "2027-07-31", "funded": False, "advanced": False},
                                      is_last=True)
        return {**base, **over}

    def rejection_lines(self):
        return [n for n in self.notices if n.startswith("Plan not created")]

    def test_a_rejected_preview_posts_exactly_one_line_with_the_deal_id_and_sub_codes_only(self):
        _data, status = plan_write_web.handle({"properties": self.bad_payload(childId="", monday="none",
                                                                              child_name=SECRETS[0]), "dealId": ID})
        self.assertEqual(status, 422)
        lines = self.rejection_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(f"deal {ID}", lines[0])
        self.assertIn("plan_rejected:session_catalogue:", lines[0])
        for code in ("child_id_missing", "day_slot_invalid"):
            self.assertIn(code, lines[0])
        for secret in SECRETS:
            self.assertNotIn(secret, lines[0])
        self.assertEqual(self.writer.note_writes[0][1], lines[0].split("(")[1].split(")")[0])   # same code as the note

    def test_a_rejected_preview_with_no_deal_id_still_posts_one_line_and_writes_nothing(self):
        plan_write_web.handle({"properties": self.bad_payload(childId="")})
        lines = self.rejection_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("deal unknown", lines[0])
        self.assertEqual(self.writer.status_writes, [])

    def test_the_slack_line_is_the_sentence_and_the_code_with_no_values(self):
        text = status_sync.rejection_text(ID, "plan_rejected:input_invalid:child_id_missing,+1")
        self.assertEqual(text, f"Plan not created for deal {ID} (plan_rejected:input_invalid:child_id_missing,+1). "
                               "Nothing was written to Famly. Fix the deal in HubSpot and send it again.")
        self.assertEqual(status_sync.rejection_text(None, "x").split(" (")[0], "Plan not created for deal unknown")

    def test_a_slack_failure_never_hides_the_hubspot_write_and_never_raises(self):
        with mock.patch.object(status_sync.slack, "post_notice", side_effect=RuntimeError("slack down")):
            status_sync.rejection_now(ID, "plan_rejected:input_invalid:other", writer=self.writer)
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:input_invalid:other")])


class QueueAndChainRejectionTests(queue_tests.QueueTestCase):
    def setUp(self):
        super().setUp()
        self.writer = StatusWriter()
        for target, name, fake in ((status_sync, "HubSpotDealWriter", lambda: self.writer),
                                   (status_sync, "_spawn", lambda work: work())):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def lines(self):
        return [n for n in self.notices if n.startswith("Plan not created")]

    def test_a_queue_request_missing_its_start_date_gets_sub_codes_and_one_slack_line(self):
        bad = self.body()
        bad.pop("start_date")
        _data, status = queue_tests.queue_web.handle({"properties": bad, "dealId": ID})
        self.assertEqual(status, 422)
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:input_invalid:start_date_missing")])
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("start_date_missing", self.lines()[0])

    def test_a_queue_request_with_a_bad_weekday_gets_segments_invalid_with_the_sub_code(self):
        bad = self.body(monday="none", child_name=SECRETS[0])
        _data, status = queue_tests.queue_web.handle({"properties": bad, "dealId": ID})
        self.assertEqual(status, 422)
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:segments_invalid:day_slot_invalid")])
        self.assertEqual(len(self.lines()), 1)
        self.assertNotIn(SECRETS[0], self.lines()[0])

    def test_a_queue_request_with_a_bad_timeline_gets_timeline_invalid_with_the_sub_code(self):
        bad = self.body(leave_date="2026-08-01")
        _data, status = queue_tests.queue_web.handle({"properties": bad, "dealId": ID})
        self.assertEqual(status, 422)
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:timeline_invalid:leave_before_start")])

    def test_a_chain_request_is_rejected_the_same_way_with_one_slack_line(self):
        from actions.create_timeline import web as chain_web
        bad = harness.payload(monday="none")
        _data, status = chain_web.handle({"properties": bad, "dealId": ID})
        self.assertEqual(status, 422)
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:segments_invalid:day_slot_invalid")])
        self.assertEqual(len(self.lines()), 1)


class PlanCheckTests(harness.ChainTestCase):
    """plan-check: read-only, partial, the same sub-codes. Fake HubSpot reads only."""

    GOOD = {"famly_childid": UUID, "actual_start_date_1": "2026-09-01", "end_date": "2027-07-31",
            "location_of_interest": "City", "monday": "morning", "tuesday": "full day", "wednesday": "",
            "thursday": "", "friday": "", "billable_fees": "1,050.00"}

    def deal(self, **over):
        return {**self.GOOD, **over}

    def test_a_good_deal_has_no_sub_codes(self):
        self.assertEqual(plan_check.check_deal(self.deal()), [])

    def test_each_input_it_can_see_reports_its_sub_code(self):
        cases = {"child_id_missing": {"famly_childid": ""}, "start_date_missing": {"actual_start_date_1": ""},
                 "start_date_invalid": {"actual_start_date_1": "soon"}, "end_date_invalid": {"end_date": "later"},
                 "leave_before_start": {"end_date": "2026-08-01"}, "day_slot_invalid": {"monday": "none"},
                 "fees_missing": {"billable_fees": ""}, "fees_unparseable": {"billable_fees": "about a grand"},
                 "site_unknown": {"location_of_interest": "Shoreditch"}}
        for code, over in cases.items():
            with self.subTest(code=code):
                self.assertIn(code, plan_check.check_deal(self.deal(**over)))
        self.assertNotIn("start_date_missing", plan_check.check_deal(self.deal(actual_start_date_1="soon")))
        self.assertNotIn("institution_missing", plan_check.check_deal(self.deal(location_of_interest="Shoreditch")))

    def test_it_reads_only_prints_counts_and_codes_and_says_it_is_partial(self):
        class Hub:
            calls = 0
            deals = {"1": TestDeals.good(), "2": TestDeals.good(monday="none", billable_fees=""), "4": {}}

            def get_deal(self, deal_id):
                self.calls += 1
                if deal_id == "3":
                    raise HubSpotError("GET failed with HTTP 404", 404)
                return self.deals[deal_id]
        with mock.patch.object(plan_check.derive, "site_for_location", return_value=("HDCITY", "x")):
            result = plan_check.run(Hub(), ["1", "2", "3", "4"])
        self.assertEqual((result["mode"], result["coverage"]), ("read_only", "partial"))
        self.assertEqual(result["perDeal"]["1"], [])
        self.assertIn("day_slot_invalid", result["perDeal"]["2"])
        self.assertEqual(result["unreadable"], {"3": "deal_not_found"})
        self.assertEqual(result["bySubcode"]["fees_missing"], 2)
        self.assertEqual((result["deals"], result["dealsChecked"], result["clean"], result["withProblems"]), (4, 3, 1, 2))
        self.assertTrue(result["notChecked"])
        dumped = json.dumps(result)
        for secret in ("Smith", "@", "none"):
            self.assertNotIn(secret, dumped.replace("day_slot_invalid", ""))

    def test_plan_check_cannot_write_anything(self):
        import actions.plan_check.runner as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("write_", "HubSpotDealWriter", "slack", "RestClient", "post_notice", ".patch("):
            self.assertNotIn(forbidden, source)


class TestDeals:
    @staticmethod
    def good(**over):
        return {**PlanCheckTests.GOOD, **over}


if __name__ == "__main__":
    unittest.main()
