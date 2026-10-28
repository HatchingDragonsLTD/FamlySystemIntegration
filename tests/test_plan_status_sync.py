"""famly_plan_status / famly_plan_note: the writer, the codes, every hook point, the success path, the legacy
switch and the backfill. Fakes only: nothing reaches Famly, HubSpot or Slack. Codes and ids only."""

import argparse
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from actions.create_timeline import engine as chain_engine
from actions.hubspot_plan_status import cli as backfill_cli
from actions.hubspot_plan_status import runner as backfill
from actions.plan_write import approval, plan_flag, preview_store, status_codes, status_sync
from actions.plan_write import web as plan_write_web
from actions.plan_write import runner as plan_runner
from actions.queue_timeline import worker
from core import hubspot
from core.hubspot import HubSpotDealWriter, HubSpotError
from actions.read_child_plans.runner import Plan
from core.rest_client import RestHTTPError
from tests import test_create_timeline as harness
from tests import test_queue_timeline as queue_tests
from tests.test_commit_approval import CHILD_ID, PLAN_BODY, PREVIEW_ID, FakeResult, StoreTestCase
from tests.test_plan_flag import FakeWriter, STATUS_DEF

ID = "5550001"
NAMES_AND_EMAILS = "Alex Smith pat.smith@example.com 07700 900123 12 Acorn Road"


class StatusWriter(FakeWriter):
    """FakeWriter whose status write can be rejected and whose reads 404 for unknown deals."""

    def __init__(self, *args, status_reject=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.status_reject = set(status_reject)
        self.known = None                       # deal ids that exist (None: all)

    def write_plan_status(self, deal_id, value):
        self.order.append("status")
        self.status_writes.append((deal_id, value))
        if deal_id in self.status_reject:
            raise HubSpotError("PATCH failed with HTTP 400", 400)
        self.status[deal_id] = value

    def read_plan_status_state(self, deal_id):
        if self.known is not None and deal_id not in self.known:
            raise HubSpotError("GET failed with HTTP 404", 404)
        return super().read_plan_status_state(deal_id)


class PlanStatusCase(StoreTestCase):
    """Approve clicks with the flag and status steps run inline against a fake writer; no real Slack."""

    def setUp(self):
        super().setUp()
        self.writer = StatusWriter({"555": None})
        self.notices = []
        for target, name, fake in (
                (plan_flag, "HubSpotDealWriter", lambda: self.writer),
                (status_sync, "HubSpotDealWriter", lambda: self.writer),
                (approval, "_spawn", lambda work: work()),
                (status_sync, "_spawn", lambda work: work()),
                (plan_flag.slack, "post_notice", lambda text: self.notices.append(text) or True)):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, deal_id="555", **kwargs):
        preview_store.save(PREVIEW_ID, PLAN_BODY, CHILD_ID, 3, deal_id=deal_id, **kwargs)

    def commit_fails(self, exc):
        return mock.patch.object(plan_runner, "commit", side_effect=exc)


# --------------------------------------------------------------------------- #
# The approval.py hook: isolation (it must never change the outcome)
# --------------------------------------------------------------------------- #
class ApprovalHookIsolationTests(PlanStatusCase):
    FAILURES = (RestHTTPError("boom", 503, ""), RestHTTPError("boom", 422, ""),
                plan_runner.PlanCommitRefused("guard"), RuntimeError("odd"))

    def outcome(self, exc, hook=None):
        """What an Approve click that fails returns and leaves behind, with `hook` standing in for the step."""
        preview_store.save(PREVIEW_ID, PLAN_BODY, CHILD_ID, 3, deal_id="555")
        with self.commit_fails(exc), mock.patch.object(plan_flag, "report_failed_commit", hook or (lambda c, d: None)):
            text = approval.handle_click("plan_approve", PREVIEW_ID, "drew")
        stored = preview_store.get(PREVIEW_ID)
        result = (text, stored.status)
        with preview_store.open_store() as connection:
            connection.execute("DELETE FROM previews")
        return result

    def test_an_exception_in_the_hook_leaves_the_claim_released_and_the_outcome_identical(self):
        def exploding(claimed, detail):
            raise RuntimeError("hubspot exploded")
        for exc in self.FAILURES:
            with self.subTest(exc=type(exc).__name__, status=getattr(exc, "status_code", None)):
                baseline = self.outcome(exc)
                with_failing_hook = self.outcome(exc, exploding)
                self.assertEqual(with_failing_hook, baseline)
                self.assertEqual(baseline[1], preview_store.STATUS_PENDING)         # released, can be clicked again

    def test_a_hook_that_cannot_even_start_changes_nothing(self):
        baseline = self.outcome(RestHTTPError("boom", 503, ""))
        with mock.patch.object(approval, "_spawn", side_effect=RuntimeError("no threads")):
            self.assertEqual(self.outcome(RestHTTPError("boom", 503, "")), baseline)

    def test_the_hook_runs_after_the_claim_is_released_and_is_the_last_thing_the_function_does(self):
        seen = {}

        def hook(claimed, detail):
            seen["status"] = preview_store.get(PREVIEW_ID).status           # the claim was already released
        self.outcome(RestHTTPError("boom", 503, ""), hook)
        self.assertEqual(seen["status"], preview_store.STATUS_PENDING)

    def test_a_failed_commit_writes_needs_attention_with_a_code_only(self):
        for exc, code in ((RestHTTPError("boom", 503, ""), "commit_failed_http_5xx"),
                          (RestHTTPError("boom", 422, ""), "commit_failed_http_4xx"),
                          (RuntimeError(NAMES_AND_EMAILS), "commit_failed"),
                          (plan_runner.PlanCommitRefused(NAMES_AND_EMAILS), "commit_failed")):
            with self.subTest(code=code):
                self.writer.status_writes.clear(), self.writer.note_writes.clear()
                self.save()
                with self.commit_fails(exc):
                    approval.handle_click("plan_approve", PREVIEW_ID, "drew")
                self.assertEqual(self.writer.status_writes, [("555", "needs_attention")])
                self.assertEqual(self.writer.note_writes, [("555", code)])
                with preview_store.open_store() as connection:
                    connection.execute("DELETE FROM previews")

    def test_a_chain_preview_is_left_to_the_chain_engine_and_no_deal_id_writes_nothing(self):
        self.save(chain_id="chain-1", chain_index=0)
        with self.commit_fails(RestHTTPError("boom", 503, "")):
            approval.handle_click("plan_approve", PREVIEW_ID, "drew")
        self.assertEqual((self.writer.status_writes, self.writer.note_writes), ([], []))
        with preview_store.open_store() as connection:
            connection.execute("DELETE FROM previews")
        self.save(deal_id=None)
        with self.commit_fails(RestHTTPError("boom", 503, "")):
            approval.handle_click("plan_approve", PREVIEW_ID, "drew")
        self.assertEqual((self.writer.status_writes, self.writer.note_writes), ([], []))


# --------------------------------------------------------------------------- #
# Note codes only
# --------------------------------------------------------------------------- #
class NoteCodeTests(unittest.TestCase):
    def assert_code(self, code):
        self.assertTrue(hubspot.PLAN_NOTE_PATTERN.match(code), code)
        for bad in ("@", " ", "Smith", "Alex", "Acorn", "07700"):
            self.assertNotIn(bad, code)

    def test_every_mapper_returns_a_code_whatever_the_error_text_holds(self):
        dirty = [NAMES_AND_EMAILS, f"Famly rejected the plan (HTTP 422). {NAMES_AND_EMAILS}",
                 f"Famly upstream error (HTTP 502) -- worth trying again. {NAMES_AND_EMAILS}"]
        codes = [status_codes.plan_rejected([NAMES_AND_EMAILS]),
                 status_codes.plan_rejected([f"session_catalogue: no UUID for {NAMES_AND_EMAILS}"]),
                 status_codes.plan_rejected([f"addon_quantity (2.5) needs a half day {NAMES_AND_EMAILS}"]),
                 status_codes.plan_rejected_by_famly(422), status_codes.plan_rejected_by_famly(NAMES_AND_EMAILS),
                 status_codes.request_invalid("segments_invalid")]
        for text in dirty:
            codes += [status_codes.commit_failed(text), status_codes.queue_job_failed(text),
                      status_codes.chain_failed(text), status_codes.chain_failed(f"after {text} was committed")]
        for code in codes:
            self.assert_code(code)
        self.assertEqual(status_codes.commit_failed(dirty[1]), "commit_failed_http_4xx")
        self.assertEqual(status_codes.commit_failed(dirty[2]), "commit_failed_http_5xx")
        self.assertEqual(status_codes.commit_failed(dirty[0]), "commit_failed")

    def test_the_writer_refuses_anything_that_is_not_a_code_and_sends_nothing(self):
        writer = HubSpotDealWriter.__new__(HubSpotDealWriter)
        with mock.patch.object(HubSpotDealWriter, "_call") as call:
            for bad in (NAMES_AND_EMAILS, "pat@example.com", "has space", "UPPER", "a" * 101, None, 12):
                with self.assertRaises(ValueError):
                    writer.write_plan_note(ID, bad)
            for bad in ("done", "Plan_Created", "", None):
                with self.assertRaises(ValueError):
                    writer.write_plan_status(ID, bad)
            call.assert_not_called()
            writer.write_plan_note(ID, "")                                       # "" clears the note
            writer.write_plan_note(ID, "plan_rejected:famly_http_422")
            writer.write_plan_status(ID, "needs_attention")
        self.assertEqual(call.call_count, 3)
        for c in call.call_args_list:
            self.assertEqual(c.kwargs["attempts"], 1)                            # never retried

    def test_the_two_properties_are_not_writable_through_the_general_writer(self):
        writer = HubSpotDealWriter.__new__(HubSpotDealWriter)
        for prop in (hubspot.PLAN_STATUS_PROPERTY, hubspot.PLAN_NOTE_PROPERTY):
            self.assertNotIn(prop, hubspot.WRITABLE_DEAL_PROPERTIES)
            with self.assertRaises(ValueError):
                writer.write_deal_properties(ID, {prop: "x"})


# --------------------------------------------------------------------------- #
# The syncer: skip rules and order
# --------------------------------------------------------------------------- #
class SyncerTests(unittest.TestCase):
    def syncer(self, writer):
        self.notices = []
        return status_sync.PlanStatusSyncer(writer, notify=self.notices.append)

    def test_the_note_is_written_first_then_the_status_and_nothing_is_retried(self):
        writer = StatusWriter()
        self.assertEqual(self.syncer(writer).mark_needs_attention(ID, "commit_failed"), [])
        self.assertEqual(writer.order, ["note", "status"])
        self.assertEqual((writer.note_writes, writer.status_writes), ([(ID, "commit_failed")], [(ID, "needs_attention")]))

    def test_a_missing_property_or_unknown_dropdown_value_is_reported_and_skipped(self):
        writer = StatusWriter()
        del writer.definitions[hubspot.PLAN_NOTE_PROPERTY]
        self.assertEqual(self.syncer(writer).mark_needs_attention(ID, "x"), ["skipped:famly_plan_note"])
        self.assertEqual((writer.note_writes, writer.status_writes), ([], [(ID, "needs_attention")]))
        self.assertEqual(len(self.notices), 1)
        writer = StatusWriter()
        writer.definitions[hubspot.PLAN_STATUS_PROPERTY] = {**STATUS_DEF, "options": ["plan_created"]}
        codes = self.syncer(writer).mark_needs_attention(ID, "x")
        self.assertEqual(codes, ["value_not_offered:famly_plan_status:needs_attention"])
        self.assertEqual(writer.status_writes, [])
        del writer.definitions[hubspot.PLAN_STATUS_PROPERTY]
        self.assertIn("skipped:famly_plan_status", self.syncer(writer).mark_plan_created(ID)[1])

    def test_a_hubspot_error_is_a_code_never_an_exception_and_the_other_write_still_happens(self):
        writer = StatusWriter(status_reject={ID})
        codes = self.syncer(writer).mark_needs_attention(ID, "x")
        self.assertEqual(codes, ["write_failed:famly_plan_status"])
        self.assertEqual(writer.status_writes, [(ID, "needs_attention")])        # one attempt only
        self.assertEqual(len(self.notices), 1)
        self.assertNotIn("HTTP 400", self.notices[0])

    def test_attention_never_raises_and_no_deal_id_means_nothing(self):
        self.assertEqual(status_sync.attention_now(None, "x"), [])
        broken = mock.Mock(deal_property_definitions=mock.Mock(side_effect=RuntimeError("x")))
        self.assertEqual(status_sync.attention_now(ID, "x", broken), ["failed"])
        with mock.patch.object(status_sync, "_spawn", side_effect=RuntimeError("no threads")):
            status_sync.attention_async(ID, "x")                                 # no exception


# --------------------------------------------------------------------------- #
# Success: plan_created, the note cleared, overwriting an earlier needs_attention
# --------------------------------------------------------------------------- #
class SuccessPathTests(PlanStatusCase):
    def land(self):
        def commit(*args, **kwargs):
            self.live_plans = list(self.live_plans) + [Plan(id="plan-created-1", from_="2026-09-01")]
            return FakeResult()
        return mock.patch.object(plan_runner, "commit", side_effect=commit)

    def test_a_later_success_overwrites_needs_attention_and_clears_the_note(self):
        self.writer.status["555"], self.writer.notes["555"] = "needs_attention", "commit_failed"
        self.save()
        with self.land():
            approval.handle_click("plan_approve", PREVIEW_ID, "drew")
        self.assertEqual((self.writer.status["555"], self.writer.notes["555"]), ("plan_created", ""))
        self.assertEqual(self.writer.order[:2], ["note", "status"])

    def test_plan_created_with_a_leftover_note_is_rewritten_and_a_clean_one_is_left_alone(self):
        syncer = status_sync.PlanStatusSyncer(self.writer, notify=self.notices.append)
        self.writer.status["555"], self.writer.notes["555"] = "plan_created", "stale_note"
        self.assertEqual(syncer.mark_plan_created("555")[0], True)
        self.assertEqual(syncer.mark_plan_created("555"), (False, []))

    def test_an_unconfirmed_plan_writes_needs_attention_commit_unknown_never_plan_created(self):
        self.save()
        with mock.patch.object(plan_runner, "commit", return_value=FakeResult()):     # the plan never shows
            approval.handle_click("plan_approve", PREVIEW_ID, "drew")
        self.assertEqual(self.writer.status_writes, [("555", "needs_attention")])
        self.assertEqual(self.writer.note_writes, [("555", "commit_unknown")])
        self.assertEqual(self.writer.writes, [])                                  # no legacy flag either


# --------------------------------------------------------------------------- #
# The legacy switch
# --------------------------------------------------------------------------- #
class LegacySwitchTests(unittest.TestCase):
    def test_only_off_switches_it_off_and_the_default_is_on(self):
        for value, expected in (("", True), ("on", True), ("ON", True), ("yes", True), ("garbage", True),
                                ("off", False), ("OFF", False), (" off ", False)):
            self.assertEqual(plan_flag.legacy_write_enabled({plan_flag.LEGACY_ENV: value}), expected, value)
        self.assertTrue(plan_flag.legacy_write_enabled({}))

    def after_commit(self, env, old_plan_id=None, confirmed=True):
        writer = StatusWriter({"555": None})
        claimed = SimpleNamespace(deal_id="555", old_plan_id=old_plan_id, child_id="c", version=3, preview_id="p")
        with mock.patch.dict("os.environ", {plan_flag.LEGACY_ENV: env}), \
                mock.patch.object(plan_flag, "_confirmed", return_value=confirmed), \
                mock.patch.object(plan_flag, "_sleep"), mock.patch.object(status_sync, "_spawn"):
            plan_flag.after_commit(claimed, "plan-1", writer=writer, notify=lambda text: True)
        return writer

    def test_on_writes_both_off_writes_only_the_status(self):
        on, off = self.after_commit("on"), self.after_commit("off")
        self.assertEqual((on.writes, on.status_writes), ([("555", "true")], [("555", "plan_created")]))
        self.assertEqual((off.writes, off.status_writes), ([], [("555", "plan_created")]))

    def test_an_additional_plan_never_writes_the_old_flag_but_does_write_the_status(self):
        writer = self.after_commit("on", old_plan_id="existing")
        self.assertEqual((writer.writes, writer.status_writes), ([], [("555", "plan_created")]))

    def test_needs_attention_never_writes_the_old_flag(self):
        for env in ("on", "off"):
            writer = self.after_commit(env, confirmed=False)
            self.assertEqual(writer.writes, [])
            self.assertEqual(writer.status_writes, [("555", "needs_attention")])


# --------------------------------------------------------------------------- #
# The backfill
# --------------------------------------------------------------------------- #
class BackfillTests(unittest.TestCase):
    def states(self, *rows):
        return list(rows)

    def run_backfill(self, states, plans, confirm=True, writer=None, cap=backfill.MAX_DEALS):
        writer = writer or StatusWriter()
        asked = []

        def has_plan(child_id):
            asked.append(child_id)
            value = plans[child_id]
            if isinstance(value, Exception):
                raise value
            return value
        result = backfill.backfill(writer, has_plan, states, confirm, cap)
        return result, writer, asked

    def test_it_writes_plan_created_only_where_the_status_is_empty_and_a_plan_exists(self):
        states = [("1", "c1", None), ("2", "c2", None), ("3", "c3", "plan_created"), ("4", "c4", "needs_attention")]
        result, writer, asked = self.run_backfill(states, {"c1": True, "c2": False, "c3": True, "c4": True})
        self.assertEqual(writer.status_writes, [("1", "plan_created")])
        self.assertEqual(result["perDeal"], {"1": "set", "2": "no_plan", "3": "skipped_has_status",
                                            "4": "skipped_has_status"})
        self.assertEqual(asked, ["c1", "c2"])                  # a deal with a status is not even read from Famly

    def test_it_never_writes_needs_attention_never_touches_a_note_and_never_overwrites(self):
        states = [(str(n), f"c{n}", status) for n, status in enumerate(
            [None, "needs_attention", "plan_created", None, "something_else"], start=1)]
        _result, writer, _asked = self.run_backfill(states, {f"c{n}": True for n in range(1, 6)})
        self.assertEqual({value for _d, value in writer.status_writes}, {"plan_created"})
        self.assertEqual({d for d, _v in writer.status_writes}, {"1", "4"})
        self.assertEqual(writer.note_writes, [])               # a note is never written or cleared
        self.assertEqual(writer.notes, {})

    def test_it_skips_a_deal_without_a_child_and_one_whose_plans_cannot_be_read(self):
        states = [("1", None, None), ("2", "c2", None), ("3", "c3", None)]
        result, writer, _ = self.run_backfill(states, {"c2": RuntimeError("down"), "c3": True})
        self.assertEqual(result["perDeal"], {"1": "no_child", "2": "plan_unreadable", "3": "set"})
        self.assertEqual(writer.status_writes, [("3", "plan_created")])

    def test_it_is_a_dry_run_unless_confirmed(self):
        result, writer, _ = self.run_backfill([("1", "c1", None)], {"c1": True}, confirm=False)
        self.assertEqual((result["mode"], result["results"], result["written"]), ("dry_run", {"would_set": 1}, 0))
        self.assertEqual(writer.status_writes, [])

    def test_it_writes_at_most_60_per_run_and_defers_the_rest(self):
        states = [(str(n), f"c{n}", None) for n in range(70)]
        result, writer, _ = self.run_backfill(states, {f"c{n}": True for n in range(70)})
        self.assertEqual((result["written"], result["results"]), (60, {"deferred": 10, "set": 60}))
        self.assertEqual(len(writer.status_writes), 60)
        with self.assertRaises(backfill.FileRefused):
            backfill.parse_deal_ids("\n".join(str(1000 + n) for n in range(61)))

    def test_a_rejected_write_is_counted_not_retried_and_the_run_continues(self):
        writer = StatusWriter(status_reject={"1"})
        result, writer, _ = self.run_backfill([("1", "c1", None), ("2", "c2", None)], {"c1": True, "c2": True},
                                              writer=writer)
        self.assertEqual(result["results"], {"rejected": 1, "set": 1})
        self.assertEqual(writer.status_writes, [("1", "plan_created"), ("2", "plan_created")])

    def test_it_stops_before_anything_when_the_property_or_the_value_is_missing(self):
        writer = StatusWriter()
        del writer.definitions[hubspot.PLAN_STATUS_PROPERTY]
        result, writer, asked = self.run_backfill([("1", "c1", None)], {"c1": True}, writer=writer)
        self.assertEqual((result["mode"], result["stopped"], asked), ("stopped", "property_missing", []))
        writer = StatusWriter()
        writer.definitions[hubspot.PLAN_STATUS_PROPERTY] = {**STATUS_DEF, "options": ["needs_attention"]}
        result, writer, _ = self.run_backfill([("1", "c1", None)], {"c1": True}, writer=writer)
        self.assertEqual(result["stopped"], "value_not_offered")
        self.assertEqual(writer.status_writes, [])

    def test_file_states_report_an_unknown_deal_and_read_the_status(self):
        writer = StatusWriter()
        writer.known = {"1"}
        writer.children["1"], writer.status["1"] = "c1", "needs_attention"
        self.assertEqual(backfill.file_states(writer, ["1", "2"]), [("1", "c1", "needs_attention"), ("2", "not_found")])

    def test_the_cli_requires_from_famly_and_refuses_the_scope_flags_with_a_file(self):
        parser = argparse.ArgumentParser()
        backfill_cli.add_args(parser)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--operations"])                                  # --from-famly is required
        args = parser.parse_args(["--from-famly", "--file", "x.txt", "--leave-after", "2026-01-01"])
        with self.assertRaises(SystemExit) as ctx:
            backfill_cli.handle(args)
        self.assertEqual(ctx.exception.code, 3)


# --------------------------------------------------------------------------- #
# The other hook points
# --------------------------------------------------------------------------- #
class HookPointTests(harness.ChainTestCase):
    def setUp(self):
        super().setUp()
        self.writer = StatusWriter()
        for target, name, fake in ((status_sync, "HubSpotDealWriter", lambda: self.writer),
                                   (status_sync, "_spawn", lambda work: work())):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_rejected_plan_preview_tells_the_deal_with_a_code_and_a_missing_deal_id_tells_nothing(self):
        base = harness.engine.segment_payload(harness.engine.clean_payload(harness.payload()),
                                              {"from": "2026-09-01", "to": "2027-07-31", "funded": False,
                                               "advanced": False}, is_last=True)
        bad = {**base, "childId": ""}
        _data, status = plan_write_web.handle({"properties": bad, "dealId": ID})
        self.assertEqual(status, 422)
        self.assertEqual(self.writer.status_writes, [(ID, "needs_attention")])
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:input_invalid")])
        self.writer.status_writes.clear(), self.writer.note_writes.clear()
        plan_write_web.handle(bad)
        self.assertEqual((self.writer.status_writes, self.writer.note_writes), ([], []))

    def test_a_famly_refusal_of_the_preview_request_tells_the_deal_and_still_raises(self):
        with mock.patch.object(plan_write_web.hubspot_intake, "handle_intake",
                               side_effect=RestHTTPError("boom " + NAMES_AND_EMAILS, 503, "")):
            with self.assertRaises(RestHTTPError):
                plan_write_web.prepare_preview({"dealId": ID})
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:famly_http_503")])

    def test_a_chain_failure_tells_the_deal_but_a_reject_or_a_cancel_does_not(self):
        data, status = self.start(harness.with_funding(**{"dealId": ID}))
        self.assertEqual(status, 200, data)
        chain_engine.stop_chain(data["chain_id"], f"was rejected by drew. {NAMES_AND_EMAILS}")
        self.assertEqual((self.writer.status_writes, self.writer.note_writes), ([], []))
        data, _ = self.start(harness.with_funding(**{"dealId": ID}))
        chain_engine.stop_for_attention(data["chain_id"], f"Plan 1 could not be committed: {NAMES_AND_EMAILS}")
        self.assertEqual(self.writer.status_writes, [(ID, "needs_attention")])
        self.assertEqual(self.writer.note_writes, [(ID, "chain_failed:commit_failed")])


class QueueHookTests(queue_tests.QueueTestCase):
    def setUp(self):
        super().setUp()
        self.writer = StatusWriter()
        for target, name, fake in ((status_sync, "HubSpotDealWriter", lambda: self.writer),
                                   (status_sync, "_spawn", lambda work: work())):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_failed_queue_job_tells_the_deal_with_a_code_never_the_reason(self):
        data = self.queue({"properties": self.body(), "dealId": ID})
        worker.fail(data["job_id"], f"Plan 1 of 3 was not created ({NAMES_AND_EMAILS})")
        self.assertEqual(self.writer.status_writes, [(ID, "needs_attention")])
        self.assertEqual(self.writer.note_writes, [(ID, "queue_job_failed:plan_not_created")])

    def test_a_rejected_queue_request_tells_the_deal(self):
        bad = self.body()
        bad.pop("start_date")
        _data, status = queue_tests.queue_web.handle({"properties": bad, "dealId": ID})
        self.assertEqual(status, 422)
        self.assertEqual(self.writer.note_writes, [(ID, "plan_rejected:input_invalid")])


if __name__ == "__main__":
    unittest.main()
