"""Tests for onboarding PHASE 2: queued child and contact creation in Famly.

Run from the project root:

    python -m unittest tests.test_onboard_phase2 -q

Fakes only: never live Famly or HubSpot. The fake Famly client raises on any
GraphQL document that deletes or updates, the fake HubSpot raises on any write
other than the four sync properties, and every Slack payload, database row and
log line is scanned for the personal values planted in the fakes.
"""

import contextlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

from actions.onboard import (
    approval, config, famly_writes, hubspot_sync, inputs, notifier, phone, store,
    summary_check, worker,
)
from actions.onboard import cli_enqueue
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.links import DealContact
from actions.urn_lookup import runner as urn_runner
from core import hubspot
from core.client import GraphQLError, GraphQLHTTPError
from core.hubspot import HubSpotDealWriter, HubSpotError
from core.rest_client import RestHTTPError
from integrations import slack
from tests.test_onboard_dry_run import FakeHubSpot, make_deal

DEAL = "1001"
SITE_ID = "site-west"
GROUP_ID = "HDWEST-l"          # Littlewings: 24 months at the 2026-01-15 start

# Planted personal values. None may appear in Slack, the database or any log.
PII = [
    "quillfeather", "lovelace", "babbage", "ada.l@example.org", "ben.b@example.org",
    "07700 900123", "07700900123", "+447700900123", "07700 900456", "2024-01-15",
    "mill lane", "e1 1aa", "zara",
]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeHub(FakeHubSpot):
    """The phase-1 fake plus the one writer. Raises on any other write."""

    def __init__(self, deal_id=DEAL):
        super().__init__()
        self.deals = {deal_id: make_deal()}
        self.assoc = {("deals", deal_id, "contacts"): [
            {"id": "c1", "types": [{"typeId": 91, "label": "Bill Payer"}]},
            {"id": "c2", "types": [{"typeId": 3, "label": None}]},
        ]}
        self.contacts = {
            "c1": {"email": "Ada.L@Example.org ", "firstname": "Ada", "lastname": "Lovelace",
                   "mobilephone": "07700 900123", "address": "1 Mill Lane", "zip": "E1 1AA",
                   "city": "London", "country": "United Kingdom"},
            "c2": {"email": "ben.b@example.org", "firstname": "Ben", "lastname": "Babbage",
                   "phone": "07700 900456"},
        }
        self.defs = {name: {"type": "string", "fieldType": "text", "options": []}
                     for name in ("famly_childid", "famly_sync_status", "famly_sync_note",
                                  "famly_sync_gaps", "end_date", "dealname")}
        self.writes = []
        self.write_error = None
        self.defs_error = None
        self.fail_props = set()            # writes to these properties fail
        self.events = []                   # shared with FakeFamly: the order things happened
        self.get_deal_hooks = {}           # nth get_deal call -> callable run AFTER it answers
        self.get_deal_calls = 0

    def deal_property_definitions(self):
        if self.defs_error:
            raise self.defs_error
        return dict(self.defs)

    def get_deal(self, deal_id):
        deal = super().get_deal(deal_id)
        self.get_deal_calls += 1
        hook = self.get_deal_hooks.pop(self.get_deal_calls, None)
        if hook:
            hook()
        return deal

    def write_deal_properties(self, deal_id, properties):
        if set(properties) - hubspot.WRITABLE_DEAL_PROPERTIES:
            raise AssertionError("write of a property outside the sync four")
        if self.write_error or set(properties) & self.fail_props:
            raise self.write_error or HubSpotError("PATCH failed with HTTP 500", 500)
        self.writes.append((deal_id, dict(properties)))
        for prop, value in properties.items():
            self.events.append(("hubspot", prop))
            if prop == "end_date":
                self.deals[deal_id]["end_date"] = value


class FakeFamly:
    """The public GraphQL API: lookups, and the two creates only."""

    def __init__(self):
        self.calls = []
        self.children = {}        # child id -> ChildInput payload
        self.contacts = {}        # child id -> [(contact id, email)]
        self.child_creates = []
        self.contact_creates = []
        self.child_effect = None          # called with the payload BEFORE it lands
        self.child_land_then_raise = None
        self.contact_effect = None
        self.hide_created_contacts = False
        self.site_children = []
        self.events = []
        self.blind_after_create = False   # the next by-deal-id lookup finds nothing
        self.after_first = {}     # operation -> callable run after its FIRST call
        self._n = 0

    def _new_id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    def execute(self, query_path, variables, operation_name):
        lines = Path(query_path).read_text(encoding="utf-8").splitlines()
        text = "\n".join(l for l in lines if not l.lstrip().startswith("#")).lower()
        if "delete" in text or "update(" in text:
            raise AssertionError(f"forbidden Famly operation: {operation_name}")
        self.calls.append(operation_name)

        if operation_name == famly_writes.CREATE_CHILD_OP:
            payload = variables["children"][0]
            self.child_creates.append(payload)
            if self.child_effect:
                self.child_effect(payload)
            self.events.append("child_create")
            self._blind = self.blind_after_create
            child_id = self._new_id("child")
            self.children[child_id] = payload
            self.contacts.setdefault(child_id, [])
            if self.child_land_then_raise:
                raise self.child_land_then_raise
            return {"data": {"children": {"create": [{"id": child_id,
                                                      "externalId": payload.get("externalId")}]}}}

        if operation_name == famly_writes.CREATE_CONTACT_OP:
            payload = variables["contacts"][0]
            self.contact_creates.append(payload)
            if self.contact_effect:
                self.contact_effect(payload)
            self.events.append("contact_create")
            contact_id = self._new_id("contact")
            if not self.hide_created_contacts:
                self.contacts[payload["childIds"][0]].append((contact_id, payload["email"]))
            return {"data": {"contacts": {"create": [{"id": contact_id}]}}}

        if operation_name == "OnboardChildByExternalRef":
            wanted = (variables["system"].strip().upper(), variables["foreignId"].strip().casefold())
            ids = [cid for cid, p in self.children.items() if any(
                (r["system"].upper(), r["foreignId"].casefold()) == wanted
                for r in p.get("externalSystemsReferences", []))]
            if getattr(self, "_blind", False):
                self._blind, ids = False, []
            response = {"data": {"children": {"listByExternalSystemReference": {
                "result": [{"id": i} for i in ids]}}}}
            hook = self.after_first.pop(operation_name, None)
            if hook:
                hook()      # the world changes AFTER this answer was given
            return response

        if operation_name == "OnboardChildContacts":
            rows = self.contacts.get(variables["childIds"][0], [])
            return {"data": {"contacts": {"list": {
                "result": [{"id": i, "email": e} for i, e in rows], "next": None}}}}

        if operation_name == urn_runner.OPERATION_NAME:
            result = [{"id": cid, "externalId": p.get("externalId"), "name": {"fullName": p["name"]}}
                      for cid, p in self.children.items()]
            return {"data": {"children": {"listBySiteIds": {"result": result, "next": None}}}}

        if operation_name == "OnboardSiteChildren":
            result = [{"id": i, "birthday": b, "name": {"fullName": n}} for i, b, n in self.site_children]
            return {"data": {"children": {"listBySiteIds": {"result": result, "next": None}}}}

        raise AssertionError(f"unexpected operation {operation_name}")


class FakeRest:
    """The internal summary endpoint, built from what was created."""

    def __init__(self, famly):
        self.famly = famly
        self.wrong_group = False
        self.custom = None          # a summary to return as it is
        self.moves_start_early = False   # a room move that starts on the creation date
        self.error = None
        self.gets = []

    def get(self, path, params=None):
        self.gets.append(path)
        if self.error:
            raise self.error
        if self.custom is not None:
            return self.custom
        child_id = path.split("/")[3]
        payload = self.famly.children.get(child_id)
        if payload is None:
            raise RestHTTPError("not found", 404, "")
        relation = payload["sitesRelation"][0]
        move_start = "2026-10-15" if self.moves_start_early else relation["firstDay"]
        child = {"id": child_id, "isTestChild": payload.get("isTestChild", False),
                 "groupId": "other-group" if self.wrong_group else payload["currentGroupId"],
                 "startDate": relation["firstDay"], "gender": "Dreng"}
        if "lastDay" in relation:
            child["endDate"] = relation["lastDay"]
        return {"child": child, "groupMoves": [
            {"startDate": move_start, "groupId": payload["currentGroupId"]}]}


class FakeNotifier:
    def __init__(self):
        self.gates = []      # (text, job_id, plan_hash)
        self.nonces = []
        self.notices = []
        self.gate_ok = True

    def gate(self, text, job_id, plan_hash, nonce):
        self.gates.append((text, job_id, plan_hash))
        self.nonces.append(nonce)
        return f"C1:{len(self.gates)}" if self.gate_ok else None

    def notice(self, text):
        self.notices.append(text)
        return True


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class Phase2Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "catalogue.json").write_text(json.dumps(
            {"sites": {"HDWEST": {"institutionId": SITE_ID}}}), encoding="utf-8")
        (root / "groups.json").write_text(json.dumps({"institutions": {"HDWEST": {"groups": {
            "HDWEST-h": "Hatchlings", GROUP_ID: "Littlewings", "HDWEST-v": "Voyagers"}}}}),
            encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "ONBOARD_STORE_PATH": str(root / "onboard.sqlite3"),
            "FAMLY_CATALOGUE_FILE": str(root / "catalogue.json"),
            "GROUPS_CATALOGUE_FILE": str(root / "groups.json"),
            "ONBOARD_MODE": "live", "ONBOARD_ALLOWED_DEAL_IDS": DEAL, "ONBOARD_TEST_DEAL_IDS": "",
            "HUBSPOT_PORTAL_ID": "12345",
        })
        env.start()
        self.addCleanup(env.stop)

        self.hub, self.famly = FakeHub(), FakeFamly()
        self.rest, self.notify = FakeRest(self.famly), FakeNotifier()
        self.events = []
        self.hub.events = self.famly.events = self.events
        self.deps = worker.Deps(self.hub, self.famly, self.rest, self.notify,
                                famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]))

        # Other test modules disable logging process-wide; this one must SEE every
        # log line to prove none carries personal data. Restored afterwards.
        previous_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous_disable)

        self.logs = []
        handler = logging.Handler()
        handler.emit = lambda record: self.logs.append(record.getMessage())
        root_logger = logging.getLogger()
        old = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        self.addCleanup(root_logger.removeHandler, handler)
        self.addCleanup(root_logger.setLevel, old)

    # -- helpers ------------------------------------------------------------ #
    def mode(self, value, allowed=DEAL, test=""):
        os.environ["ONBOARD_MODE"] = value
        os.environ["ONBOARD_ALLOWED_DEAL_IDS"] = allowed
        os.environ["ONBOARD_TEST_DEAL_IDS"] = test

    def enqueue(self, deal=DEAL):
        return store.enqueue(deal, config.mode())

    def run_worker(self):
        return worker.run_once(self.deps)

    def written(self):
        """Everything written to the deal, property -> last value."""
        out = {}
        for _deal, props in self.hub.writes:
            out.update(props)
        return out

    def job(self, deal=DEAL):
        return store.latest_for_deal(deal)

    def approve(self, job=None):
        job = store.get((job or self.job()).id)
        return approval.handle_click(
            slack.ACTION_ONBOARD_APPROVE, approval.button_value(job.id, job.plan_hash, job.approval_nonce), "U1")

    def run_to_approval(self):
        self.enqueue()
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        return self.job()

    def run_through(self):
        """enqueue, plan, approve, write: returns the final run result."""
        self.run_to_approval()
        self.approve()
        return self.run_worker()

    @property
    def sent_anything(self):
        return bool(self.famly.child_creates or self.famly.contact_creates or self.hub.writes)

    def all_output(self):
        return "\n".join([t for t, _, _ in self.notify.gates] + self.notify.notices
                         + self.logs + [store.all_rows_as_text()]).casefold()


# --------------------------------------------------------------------------- #
# Mode and allowlist gates, and the queue
# --------------------------------------------------------------------------- #
class ModeAndAllowlistTests(Phase2Case):
    def test_off_refuses_to_enqueue_and_the_worker_does_nothing(self):
        self.mode("off")
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            cli_enqueue.handle(mock.Mock(deal_id=DEAL))
        self.assertEqual(ctx.exception.code, 3)
        self.assertIsNone(store.latest_for_deal(DEAL))
        # A job that is already queued is left alone while the mode is off.
        self.mode("live")
        self.enqueue()
        self.mode("off")
        self.assertEqual(self.run_worker(), {"ran": False, "mode": "off"})
        self.assertEqual(self.job().status, store.QUEUED)
        self.assertFalse(self.sent_anything)
        self.assertEqual(self.hub.calls, [])
        self.assertEqual(self.famly.calls, [])

    def test_unset_or_unknown_mode_is_off(self):
        os.environ.pop("ONBOARD_MODE")
        self.assertEqual(config.mode(), "off")
        os.environ["ONBOARD_MODE"] = "yes-please"
        self.assertEqual(config.mode(), "off")

    def test_dry_runs_the_plan_and_the_slack_message_but_writes_nothing(self):
        self.mode("dry")
        self.enqueue()
        result = self.run_worker()
        self.assertEqual(result["status"], store.DRY_DONE)
        self.assertFalse(self.sent_anything)
        self.assertEqual(self.notify.gates, [])          # no approval buttons in dry
        self.assertTrue(any("[DRY RUN]" in n for n in self.notify.notices))
        self.assertIn("OnboardChildByExternalRef", self.famly.calls)   # the live plan ran
        self.assertNotIn(famly_writes.CREATE_CHILD_OP, self.famly.calls)
        self.assertEqual(self.hub.writes, [])
        # A dry run does not use the deal's one active slot, so a live run can follow.
        self.mode("live")
        self.assertEqual(self.enqueue().outcome, store.CREATED)

    def test_a_deal_outside_the_allowlist_is_held_before_any_read_or_write(self):
        self.mode("live", allowed="999")
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.HELD, "deal_not_allowlisted"))
        self.assertFalse(self.sent_anything)
        self.assertEqual(self.hub.calls, [])      # not even a HubSpot read
        self.assertEqual(self.famly.calls, [])
        self.assertEqual(self.notify.gates, [])
        self.assertTrue(self.notify.notices)

    def test_an_empty_allowlist_allows_nothing(self):
        self.mode("live", allowed="")
        self.enqueue()
        self.assertEqual(self.run_worker()["code"], "deal_not_allowlisted")

    def test_a_test_deal_creates_a_test_child_and_nothing_else_ever_does(self):
        self.mode("live", test=DEAL)
        self.run_through()
        self.assertIs(self.famly.child_creates[0]["isTestChild"], True)
        self.assertEqual(self.job().status, store.DONE)

        other = FakeFamly()
        other.events = self.events
        self.famly, self.rest = other, FakeRest(other)
        self.deps = worker.Deps(self.hub, other, self.rest, self.notify,
                                famly_factory=lambda: FamlyReads(other, site_ids=[SITE_ID]))
        self.mode("live", test="")            # not a test deal
        store._reset_for_tests()
        self.run_through()
        self.assertNotIn("isTestChild", self.famly.child_creates[0])   # never false, never sent
        # A test id that is NOT in the allowlist is not a test deal either.
        self.mode("live", allowed="999", test=DEAL)
        self.assertFalse(config.is_test_deal(DEAL))


class QueueTests(Phase2Case):
    def test_one_active_job_per_deal_is_enforced_by_a_unique_partial_index(self):
        first = self.enqueue()
        again = self.enqueue()
        self.assertEqual((first.outcome, again.outcome), (store.CREATED, store.ALREADY_QUEUED))
        self.assertEqual(first.job.id, again.job.id)
        # The database itself refuses a second active row, whatever the code does.
        with contextlib.closing(sqlite3.connect(store.store_path())) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO onboard_jobs (id, deal_id, status, mode, created_at, updated_at) "
                    "VALUES ('x', ?, 'awaiting_approval', 'live', 'now', 'now')", (DEAL,))
            indexes = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'onboard_one_active_per_deal'"
            ).fetchone()[0]
        self.assertIn("UNIQUE", indexes.upper())
        self.assertIn("WHERE", indexes.upper())

    def test_a_finished_job_frees_the_slot_but_a_done_deal_creates_no_job(self):
        self.run_to_approval()
        self.assertEqual(self.enqueue().outcome, store.ALREADY_QUEUED)   # awaiting is active
        self.approve()
        self.run_worker()                                                # -> done
        self.assertEqual(self.job().status, store.DONE)
        result = self.enqueue()
        self.assertEqual(result.outcome, store.ALREADY_DONE)
        self.assertEqual(sum(store.status_counts_for_deal(DEAL).values()), 1)

    def test_status_cli_output_is_counts_and_codes_only(self):
        from actions.onboard import cli_status
        self.run_through()
        out = json.dumps(cli_status.handle(mock.Mock(deal_id=DEAL)))
        for value in PII:
            self.assertNotIn(value, out.casefold())
        self.assertIn('"done": 1', out)


# --------------------------------------------------------------------------- #
# Gate A
# --------------------------------------------------------------------------- #
class GateATests(Phase2Case):
    def test_the_first_run_asks_for_approval_and_writes_nothing(self):
        job = self.run_to_approval()
        self.assertFalse(self.sent_anything)
        text, job_id, digest = self.notify.gates[0]
        self.assertEqual((job_id, digest), (job.id, job.plan_hash))
        for part in (DEAL, "https://app.hubspot.com/contacts/12345/record/0-3/" + DEAL,
                     "HDWEST", "Littlewings", "Start date: present", "Contacts: 2",
                     "Bill payer identified: yes", "Warnings:"):
            self.assertIn(part, text)
        self.assertEqual(job.approval_ref, "C1:1")

    def test_approve_then_the_worker_writes(self):
        self.run_to_approval()
        self.assertIn("Approved", self.approve())
        self.assertEqual(self.job().status, store.QUEUED)
        self.assertEqual(self.job().approved_hash, self.job().plan_hash)
        self.assertFalse(self.sent_anything)         # a click alone writes nothing
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(len(self.famly.child_creates), 1)

    def test_a_changed_plan_forces_re_approval_and_writes_nothing(self):
        first = self.run_to_approval()
        self.approve()
        # Before the worker runs, the deal changes: a different start date, so a
        # different plan (the group moves up).
        self.hub.deals[DEAL]["actual_start_date_1"] = "2026-02-15"
        self.hub.deals[DEAL]["child_date_of_birth"] = "2023-02-15"
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        self.assertFalse(self.sent_anything)
        self.assertEqual(len(self.notify.gates), 2)
        self.assertIn("approval is needed again", self.notify.gates[1][0])
        second = self.job()
        self.assertNotEqual(second.plan_hash, first.plan_hash)
        self.assertIsNone(second.approved_hash)
        # The OLD message's button no longer works.
        stale = approval.handle_click(slack.ACTION_ONBOARD_APPROVE,
                                      approval.button_value(first.id, first.plan_hash, first.approval_nonce), "U1")
        self.assertEqual(stale, approval.MSG_STALE)
        self.assertEqual(self.job().status, store.AWAITING_APPROVAL)

    def test_an_old_message_for_the_same_plan_is_stale_once_the_job_asks_again(self):
        first = self.run_to_approval()
        old = approval.button_value(first.id, first.plan_hash, first.approval_nonce)
        # The job goes back to waiting with the SAME plan hash and a new request.
        store.set_status(first.id, store.QUEUED, approved_hash=None)
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        again = self.job()
        self.assertEqual(again.plan_hash, first.plan_hash)
        self.assertNotEqual(again.approval_nonce, first.approval_nonce)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_APPROVE, old, "U1"),
                         approval.MSG_STALE)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_REJECT, old, "U1"),
                         approval.MSG_STALE)
        self.assertEqual(self.job().status, store.AWAITING_APPROVAL)       # untouched
        # The current message works, and a redelivery of the same click does nothing.
        new = approval.button_value(again.id, again.plan_hash, again.approval_nonce)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_APPROVE, new, "U1"),
                         approval.MSG_APPROVED)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_APPROVE, new, "U1"),
                         approval.MSG_HANDLED)
        self.assertEqual(self.run_worker()["status"], store.DONE)           # proceeds, no re-ask
        self.assertEqual(len(self.famly.child_creates), 1)

    def _lagging_ask(self, job):
        plan = {"dealId": DEAL, "contacts": [], "child": {}, "dates": {}, "warnings": []}
        worker._ask_for_approval(job, self.deps, plan, "f" * 64, "live", plan_changed=False)

    def test_a_lagging_run_cannot_clear_an_approval_or_drag_a_job_back_to_waiting(self):
        self.enqueue()
        lagging = store.claim_next()                       # a run that is slow to finish
        # Meanwhile the job was asked and approved by another run and a click.
        store.set_status(lagging.id, store.AWAITING_APPROVAL, plan_hash="a" * 64,
                         approval_nonce="n1")
        self.assertTrue(store.transition(lagging.id, store.AWAITING_APPROVAL, store.QUEUED,
                                         approved_hash="a" * 64, approval_nonce=None))
        self._lagging_ask(lagging)
        after = self.job()
        self.assertEqual((after.status, after.approved_hash), (store.QUEUED, "a" * 64))
        self.assertEqual(self.notify.gates, [])            # nothing was posted

    def test_a_run_whose_claim_was_taken_again_cannot_ask(self):
        self.enqueue()
        old = store.claim_next()
        store.set_status(old.id, store.QUEUED)             # released, then claimed anew
        current = store.claim_next()
        self.assertNotEqual(old.claimed_at, current.claimed_at)
        self._lagging_ask(old)
        self.assertEqual(self.job().status, store.QUEUED)
        self.assertEqual(self.notify.gates, [])
        self._lagging_ask(current)                         # the holder of the claim may
        self.assertEqual(self.job().status, store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), 1)

    def test_a_finished_job_is_never_dragged_back_to_waiting(self):
        self.run_through()
        done = self.job()
        self.assertEqual(done.status, store.DONE)
        self._lagging_ask(done)
        self.assertEqual(self.job().status, store.DONE)
        self.assertEqual(len(self.notify.gates), 1)

    def _reorder_hubspot(self):
        """From now on HubSpot answers in the opposite order: contacts and types."""
        original = self.hub.associations

        def reversed_answer(from_type, from_id, to_type):
            return [{**a, "types": list(reversed(a["types"]))}
                    for a in reversed(original(from_type, from_id, to_type))]
        self.hub.associations = reversed_answer

    def _two_labels_each(self):
        key = ("deals", DEAL, "contacts")
        self.hub.assoc[key] = [
            {"id": "c1", "types": [{"typeId": 91, "label": "Bill Payer"},
                                   {"typeId": 3, "label": None}]},
            {"id": "c2", "types": [{"typeId": 3, "label": None}, {"typeId": 1, "label": "Other"}]},
        ]

    def test_a_reordered_hubspot_answer_does_not_re_ask_after_approval(self):
        self._two_labels_each()
        self.run_to_approval()
        self.approve()
        self._reorder_hubspot()                    # the same plan, answered in another order
        result = self.run_worker()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(len(self.notify.gates), 1)           # never asked again
        self.assertEqual(len(self.famly.child_creates), 1)

    def test_a_plan_that_changes_again_after_one_re_ask_ends_plan_unstable(self):
        self.run_to_approval()
        self.approve()
        self.hub.deals[DEAL]["actual_start_date_1"] = "2026-02-15"       # change one
        self.hub.deals[DEAL]["child_date_of_birth"] = "2023-02-15"
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), 2)           # re-asked once
        self.assertEqual(self.job().reask_count, 1)
        self.approve()
        self.hub.contacts["c1"]["address"] = ""                           # change two
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.NEEDS_REVIEW, "plan_unstable"))
        self.assertEqual(len(self.notify.gates), 2)           # NOT asked a third time
        self.assertFalse(self.sent_anything)
        notice = [n for n in self.notify.notices if "plan_unstable" in n]
        self.assertEqual(len(notice), 1)                      # exactly one notice
        self.assertIn("contacts:", notice[0])                 # the section that differed
        self.assertNotIn("child:", notice[0])                 # and only that one
        self.assertEqual(self.job().status, store.NEEDS_REVIEW)

    def test_a_genuine_change_re_asks_exactly_once_and_then_proceeds(self):
        self.run_to_approval()
        self.approve()
        self.hub.deals[DEAL]["actual_start_date_1"] = "2026-02-15"
        self.hub.deals[DEAL]["child_date_of_birth"] = "2023-02-15"
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertEqual(self.job().reask_count, 1)
        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)      # unchanged since: proceeds
        self.assertEqual(len(self.notify.gates), 2)

    def test_a_click_without_the_nonce_never_approves(self):
        job = self.run_to_approval()
        for value in (f"{job.id}:{job.plan_hash[:16]}", f"{job.id}:{job.plan_hash[:16]}:",
                      f"{job.id}:{job.plan_hash[:16]}:wrong"):
            self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_APPROVE, value, "U1"),
                             approval.MSG_STALE)
        self.assertEqual(self.job().status, store.AWAITING_APPROVAL)

    def test_a_changed_plan_asks_once_not_in_a_loop_and_logs_the_changed_section(self):
        self.run_to_approval()
        self.approve()
        self.hub.deals[DEAL]["actual_start_date_1"] = "2026-02-15"
        self.hub.deals[DEAL]["child_date_of_birth"] = "2023-02-15"
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)     # asks once
        self.assertEqual(len(self.notify.gates), 2)
        self.assertTrue(any("sections=" in line and "digest=" in line for line in self.logs))
        self.approve()                                       # the NEW message is approved
        self.assertEqual(self.run_worker()["status"], store.DONE)    # and nothing asks again
        self.assertEqual(len(self.notify.gates), 2)

    def test_clicks_are_idempotent_and_reject_writes_nothing(self):
        job = self.run_to_approval()
        value = approval.button_value(job.id, job.plan_hash, job.approval_nonce)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_APPROVE, value, "U1"),
                         approval.MSG_APPROVED)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_APPROVE, value, "U1"),
                         approval.MSG_HANDLED)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_REJECT, value, "U1"),
                         approval.MSG_HANDLED)       # too late: it already moved on
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_APPROVE, "nope:x", "U1"),
                         approval.MSG_UNKNOWN)

        store._reset_for_tests()
        job = self.run_to_approval()
        value = approval.button_value(job.id, job.plan_hash, job.approval_nonce)
        self.assertEqual(approval.handle_click(slack.ACTION_ONBOARD_REJECT, value, "U1"),
                         approval.MSG_REJECTED)
        self.assertEqual((self.job().status, self.job().last_error_code),
                         (store.HELD, "approval_rejected"))
        self.assertFalse(self.sent_anything)

    def test_an_approval_that_could_not_be_posted_does_not_wait_forever(self):
        self.notify.gate_ok = False
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]),
                         (store.HELD, "approval_request_not_posted"))

    def test_a_plan_with_a_hold_ends_held_with_the_codes_and_a_notice(self):
        self.hub.deals[DEAL]["location_of_interest"] = "Shoreditch"
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.HELD, "site_unmapped"))
        self.assertEqual(self.notify.gates, [])
        self.assertFalse(self.famly.child_creates or self.famly.contact_creates)
        self.assertTrue(any("site_unmapped" in n for n in self.notify.notices))
        # Live + allowlisted: the held state is also written to HubSpot, as codes.
        self.assertEqual(self.written()["famly_sync_status"], "needs_attention")
        self.assertEqual(self.written()["famly_sync_note"], "held:site_unmapped")


# --------------------------------------------------------------------------- #
# Child create
# --------------------------------------------------------------------------- #
class ChildCreateTests(Phase2Case):
    def test_the_child_input_is_exactly_the_probed_shape(self):
        self.hub.deals[DEAL]["end_date"] = "2026-12-31"
        self.run_through()
        payload = self.famly.child_creates[0]
        self.assertEqual(payload, {
            "name": "Zara Quillfeather",
            "birthday": "2024-01-15",
            "currentGroupId": GROUP_ID,
            "records": [],
            "sensitiveRecords": [],
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15", "lastDay": "2026-12-31"}],
            "externalId": "URN-1",
            "externalSystemsReferences": [
                {"foreignId": DEAL, "system": "HUBSPOT"},
                {"foreignId": "URN-1", "system": "URN"},
            ],
        })
        self.assertNotIn("gender", payload)

    def test_no_urn_means_no_urn_reference_and_a_missing_end_date_is_defaulted(self):
        self.hub.deals[DEAL]["child_urn"] = ""
        self.run_through()
        payload = self.famly.child_creates[0]
        # Born 2024-01-15: 4th birthday 2028-01-15, so the first 31 August after it.
        self.assertEqual(payload["sitesRelation"][0]["lastDay"], "2028-08-31")
        self.assertNotIn("externalId", payload)
        self.assertEqual(payload["externalSystemsReferences"],
                         [{"foreignId": DEAL, "system": "HUBSPOT"}])

    def appear(self, *child_ids):
        """Make children with this deal's HUBSPOT reference appear AFTER the plan
        has looked (and found none), so only the pre-write lookup sees them."""
        def add():
            for child_id in child_ids:
                self.famly.children[child_id] = {
                    "name": "x", "externalId": f"OTHER-{child_id}", "currentGroupId": GROUP_ID,
                    "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
                    "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
                self.famly.contacts[child_id] = []
        self.famly.after_first["OnboardChildByExternalRef"] = add

    def test_a_child_that_now_exists_is_used_not_created_and_gets_no_unmatched_contact(self):
        self.run_to_approval()
        self.approve()
        self.appear("child-existing")      # somebody else's child, with no contacts on it
        result = self.run_worker()
        self.assertEqual(self.famly.child_creates, [])               # no create
        self.assertEqual(self.job().child_id, "child-existing")
        self.assertEqual(self.famly.contact_creates, [])             # and nothing added to it
        self.assertEqual(self.job().outcomes, ["c1:contact_unmatched_on_existing_child",
                                               "c2:contact_unmatched_on_existing_child"])
        self.assertEqual(result["status"], store.NEEDS_REVIEW)

    def test_two_children_found_before_the_write_is_needs_review(self):
        self.run_to_approval()
        self.approve()
        self.appear("c1", "c2")
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.NEEDS_REVIEW, "child_ambiguous"))
        self.assertEqual(self.famly.child_creates, [])
        self.assertEqual(self.famly.contact_creates, [])

    def test_two_children_found_by_the_plan_is_a_hold_with_no_approval_asked(self):
        for child_id in ("c1", "c2"):     # already there when the plan looks
            self.famly.children[child_id] = {
                "name": "x", "currentGroupId": GROUP_ID,
                "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
                "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
            self.famly.contacts[child_id] = []
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.HELD, "child_ambiguous"))
        self.assertEqual(self.notify.gates, [])

    def test_a_400_is_never_retried_whatever_its_display(self):
        body = json.dumps({"errors": [{"extensions": {"display": "Transient"}}]})
        self.famly.child_effect = mock.Mock(side_effect=GraphQLHTTPError("bad", 400, body))
        result = self.run_through()
        self.assertEqual(len(self.famly.child_creates), 1)        # exactly one request
        self.assertEqual((result["status"], result["code"]), (store.FAILED, "child_http_400"))
        self.assertEqual(self.famly.contact_creates, [])
        self.assertEqual(self.hub.writes, [])
        # A GraphQL `errors` array is also one request, not retried.
        store._reset_for_tests()
        self.famly.child_creates.clear()
        self.famly.child_effect = mock.Mock(side_effect=GraphQLError("bad", [{"message": "x"}]))
        self.run_through()
        self.assertEqual(len(self.famly.child_creates), 1)
        self.assertEqual(self.job().last_error_code, "child_graphql_error")

    def test_a_timeout_then_a_lookup_that_finds_the_child_carries_on(self):
        self.famly.child_land_then_raise = requests.exceptions.Timeout()
        result = self.run_through()
        self.assertEqual(len(self.famly.child_creates), 1)        # never sent twice
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.job().child_id, "child-1")

    def test_an_unknown_outcome_that_did_not_land_needs_review_without_a_second_create(self):
        for error in (requests.exceptions.ConnectionError(),
                      GraphQLHTTPError("boom", 503, "")):
            with self.subTest(error=type(error).__name__):
                store._reset_for_tests()
                self.famly.children.clear()
                self.famly.child_creates.clear()
                self.notify.notices.clear()
                self.famly.child_effect = mock.Mock(side_effect=error)   # fails BEFORE landing
                result = self.run_through()
                self.assertEqual(len(self.famly.child_creates), 1)
                self.assertEqual((result["status"], result["code"]),
                                 (store.NEEDS_REVIEW, "create_outcome_unknown"))
                self.assertEqual(self.famly.contact_creates, [])
                self.assertTrue(any("Check Famly for the deal ID before running again" in n
                                    for n in self.notify.notices))

    def test_a_job_never_sends_a_second_child_create(self):
        job = self.run_to_approval()
        self.assertTrue(store.mark_child_create_sent(job.id))
        self.assertFalse(store.mark_child_create_sent(job.id))     # the persisted flag
        self.approve()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]),
                         (store.NEEDS_REVIEW, "duplicate_create_blocked"))
        self.assertEqual(self.famly.child_creates, [])


# --------------------------------------------------------------------------- #
# Read-back
# --------------------------------------------------------------------------- #
class ReadBackTests(Phase2Case):
    def test_the_room_move_is_not_checked_at_all(self):
        self.rest.moves_start_early = True      # a room move dated the creation day
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)              # nothing stops
        self.assertEqual(len(self.famly.contact_creates), 2)
        self.assertNotIn("room_move_mismatch", self.all_output())
        self.assertFalse(hasattr(summary_check, "ROOM_MOVE_MISMATCH"))
        self.assertFalse(hasattr(summary_check, "_moves"))          # the check is gone, not hidden

    def test_a_summary_mismatch_is_a_flag_not_a_hold_and_never_stops_the_job(self):
        self.rest.wrong_group = True
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)              # not held, not stopped
        self.assertEqual(len(self.famly.contact_creates), 2)
        self.assertIn("summary_group_mismatch", self.written()["famly_sync_gaps"])
        self.assertTrue(any("summary_group_mismatch" in n for n in self.notify.notices))

    def test_an_unrecognised_or_unreadable_summary_is_flagged_too(self):
        self.rest.custom = {"weird": {"shape": 1}}
        self.assertEqual(self.run_through()["status"], store.DONE)
        self.assertIn("summary_unrecognised", self.written()["famly_sync_gaps"])
        store._reset_for_tests()
        self.famly.children.clear()
        self.famly.child_creates.clear()
        self.hub.writes.clear()
        self.rest.custom = None
        self.rest.error = RestHTTPError("boom", 500, "")
        self.assertEqual(self.run_through()["status"], store.DONE)
        self.assertIn("summary_unreadable", self.written()["famly_sync_gaps"])

    def test_summary_flags_unit_cases(self):
        base = {"child": {"groupId": "g", "startDate": "2026-01-15", "endDate": "2028-08-31",
                          "isTestChild": False}}
        ok = dict(expected_group_id="g", expected_start="2026-01-15", expected_end="2028-08-31",
                  expected_test=False)
        self.assertEqual(summary_check.flags(base, **ok), [])
        self.assertEqual(summary_check.flags(base, **{**ok, "expected_test": True}),
                         ["test_flag_mismatch"])
        self.assertEqual(summary_check.flags(base, **{**ok, "expected_end": "2029-08-31"}),
                         ["summary_end_mismatch"])
        self.assertEqual(summary_check.flags(base, **{**ok, "expected_start": "2026-01-16"}),
                         ["summary_start_mismatch"])
        self.assertEqual(summary_check.flags(base, **{**ok, "expected_group_id": "x"}),
                         ["summary_group_mismatch"])
        # Several differences give several flags; an unknown shape is one flag.
        self.assertEqual(summary_check.flags(base, expected_group_id="x", expected_start="2026-01-16",
                                             expected_end=None, expected_test=True),
                         ["summary_group_mismatch", "summary_start_mismatch",
                          "summary_end_mismatch", "test_flag_mismatch"])
        self.assertEqual(summary_check.flags({"weird": {"a": 1}}, **ok),
                         ["summary_unrecognised"])
        self.assertEqual(summary_check.flags("nope", **ok), ["summary_unrecognised"])
        # A room move, early or not, is simply not looked at.
        early = {**base, "groupMoves": [{"startDate": "2026-01-01", "groupId": "g"}]}
        self.assertEqual(summary_check.flags(early, **ok), [])

    def test_a_contact_that_does_not_read_back_does_not_stop_the_others(self):
        calls = {"n": 0}
        real = self.famly.contact_effect

        def hide_the_first(payload):
            calls["n"] += 1
            self.famly.hide_created_contacts = calls["n"] == 1     # only the first one vanishes
        self.famly.contact_effect = hide_the_first
        result = self.run_through()
        self.assertEqual(len(self.famly.contact_creates), 2)          # the second IS attempted
        self.assertEqual(self.job().outcomes, ["c1:readback_failed", "c2:created"])
        self.assertEqual(result["status"], store.NEEDS_REVIEW)
        self.assertEqual(self.job().child_id, "child-1")               # the child is not rolled back

# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
class ContactTests(Phase2Case):
    def test_contacts_are_created_one_per_call_with_empty_roles(self):
        self.run_through()
        creates = self.famly.contact_creates
        self.assertEqual(len(creates), 2)
        for payload in creates:
            self.assertEqual(payload["roles"], [])
            self.assertEqual(payload["childIds"], ["child-1"])
            self.assertEqual(payload["records"], [{"key": "RELATION", "value": "Parent"}])
            self.assertEqual(self.famly.calls.count(famly_writes.CREATE_CONTACT_OP), 2)
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:created"])

    def test_only_the_bill_payer_is_the_emergency_contact(self):
        self.run_through()
        by_email = {p["email"]: p for p in self.famly.contact_creates}
        self.assertIs(by_email["ada.l@example.org"]["emergencyContact"], True)
        self.assertIs(by_email["ben.b@example.org"]["emergencyContact"], False)

    def test_email_is_trimmed_and_lower_cased_and_phone_is_e164(self):
        self.run_through()
        first, second = self.famly.contact_creates
        self.assertEqual(first["email"], "ada.l@example.org")
        self.assertEqual(first["phoneNumbers"], [{"value": "+447700900123", "phoneType": "MOBILE"}])
        self.assertEqual(second["phoneNumbers"], [{"value": "+447700900456", "phoneType": "WORK"}])

    def test_the_address_is_sent_only_when_street_zip_and_city_are_all_present(self):
        self.run_through()
        first, second = self.famly.contact_creates
        self.assertEqual(first["address"], {"street": "1 Mill Lane", "zip": "E1 1AA",
                                            "city": "London", "country": "United Kingdom"})
        self.assertNotIn("address", second)            # c2 has none: the whole address is omitted
        gaps = [w for d, w in self.hub.writes][-1]["famly_sync_gaps"]
        self.assertIn("address_incomplete", gaps)

    def test_address_and_country_rules_unit(self):
        def contact(**props):
            base = {"firstname": "A", "lastname": "B", "email": "a@b.co"}
            return DealContact("c9", "a@b.co", False, props={**base, **props})

        full = dict(address="1 St", zip="Z1", city="C")
        payload, warnings = inputs.build_contact_input(contact(**full), "ch")
        self.assertNotIn("country", payload["address"])    # never defaulted
        self.assertNotIn("state", payload["address"])
        self.assertNotIn("address_incomplete", warnings)
        payload, _ = inputs.build_contact_input(contact(**full, state="X", country="GB"), "ch")
        self.assertEqual(payload["address"]["country"], "GB")   # as given
        self.assertEqual(payload["address"]["state"], "X")
        for missing in ("address", "zip", "city"):
            props = dict(full, state="X", country="GB")
            props[missing] = " "
            payload, warnings = inputs.build_contact_input(contact(**props), "ch")
            self.assertNotIn("address", payload)
            self.assertIn("address_incomplete", warnings)

    def test_phone_normalisation_table(self):
        cases = {
            "07700 900123": "+447700900123", "07700-900-123": "+447700900123",
            "+44 7700 900123": "+447700900123", "+44 (0)7700 900123": "+447700900123",
            "00447700900123": "+447700900123", "447700900123": "+447700900123",
            "020 7946 0000": "+442079460000", "+447700900123": "+447700900123",
            "+1 415 555 2671": "+14155552671",
            "077009001": None, "0770090012345": None, "abc": None, "": None,
            "07700 900123 / 07700 900124": None, "7700900123": None, "+44 7700": None,
            "0770 090 0123 ext 5": None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(phone.normalise_uk(raw), expected)

    def test_phone_order_and_unnormalised_or_missing_phones_warn(self):
        wa = hubspot.WHATSAPP_PROPERTY
        self.assertEqual(phone.pick_phone({"mobilephone": "07700 900123", "phone": "02079460000"}),
                         ("mobile", "MOBILE", "+447700900123"))
        self.assertEqual(phone.pick_phone({"phone": "02079460000", wa: "07700 900123"}),
                         ("work", "WORK", "+442079460000"))
        self.assertEqual(phone.pick_phone({wa: "07700 900123"}),
                         ("whatsapp", "MOBILE", "+447700900123"))
        self.assertEqual(phone.pick_phone({}), ("none", None, None))
        # An unusable first choice is omitted, not swapped for a later property.
        self.assertEqual(phone.pick_phone({"mobilephone": "garbage", "phone": "02079460000"}),
                         ("mobile", "MOBILE", None))

        def contact(**props):
            return DealContact("c9", "a@b.co", False,
                               props={"firstname": "A", "lastname": "B", "email": "a@b.co", **props})
        payload, warnings = inputs.build_contact_input(contact(mobilephone="garbage"), "ch")
        self.assertEqual((payload["phoneNumbers"], "phone_unnormalised" in warnings), ([], True))
        payload, warnings = inputs.build_contact_input(contact(), "ch")
        self.assertEqual((payload["phoneNumbers"], "phone_missing" in warnings), ([], True))

    def test_a_contact_without_both_names_is_held_alone_and_the_job_needs_review(self):
        self.hub.contacts["c2"]["lastname"] = ""
        result = self.run_through()
        self.assertEqual(len(self.famly.contact_creates), 1)        # c1 only
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:contact_name_incomplete"])
        self.assertEqual(result["status"], store.NEEDS_REVIEW)
        self.assertEqual(self.job().child_id, "child-1")             # the child stays
        written = self.written()
        self.assertEqual(written["famly_sync_status"], "needs_attention")
        self.assertIn("contact_name_incomplete", written["famly_sync_note"])

    def test_an_unknown_contact_outcome_that_did_not_land_needs_review(self):
        def lose_ada(payload):
            if payload["email"] == "ada.l@example.org":
                raise requests.exceptions.Timeout()          # fails BEFORE landing
        self.famly.contact_effect = lose_ada
        result = self.run_through()
        self.assertEqual([p["email"] for p in self.famly.contact_creates],
                         ["ada.l@example.org", "ben.b@example.org"])    # none sent twice
        self.assertEqual(self.job().outcomes, ["c1:create_outcome_unknown", "c2:created"])
        self.assertEqual(result["status"], store.NEEDS_REVIEW)
        self.assertTrue(any("Check Famly for the deal ID before running again" in n
                            for n in self.notify.notices))

    def test_a_rejected_contact_does_not_stop_the_others_and_is_not_retried(self):
        def reject_ada(payload):
            if payload["email"] == "ada.l@example.org":      # the BILL PAYER, tried first
                raise GraphQLHTTPError("bad", 400, "{}")
        self.famly.contact_effect = reject_ada
        result = self.run_through()
        self.assertEqual([p["email"] for p in self.famly.contact_creates],
                         ["ada.l@example.org", "ben.b@example.org"])    # both tried, once each
        self.assertEqual(self.job().outcomes, ["c1:contact_http_400", "c2:created"])
        self.assertEqual(result["status"], store.NEEDS_REVIEW)
        self.assertEqual(self.job().last_error_code, "contact_outcomes")

    def test_an_existing_contact_on_the_child_is_skipped_as_present(self):
        self.famly.children["child-9"] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": GROUP_ID,
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts["child-9"] = [("fc-1", "ADA.L@googlemail.com"),
                                          ("fc-2", " Ben.B@Example.org ")]
        self.hub.contacts["c1"]["email"] = "ada.l@gmail.com"         # gmail == googlemail
        self.enqueue()
        result = self.run_worker()      # nothing to write: no approval is needed
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:present"])
        self.assertEqual(self.famly.contact_creates, [])
        self.assertEqual(self.notify.gates, [])


# --------------------------------------------------------------------------- #
# Re-runs
# --------------------------------------------------------------------------- #
class RerunTests(Phase2Case):
    def test_a_rerun_after_a_held_contact_completes_it_on_the_pipelines_own_child(self):
        self.hub.contacts["c2"]["lastname"] = ""
        self.run_through()
        self.assertEqual(self.job().status, store.NEEDS_REVIEW)
        first_job = self.job()

        self.hub.contacts["c2"]["lastname"] = "Babbage"       # a person fixed the data
        again = self.enqueue()
        self.assertEqual(again.outcome, store.CREATED)         # a fresh job
        self.assertNotEqual(again.job.id, first_job.id)
        result = self.run_worker()
        # The child is the PIPELINE's (job history), so c2 stays would_create.
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        self.assertIn("created by pipeline", self.notify.gates[-1][0])
        self.assertIn("1 to create, 1 present", self.notify.gates[-1][0])
        self.approve()
        self.run_worker()
        self.assertEqual(self.job().status, store.DONE)
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:created"])
        self.assertEqual(len(self.famly.child_creates), 1)      # never a second child
        self.assertEqual(len(self.famly.contact_creates), 2)    # c1 once, c2 once

    def test_a_rerun_after_our_own_contact_creation_creates_nothing(self):
        # The first job creates the child and both contacts, but the child id cannot be
        # written to the deal, so it ends needs_review although Famly is complete.
        self.hub.fail_props = {"famly_childid"}
        self.run_through()
        self.assertEqual((self.job().status, self.job().last_error_code),
                         (store.NEEDS_REVIEW, "hubspot_writeback_failed"))
        self.assertEqual((len(self.famly.child_creates), len(self.famly.contact_creates)), (1, 2))

        self.hub.fail_props = set()
        self.assertEqual(self.enqueue().outcome, store.CREATED)
        gates_before = len(self.notify.gates)
        result = self.run_worker()
        self.assertEqual(result["status"], store.DONE)         # nothing to write: no approval
        self.assertEqual(len(self.notify.gates), gates_before)
        self.assertEqual((len(self.famly.child_creates), len(self.famly.contact_creates)), (1, 2))
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:present"])
        self.assertEqual(self.written()["famly_childid"], "child-1")     # the write-back finished
        self.assertEqual(self.written()["famly_sync_status"], "contacts_linked")

    def test_a_rerun_after_a_failed_create_does_not_double_create(self):
        self.famly.child_land_then_raise = None
        self.famly.child_effect = mock.Mock(side_effect=GraphQLHTTPError("bad", 400, "{}"))
        self.run_through()
        self.assertEqual(self.job().status, store.FAILED)
        self.famly.child_effect = None
        self.enqueue()
        self.run_worker()
        self.approve()
        self.run_worker()
        self.assertEqual(self.job().status, store.DONE)
        self.assertEqual(len(self.famly.children), 1)

    def test_a_worker_that_died_mid_write_is_never_replayed(self):
        job = self.run_to_approval()
        store.set_status(job.id, store.CREATING_CHILD)
        self.assertEqual(worker.recover(), 1)
        self.assertEqual((self.job().status, self.job().last_error_code),
                         (store.NEEDS_REVIEW, "interrupted"))
        self.assertFalse(self.sent_anything)


# --------------------------------------------------------------------------- #
# HubSpot write-back
# --------------------------------------------------------------------------- #
class WriteBackTests(Phase2Case):
    def test_missing_hubspot_properties_warn_but_never_block_famly(self):
        del self.hub.defs["famly_sync_gaps"]
        del self.hub.defs["famly_sync_note"]
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)              # not blocked, not review
        self.assertEqual((len(self.famly.child_creates), len(self.famly.contact_creates)), (1, 2))
        self.assertEqual(result["hubspotSyncPropertiesFound"],
                         ["famly_childid", "famly_sync_status"])
        warned = " ".join(self.notify.notices)
        self.assertIn("missing:famly_sync_gaps", warned)             # in Slack ...
        self.assertIn("missing:famly_sync_note", warned)
        self.assertTrue(any("missing:famly_sync_gaps" in line for line in self.logs))   # ... and the log
        written = self.written()
        self.assertNotIn("famly_sync_gaps", written)                 # their writes were skipped
        self.assertNotIn("famly_sync_note", written)
        self.assertEqual((written["famly_childid"], written["famly_sync_status"]),
                         ("child-1", "contacts_linked"))
        self.assertIn("skipped:famly_sync_gaps", warned)             # and the skip is reported

    def test_a_missing_child_id_or_end_date_property_is_needs_review_after_famly_is_done(self):
        for missing in ("famly_childid", "end_date"):
            with self.subTest(missing=missing):
                store._reset_for_tests()
                self.famly.children.clear()
                self.famly.child_creates.clear()
                self.famly.contact_creates.clear()
                self.hub.writes.clear()
                self.hub.deals[DEAL].pop("end_date", None)
                self.hub.defs = {k: v for k, v in FakeHub().defs.items() if k != missing}
                result = self.run_through()
                self.assertEqual((result["status"], result["code"]),
                                 (store.NEEDS_REVIEW, "hubspot_writeback_failed"))
                self.assertEqual((len(self.famly.child_creates), len(self.famly.contact_creates)),
                                 (1, 2))                              # all of Famly was done
                self.assertEqual(self.job().outcomes, ["c1:created", "c2:created"])

    def test_a_status_dropdown_that_does_not_offer_a_value_skips_only_that_write(self):
        self.hub.defs["famly_sync_status"]["options"] = ["queued", "child_ready"]
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.written()["famly_sync_status"], "child_ready")   # contacts_linked skipped
        self.assertIn("value_not_offered:famly_sync_status:contacts_linked", " ".join(self.notify.notices))
        self.assertEqual(len(self.famly.contact_creates), 2)

    def test_a_gaps_checkbox_only_gets_the_codes_it_offers(self):
        self.hub.defs["famly_sync_gaps"]["options"] = ["phone_missing", "address_incomplete"]
        self.rest.wrong_group = True                                 # adds a summary flag
        self.run_through()
        gaps = self.written()["famly_sync_gaps"].split(";")
        self.assertLessEqual(set(gaps), {"phone_missing", "address_incomplete"})
        self.assertIn("gap_not_offered:summary_group_mismatch", " ".join(self.notify.notices))

    def test_a_property_listing_failure_never_blocks_famly_either(self):
        self.hub.defs_error = HubSpotError("GET failed with HTTP 500", 500)
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual((len(self.famly.child_creates), len(self.famly.contact_creates)), (1, 2))
        self.assertIn("preflight_failed", " ".join(self.notify.notices))
        self.assertEqual(self.written()["famly_childid"], "child-1")   # writes were still attempted

    def test_a_failed_advisory_write_is_reported_but_the_job_is_still_done(self):
        self.hub.fail_props = {"famly_sync_status", "famly_sync_note", "famly_sync_gaps"}
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertIn("write_failed:famly_sync_status", " ".join(self.notify.notices))
        self.assertEqual(self.written()["famly_childid"], "child-1")

    def test_a_failed_child_id_write_ends_needs_review_but_never_blocks_famly(self):
        self.hub.write_error = HubSpotError("PATCH failed with HTTP 500", 500)   # every write fails
        result = self.run_through()
        self.assertEqual((result["status"], result["code"]),
                         (store.NEEDS_REVIEW, "hubspot_writeback_failed"))
        self.assertEqual(len(self.famly.child_creates), 1)
        self.assertEqual(len(self.famly.contact_creates), 2)           # the contacts were still created
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:created"])
        self.assertEqual(self.job().child_id, "child-1")

    def test_hubspot_writes_are_one_property_per_request_and_only_allow_listed_ones(self):
        self.run_through()
        for _deal, props in self.hub.writes:
            self.assertEqual(len(props), 1)                            # one per request
            self.assertLessEqual(set(props), hubspot.WRITABLE_DEAL_PROPERTIES)
        order = [p for _d, props in self.hub.writes for p in props]
        self.assertEqual(order[:3], ["famly_childid", "end_date", "famly_sync_status"])
        self.assertEqual(self.written()["famly_sync_note"], "phase2_done")
        # Re-running a property write sends the same value.
        syncer = hubspot_sync.Syncer(self.hub, DEAL)
        for _ in range(2):
            syncer.write("famly_sync_status", "contacts_linked")
            syncer.write_gaps(["b", "a", "a"])
        self.assertEqual(self.hub.writes[-1][1], {"famly_sync_gaps": "a;b"})

    def test_there_is_no_write_back_in_dry_mode(self):
        self.mode("dry")
        self.enqueue()
        self.run_worker()
        self.assertEqual(self.hub.writes, [])

    def test_the_real_writer_refuses_any_other_property_and_retries_throttling(self):
        class Resp:
            def __init__(self, status): self.status_code, self.headers = status, {}
            def json(self): return {}

        class Session:
            def __init__(self, statuses): self.statuses, self.sent = list(statuses), []
            def patch(self, url, headers=None, json=None, timeout=None):
                self.sent.append((url, json))
                return Resp(self.statuses.pop(0))

        session = Session([429, 200])
        sleeps = []
        writer = HubSpotDealWriter("tok", session=session, sleep=sleeps.append)
        with self.assertRaises(ValueError):
            writer.write_deal_properties(DEAL, {"dealname": "x"})
        with self.assertRaises(ValueError):
            writer.write_deal_properties(DEAL, {})
        self.assertEqual(session.sent, [])
        writer.write_deal_properties(DEAL, {"famly_sync_status": "child_ready"})
        self.assertEqual(len(session.sent), 2)                       # 429 then success
        self.assertEqual(session.sent[1][1], {"properties": {"famly_sync_status": "child_ready"}})
        self.assertTrue(session.sent[0][0].endswith(f"/crm/v3/objects/deals/{DEAL}"))
        # The read-only base client still has no write path.
        self.assertFalse(hasattr(hubspot.HubSpotClient, "write_deal_properties"))


# --------------------------------------------------------------------------- #
# Whole-run guarantees
# --------------------------------------------------------------------------- #
class MigrationTests(Phase2Case):
    """The store is opened by several processes at once; the column migration must
    be idempotent and survive losing the race to another process."""

    def _old_store(self):
        """A store as an older release left it: without the newer columns."""
        path = store.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE onboard_jobs (id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, "
                "status TEXT NOT NULL, mode TEXT NOT NULL, plan_hash TEXT, approved_hash TEXT, "
                "child_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, "
                "approval_ref TEXT, child_create_sent INTEGER NOT NULL DEFAULT 0, outcomes TEXT, "
                "claimed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            connection.commit()

    def _columns(self):
        with contextlib.closing(sqlite3.connect(store.store_path())) as connection:
            return [row[1] for row in connection.execute("PRAGMA table_info(onboard_jobs)")]

    def test_migrating_twice_on_a_store_that_already_has_the_columns_changes_nothing(self):
        self.enqueue()                                   # creates the current schema
        before = self._columns()
        with contextlib.closing(sqlite3.connect(store.store_path())) as connection:
            connection.row_factory = sqlite3.Row
            store._migrate(connection)
            store._migrate(connection)
        self.assertEqual(self._columns(), before)
        self.assertEqual(self.job().status, store.QUEUED)           # and the data is intact

    def test_an_old_store_gets_the_columns_once_however_often_it_is_opened(self):
        self._old_store()
        for _ in range(3):
            store.get("nothing")                         # every open runs the migration
        columns = self._columns()
        for name in ("source", "approval_nonce", "reask_count", "plan_sections"):
            self.assertEqual(columns.count(name), 1, name)

    def test_a_duplicate_column_error_from_a_concurrent_process_is_tolerated(self):
        self._old_store()
        real = sqlite3.connect(store.store_path())
        real.row_factory = sqlite3.Row

        class RacingConnection:
            """Another process adds each column between our check and our ALTER."""
            def execute(self_inner, sql, *args):
                if sql.startswith("ALTER TABLE"):
                    real.execute(sql)                    # the other process wins the race
                    real.commit()
                return real.execute(sql, *args)          # ours now fails: duplicate column

        try:
            store._migrate(RacingConnection())           # must not raise
        finally:
            real.close()
        for name in ("source", "approval_nonce", "reask_count", "plan_sections"):
            self.assertEqual(self._columns().count(name), 1, name)

    def test_any_other_migration_error_is_still_raised(self):
        class BrokenConnection:
            def execute(self_inner, sql, *args):
                if sql.startswith("PRAGMA"):
                    return []
                raise sqlite3.OperationalError("disk I/O error")

        with self.assertRaises(sqlite3.OperationalError):
            store._migrate(BrokenConnection())


class GuaranteeTests(Phase2Case):
    def test_no_slack_payload_database_row_or_log_line_carries_personal_data(self):
        self.run_through()
        blob = self.all_output()
        self.assertTrue(self.notify.gates and self.notify.notices and self.logs)
        for value in PII:
            self.assertNotIn(value, blob, value)
        slack_text = "\n".join([t for t, _, _ in self.notify.gates] + self.notify.notices)
        self.assertNotIn("@", slack_text)          # no email address of any kind

    def test_error_paths_do_not_leak_response_bodies_either(self):
        body = json.dumps({"errors": [{"message": "bad name Zara Quillfeather ada.l@example.org"}]})
        self.famly.child_effect = mock.Mock(side_effect=GraphQLHTTPError("bad", 400, body))
        self.run_through()
        blob = self.all_output()
        for value in PII:
            self.assertNotIn(value, blob, value)
        self.assertEqual(self.job().last_error_code, "child_http_400")

    def test_the_jobs_table_has_no_columns_for_personal_data(self):
        self.enqueue()
        with contextlib.closing(sqlite3.connect(store.store_path())) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(onboard_jobs)")]
        self.assertEqual(columns, ["id", "deal_id", "status", "mode", "plan_hash", "approved_hash",
                                   "child_id", "attempts", "last_error_code", "approval_ref",
                                   "child_create_sent", "outcomes", "claimed_at", "created_at",
                                   "updated_at", "source", "approval_nonce", "reask_count",
                                   "plan_sections"])

    def test_the_famly_writers_are_create_only_and_the_fake_would_catch_anything_else(self):
        for path in (famly_writes.CREATE_CHILD_QUERY, famly_writes.CREATE_CONTACT_QUERY):
            famly_writes._assert_create_only(path)
            text = path.read_text(encoding="utf-8").lower()
            body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn("delete", body)
            self.assertNotIn("update(", body)
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "delete.graphql"
            bad.write_text("mutation M { children { delete(childIds: []) } }", encoding="utf-8")
            with self.assertRaises(AssertionError):
                self.famly.execute(bad, {}, "Bad")
            with self.assertRaises(AssertionError):
                famly_writes._assert_create_only(bad)
        self.run_through()
        for op in self.famly.calls:
            self.assertNotIn("delete", op.lower())

    def test_the_slack_buttons_and_click_routing_for_gate_a_exist(self):
        blocks = slack._onboard_blocks("text", "job:hash")
        ids = [e["action_id"] for e in blocks[1]["elements"]]
        self.assertEqual(ids, [slack.ACTION_ONBOARD_APPROVE, slack.ACTION_ONBOARD_REJECT])
        self.assertTrue(all(e["value"] == "job:hash" for e in blocks[1]["elements"]))
        import web_registry
        job = self.run_to_approval()
        text = web_registry.handle_slack_click(
            slack.ACTION_ONBOARD_APPROVE,
            approval.button_value(job.id, job.plan_hash, job.approval_nonce), "U1")
        self.assertEqual(text, approval.MSG_APPROVED)
        server_source = Path("server.py").read_text(encoding="utf-8")
        self.assertIn("*slack.ONBOARD_ACTIONS", server_source)

    def test_the_existing_plan_approval_buttons_still_route_to_the_plan_handler(self):
        import web_registry
        with mock.patch.object(web_registry, "SLACK_CLICK_HANDLER", return_value="plan") as handler:
            self.assertEqual(web_registry.handle_slack_click(slack.ACTION_APPROVE, "pid", "U1"),
                             "plan")
        handler.assert_called_once()


# --------------------------------------------------------------------------- #
# Existing child vs a child this job created; Gate A for contact-only plans
# --------------------------------------------------------------------------- #
class ExistingChildRuleTests(Phase2Case):
    def existing_child(self, contacts):
        self.famly.children["child-9"] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": GROUP_ID,
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts["child-9"] = list(contacts)

    def own_child_job(self, contacts):
        """A job whose record says it already created the child (as after a worker
        that created it and then stopped), with these contacts on it."""
        self.existing_child(contacts)
        job = self.enqueue().job
        store.update(job.id, child_id="child-9")
        self.assertTrue(store.mark_child_create_sent(job.id))
        return job

    def test_an_existing_child_with_an_unmatched_contact_holds_with_no_writes(self):
        self.existing_child([("fc-1", "ada.l@example.org"), ("fc-3", "ben.b@other.com")])
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]),
                         (store.HELD, "contact_unmatched_on_existing_child"))
        self.assertEqual(self.notify.gates, [])                    # nothing to approve
        self.assertFalse(self.famly.child_creates or self.famly.contact_creates)
        self.assertTrue(any("contact_unmatched_on_existing_child" in n for n in self.notify.notices))
        self.assertEqual(self.written()["famly_sync_note"],
                         "held:contact_unmatched_on_existing_child")
        # Candidate ids live in the plan only; no email or name reaches Slack or the DB.
        blob = self.all_output()
        for value in PII:
            self.assertNotIn(value, blob, value)

    def test_every_unmatched_contact_on_an_existing_child_gets_its_own_hold(self):
        from actions.onboard_dry_run.runner import DryRun
        self.existing_child([])
        plan = DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL)
        holds = [h for h in plan["holds"] if h["reason"] == "contact_unmatched_on_existing_child"]
        self.assertEqual([(h["hubspotContactId"], h["candidateIds"]) for h in holds],
                         [("c1", []), ("c2", [])])

    def test_the_same_plan_for_a_child_this_job_created_proceeds(self):
        job = self.own_child_job([("fc-1", "ada.l@example.org")])
        result = self.run_worker()
        # c2 is unmatched but the child is OURS: it stays would_create, so the job
        # asks for approval rather than holding.
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        self.assertIn("Child: created by this job", self.notify.gates[0][0])
        self.approve(job)
        final = self.run_worker()
        self.assertEqual(final["status"], store.DONE)
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:created"])
        self.assertEqual(self.famly.child_creates, [])             # not a second child
        self.assertEqual(len(self.famly.contact_creates), 1)

    def test_a_contact_only_plan_needs_approval_and_writes_nothing_before_it(self):
        job = self.own_child_job([("fc-1", "ada.l@example.org")])
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), 1)
        self.assertFalse(self.famly.child_creates or self.famly.contact_creates)
        self.assertEqual(self.hub.writes, [])
        text = self.notify.gates[0][0]
        self.assertIn("Famly writes planned: 0 child, 1 contact(s)", text)
        self.assertIn("1 to create, 1 present", text)
        # The click alone writes nothing either; the worker does, only afterwards.
        self.approve(job)
        self.assertFalse(self.famly.contact_creates)
        self.run_worker()
        self.assertEqual(len(self.famly.contact_creates), 1)

    def test_a_contact_only_plan_whose_hash_changes_is_re_approved(self):
        job = self.own_child_job([("fc-1", "ada.l@example.org")])
        self.run_worker()
        self.approve(job)
        # A change the plan can see (the plan JSON holds presence flags and codes,
        # never an email, so an email edit alone would not change the hash).
        self.hub.contacts["c2"]["phone"] = ""
        result = self.run_worker()
        self.assertEqual(result["status"], store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), 2)
        self.assertIn("approval is needed again", self.notify.gates[1][0])
        self.assertFalse(self.famly.contact_creates)

    def test_a_plan_with_nothing_to_write_needs_no_approval(self):
        self.existing_child([("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")])
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(self.notify.gates, [])

    def test_gmail_and_googlemail_are_the_same_parent_and_nothing_else_is(self):
        self.existing_child([("fc-1", "ADA.L@GoogleMail.com "), ("fc-2", "ben.b@example.org")])
        self.hub.contacts["c1"]["email"] = "ada.l@gmail.com"
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)    # both matched
        # A dot or a +tag is a different address: unmatched, so a hold.
        store._reset_for_tests()
        self.famly.contacts["child-9"] = [("fc-1", "a.da.l@gmail.com"), ("fc-2", "ben.b@example.org")]
        self.enqueue()
        result = self.run_worker()
        self.assertEqual(result["code"], "contact_unmatched_on_existing_child")


# --------------------------------------------------------------------------- #
# child.origin comes from the job HISTORY for the deal
# --------------------------------------------------------------------------- #
class OriginFromHistoryTests(Phase2Case):
    def record_history(self, deal, child_id, sent=True):
        """A finished earlier job for `deal` that recorded `child_id`."""
        job = store.enqueue(deal, "live").job
        store.update(job.id, child_id=child_id)
        if sent:
            self.assertTrue(store.mark_child_create_sent(job.id))
        store.set_status(job.id, store.NEEDS_REVIEW, last_error_code="earlier")
        return job

    def put_child(self, child_id, contacts, with_deal_reference=True, urn="URN-1"):
        refs = [{"foreignId": DEAL, "system": "HUBSPOT"}] if with_deal_reference else []
        self.famly.children[child_id] = {
            "name": "x", "externalId": urn, "currentGroupId": GROUP_ID,
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": refs}
        self.famly.contacts[child_id] = list(contacts)

    def test_a_rerun_after_a_rejected_contact_completes_it_with_no_duplicates(self):
        state = {"rejected": False}

        def reject_ben_once(payload):
            if payload["email"] == "ben.b@example.org" and not state["rejected"]:
                state["rejected"] = True
                raise GraphQLHTTPError("bad", 400, "{}")
        self.famly.contact_effect = reject_ben_once

        self.run_through()
        first = self.job()
        self.assertEqual(first.status, store.NEEDS_REVIEW)
        self.assertEqual(first.outcomes, ["c1:created", "c2:contact_http_400"])
        self.assertEqual(first.child_id, "child-1")
        self.assertEqual(store.created_child_ids_for_deal(DEAL), frozenset({"child-1"}))

        second = self.enqueue()                                  # the same deal, again
        self.assertEqual(second.outcome, store.CREATED)
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        text = self.notify.gates[-1][0]
        self.assertIn("Child: created by pipeline", text)         # an EARLIER job made it
        self.assertIn("Famly writes planned: 0 child, 1 contact(s)", text)
        self.assertFalse(self.famly.child_creates[1:])             # nothing written before approval
        self.assertEqual(len(self.famly.contact_creates), 2)       # c1, and the rejected c2

        self.approve()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(self.job().outcomes, ["c1:present", "c2:created"])
        self.assertEqual(len(self.famly.child_creates), 1)         # no second child
        emails = [email for _, email in self.famly.contacts["child-1"]]
        self.assertEqual(sorted(emails), ["ada.l@example.org", "ben.b@example.org"])   # each once
        sent = [p["email"] for p in self.famly.contact_creates]
        self.assertEqual(sent.count("ada.l@example.org"), 1)       # c1 never re-sent
        self.assertFalse(self.job().child_create_sent)             # the second job sent no create

    def test_a_child_whose_read_back_failed_is_still_the_pipelines_on_the_next_job(self):
        self.famly.blind_after_create = True       # the read-back lookup finds nothing
        self.run_through()
        self.assertEqual(self.job().last_error_code, "readback_child_lookup_failed")
        # Recorded at once, before the read-back, so the failure did not lose it.
        self.assertEqual(store.created_child_ids_for_deal(DEAL), frozenset({"child-1"}))
        self.assertEqual(self.famly.contact_creates, [])            # the one stopping read-back

        self.famly.blind_after_create = False
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertIn("created by pipeline", self.notify.gates[-1][0])
        self.approve()
        self.run_worker()
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:created"])  # the last-moment rule agrees
        self.assertEqual(len(self.famly.child_creates), 1)

    def test_a_child_with_the_deal_reference_but_no_job_record_holds(self):
        self.put_child("child-9", [("fc-1", "ada.l@example.org")])    # nobody's job made it
        self.assertEqual(store.created_child_ids_for_deal(DEAL), frozenset())
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]),
                         (store.HELD, "contact_unmatched_on_existing_child"))
        self.assertFalse(self.famly.child_creates or self.famly.contact_creates)
        self.assertEqual(self.notify.gates, [])

    def test_a_child_found_only_by_the_urn_holds_even_if_a_job_recorded_its_id(self):
        self.put_child("child-9", [("fc-1", "ada.l@example.org")], with_deal_reference=False)
        self.record_history(DEAL, "child-9")          # an earlier job "created" child-9 ...
        self.assertEqual(store.created_child_ids_for_deal(DEAL), frozenset({"child-9"}))
        self.enqueue()
        result = self.run_worker()                    # ... but it is found via the URN fallback
        self.assertEqual((result["status"], result["code"]),
                         (store.HELD, "contact_unmatched_on_existing_child"))
        self.assertFalse(self.famly.contact_creates)

    def test_a_job_record_for_another_deal_does_not_make_this_child_the_pipelines(self):
        self.put_child("child-9", [("fc-1", "ada.l@example.org")])
        self.record_history("2002", "child-777")      # another deal, another child
        self.record_history("2002", "child-9")        # another deal, even the SAME child id
        self.assertEqual(store.created_child_ids_for_deal(DEAL), frozenset())
        self.assertEqual(store.created_child_ids_for_deal("2002"), frozenset({"child-777", "child-9"}))
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]),
                         (store.HELD, "contact_unmatched_on_existing_child"))
        self.assertFalse(self.famly.contact_creates)

    def test_a_job_that_only_found_an_existing_child_does_not_make_it_the_pipelines(self):
        self.put_child("child-9", [("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")])
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)      # both present: nothing to do
        self.assertEqual(self.job().child_id, "child-9")               # recorded ...
        self.assertFalse(self.job().child_create_sent)                 # ... but never created by us
        self.assertEqual(store.created_child_ids_for_deal(DEAL), frozenset())

    def test_the_slack_wording_distinguishes_the_four_child_states(self):
        from actions.onboard import notifier as notifier_mod
        plan = {"dealId": DEAL, "siteCode": "HDWEST", "groupTitle": "Littlewings",
                "dates": {"firstDayPresent": True}, "warnings": [],
                "contacts": [{"status": "would_create", "isBillPayer": True}]}

        def text(child, this_job=None):
            return notifier_mod.gate_text({**plan, "child": child}, "h" * 16, "live",
                                          this_job_child_id=this_job)
        self.assertIn("Child: would be created", text({"status": "child_would_create"}))
        self.assertIn("Child: already in Famly",
                      text({"status": "child_found", "famlyChildId": "c", "origin": "existing"}))
        mine = {"status": "child_found", "famlyChildId": "c", "origin": "created_by_pipeline"}
        self.assertIn("Child: created by this job", text(mine, this_job="c"))
        self.assertIn("Child: created by pipeline", text(mine, this_job="other"))
        self.assertIn("Child: created by pipeline", text(mine))


# --------------------------------------------------------------------------- #
# Drew's review: the end date, flags, independent contacts, the lookup rule
# --------------------------------------------------------------------------- #
class EndDateTests(Phase2Case):
    def test_a_missing_end_date_is_defaulted_and_sent_as_last_day(self):
        self.run_through()
        # Born 2024-01-15: the 4th birthday is 2028-01-15, so the first 31 August on or after it.
        self.assertEqual(self.famly.child_creates[0]["sitesRelation"][0]["lastDay"], "2028-08-31")

    def test_an_existing_end_date_is_used_and_never_overwritten(self):
        self.hub.deals[DEAL]["end_date"] = "2027-03-31"
        self.run_through()
        self.assertEqual(self.famly.child_creates[0]["sitesRelation"][0]["lastDay"], "2027-03-31")
        self.assertNotIn("end_date", self.written())                 # the deal is left alone
        self.assertEqual(self.hub.deals[DEAL]["end_date"], "2027-03-31")

    def test_the_default_is_written_to_the_deal_only_after_the_famly_write_succeeds(self):
        self.run_through()
        self.assertEqual(self.hub.deals[DEAL]["end_date"], "2028-08-31")
        self.assertLess(self.events.index("child_create"), self.events.index(("hubspot", "end_date")))
        self.assertLess(self.events.index("child_create"), self.events.index(("hubspot", "famly_childid")))
        self.assertIn(("hubspot", "end_date"), self.events)

    def test_nothing_is_written_to_the_deal_if_the_child_was_not_created(self):
        self.famly.child_effect = mock.Mock(side_effect=GraphQLHTTPError("bad", 400, "{}"))
        self.run_through()
        self.assertEqual(self.job().last_error_code, "child_http_400")
        self.assertNotIn("end_date", self.hub.deals[DEAL])           # still empty
        self.assertEqual(self.hub.writes, [])

    def test_an_existing_child_gets_no_end_date_written(self):
        self.famly.children["child-9"] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": GROUP_ID,
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts["child-9"] = [("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")]
        self.enqueue()
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertNotIn("end_date", self.written())                 # no Famly write, so none here

    def test_it_is_never_overwritten_even_if_it_was_filled_in_between_the_plan_and_the_write(self):
        self.run_to_approval()
        self.approve()
        # After the plan read of the next run (call 1), somebody types an end date.
        self.hub.get_deal_hooks[self.hub.get_deal_calls + 1] = (
            lambda: self.hub.deals[DEAL].__setitem__("end_date", "2030-01-01"))
        self.assertEqual(self.run_worker()["status"], store.DONE)
        self.assertEqual(self.hub.deals[DEAL]["end_date"], "2030-01-01")   # theirs, untouched
        self.assertNotIn("end_date", self.written())
        self.assertIn("end_date_already_set", " ".join(self.notify.notices))

    def test_a_defaulted_end_date_that_cannot_be_written_is_needs_review(self):
        self.hub.fail_props = {"end_date"}
        result = self.run_through()
        self.assertEqual((result["status"], result["code"]),
                         (store.NEEDS_REVIEW, "hubspot_writeback_failed"))
        self.assertEqual(len(self.famly.contact_creates), 2)         # Famly was not held up
        self.assertEqual(self.written()["famly_childid"], "child-1")

    def test_an_end_date_before_the_start_holds_with_no_writes(self):
        self.hub.deals[DEAL]["child_date_of_birth"] = "2022-01-15"     # default end: 2026-08-31
        self.hub.deals[DEAL]["actual_start_date_1"] = "2026-10-01"     # already past it
        self.enqueue()
        result = self.run_worker()
        self.assertEqual((result["status"], result["code"]), (store.HELD, "end_date_before_start"))
        self.assertFalse(self.famly.child_creates or self.famly.contact_creates)
        self.assertEqual(self.notify.gates, [])

    def test_the_gate_message_says_when_the_end_date_was_defaulted(self):
        self.run_to_approval()
        self.assertIn("End date: defaulted (31 August after the 4th birthday)", self.notify.gates[0][0])
        store._reset_for_tests()
        self.hub.deals[DEAL]["end_date"] = "2027-03-31"
        self.run_to_approval()
        self.assertIn("End date: present", self.notify.gates[1][0])
        self.assertNotIn("defaulted", self.notify.gates[1][0])
        for value in PII + ["2028-08-31", "2027-03-31"]:
            self.assertNotIn(value, self.notify.gates[0][0] + self.notify.gates[1][0])


class ContactOrderAndLookupTests(Phase2Case):
    def test_the_bill_payer_is_created_first_whatever_the_order_on_the_deal(self):
        self.hub.assoc[("deals", DEAL, "contacts")] = [
            {"id": "c2", "types": [{"typeId": 3, "label": None}]},                    # not the bill payer
            {"id": "c1", "types": [{"typeId": 91, "label": "Bill Payer"}]},
        ]
        self.run_through()
        self.assertEqual([p["email"] for p in self.famly.contact_creates],
                         ["ada.l@example.org", "ben.b@example.org"])
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:created"])

    def test_one_failing_contact_never_stops_or_holds_the_others(self):
        self.hub.contacts["c3"] = {"email": "cy.c@example.org", "firstname": "Cy", "lastname": "Cade"}
        self.hub.assoc[("deals", DEAL, "contacts")].append(
            {"id": "c3", "types": [{"typeId": 3, "label": None}]})
        seen = []

        def effect(payload):
            seen.append(payload["email"])
            if payload["email"] == "ada.l@example.org":
                raise GraphQLHTTPError("bad", 400, "{}")         # the bill payer is rejected
            if payload["email"] == "ben.b@example.org":
                raise RuntimeError("an unexpected failure")      # ... and so is an odd error
        self.famly.contact_effect = effect
        self.hub.contacts["c2"]["lastname"] = "Babbage"
        result = self.run_through()
        self.assertEqual(seen, ["ada.l@example.org", "ben.b@example.org", "cy.c@example.org"])
        self.assertEqual(self.job().outcomes,
                         ["c1:contact_http_400", "c2:contact_error", "c3:created"])
        self.assertEqual(result["status"], store.NEEDS_REVIEW)
        self.assertEqual(self.job().last_error_code, "contact_outcomes")
        self.assertFalse(any("Babbage" in n or "ben.b@" in n for n in self.notify.notices))

    def calls_between(self, start_marker, end_marker, calls=None):
        calls = calls if calls is not None else self.famly.calls
        a = calls.index(start_marker)
        b = calls.index(end_marker, a + 1)
        return calls[a + 1:b]

    def test_the_existing_contact_lookup_is_skipped_only_for_the_first_contact_of_a_new_child(self):
        self.run_through()
        calls = self.famly.calls
        self.assertEqual(calls.count(famly_writes.CREATE_CONTACT_OP), 2)
        # No contact lookup between creating the child and the first contact create ...
        first = self.calls_between(famly_writes.CREATE_CHILD_OP, famly_writes.CREATE_CONTACT_OP)
        self.assertNotIn("OnboardChildContacts", first)
        # ... but once a contact create has been sent, the next contact is looked up first.
        a = calls.index(famly_writes.CREATE_CONTACT_OP)
        b = calls.index(famly_writes.CREATE_CONTACT_OP, a + 1)
        self.assertIn("OnboardChildContacts", calls[a + 1:b])

    def test_the_lookup_is_used_on_a_re_run(self):
        self.famly.contact_effect = mock.Mock(side_effect=[GraphQLHTTPError("bad", 400, "{}"), None])
        self.run_through()                                          # c1 rejected, c2 created
        self.assertEqual(self.job().outcomes, ["c1:contact_http_400", "c2:created"])
        mark = len(self.famly.calls)
        self.famly.contact_effect = None
        self.enqueue()
        self.run_worker()
        self.approve()
        self.run_worker()
        again = self.famly.calls[mark:]
        before_first_create = again[:again.index(famly_writes.CREATE_CONTACT_OP)]
        self.assertIn("OnboardChildContacts", before_first_create)   # looked up first
        self.assertEqual(self.job().outcomes, ["c1:created", "c2:present"])   # and c2 is not duplicated
        self.assertEqual(sorted(e for _, e in self.famly.contacts["child-1"]),
                         ["ada.l@example.org", "ben.b@example.org"])

    def test_the_lookup_is_used_for_an_existing_child_and_after_an_unknown_child_outcome(self):
        # An existing child: looked up (and found present), no contact create at all.
        self.famly.children["child-9"] = {
            "name": "x", "externalId": "URN-1", "currentGroupId": GROUP_ID,
            "sitesRelation": [{"siteId": SITE_ID, "firstDay": "2026-01-15"}],
            "externalSystemsReferences": [{"foreignId": DEAL, "system": "HUBSPOT"}]}
        self.famly.contacts["child-9"] = [("fc-1", "ada.l@example.org"), ("fc-2", "ben.b@example.org")]
        self.enqueue()
        self.run_worker()
        self.assertGreaterEqual(self.famly.calls.count("OnboardChildContacts"), 2)
        self.assertEqual(self.famly.contact_creates, [])

        # A child create whose outcome was unknown (timeout, then found by lookup): the
        # first contact is looked up before it is created.
        store._reset_for_tests()
        other = FakeFamly()
        other.events = self.events
        self.famly, self.rest = other, FakeRest(other)
        self.deps = worker.Deps(self.hub, other, self.rest, self.notify,
                                famly_factory=lambda: FamlyReads(other, site_ids=[SITE_ID]))
        self.hub.deals[DEAL]["end_date"] = "2028-08-31"
        other.child_land_then_raise = requests.exceptions.Timeout()
        self.run_through()
        self.assertEqual(self.job().status, store.DONE)
        first = self.calls_between(famly_writes.CREATE_CHILD_OP, famly_writes.CREATE_CONTACT_OP, other.calls)
        self.assertIn("OnboardChildContacts", first)


# --------------------------------------------------------------------------- #
# Transport: a create is sent exactly once, whatever the failure
# --------------------------------------------------------------------------- #
class CreateSentOnceTests(unittest.TestCase):
    """Patches requests.post BELOW GraphQLClient, so a retry in any layer shows."""

    @staticmethod
    def _response(status, body):
        response = mock.Mock()
        response.status_code = status
        response.text = json.dumps(body)
        response.json.return_value = body
        return response

    def _client(self):
        from core.client import GraphQLClient
        session = mock.Mock()
        session.auth_headers.return_value = {"x-famly-accesstoken": "t"}
        config = mock.Mock(graphql_url="https://famly.invalid/graphql")
        return GraphQLClient(session=session, config=config)

    def test_each_failure_sends_one_request_and_is_classified(self):
        transient = {"errors": [{"message": "x", "extensions": {"display": "Transient"}}]}
        cases = [
            ("timeout", mock.Mock(side_effect=requests.Timeout()), famly_writes.OutcomeUnknown),
            ("connection", mock.Mock(side_effect=requests.ConnectionError()),
             famly_writes.OutcomeUnknown),
            ("http_500", mock.Mock(return_value=self._response(500, {})), famly_writes.OutcomeUnknown),
            ("http_429", mock.Mock(return_value=self._response(429, {})), famly_writes.CreateRejected),
            ("transient_200", mock.Mock(return_value=self._response(200, transient)),
             famly_writes.CreateRejected),
        ]
        calls = (("create_child", famly_writes.create_child, {"name": "x"}),
                 ("create_contact", famly_writes.create_contact, {"email": "a@b.co"}))
        for label, post, expected in cases:
            for call_name, func, payload in calls:
                with self.subTest(case=label, call=call_name):
                    post.reset_mock()
                    with mock.patch("core.client.requests.post", post):
                        with self.assertRaises(expected):
                            func(self._client(), payload)
                    self.assertEqual(post.call_count, 1)


# --------------------------------------------------------------------------- #
# Logging reaches the journal: the approval lines are emitted at INFO
# --------------------------------------------------------------------------- #
class ApprovalLoggingTests(Phase2Case):
    def test_the_recheck_and_unstable_lines_carry_hashes_and_codes_only(self):
        from core.logging_setup import configure_logging
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        self.addCleanup(lambda: (root.handlers.clear(), root.handlers.extend(saved_handlers),
                                 root.setLevel(saved_level)))
        root.setLevel(logging.WARNING)
        stream = io.StringIO()
        configure_logging(stream=stream, force=True)
        self.run_to_approval()
        self.approve()
        self.hub.deals[DEAL]["actual_start_date_1"] = "2026-02-15"
        self.hub.deals[DEAL]["child_date_of_birth"] = "2023-02-15"
        self.run_worker()
        self.approve()
        self.hub.contacts["c1"]["address"] = ""
        self.run_worker()
        out = stream.getvalue()
        self.assertIn("approval re-check", out)
        self.assertIn("plan unstable", out)
        self.assertIn("contacts:", out)
        for value in PII:
            self.assertNotIn(value, out)
        for line in self.notify.notices:
            for value in PII:
                self.assertNotIn(value, line.casefold())

    def test_ask_approve_recheck_and_reask_lines_are_emitted_once_logging_is_configured(self):
        from core.logging_setup import configure_logging
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        self.addCleanup(lambda: (root.handlers.clear(), root.handlers.extend(saved_handlers),
                                 root.setLevel(saved_level)))
        root.setLevel(logging.WARNING)             # what gunicorn leaves behind
        stream = io.StringIO()
        configure_logging(stream=stream, force=True)

        self.run_to_approval()                                  # ask
        self.approve()                                          # approve (a click)
        self.hub.deals[DEAL]["actual_start_date_1"] = "2026-02-15"
        self.hub.deals[DEAL]["child_date_of_birth"] = "2023-02-15"
        self.run_worker()                                       # re-check, then re-ask
        out = stream.getvalue()

        self.assertEqual(root.level, logging.INFO)
        self.assertIn("ONBOARD APPROVED", out)
        self.assertIn("ONBOARD CLICK action=onboard_approve", out)
        self.assertIn("result=approved", out)
        self.assertIn("approval re-check", out)
        self.assertIn("unchanged=False", out)                   # the plan really changed
        self.assertEqual(out.count("approval asked"), 2)        # the ask and the re-ask
        self.assertIn("plan_changed=True", out)
        for value in PII:
            self.assertNotIn(value, out)


if __name__ == "__main__":
    unittest.main()
