"""ONE-OFF WRITE PROBE: create ONE test bill payer in Famly, measure how fast it can
be read back, then delete it. Like probe-child-create and probe-contact-create it is
an explicit, narrow exception to "never call a write against Famly outside tests".

What it may send, and nothing else:
  * ONE `billPayers.create` and ONE `billPayers.delete` (the delete names the bill
    payer id only; an argument that would act on a whole site is never declared);
  * with --create-test-child: ONE test child create (`isTestChild` true) and ONE
    delete of that child by `childIds` only, through the existing, guarded
    `ProbeSession` in actions/probe_child_create (never `siteIds`).
Every mutation is sent ONCE and NEVER retried, including on HTTP 400 "Transient".
A rejected request is printed and the probe stops; no variants are tried.

Guards, all before the first request: --confirm, --site, --test-email, and exactly
one of --child-id (which must be a verified test child) or --create-test-child.
The probe scans the site first and aborts if the test email or the probe externalId
already exists.

The create and delete documents are BUILT FROM THE LIVE SCHEMA (argument names and
types are not recorded in the repo), checked by `assert_probe_mutation`, and
anything that cannot be filled in (an unknown required input field) stops the probe
before any write and is printed.

PRIVACY: the test email is held in memory only. It is redacted from every printed
line (an error body may echo it), never logged, never written to a file or the jobs
DB. The bill payer id and the child id are printed to the TERMINAL ONLY, so a failed
step can be cleaned up by hand; they are never written anywhere.
"""

import json
import re
import tempfile
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from actions.onboard_dry_run import derive
from actions.onboard_dry_run.famly_reads import normalise_email
from actions.probe_child_create import runner as child_probe
from core.client import GraphQLError, GraphQLHTTPError
from core.rest_client import RestHTTPError
from integrations import catalogue

from . import runner

PROBE_NAME = "ZZ Probe Test"
CREATE_OP = "ProbeBillPayerCreate"
DELETE_OP = "ProbeBillPayerDelete"
MAX_BILL_PAYER_CREATES = 1
MAX_BILL_PAYER_DELETES = 1
SCAN_OFFSETS = (0, 10, 60)          # seconds after the create
TEST_CHILD_NAME = "AAProbe BillPayerChild"
TEST_CHILD_BIRTHDAY = date(2025, 6, 1)


class ProbeRefused(Exception):
    """A guard failed. Nothing further was written."""


# --------------------------------------------------------------------------- #
# Guards on what may be sent
# --------------------------------------------------------------------------- #
def assert_probe_mutation(text: str, kind: str) -> None:
    """`kind` is "create" or "delete". The document must be exactly one probe
    mutation on `billPayers`, with no site-wide argument and nothing else."""
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).lower()
    expected = CREATE_OP if kind == "create" else DELETE_OP
    if code.count("mutation") != 1 or f"mutation {expected.lower()}(" not in code:
        raise ProbeRefused(f"refusing: not a single {expected} mutation")
    if "billpayers {" not in code or f"{kind}(" not in code:
        raise ProbeRefused(f"refusing: the document is not billPayers.{kind}")
    for forbidden in ("siteids", "update(", "addchildren(") + (("create(",) if kind == "delete" else ("delete(",)):
        if forbidden in code:
            raise ProbeRefused(f"refusing: the document contains {forbidden!r}")


class Budget:
    """The write caps. `spend` raises before a request that would exceed one."""

    CAPS = {"billpayer_create": MAX_BILL_PAYER_CREATES, "billpayer_delete": MAX_BILL_PAYER_DELETES}

    def __init__(self):
        self.used: dict[str, int] = {}

    def spend(self, what: str) -> None:
        if self.used.get(what, 0) >= self.CAPS[what]:
            raise ProbeRefused(f"cap reached: at most {self.CAPS[what]} {what.replace('_', ' ')}")
        self.used[what] = self.used.get(what, 0) + 1


# --------------------------------------------------------------------------- #
# Working out the mutations from the live schema
# --------------------------------------------------------------------------- #
@dataclass
class WritePlan:
    create_arg: str = ""
    create_arg_type: str = ""
    create_select: str = ""
    delete_arg: str | None = None
    delete_arg_type: str | None = None
    delete_is_list: bool = False
    delete_select: str = ""
    delete_unusable: str | None = None
    site_field: str = "siteId"
    name_field: str = "name"
    email_field: str | None = "email"
    external_field: str | None = None
    children_field: str | None = None
    child_id_field: str | None = None
    share_field: str | None = None
    share_required: bool = False
    share_hint: str = ""
    share_is_object: bool = False
    share_wrap_key: str | None = None
    blockers: list[str] = field(default_factory=list)


def _required(arg) -> bool:
    return (arg.get("type") or {}).get("kind") == "NON_NULL" and arg.get("defaultValue") is None


def plan_from_schema(body: dict) -> WritePlan:
    types, roots = runner._types(body)
    by_name = {t.get("name"): t for t in types}
    schema = ((body or {}).get("data") or {}).get("__schema") or {}
    plan = WritePlan()

    mutation_root = by_name.get((schema.get("mutationType") or {}).get("name") or "") or {}
    bp_field = next((f for f in mutation_root.get("fields") or [] if f["name"] == "billPayers"), None)
    bp_mutation = by_name.get(runner.named_type(bp_field["type"]) if bp_field else "") or {}
    mut_fields = {f["name"]: f for f in bp_mutation.get("fields") or []}

    # ---- create ---------------------------------------------------------------
    create = mut_fields.get("create")
    if create is None:
        plan.blockers.append("the schema has no billPayers.create")
    else:
        main = [a for a in create.get("args") or [] if runner.named_type(a["type"]) == "BillPayerInput"]
        others = [a["name"] for a in create.get("args") or [] if a not in main and _required(a)]
        if len(main) != 1:
            plan.blockers.append("billPayers.create has no single BillPayerInput argument")
        else:
            plan.create_arg, plan.create_arg_type = main[0]["name"], runner.type_str(main[0]["type"])
        if others:
            plan.blockers.append(f"billPayers.create has other required arguments: {', '.join(others)}")
        ret = by_name.get(runner.named_type(create["type"]) or "") or {}
        if ret.get("kind") == "OBJECT" and any(f["name"] == "billPayerId" for f in ret.get("fields") or []):
            plan.create_select = " { billPayerId }"
        elif ret.get("kind") == "OBJECT":
            plan.blockers.append("billPayers.create returns an object with no billPayerId")

    # ---- delete ---------------------------------------------------------------
    delete = mut_fields.get("delete")
    if delete is None:
        plan.delete_unusable = "the schema has no billPayers.delete"
    else:
        id_args = [a for a in delete.get("args") or []
                   if "id" in a["name"].lower() and "site" not in a["name"].lower()]
        required_site = [a["name"] for a in delete.get("args") or []
                         if "site" in a["name"].lower() and _required(a)]
        required_other = [a["name"] for a in delete.get("args") or []
                          if a not in id_args and "site" not in a["name"].lower() and _required(a)]
        if len(id_args) != 1:
            plan.delete_unusable = "billPayers.delete has no single bill payer id argument"
        elif required_site or required_other:
            plan.delete_unusable = ("billPayers.delete requires other arguments: "
                                    + ", ".join(required_site + required_other))
        else:
            arg = id_args[0]
            plan.delete_arg, plan.delete_arg_type = arg["name"], runner.type_str(arg["type"])
            plan.delete_is_list = "[" in plan.delete_arg_type
        ret = by_name.get(runner.named_type(delete["type"]) or "") or {}
        if ret.get("kind") == "OBJECT":
            plan.delete_select = " { billPayerId }"

    # ---- the input ------------------------------------------------------------
    bp_input = by_name.get("BillPayerInput") or {}
    fields = {f["name"]: f for f in bp_input.get("inputFields") or []}
    if not fields:
        plan.blockers.append("BillPayerInput not found")
        return plan
    plan.email_field = "email" if "email" in fields else None
    plan.external_field = "externalId" if "externalId" in fields else None
    if plan.external_field is None:
        plan.blockers.append("BillPayerInput has no externalId, so the probe cannot be tagged")
    for name in ("siteId", "name"):
        if name not in fields:
            plan.blockers.append(f"BillPayerInput has no {name}")

    child_input_name = None
    for name, f in fields.items():
        if runner.named_type(f["type"]) in ("BillPayerChildInput", "ChildShareInput"):
            plan.children_field, child_input_name = name, runner.named_type(f["type"])
    if plan.children_field is None:
        plan.blockers.append("BillPayerInput has no children field")
    else:
        child_fields = {f["name"]: f for f in (by_name.get(child_input_name) or {}).get("inputFields") or []}
        plan.child_id_field = "childId" if "childId" in child_fields else None
        if plan.child_id_field is None:
            plan.blockers.append(f"{child_input_name} has no childId")
        for name, f in child_fields.items():
            if name != plan.child_id_field:
                plan.share_field, plan.share_required = name, _required(f)
                plan.share_hint = (f"{name}: {runner.type_str(f['type'])}"
                                   + (f" = {f['defaultValue']}" if f.get("defaultValue") is not None else ""))
                share_type = by_name.get(runner.named_type(f["type"]) or "") or {}
                if share_type.get("kind") == "INPUT_OBJECT":
                    plan.share_is_object = True
                    inner = share_type.get("inputFields") or []
                    plan.share_wrap_key = inner[0]["name"] if len(inner) == 1 else None

    handled = {"siteId", "name", "email", "externalId", plan.children_field}
    unfillable = [n for n, f in fields.items() if n not in handled and _required(f)]
    if unfillable:
        plan.blockers.append(f"BillPayerInput has other required fields: {', '.join(unfillable)}")
    return plan


def create_document(plan: WritePlan) -> str:
    return (f"mutation {CREATE_OP}($input: {plan.create_arg_type}) {{ billPayers "
            f"{{ create({plan.create_arg}: $input){plan.create_select} }} }}\n")


def delete_document(plan: WritePlan) -> str:
    return (f"mutation {DELETE_OP}($id: {plan.delete_arg_type}) {{ billPayers "
            f"{{ delete({plan.delete_arg}: $id){plan.delete_select} }} }}\n")


def build_share(plan: WritePlan, raw):
    """The share value to send. `--share 1.0` is wrapped into the share input's own
    single field (`{"multiplier": 1.0}`); a full JSON object is sent as given."""
    if raw is None or not plan.share_field:
        return None
    if plan.share_is_object:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and plan.share_wrap_key:
            return {plan.share_wrap_key: float(raw)}
        raise ProbeRefused("refusing: --share must be a number, or a full JSON object for "
                           f"the share input ({plan.share_hint})")
    if isinstance(raw, dict):
        raise ProbeRefused(f"refusing: the share field is not an object ({plan.share_hint})")
    return raw


def make_input(plan: WritePlan, site_id: str, email: str, external_id: str, child_id: str, share) -> dict:
    payload = {plan.site_field: site_id, plan.name_field: PROBE_NAME,
               plan.external_field: external_id}
    if plan.email_field:
        payload[plan.email_field] = email
    child = {plan.child_id_field: child_id}
    if plan.share_field and share is not None:
        child[plan.share_field] = share
    payload[plan.children_field] = [child]
    return payload


# --------------------------------------------------------------------------- #
# Scanning for the probe bill payer (in memory; nothing is printed but booleans)
# --------------------------------------------------------------------------- #
def probe_scan(client, site_id, shape, include_deleted, email_key, external_id, child_id) -> dict:
    """One full scan with `includeDeleted` passed EXPLICITLY (a bool, never omitted)."""
    if not isinstance(include_deleted, bool):
        raise ProbeRefused("refusing: a scan must pass includeDeleted explicitly")
    text = runner.build_query(shape, True)
    handle = tempfile.NamedTemporaryFile("w", suffix=".graphql", delete=False, encoding="utf-8")
    try:
        handle.write(text)
        handle.close()
        path = Path(handle.name)
        runner.assert_read_only(path)
        by_email = by_ext = 0
        attached = None
        found_id = None
        cursor, pages = None, 0
        while pages < runner.MAX_PAGES:
            variables = {"siteIds": [site_id], "next": cursor, "includeDeleted": include_deleted}
            body = client.execute(query_path=path, variables=variables,
                                  operation_name=runner.BILL_PAYERS_OP)
            pages += 1
            listing = (((body or {}).get("data") or {}).get("billPayers") or {}).get("listBySiteIds") or {}
            for node in listing.get("result") or []:
                if not isinstance(node, dict):
                    continue
                is_email = bool(email_key) and normalise_email(node.get("email")) == email_key
                is_ext = str(node.get("externalId") or "").strip() == external_id
                by_email += is_email
                by_ext += is_ext
                if is_email or is_ext:
                    found_id = found_id or str(node.get("billPayerId"))
                if is_ext and shape["children_field"]:
                    attached = child_id in runner._child_ids(node, shape)
            cursor = listing.get("next") or None
            if not cursor:
                break
        return {"byEmail": by_email, "byExternalId": by_ext, "attached": attached,
                "id": found_id}
    finally:
        os.unlink(handle.name)


def _yes(value) -> str:
    return "unknown" if value is None else "yes" if value else "no"


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
@dataclass
class WriteResult:
    bill_payer_created: bool = False
    bill_payer_deleted: bool = False
    test_child_created: bool = False
    test_child_deleted: bool = False
    stopped: str | None = None


class Probe:
    def __init__(self, public, rest, out: Callable[[str], None] = print,
                 clock=None, sleep=None, utcnow=None):
        import time
        self.public, self.rest, self.out = public, rest, out
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.budget = Budget()
        self.result = WriteResult()
        self._email = ""

    # -- output that can never carry the test email ---------------------------- #
    def say(self, text: str) -> None:
        if self._email:
            text = re.sub(re.escape(self._email), "<test-email>", text, flags=re.IGNORECASE)
        self.out(text)

    def _error(self, exc) -> str:
        return child_probe.error_text(exc)

    # -- one send, never retried ---------------------------------------------- #
    def _send(self, text: str, variables: dict, operation: str, kind: str):
        assert_probe_mutation(text, kind)
        handle = tempfile.NamedTemporaryFile("w", suffix=".graphql", delete=False, encoding="utf-8")
        try:
            handle.write(text)
            handle.close()
            return self.public.execute(query_path=Path(handle.name), variables=variables,
                                       operation_name=operation)
        finally:
            os.unlink(handle.name)

    def run(self, site_code: str, test_email: str, child_id: str | None, create_test_child: bool,
            share, confirm: bool) -> WriteResult:
        self._email = (test_email or "").strip()
        # ---- guards: all before the first request -------------------------------
        if not confirm:
            raise ProbeRefused("refusing without --confirm; nothing was written")
        if not site_code or not self._email or "@" not in self._email:
            raise ProbeRefused("refusing: --site and a --test-email are required")
        if bool(child_id) == bool(create_test_child):
            raise ProbeRefused("refusing: give exactly one of --child-id or --create-test-child")
        site_code = site_code.strip().upper()
        entry = catalogue.load_sites().get(site_code)
        site_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if not site_id:
            raise ProbeRefused(f"refusing: no institutionId for site {site_code}")

        if child_id:
            # Verified BEFORE anything is scanned or written: only a test child is used.
            try:
                summary = self.rest.get(f"/v2/children/{child_id}/summary")
            except RestHTTPError as exc:
                raise ProbeRefused(f"could not verify the child (HTTP {exc.status_code})") from None
            if not child_probe.is_test_child(summary):
                raise ProbeRefused("refusing: that child is not a test child (isTestChild is not true)")

        self.say(f"CAPS: at most {MAX_BILL_PAYER_CREATES} billPayers.create and "
                 f"{MAX_BILL_PAYER_DELETES} billPayers.delete"
                 + ("; one test child created and deleted" if create_test_child else ""))
        self.say("Mutations are sent once and never retried, including on a 400 Transient.")

        # ---- read: the schema, then the site ------------------------------------
        schema_body = runner.fetch_schema(self.public)
        plan = plan_from_schema(schema_body)
        shape = runner.discover_shape(schema_body)
        try:
            runner.require_include_deleted(shape)
        except runner.AuditRefused as exc:
            raise ProbeRefused(str(exc)) from None
        if plan.blockers:
            for blocker in plan.blockers:
                self.say(f"STOPPED before any write: {blocker}")
            self.result.stopped = "schema"
            return self.result
        if plan.share_required and share is None:
            self.say(f"STOPPED before any write: {plan.share_hint} is required; "
                     "give --share with the format found in the schema report")
            self.result.stopped = "share"
            return self.result
        share = build_share(plan, share)        # a bad --share refuses before any write
        self.say(f"children input: {plan.children_field}[{plan.child_id_field}"
                 + (f", {plan.share_hint}" if plan.share_field else "") + "]")
        self.say("delete: " + (f"{plan.delete_arg}: {plan.delete_arg_type}" if plan.delete_arg
                               else f"UNAVAILABLE ({plan.delete_unusable})"))

        include_deleted = False        # the live view, as the pipeline will scan
        scan_flag = True               # the pre-scan also sees deleted twins
        stamp = self.utcnow().strftime("%Y%m%dT%H%M%SZ")
        external_id = f"probe-{stamp}"
        email_key = normalise_email(self._email)

        before = probe_scan(self.public, site_id, shape, scan_flag, email_key, external_id, "")
        if before["byEmail"] or before["byExternalId"]:
            self.say("STOPPED: the test email or the probe externalId already exists at this site; "
                     "nothing was written")
            self.result.stopped = "exists"
            return self.result
        self.say("PRE-SCAN: test email and probe externalId are absent")

        # ---- the test child ------------------------------------------------------
        session = None
        bill_payer_id = None
        try:
            if create_test_child:
                session = child_probe.ProbeSession(self.rest, self.public, None, self.say)
                group_id = derive.group_id_for(site_code, "Hatchlings")
                spec = child_probe.ChildSpec(
                    "BP", TEST_CHILD_NAME, TEST_CHILD_BIRTHDAY, f"AAPROBE-BP-{stamp}",
                    f"AAPROBE-BP-DEAL-{stamp}", "2027-01-04", "2027-12-31")
                payload = child_probe.make_child_input(spec, site_id, group_id, "%Y-%m-%d")
                child_id = session.create_child(payload, "test child")
                if not child_id:
                    self.say("STOPPED: the test child was rejected; nothing else was written")
                    self.result.stopped = "child"
                    return self.result
                self.result.test_child_created = True
                self.say(f"TEST CHILD id (terminal only): {child_id}")

            bill_payer_id = self._create_and_measure(plan, shape, site_id, email_key, external_id,
                                                     child_id, share, include_deleted)
        except ProbeRefused as exc:
            self.say(f"STOPPED: {exc}")
            self.result.stopped = self.result.stopped or "refused"
        except Exception as exc:  # noqa: BLE001 - the clean-up below must still run
            self.say(f"ERROR ({type(exc).__name__}); going on to the clean-up")
        finally:
            self._cleanup(plan, shape, site_id, email_key, external_id, child_id, bill_payer_id,
                          include_deleted, session)
        self.say("=== DONE. Ids for a manual clean-up are above (terminal only; nothing was stored) ===")
        return self.result

    # -- create the bill payer, then measure read-after-write lag ----------------- #
    def _create_and_measure(self, plan, shape, site_id, email_key, external_id, child_id, share,
                            include_deleted):
        payload = make_input(plan, site_id, self._email, external_id, child_id, share)
        variables = {"input": [payload] if "[" in plan.create_arg_type else payload}
        # The exact variable about to be sent (the test email is redacted by `say`).
        self.say("VARIABLES TO SEND: " + json.dumps(variables))
        self.budget.spend("billpayer_create")
        self.say(f"SENDING billPayers.create (externalId {external_id}) ...")
        started = self.clock()
        bill_payer_id = None
        try:
            body = self._send(create_document(plan), variables, CREATE_OP, "create")
        except (GraphQLError, GraphQLHTTPError) as exc:
            self.say(f"REJECTED (sent once, not retried, no variants tried): {self._error(exc)}")
            self.result.stopped = "rejected"
            return None
        except (requests.RequestException, TimeoutError, ConnectionError) as exc:
            self.say(f"UNKNOWN OUTCOME ({type(exc).__name__}); not resent; settling with a scan")
            body = None
        else:
            self.say(f"RAW RESPONSE: {json.dumps(body)}")
            bill_payer_id = self._created_id(body)
            if bill_payer_id:
                self.say(f"BILL PAYER id (terminal only): {bill_payer_id}")
            self.result.bill_payer_created = True

        # Read-after-write lag: immediately, after 10 s and after 60 s.
        for offset in SCAN_OFFSETS:
            wait = offset - (self.clock() - started)
            if wait > 0:
                self.sleep(wait)
            found = probe_scan(self.public, site_id, shape, include_deleted, email_key,
                               external_id, child_id)
            self.say(f"T+{offset:>2}s scan: found by email={_yes(found['byEmail'] > 0)} "
                     f"(count {found['byEmail']}), found by externalId={_yes(found['byExternalId'] > 0)} "
                     f"(count {found['byExternalId']}), child attached={_yes(found['attached'])}")
            if found["id"] and not bill_payer_id:
                bill_payer_id = found["id"]
                self.result.bill_payer_created = True
                self.say(f"BILL PAYER id (terminal only, found by scan): {bill_payer_id}")
        return bill_payer_id

    @staticmethod
    def _created_id(body) -> str | None:
        data = ((body or {}).get("data") or {}).get("billPayers") or {}
        created = data.get("create")
        if isinstance(created, list):
            created = created[0] if created else None
        if isinstance(created, dict):
            return str(created.get("billPayerId") or "") or None
        return str(created) if created else None

    # -- the clean-up: bill payer first, then the child ----------------------------- #
    def _cleanup(self, plan, shape, site_id, email_key, external_id, child_id, bill_payer_id,
                 include_deleted, session) -> None:
        if bill_payer_id:
            self._delete_bill_payer(plan, shape, site_id, email_key, external_id, child_id,
                                    bill_payer_id)
        if session is not None and child_id and self.result.test_child_created:
            try:
                self.result.test_child_deleted = session.delete_child(child_id)
            except ProbeRefused as exc:
                self.say(f"TEST CHILD NOT DELETED: {exc}")
            except Exception as exc:  # noqa: BLE001
                self.say(f"TEST CHILD NOT DELETED ({type(exc).__name__})")
            if not self.result.test_child_deleted:
                self.say(f"DELETE THIS TEST CHILD BY HAND: {child_id}")

    def _delete_bill_payer(self, plan, shape, site_id, email_key, external_id, child_id,
                           bill_payer_id) -> None:
        if plan.delete_arg is None:
            self.say(f"NO SAFE DELETE ({plan.delete_unusable}). DELETE THIS BILL PAYER IN THE "
                     f"FAMLY UI: {bill_payer_id}")
            return
        variables = {"id": [bill_payer_id] if plan.delete_is_list else bill_payer_id}
        assert set(variables) == {"id"}
        try:
            self.budget.spend("billpayer_delete")
        except ProbeRefused as exc:
            self.say(f"STOPPED: {exc}")
            return
        self.say("SENDING billPayers.delete (the bill payer id only) ...")
        try:
            body = self._send(delete_document(plan), variables, DELETE_OP, "delete")
        except (GraphQLError, GraphQLHTTPError) as exc:
            self.say(f"DELETE REJECTED (sent once, not retried): {self._error(exc)}")
            self.say(f"DELETE THIS BILL PAYER BY HAND: {bill_payer_id}")
            return
        except (requests.RequestException, TimeoutError, ConnectionError) as exc:
            self.say(f"DELETE OUTCOME UNKNOWN ({type(exc).__name__}); not resent")
            self.say(f"CHECK / DELETE THIS BILL PAYER BY HAND: {bill_payer_id}")
            return
        self.say(f"DELETE RAW RESPONSE: {json.dumps(body)}")
        self.result.bill_payer_deleted = True
        for flag in (False, True):
            found = probe_scan(self.public, site_id, shape, flag, email_key, external_id, child_id)
            label = "includeDeleted=" + str(flag).lower()
            hidden = found["byExternalId"] == 0 and found["byEmail"] == 0
            self.say(f"AFTER DELETE ({label}): still returned = {_yes(not hidden)}"
                     f" (by email {found['byEmail']}, by externalId {found['byExternalId']})")
