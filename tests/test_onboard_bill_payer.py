"""Phase 3: the bill payer stage. Fakes only; nothing is sent anywhere.

Personal data in the fixtures is synthetic and is only ever COMPARED with, never
hard-coded into an expectation.
"""

import ast
import contextlib
import copy
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import requests

from actions.onboard import bill_payer_writes as bpw
from actions.onboard import hubspot_sync, messages, notifier, store, worker
from actions.onboard_dry_run import bill_payers as bp
from actions.onboard_dry_run import runner as dry_runner
from actions.onboard_dry_run.famly_reads import FamlyReads
from actions.onboard_dry_run.links import DealContact
from actions.probe_bill_payers import runner as probe_runner
from core.client import GraphQLError, GraphQLHTTPError
from tests.test_onboard_phase2 import DEAL, PII, SITE_ID, FakeFamly, FakeRest, Phase2Case
from tests.test_probe_bill_payers_write import schema as write_schema

CHILD = "child-1"          # the first child the phase 2 fake creates
SENSITIVE = ("socialSecurityNumber", "accountNumber", "sortCode")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class BPFamly(FakeFamly):
    """The phase 2 Famly fake plus live bill payers: a paged scan that REQUIRES an explicit
    includeDeleted, create and addChildren with read-after-write lag, error injection."""

    def __init__(self):
        super().__init__()
        self.payers = []            # {"id","email","deleted","children":{child: multiplier},"details"}
        self.scans = 0
        self.scan_flags, self.scan_sites, self.scan_pages = [], [], 0
        self.bp_writes = []         # (operation, variables, document text)
        self.create_error = None          # raised BEFORE anything happens
        self.create_after_error = None    # the create RUNS, then this is raised
        self.add_error = None
        self.add_after_error = None
        self.lag_scans = 0
        self.after_scan = {}              # scan number -> callable run after that scan answers
        self.scan_error = None            # scan number -> exception (or None)
        self.stored_multiplier = None     # what is really stored (a read-back mismatch)
        self._bp_n = 1

    def execute(self, query_path, variables, operation_name):
        if operation_name == bp.SCAN_OP:
            return self._scan(variables)
        if operation_name == probe_runner.SCHEMA_OP:
            return {"data": {"__schema": write_schema()}}
        if operation_name in (bpw.CREATE_OP, bpw.ADD_CHILDREN_OP):
            return self._write(query_path, variables, operation_name)
        return super().execute(query_path, variables, operation_name)

    def node(self, payer):
        return {"billPayerId": payer["id"], "email": payer["email"],
                "deletedAt": "2026-01-01T00:00:00Z" if payer["deleted"] else None,
                "children": [{"childId": c, "share": {"multiplier": m}}
                             for c, m in payer["children"].items()
                             if self.scans > payer.get("children_visible_after", 0)],
                **payer.get("details", {})}

    def _scan(self, variables):
        self.scans += 1
        self.scan_pages += 1
        flag = variables.get("includeDeleted", "OMITTED")
        self.scan_flags.append(flag)
        self.scan_sites.append(variables["siteIds"])
        if self.scan_error:
            error = self.scan_error(self.scans)
            if error:
                raise error
        rows = []
        if variables["siteIds"] == [SITE_ID]:
            rows = [p for p in self.payers if (flag is True or not p["deleted"])
                    and self.scans > p.get("visible_at", 0)]
        start = int(variables["next"] or 0)
        nxt = str(start + 2) if start + 2 < len(rows) else None
        response = {"data": {"billPayers": {"listBySiteIds": {
            "result": [self.node(p) for p in rows[start:start + 2]], "next": nxt}}}}
        hook = self.after_scan.pop(self.scans, None)
        if hook:
            hook()
        return response

    def _write(self, query_path, variables, op):
        text = Path(query_path).read_text(encoding="utf-8")
        self.bp_writes.append((op, variables, text))
        if op == bpw.CREATE_OP:
            if self.create_error:
                raise self.create_error
            item = variables["input"][0]
            payer = {"id": f"bp-{self._bp_n}", "email": item["email"], "deleted": False,
                     "children": {c["childId"]: self.stored_multiplier or (c.get("share") or {}).get("multiplier", 1.0)
                                  for c in item["children"]},
                     "visible_at": self.scans + self.lag_scans, "details": {}}
            self._bp_n += 1
            self.payers.append(payer)
            if self.create_after_error:
                raise self.create_after_error
            return {"data": {"billPayers": {"create": [{"billPayerId": payer["id"]}]}}}
        if self.add_error:
            raise self.add_error
        entries = variables["input"]
        for entry in entries:
            payer = next(p for p in self.payers if p["id"] == entry["billPayerId"])
            payer["children"][entry["childId"]] = self.stored_multiplier or entry["share"]["multiplier"]
            if self.lag_scans:
                payer["children_visible_after"] = self.scans + self.lag_scans
        if self.add_after_error:
            raise self.add_after_error
        return {"data": {"billPayers": {"addChildren": [
            {"billPayerId": e["billPayerId"], "childId": e["childId"],
             "share": {"multiplier": e["share"]["multiplier"]}} for e in entries]}}}

    def ops(self):
        return [w[0] for w in self.bp_writes]


def payer(email, children=None, deleted=False, details=None, pid=None):
    return {"id": pid or f"existing-{abs(hash(email + str(children))) % 1000}", "email": email,
            "deleted": deleted, "children": dict(children or {}), "details": details or {}}


def deal_contact(email="ada@x.org", bill_payer=True, named=True, props=None):
    base = {"firstname": "A" if named else "", "lastname": "B" if named else "", "email": email}
    base.update(props or {})
    return DealContact("c1", email, bill_payer, props=base, has_first_and_last_name=named)


def live_payer(email, children=None, pid="p1"):
    return bp.Payer(pid, bp.normalise_email(email), dict(children or {}))


class BPCase(Phase2Case):
    def setUp(self):
        super().setUp()
        os.environ["ONBOARD_BILL_PAYER"] = "on"
        self.famly = BPFamly()
        self.rest = FakeRest(self.famly)
        self.hub.events = self.famly.events = self.events
        self.sleeps = []
        self.deps = worker.Deps(
            self.hub, self.famly, self.rest, self.notify,
            famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]), sleep=self.sleeps.append)

    @property
    def email(self):
        return self.hub.contacts["c1"]["email"].strip().lower()

    def final_notice(self):
        return [n for n in self.notify.notices if "deal" in n][-1]

    def codes(self):
        return self.job().last_error_code or ""


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
class ScanTests(BPCase):
    def test_include_deleted_is_explicit_false_site_scoped_and_every_page_is_read(self):
        for i in range(5):                                   # 5 payers, 2 a page = 3 pages
            self.famly.payers.append(payer(f"p{i}@x.org", pid=f"p{i}"))
        self.famly.payers.append(payer(self.email, pid="target"))   # lands on page 3
        self.famly.payers.append(payer("gone@x.org", deleted=True, pid="gone"))
        result = FamlyReads(self.famly, site_ids=[SITE_ID]).bill_payers(SITE_ID)
        self.assertEqual(self.famly.scan_pages, 3)                   # 6 live rows, 2 a page: every page read
        self.assertEqual(self.famly.scan_flags, [False] * self.famly.scan_pages)
        self.assertEqual(self.famly.scan_sites, [[SITE_ID]] * self.famly.scan_pages)
        self.assertIn("target", {p.id for p in result})
        self.assertNotIn("gone", {p.id for p in result})             # archived ones are ignored

    def test_every_scan_in_a_full_run_passes_include_deleted_false(self):
        self.run_through()
        self.assertGreaterEqual(self.famly.scans, 3)
        self.assertEqual(set(self.famly.scan_flags), {False})
        self.assertNotIn("OMITTED", self.famly.scan_flags)

    def test_a_payer_with_deleted_at_is_ignored_even_if_the_server_returned_it(self):
        self.assertIsNone(bp._payer({"billPayerId": "x", "email": "a@b.co", "deletedAt": "2026-01-01"}))

    def test_a_failed_page_or_a_page_cap_fails_the_scan_and_never_a_partial_result(self):
        self.famly.scan_error = lambda n: GraphQLHTTPError("x", 500, "") if n == 2 else None
        for i in range(6):
            self.famly.payers.append(payer(f"p{i}@x.org", pid=f"p{i}"))
        self.assertIsNone(FamlyReads(self.famly, site_ids=[SITE_ID]).bill_payers(SITE_ID))

        class Forever:
            def execute(self, query_path, variables, operation_name):
                return {"data": {"billPayers": {"listBySiteIds": {"result": [], "next": "again"}}}}
        with self.assertRaises(bp.ScanFailed):
            bp.scan_site(Forever(), "S")

    def test_the_scan_has_a_deadline(self):
        ticks = iter([0, 0, 1000, 1000, 1000])
        class Endless:
            def execute(self, query_path, variables, operation_name):
                return {"data": {"billPayers": {"listBySiteIds": {"result": [], "next": "more"}}}}
        with self.assertRaises(bp.ScanFailed):
            bp.scan_site(Endless(), "S", clock=lambda: next(ticks))

    def test_the_query_never_selects_the_sensitive_fields_or_external_id(self):
        text = bp.SCAN_QUERY.read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for name in SENSITIVE + ("externalId",):
            self.assertNotIn(name, code)
        self.assertIn("$includeDeleted: Boolean!", code)


# --------------------------------------------------------------------------- #
# Canonical email and the decision matrix
# --------------------------------------------------------------------------- #
class DecisionTests(unittest.TestCase):
    def test_canonical_email_case_whitespace_googlemail_and_plus_tags(self):
        c = bp.normalise_email
        self.assertEqual(c("  Ada.L@Example.ORG "), "ada.l@example.org")
        self.assertEqual(c("Someone@GoogleMail.com"), c("someone@gmail.com"))
        self.assertNotEqual(c("a+tag@x.org"), c("a@x.org"))            # a plus tag is preserved
        self.assertEqual(c("a+tag@x.org"), "a+tag@x.org")

    def test_matching_uses_the_canonical_email(self):
        decision = bp.decide([deal_contact(" ADA@Gmail.com ")], [live_payer("ada@googlemail.com")])
        self.assertEqual((decision.status, decision.matches), (bp.WOULD_ATTACH, 1))
        decision = bp.decide([deal_contact("a+tag@x.org")], [live_payer("a@x.org")])
        self.assertEqual(decision.status, bp.WOULD_CREATE)

    def test_the_decision_matrix(self):
        contact = deal_contact()
        cases = [
            ("no bill payer contact", [deal_contact(bill_payer=False)], [], None, (bp.FLAGGED, bp.NO_CONTACT)),
            ("two bill payer contacts", [deal_contact(), deal_contact()], [], None, (bp.FLAGGED, bp.NO_CONTACT)),
            ("no email", [deal_contact(email="  ")], [], None, (bp.FLAGGED, bp.NO_EMAIL)),
            ("scan failed", [contact], None, None, (bp.FLAGGED, bp.SCAN_FAILED)),
            ("0 matches", [contact], [live_payer("other@x.org")], None, (bp.WOULD_CREATE, None)),
            ("1 match", [contact], [live_payer("ada@x.org")], None, (bp.WOULD_ATTACH, None)),
            ("2 matches", [contact], [live_payer("ada@x.org", pid="a"), live_payer("ADA@x.org", pid="b")],
             None, (bp.FLAGGED, bp.AMBIGUOUS)),
            ("present", [contact], [live_payer("ada@x.org", {"k": 1.0})], "k", (bp.STATUS_PRESENT, None)),
            ("present at 0.0 still present", [contact], [live_payer("ada@x.org", {"k": 0.0})], "k",
             (bp.STATUS_PRESENT, None)),
            ("linked elsewhere (create)", [contact], [live_payer("other@x.org", {"k": 1.0})], "k",
             (bp.FLAGGED, bp.CHILD_LINKED_ELSEWHERE)),
            ("linked elsewhere (attach)", [contact],
             [live_payer("ada@x.org", pid="a"), live_payer("other@x.org", {"k": 1.0}, pid="o")], "k",
             (bp.FLAGGED, bp.CHILD_LINKED_ELSEWHERE)),
            ("another payer at 0.0 is not billing", [contact], [live_payer("other@x.org", {"k": 0.0})], "k",
             (bp.WOULD_CREATE, None)),
            ("a name is needed to create", [deal_contact(named=False)], [], None,
             (bp.FLAGGED, bp.NAME_INCOMPLETE)),
            ("a name is not needed to attach", [deal_contact(named=False)], [live_payer("ada@x.org")], None,
             (bp.WOULD_ATTACH, None)),
        ]
        for label, contacts, payers, child, expected in cases:
            with self.subTest(label):
                decision = bp.decide(contacts, payers, child)
                self.assertEqual((decision.status, decision.flag), expected)

    def test_the_match_count_and_id_are_carried(self):
        decision = bp.decide([deal_contact()], [live_payer("ada@x.org", pid="the-one")])
        self.assertEqual((decision.bill_payer_id, decision.matches), ("the-one", 1))
        decision = bp.decide([deal_contact()], [live_payer("ada@x.org", pid="a"), live_payer("ada@x.org", pid="b")])
        self.assertEqual((decision.bill_payer_id, decision.matches), (None, 2))

    def test_differences_name_the_fields_only(self):
        contact = deal_contact(props={"firstname": "Ada", "lastname": "Lovelace", "mobilephone": "07700 900123"})
        same = bp.Payer("p", "ada@x.org", {}, {"name": "ada  LOVELACE", "phoneNumbers": [{"value": "07700 900123"}]})
        other = bp.Payer("p", "ada@x.org", {}, {"name": "Someone Else", "phoneNumbers": [{"value": "+447700900999"}]})
        self.assertEqual(bp.differences(contact, same), [])
        self.assertEqual(bp.differences(contact, other), ["name", "phone"])
        # A field the scan did not select is not compared.
        self.assertEqual(bp.differences(contact, bp.Payer("p", "ada@x.org", {})), [])
        self.assertEqual(bp.differences(contact, bp.Payer("p", "ada@x.org", {}, {"name": {"fullName": "Someone Else"}})),
                         ["name"])


# --------------------------------------------------------------------------- #
# The plan, the hash, the gates
# --------------------------------------------------------------------------- #
class PlanTests(BPCase):
    def plan(self):
        return dry_runner.DryRun(self.hub, FamlyReads(self.famly, site_ids=[SITE_ID])).plan_deal(DEAL, {})

    def test_the_plan_carries_the_decision_the_matched_id_and_the_count(self):
        self.assertEqual(self.plan()["billPayer"],
                         {"status": "would_create", "flag": None, "billPayerId": None, "matches": 0})
        self.famly.payers.append(payer(self.email, pid="the-one"))
        self.assertEqual(self.plan()["billPayer"],
                         {"status": "would_attach", "flag": None, "billPayerId": "the-one", "matches": 1})

    def test_the_hash_covers_the_decision_and_changes_when_it_does(self):
        create = dry_runner.plan_hash(self.plan())
        self.famly.payers.append(payer(self.email, pid="the-one"))
        attach = dry_runner.plan_hash(self.plan())
        self.famly.payers.append(payer(self.email, pid="the-other"))
        ambiguous = dry_runner.plan_hash(self.plan())
        self.assertEqual(len({create, attach, ambiguous}), 3)

    def test_labels_and_differences_stay_out_of_the_hash(self):
        self.famly.payers.append(payer(self.email, pid="the-one"))
        plan = self.plan()
        base = dry_runner.plan_hash(plan)
        more = copy.deepcopy(plan)
        more["billPayer"]["differs"] = ["name", "phone"]
        more["contacts"][0]["labels"] = [{"typeId": 1, "label": "Anything", "direction": "x"}]
        self.assertEqual(dry_runner.plan_hash(more), base)
        other = copy.deepcopy(plan)
        other["billPayer"]["billPayerId"] = "different"
        self.assertNotEqual(dry_runner.plan_hash(other), base)

    def test_the_flag_off_leaves_the_plan_and_the_scan_alone(self):
        os.environ["ONBOARD_BILL_PAYER"] = "off"
        plan = self.plan()
        self.assertNotIn("billPayer", plan)
        self.assertEqual(self.famly.scans, 0)
        self.assertFalse(bp.enabled())

    def test_a_bill_payer_write_needs_gate_a_and_a_flag_does_not(self):
        base = {"child": {"status": "child_found"}, "contacts": [{"status": "present"}]}
        self.assertTrue(worker.famly_writes_planned({**base, "billPayer": {"status": "would_create"}}))
        self.assertTrue(worker.famly_writes_planned({**base, "billPayer": {"status": "would_attach"}}))
        self.assertFalse(worker.famly_writes_planned({**base, "billPayer": {"status": "present"}}))
        self.assertFalse(worker.famly_writes_planned({**base, "billPayer": {"status": "flagged"}}))
        self.assertFalse(worker.famly_writes_planned(base))

    def test_the_gate_message_shows_the_decision_without_personal_data(self):
        self.run_to_approval()
        text = self.notify.gates[0][0]
        self.assertIn("Bill payer: would be created in Famly", text)
        for value in PII:
            self.assertNotIn(value, text.casefold())

    def test_a_changed_bill_payer_decision_re_asks_under_the_existing_rules(self):
        self.run_to_approval()
        self.approve()
        self.famly.payers.append(payer(self.email, pid="appeared"))       # the situation changed
        self.assertEqual(self.run_worker()["status"], store.AWAITING_APPROVAL)
        self.assertEqual(len(self.notify.gates), 2)
        self.assertIn("would be linked", self.notify.gates[1][0])
        self.assertEqual(self.famly.bp_writes, [])


# --------------------------------------------------------------------------- #
# The write stage end to end
# --------------------------------------------------------------------------- #
class CreateTests(BPCase):
    def test_create_sends_the_exact_payload_and_records_only_the_id(self):
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.famly.ops(), [bpw.CREATE_OP])
        op, variables, text = self.famly.bp_writes[0]
        payload = variables["input"][0]
        self.assertEqual(set(payload), {"siteId", "name", "email", "children"})   # no externalId, no address
        self.assertEqual(payload["siteId"], SITE_ID)
        contact = self.hub.contacts["c1"]
        self.assertEqual(payload["name"], f"{contact['firstname']} {contact['lastname']}")
        self.assertEqual(payload["email"], self.email)
        self.assertEqual(payload["children"], [{"childId": CHILD, "share": {"multiplier": 1.0}}])
        self.assertIn("create(billPayers: $input) { billPayerId }", text)         # the id ONLY
        self.assertEqual(self.job().bill_payer_id, "bp-1")
        self.assertIn("bill_payer_created", self.final_notice())
        self.assertIn("Bill payer created.", self.final_notice())

    def test_no_sensitive_field_is_ever_in_a_document(self):
        self.run_through()
        for _op, _variables, text in self.famly.bp_writes:
            for name in SENSITIVE + ("externalId",):
                self.assertNotIn(name, text)
        plan = bpw.load_plan(self.famly)
        for text in (bpw.create_document(plan), bpw.add_children_document(plan)):
            for name in SENSITIVE:
                self.assertNotIn(name, text)

    def test_a_phone_the_normaliser_rejects_is_omitted_with_the_existing_warning(self):
        self.hub.contacts["c1"]["mobilephone"] = "not a number"
        self.run_through()
        payload = self.famly.bp_writes[0][1]["input"][0]
        self.assertFalse([k for k in payload if "phone" in k.lower()])
        self.assertIn("phone_unnormalised", self.final_notice() + self.hub.writes.__repr__())

    def test_a_phone_that_the_normaliser_accepts_is_sent_in_the_schemas_shape(self):
        info = {"name": "phoneNumbers", "is_list": True, "object": True, "type_field": True}
        self.assertEqual(bpw.phone_value(info, "+447700900123"),
                         [{"value": "+447700900123", "phoneType": "MOBILE"}])
        self.assertEqual(bpw.phone_value({"name": "phone", "is_list": False, "object": False,
                                          "type_field": False}, "+447700900123"), "+447700900123")

    def test_the_hubspot_status_is_bill_payer_linked_only_when_the_dropdown_offers_it(self):
        self.run_through()
        self.assertEqual(self.written()["famly_sync_status"], "contacts_linked")
        store._reset_for_tests()
        self.hub.writes.clear()
        self.famly.payers.clear()
        self.famly.children.clear()
        self.famly.child_creates.clear()
        self.hub.defs["famly_sync_status"]["options"] = [
            "child_ready", "contacts_linked", "needs_attention", "bill_payer_linked"]
        self.run_through()
        self.assertEqual(self.written()["famly_sync_status"], "bill_payer_linked")

    def test_a_status_the_dropdown_does_not_offer_is_never_written(self):
        self.hub.defs["famly_sync_status"]["options"] = ["child_ready", "contacts_linked", "needs_attention"]
        self.run_through()
        self.assertEqual(self.written()["famly_sync_status"], "contacts_linked")
        self.assertNotIn("value_not_offered", " ".join(self.notify.notices))


class AttachTests(BPCase):
    def test_attach_sends_one_add_children_for_the_new_child_and_never_edits_a_share(self):
        self.famly.payers.append(payer(f" {self.email.upper()} ", {"older-child": 1.0}, pid="the-one"))
        self.assertEqual(self.run_through()["status"], store.DONE)
        self.assertEqual(self.famly.ops(), [bpw.ADD_CHILDREN_OP])
        _op, variables, text = self.famly.bp_writes[0]
        self.assertEqual(variables, {"input": [{"childId": CHILD, "billPayerId": "the-one",
                                                "share": {"multiplier": 1.0}}]})
        self.assertIn("addChildren(shares: $input) { billPayerId childId share { multiplier } }", text)
        self.assertEqual(self.famly.payers[0]["children"], {"older-child": 1.0, CHILD: 1.0})   # older untouched
        self.assertEqual(self.job().bill_payer_id, "the-one")
        self.assertIn("Child linked to the existing bill payer.", self.final_notice())

    def test_two_matching_payers_flag_ambiguous_and_write_nothing(self):
        self.famly.payers += [payer(self.email, pid="a"), payer(self.email, pid="b")]
        result = self.run_through()
        self.assertEqual(self.famly.bp_writes, [])
        self.assertEqual((result["status"], self.codes()), (store.NEEDS_REVIEW, "bill_payer_ambiguous"))
        self.assertEqual(len(self.famly.child_creates), 1)                 # the child still went ahead
        self.assertEqual(len(self.famly.contact_creates), 2)               # and the contacts
        self.assertIsNone(self.job().bill_payer_id)

    def test_the_child_already_billed_by_another_payer_is_never_double_billed(self):
        self.famly.payers.append(payer("someone.else@x.org", {CHILD: 1.0}, pid="other"))
        result = self.run_through()
        self.assertEqual(self.famly.bp_writes, [])
        self.assertEqual((result["status"], self.codes()), (store.NEEDS_REVIEW, "bill_payer_child_linked_elsewhere"))

    def test_a_scan_that_fails_flags_and_writes_nothing_but_the_child_and_contacts_go_ahead(self):
        self.famly.scan_error = lambda n: GraphQLHTTPError("x", 500, "")
        result = self.run_through()
        self.assertEqual(self.famly.bp_writes, [])
        self.assertEqual((result["status"], self.codes()), (store.NEEDS_REVIEW, "bill_payer_scan_failed"))
        self.assertEqual(len(self.famly.child_creates), 1)
        self.assertEqual(len(self.famly.contact_creates), 2)

    def test_differences_are_a_to_check_line_and_never_by_value_and_do_not_need_review(self):
        os.environ["ONBOARD_BILL_PAYER_DETAIL_SELECTION"] = "name phone"
        self.famly.payers.append(payer(self.email, {}, pid="the-one",
                                       details={"name": "Zed Zebra", "phone": "+447700900999"}))
        result = self.run_through()
        self.assertEqual((result["status"], self.codes()), (store.DONE, ""))
        self.assertEqual(self.famly.ops(), [bpw.ADD_CHILDREN_OP])           # the link is still made
        notice = self.final_notice()
        self.assertIn("differs:name,phone", notice)
        self.assertIn("To check", notice)
        self.assertIn("has different details from HubSpot", notice)        # the plain sentence
        self.assertIn("bill_payer_differs", notice)                         # and the code
        self.assertNotIn("needs a person", notice)
        self.assertIn("different details", self.written()["famly_sync_gaps"])
        for value in ("zed zebra", "900999"):
            self.assertNotIn(value, self.all_output())
        self.assertEqual(self.famly.payers[0]["details"]["name"], "Zed Zebra")   # nothing overwritten


class SendOnceTests(BPCase):
    ERRORS = [
        ("timeout", lambda: requests.Timeout()),
        ("connection", lambda: requests.ConnectionError()),
        ("400", lambda: GraphQLHTTPError("x", 400, "{}")),
        ("403 with data", lambda: GraphQLHTTPError("x", 403, json.dumps({
            "errors": [{"message": "m"}], "data": {"billPayers": {"create": [{"billPayerId": "bp-1"}]}}}))),
        ("429", lambda: GraphQLHTTPError("x", 429, "")),
        ("500", lambda: GraphQLHTTPError("x", 500, "")),
        ("graphql errors", lambda: GraphQLError("x", [{"message": "m"}], body={"errors": []})),
    ]

    def test_create_is_sent_exactly_once_for_every_failure_type(self):
        for label, make in self.ERRORS:
            for when in ("create_error", "create_after_error"):
                with self.subTest(error=label, when=when):
                    store._reset_for_tests()
                    self.famly = BPFamly()
                    self.rest = FakeRest(self.famly)
                    self.hub.events = self.famly.events = self.events
                    self.hub.writes.clear()
                    self.deps = worker.Deps(self.hub, self.famly, self.rest, self.notify,
                                            famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]),
                                            sleep=self.sleeps.append)
                    setattr(self.famly, when, make())
                    self.run_through()
                    self.assertEqual(self.famly.ops().count(bpw.CREATE_OP), 1)       # never resent
                    self.assertNotIn(bpw.ADD_CHILDREN_OP, self.famly.ops())

    def test_add_children_is_sent_exactly_once_for_every_failure_type(self):
        for label, make in self.ERRORS:
            for when in ("add_error", "add_after_error"):
                with self.subTest(error=label, when=when):
                    store._reset_for_tests()
                    self.famly = BPFamly()
                    self.famly.payers.append(payer(self.email, {}, pid="the-one"))
                    self.rest = FakeRest(self.famly)
                    self.hub.events = self.famly.events = self.events
                    self.hub.writes.clear()
                    self.deps = worker.Deps(self.hub, self.famly, self.rest, self.notify,
                                            famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]),
                                            sleep=self.sleeps.append)
                    setattr(self.famly, when, make())
                    self.run_through()
                    self.assertEqual(self.famly.ops().count(bpw.ADD_CHILDREN_OP), 1)
                    self.assertNotIn(bpw.CREATE_OP, self.famly.ops())

    def test_a_403_with_data_is_an_unknown_outcome_settled_by_two_rescans_never_a_resend(self):
        # The create RAN (403 with data) but the first rescan cannot see it yet: the stage
        # waits 10 s, scans again, finds it, and calls it a success. It is never resent.
        self.famly.lag_scans = 1
        self.famly.create_after_error = self.ERRORS[3][1]()
        result = self.run_through()
        self.assertEqual(self.famly.ops(), [bpw.CREATE_OP])
        self.assertEqual(self.sleeps, [10])
        self.assertEqual(result["status"], store.DONE)
        self.assertIn("bill_payer_created", self.final_notice())
        self.assertEqual(self.job().bill_payer_id, "bp-1")

    def test_a_clean_create_is_read_back_again_while_the_scan_lags(self):
        self.famly.lag_scans = 2                  # not visible to the first two read-back scans
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.famly.ops(), [bpw.CREATE_OP])           # one create, never resent
        self.assertIn("bill_payer_created", self.final_notice())

    def test_a_clean_create_that_never_shows_is_a_readback_mismatch_after_five_reads(self):
        self.famly.lag_scans = 99
        self.run_through()
        self.assertEqual(self.famly.ops(), [bpw.CREATE_OP])
        self.assertIn("bill_payer_readback_mismatch", self.codes())

    def test_a_clean_attach_is_read_back_again_while_the_scan_lags(self):
        self.famly.payers.append(payer(self.email, {}, pid="the-one"))
        self.famly.lag_scans = 2
        result = self.run_through()
        self.assertEqual(self.famly.ops(), [bpw.ADD_CHILDREN_OP])
        self.assertEqual(result["status"], store.DONE)

    def test_a_partial_response_that_cannot_be_verified_is_never_called_a_success(self):
        self.famly.lag_scans = 99                                    # never visible to the rescans
        self.famly.create_after_error = self.ERRORS[3][1]()
        result = self.run_through()
        self.assertEqual(self.famly.ops(), [bpw.CREATE_OP])
        self.assertEqual(self.sleeps, [10])
        self.assertEqual((result["status"], self.codes()), (store.NEEDS_REVIEW, "bill_payer_write_rejected"))

    def test_a_rejection_with_nothing_created_is_write_rejected_and_a_5xx_is_unknown(self):
        self.famly.create_error = GraphQLHTTPError("x", 400, "{}")
        self.run_through()
        self.assertEqual(self.codes(), "bill_payer_write_rejected")
        self.assertEqual(self.famly.scan_pages >= 4, True)           # settled by scans, not by guessing
        store._reset_for_tests()
        self.famly = BPFamly()
        self.famly.create_error = GraphQLHTTPError("x", 500, "")
        self.rest = FakeRest(self.famly)
        self.hub.events = self.famly.events = self.events
        self.deps = worker.Deps(self.hub, self.famly, self.rest, self.notify,
                                famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]),
                                sleep=self.sleeps.append)
        self.hub.writes.clear()
        self.run_through()
        self.assertEqual(self.codes(), "bill_payer_outcome_unknown")

    def test_a_clean_response_that_does_not_read_back_is_a_mismatch(self):
        self.famly.stored_multiplier = 0.5
        result = self.run_through()
        self.assertEqual((result["status"], self.codes()), (store.NEEDS_REVIEW, "bill_payer_readback_mismatch"))
        self.assertEqual(self.famly.ops(), [bpw.CREATE_OP])


class PlanChangedTests(BPCase):
    def test_a_pre_write_rescan_that_changes_the_decision_writes_nothing(self):
        self.run_to_approval()
        self.approve()
        # Run 2: the plan scan is scan 2; a matching payer appears right after it answers,
        # so the stage's own rescan (scan 3) sees something the approval did not.
        self.famly.after_scan[2] = lambda: self.famly.payers.append(payer(self.email, pid="appeared"))
        result = self.run_worker()
        self.assertEqual(self.famly.bp_writes, [])
        self.assertEqual((result["status"], self.codes()), (store.NEEDS_REVIEW, "bill_payer_plan_changed"))
        self.assertEqual(len(self.famly.child_creates), 1)                 # the child stands


class SwitchTests(BPCase):
    def test_dry_mode_shows_the_decision_and_writes_nothing(self):
        self.mode("dry")
        self.enqueue()
        result = self.run_worker()
        self.assertEqual(result["status"], store.DRY_DONE)
        self.assertEqual(self.famly.bp_writes, [])
        self.assertEqual(self.famly.child_creates, [])
        self.assertTrue(any("Bill payer: would be created in Famly" in n for n in self.notify.notices))

    def test_the_flag_off_in_live_mode_performs_no_bill_payer_read_or_write(self):
        os.environ["ONBOARD_BILL_PAYER"] = "off"
        result = self.run_through()
        self.assertEqual(result["status"], store.DONE)
        self.assertEqual(self.famly.scans, 0)
        self.assertEqual(self.famly.bp_writes, [])
        self.assertIsNone(self.job().bill_payer_id)

    def test_the_flag_unset_is_off(self):
        del os.environ["ONBOARD_BILL_PAYER"]
        self.assertFalse(bp.enabled())
        self.assertEqual(self.run_through()["status"], store.DONE)
        self.assertEqual(self.famly.bp_writes, [])


# --------------------------------------------------------------------------- #
# What may be sent, and what may be kept
# --------------------------------------------------------------------------- #
class NoDestructiveMutationTests(BPCase):
    def test_the_stage_guard_refuses_anything_but_create_and_add_children(self):
        plan = bpw.load_plan(self.famly)
        bpw.assert_stage_mutation(bpw.create_document(plan), "create")
        bpw.assert_stage_mutation(bpw.add_children_document(plan), "addChildren")
        for bad, kind in (
                ("mutation OnboardBillPayerCreate($i: X) { billPayers { create(a: $i) { billPayerId } delete(a: $i) } }", "create"),
                ("mutation OnboardBillPayerCreate($i: X) { billPayers { create(a: $i) { billPayerId archive } } }", "create"),
                ("mutation OnboardBillPayerAddChildren($i: X) { billPayers { addChildren(a: $i) { x } update(a: 1) } }", "addChildren"),
                ("mutation OnboardBillPayerAddChildren($i: X) { billPayers { deleteChildren(a: $i) { x } } }", "addChildren"),
                ("mutation OnboardBillPayerCreate($i: X, $s: Y) { billPayers { create(a: $i, siteIds: $s) { billPayerId } } }", "create"),
                ("mutation OnboardBillPayerCreate($i: X) { billPayers { create(a: $i) { billPayerId socialSecurityNumber } } }", "create"),
        ):
            with self.subTest(bad[:60]):
                with self.assertRaises(AssertionError):
                    bpw.assert_stage_mutation(bad, kind)

    def test_no_string_in_the_stage_code_names_a_destructive_mutation(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("actions/onboard/bill_payer_writes.py", "actions/onboard_dry_run/bill_payers.py"):
            tree = ast.parse((root / relative).read_text(encoding="utf-8"))
            docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                          and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    text = node.value.lower()
                    if text.startswith("mutation ") or "billpayers" in text:
                        self.assertNotRegex(text, r"delete|archive|update\(")

    def test_across_every_scenario_only_create_and_add_children_were_ever_sent(self):
        for setup in ("create", "attach", "fail"):
            store._reset_for_tests()
            self.famly = BPFamly()
            if setup == "attach":
                self.famly.payers.append(payer(self.email, {}, pid="the-one"))
            if setup == "fail":
                self.famly.create_error = GraphQLHTTPError("x", 403, "{}")
            self.rest = FakeRest(self.famly)
            self.hub.events = self.famly.events = self.events
            self.hub.writes.clear()
            self.deps = worker.Deps(self.hub, self.famly, self.rest, self.notify,
                                    famly_factory=lambda: FamlyReads(self.famly, site_ids=[SITE_ID]),
                                    sleep=self.sleeps.append)
            self.run_through()
            self.assertTrue(set(self.famly.ops()) <= {bpw.CREATE_OP, bpw.ADD_CHILDREN_OP})
            self.assertFalse([p for p in self.famly.payers if p["deleted"]])      # nothing archived


class PrivacyTests(BPCase):
    def test_no_personal_data_in_logs_slack_state_or_the_jobs_table(self):
        os.environ["ONBOARD_BILL_PAYER_DETAIL_SELECTION"] = "name phone"
        self.famly.payers.append(payer(self.email, {}, pid="the-one",
                                       details={"name": "Zed Zebra", "phone": "+447700900999"}))
        self.run_through()
        blob = self.all_output()
        for value in PII + ["zed zebra", "900999"]:
            self.assertNotIn(value.casefold(), blob, value)
        self.assertEqual(self.job().bill_payer_id, "the-one")               # the id is the only thing kept
        with contextlib.closing(sqlite3.connect(store.store_path())) as connection:
            row = connection.execute("SELECT * FROM onboard_jobs").fetchone()
        self.assertNotIn(self.email, json.dumps([str(x) for x in row]).casefold())

    def test_messages_cover_every_bill_payer_code(self):
        for code in list(bp.ALL_CODES) + [bp.NAME_INCOMPLETE]:
            with self.subTest(code=code):
                found = messages.lookup(code)
                self.assertIsNotNone(found)
                if found[0] != messages.INFO:
                    self.assertTrue(found[1].endswith("."))
                    self.assertNotIn("_", found[1])
        for code in bp.SUCCESS_CODES:
            self.assertEqual(messages.lookup(code)[0], messages.INFO)


class MigrationTests(BPCase):
    NEW = ("source", "approval_nonce", "reask_count", "plan_sections", "bill_payer_id")

    def test_an_old_store_gets_the_bill_payer_column_once(self):
        path = store.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE onboard_jobs (id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, status TEXT NOT NULL, "
                "mode TEXT NOT NULL, plan_hash TEXT, approved_hash TEXT, child_id TEXT, "
                "attempts INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, approval_ref TEXT, "
                "child_create_sent INTEGER NOT NULL DEFAULT 0, outcomes TEXT, claimed_at TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        for _ in range(3):
            store.get("nothing")
        with contextlib.closing(sqlite3.connect(path)) as connection:
            columns = [r[1] for r in connection.execute("PRAGMA table_info(onboard_jobs)")]
        for name in self.NEW:
            self.assertEqual(columns.count(name), 1, name)

    def test_a_concurrent_duplicate_column_error_is_tolerated_for_the_new_column(self):
        path = store.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        real = sqlite3.connect(path)
        real.row_factory = sqlite3.Row
        real.execute("CREATE TABLE onboard_jobs (id TEXT PRIMARY KEY, deal_id TEXT)")
        real.commit()

        class Racing:
            """Another process adds each column between our check and our ALTER."""
            def execute(self_inner, sql, *args):
                if sql.startswith("ALTER TABLE"):
                    real.execute(sql)
                    real.commit()
                return real.execute(sql, *args)
        try:
            store._migrate(Racing())                              # must not raise
        finally:
            real.close()
        with contextlib.closing(sqlite3.connect(path)) as connection:
            columns = [r[1] for r in connection.execute("PRAGMA table_info(onboard_jobs)")]
        self.assertEqual(columns.count("bill_payer_id"), 1)

    def test_the_jobs_table_has_no_column_for_a_bill_payer_name_email_or_phone(self):
        self.enqueue()
        with contextlib.closing(sqlite3.connect(store.store_path())) as connection:
            columns = [r[1] for r in connection.execute("PRAGMA table_info(onboard_jobs)")]
        self.assertIn("bill_payer_id", columns)
        self.assertFalse([c for c in columns if any(w in c for w in ("email", "name", "phone", "address"))])


if __name__ == "__main__":
    unittest.main()


class GateACountTests(BPCase):
    """The Gate A "writes planned" count equals the writes the worker will make."""

    def text(self, child_status, contact_statuses, bill_payer):
        from actions.onboard import notifier
        plan = {"dealId": "1", "siteCode": "S", "groupTitle": "G",
                "child": {"status": child_status},
                "contacts": [{"status": st, "isBillPayer": i == 0} for i, st in enumerate(contact_statuses)],
                "dates": {}, "warnings": []}
        if bill_payer is not None:
            plan["billPayer"] = {"status": bill_payer}
        return notifier.gate_text(plan, "h" * 64, "live")

    def test_create(self):
        self.assertIn("Famly writes planned: 1 child, 2 contact(s), 1 bill payer",
                      self.text("child_would_create", ["would_create"] * 2, "would_create"))

    def test_attach_counts_one_write(self):
        self.assertIn("Famly writes planned: 1 child, 2 contact(s), 1 bill payer",
                      self.text("child_would_create", ["would_create"] * 2, "would_attach"))

    def test_flagged_and_present_count_zero(self):
        for status in ("flagged", "present"):
            self.assertIn("Famly writes planned: 1 child, 2 contact(s), 0 bill payer",
                          self.text("child_would_create", ["would_create"] * 2, status))

    def test_an_existing_child_counts_zero_child_writes(self):
        self.assertIn("Famly writes planned: 0 child, 0 contact(s), 1 bill payer",
                      self.text("child_found", ["present"], "would_create"))

    def test_no_bill_payer_section_leaves_the_line_as_before(self):
        self.assertIn("Famly writes planned: 1 child, 1 contact(s)\n",
                      self.text("child_would_create", ["would_create"], None))

    def test_the_real_message_for_a_new_deal_counts_the_payer(self):
        self.run_to_approval()
        self.assertIn("Famly writes planned: 1 child, 2 contact(s), 1 bill payer", self.notify.gates[-1][0])


class UnconfirmedPayerGuardTests(BPCase):
    """After bill_payer_readback_mismatch / bill_payer_outcome_unknown a re-run must not
    write the payer again while the scan does not show it."""

    def creates(self):
        return self.famly.ops().count(bpw.CREATE_OP)

    def test_unresolved_outcome_and_a_lagging_scan_stops_without_a_write(self):
        self.famly.lag_scans = 999                          # the payer never shows
        self.run_through()
        self.assertIn("bill_payer_readback_mismatch", self.codes())
        self.assertEqual(self.creates(), 1)
        result = self.run_through()                         # the re-run
        self.assertEqual((result["status"], self.codes()), (store.NEEDS_REVIEW, "bill_payer_create_unconfirmed"))
        self.assertEqual(self.creates(), 1)                 # no second create
        self.assertIn("Check the child's bill payer in Famly", self.final_notice())

    def test_unresolved_outcome_and_a_scan_that_shows_the_payer_proceeds_without_a_write(self):
        self.famly.lag_scans = 999
        self.run_through()
        self.famly.payers[0]["visible_at"] = 0               # the scan has caught up
        self.enqueue()
        result = self.run_worker()                           # nothing to write: no Gate A
        self.assertEqual(self.creates(), 1)
        self.assertEqual(result["status"], store.DONE)
        self.assertIn("bill_payer_present", self.final_notice())

    def test_a_clean_create_earlier_is_not_blocked(self):
        def reject_ben(payload):
            if payload["email"] == "ben.b@example.org":
                raise GraphQLHTTPError("bad", 400, "{}")
        self.famly.contact_effect = reject_ben
        self.run_through()                                   # payer created cleanly, a contact failed
        self.assertEqual(self.creates(), 1)
        self.famly.payers.clear()                            # removed in Famly by hand
        self.famly.contact_effect = None
        self.run_through()
        self.assertEqual(self.creates(), 2)

    def test_a_first_run_is_unchanged(self):
        self.assertEqual(self.run_through()["status"], store.DONE)
        self.assertEqual(self.creates(), 1)
