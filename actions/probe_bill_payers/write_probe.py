"""ONE-OFF WRITE PROBES: create ONE test bill payer in Famly (`write`), or attach a
second test child to one (`attach`), measure how fast it can be read back, then clean
up. Like probe-child-create and probe-contact-create these are explicit, narrow
exceptions to "never call a write against Famly outside tests".

What they may send, and nothing else:
  * ONE `billPayers.create` and ONE `billPayers.delete` (the delete names the bill
    payer id only; an argument that would act on a whole site is never declared);
  * `billPayers.addChildren` ONCE (attach mode only) and `billPayers.deleteChildren`
    ONCE (a list argument carries every share in one request) to remove the probe's
    shares, because Famly refuses to archive a bill payer that still has shares;
  * with --create-test-child: test children (`isTestChild` true), each deleted at the
    end by `childIds` only, through the existing, guarded `ProbeSession` in
    actions/probe_child_create (never `siteIds`).
CLEAN-UP ORDER (Famly refuses `billPayers.delete` while a share exists): remove the
shares with deleteChildren, THEN delete the bill payer, THEN delete the test children.
Every mutation is sent ONCE and NEVER retried, including on HTTP 400 "Transient". A
rejected request is printed and the probe stops going forward; the clean-up path
still runs, and no variants are tried.

`--probe-delete-after-child-delete` (write mode, with --create-test-child only) swaps
the clean-up to answer one question: does deleting a child release its share? It
deletes the test child FIRST, then tries billPayers.delete once and reports the
outcome. If that is refused it falls back to deleteChildren and a second delete (the
delete cap is 2 in that mode, printed up front).

Guards, all before the first request: --confirm, --site, --test-email, and the child
options. The probe scans the site first and aborts if the test email or the probe
externalId already exists.

The documents are BUILT FROM THE LIVE SCHEMA (argument names and types are not
recorded in the repo), checked by `assert_probe_mutation`, and anything that cannot be
filled in (an unknown required input field) stops the probe before any write.

PRIVACY: the test email is held in memory only. It is redacted from every printed line
(an error body may echo it), never logged, never written to a file or the jobs DB. The
bill payer id and the child ids are printed to the TERMINAL ONLY, so a failed step can
be cleaned up by hand; they are never written anywhere.
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
ADD_CHILDREN_OP = "ProbeBillPayerAddChildren"
DELETE_CHILDREN_OP = "ProbeBillPayerDeleteChildren"
MAX_BILL_PAYER_CREATES = 1
MAX_BILL_PAYER_DELETES = 1
SCAN_OFFSETS = (0, 10, 60)          # seconds after the create (write mode)
ATTACH_OFFSETS = (0, 10)            # seconds after addChildren (attach mode)
TEST_CHILD_NAME = "AAProbe BillPayerChild"
TEST_CHILD_BIRTHDAY = date(2025, 6, 1)

OPS = {"create": CREATE_OP, "delete": DELETE_OP, "addChildren": ADD_CHILDREN_OP,
       "deleteChildren": DELETE_CHILDREN_OP}
TOKENS = {"create": "create(", "delete": "delete(", "addChildren": "addchildren(",
          "deleteChildren": "deletechildren("}


class ProbeRefused(Exception):
    """A guard failed. Nothing further was written."""


# --------------------------------------------------------------------------- #
# Guards on what may be sent
# --------------------------------------------------------------------------- #
def assert_probe_mutation(text: str, kind: str) -> None:
    """`kind` is create, delete, addChildren or deleteChildren. The document must be
    exactly one probe mutation on `billPayers`, with no site-wide argument, nothing
    but its own field, and no update."""
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).lower()
    expected = OPS[kind]
    if code.count("mutation") != 1 or f"mutation {expected.lower()}(" not in code:
        raise ProbeRefused(f"refusing: not a single {expected} mutation")
    if "billpayers {" not in code or TOKENS[kind] not in code:
        raise ProbeRefused(f"refusing: the document is not billPayers.{kind}")
    forbidden = ["siteids", "update("] + [t for k, t in TOKENS.items() if k != kind]
    for token in forbidden:
        if token in code:
            raise ProbeRefused(f"refusing: the document contains {token!r}")


class Budget:
    """The write caps. `spend` raises before a request that would exceed one."""

    def __init__(self, delete_cap: int = MAX_BILL_PAYER_DELETES, delete_children_cap: int = 1):
        self.caps = {"billpayer_create": MAX_BILL_PAYER_CREATES,
                     "billpayer_delete": delete_cap,
                     "billpayer_addchildren": 1,
                     "billpayer_deletechildren": delete_children_cap}
        self.used: dict[str, int] = {}

    def spend(self, what: str) -> None:
        if self.used.get(what, 0) >= self.caps[what]:
            raise ProbeRefused(f"cap reached: at most {self.caps[what]} {what.replace('_', ' ')}")
        self.used[what] = self.used.get(what, 0) + 1

    def text(self) -> str:
        return (f"CAPS: at most {self.caps['billpayer_create']} billPayers.create, "
                f"{self.caps['billpayer_delete']} billPayers.delete, "
                f"{self.caps['billpayer_addchildren']} addChildren (attach only) and "
                f"{self.caps['billpayer_deletechildren']} deleteChildren request(s)")


# --------------------------------------------------------------------------- #
# Working out the mutations from the live schema
# --------------------------------------------------------------------------- #
@dataclass
class ListOp:
    """A billPayers mutation taking a list of {childId, billPayerId, ...} entries."""
    arg: str = ""
    arg_type: str = ""
    is_list: bool = False
    select: str = ""
    child_id_field: str = "childId"
    bill_payer_field: str | None = None
    share_field: str | None = None
    share_required: bool = False
    share_is_object: bool = False
    share_wrap_key: str | None = None
    share_hint: str = ""


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
    add_children: ListOp | None = None
    add_children_unusable: str | None = None
    delete_children: ListOp | None = None
    delete_children_unusable: str | None = None
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


# Fields that need an extra permission to READ. Selecting one can turn a successful
# mutation into an HTTP 403 whose `data` still holds the created record.
SENSITIVE_FIELDS = frozenset({"socialSecurityNumber", "accountNumber", "sortCode"})


def selection_for(by_name: dict, type_ref) -> str:
    """The selection set for a mutation's RETURN TYPE, read from the live schema (never
    hand-assumed): every scalar/enum field that needs no arguments, plus `share { ... }`
    (its scalar fields, e.g. `share { multiplier }`) where the type has a share field.
    "" when the return type is not an object (a scalar or a list of scalars)."""
    node = by_name.get(runner.named_type(type_ref) or "") or {}
    if node.get("kind") != "OBJECT":
        return ""

    def no_required_args(f) -> bool:
        return not any(_required(a) for a in f.get("args") or [])

    def scalar_names(obj) -> list[str]:
        return [f["name"] for f in obj.get("fields") or []
                if f["name"] not in SENSITIVE_FIELDS and no_required_args(f)
                and (by_name.get(runner.named_type(f["type"]) or "") or {}).get("kind", "SCALAR")
                in ("SCALAR", "ENUM")]

    parts = scalar_names(node)
    for f in node.get("fields") or []:
        inner = by_name.get(runner.named_type(f["type"]) or "") or {}
        if f["name"] == "share" and inner.get("kind") == "OBJECT" and no_required_args(f):
            inner_scalars = scalar_names(inner)
            if inner_scalars:
                parts.append("share { " + " ".join(inner_scalars) + " }")
    return " { " + " ".join(parts) + " }" if parts else " { __typename }"


def _required(arg) -> bool:
    return (arg.get("type") or {}).get("kind") == "NON_NULL" and arg.get("defaultValue") is None


def _share_info(by_name: dict, f: dict) -> dict:
    share_type = by_name.get(runner.named_type(f["type"]) or "") or {}
    is_object = share_type.get("kind") == "INPUT_OBJECT"
    inner = share_type.get("inputFields") or []
    return {
        "hint": (f"{f['name']}: {runner.type_str(f['type'])}"
                 + (f" = {f['defaultValue']}" if f.get("defaultValue") is not None else "")),
        "is_object": is_object,
        "wrap_key": inner[0]["name"] if is_object and len(inner) == 1 else None,
    }


def _list_op(by_name: dict, mut_fields: dict, name: str, want_share: bool) -> tuple[ListOp | None, str | None]:
    """The plan for billPayers.<name>(<list of {childId, billPayerId[, share]}>)."""
    mutation = mut_fields.get(name)
    if mutation is None:
        return None, f"the schema has no billPayers.{name}"
    main = []
    for arg in mutation.get("args") or []:
        input_type = by_name.get(runner.named_type(arg["type"]) or "") or {}
        if input_type.get("kind") == "INPUT_OBJECT" and any(
                f["name"] == "childId" for f in input_type.get("inputFields") or []):
            main.append((arg, input_type))
    if len(main) != 1:
        return None, f"billPayers.{name} has no single input argument with a childId"
    arg, input_type = main[0]
    others = [a["name"] for a in mutation.get("args") or [] if a is not arg and _required(a)]
    if others:
        return None, f"billPayers.{name} has other required arguments: {', '.join(others)}"
    fields = {f["name"]: f for f in input_type.get("inputFields") or []}
    op = ListOp(arg=arg["name"], arg_type=runner.type_str(arg["type"]),
                is_list="[" in runner.type_str(arg["type"]))
    op.bill_payer_field = next((n for n in fields if re.search(r"bill\s*payer", n, re.IGNORECASE)
                                and "id" in n.lower()), None)
    if op.bill_payer_field is None:
        return None, f"{input_type.get('name')} has no bill payer id field"
    handled = {"childId", op.bill_payer_field}
    if want_share:
        share_name = "share" if "share" in fields else next(
            (n for n in fields if n not in handled), None)
        if share_name is None:
            return None, f"{input_type.get('name')} has no share field"
        op.share_field, op.share_required = share_name, _required(fields[share_name])
        info = _share_info(by_name, fields[share_name])
        op.share_hint, op.share_is_object, op.share_wrap_key = info["hint"], info["is_object"], info["wrap_key"]
        handled.add(share_name)
    unfillable = [n for n, f in fields.items() if n not in handled and _required(f)]
    if unfillable:
        return None, f"{input_type.get('name')} has other required fields: {', '.join(unfillable)}"
    op.select = selection_for(by_name, mutation["type"])
    return op, None


def plan_from_schema(body: dict, shape: dict | None = None) -> WritePlan:
    types, roots = runner._types(body)
    by_name = {t.get("name"): t for t in types}
    schema = ((body or {}).get("data") or {}).get("__schema") or {}
    shape = shape if shape is not None else runner.discover_shape(body)
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
        # MINIMAL: only the id. Reading anything else back is a way to turn a mutation that
        # ran into an error response (e.g. a field the token may not read).
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
        plan.delete_select = selection_for(by_name, delete["type"])

    # ---- the share operations (removing shares must work before a delete can) ---
    plan.add_children, plan.add_children_unusable = _list_op(by_name, mut_fields, "addChildren", True)
    plan.delete_children, plan.delete_children_unusable = _list_op(
        by_name, mut_fields, "deleteChildren", False)

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
                info = _share_info(by_name, f)
                plan.share_hint, plan.share_is_object, plan.share_wrap_key = (
                    info["hint"], info["is_object"], info["wrap_key"])

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


def add_children_document(op: ListOp) -> str:
    return (f"mutation {ADD_CHILDREN_OP}($input: {op.arg_type}) {{ billPayers "
            f"{{ addChildren({op.arg}: $input){op.select} }} }}\n")


def delete_children_document(op: ListOp) -> str:
    return (f"mutation {DELETE_CHILDREN_OP}($input: {op.arg_type}) {{ billPayers "
            f"{{ deleteChildren({op.arg}: $input){op.select} }} }}\n")


def _wrap(raw, is_object: bool, wrap_key, hint: str):
    if raw is None:
        return None
    if is_object:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and wrap_key:
            return {wrap_key: float(raw)}
        raise ProbeRefused("refusing: --share must be a number, or a full JSON object for "
                           f"the share input ({hint})")
    if isinstance(raw, dict):
        raise ProbeRefused(f"refusing: the share field is not an object ({hint})")
    return raw


def build_share(plan: WritePlan, raw):
    """The share value to send. `--share 1.0` is wrapped into the share input's own
    single field (`{"multiplier": 1.0}`); a full JSON object is sent as given."""
    if raw is None or not plan.share_field:
        return None
    return _wrap(raw, plan.share_is_object, plan.share_wrap_key, plan.share_hint)


def build_add_share(op: ListOp, raw):
    if raw is None or not op.share_field:
        return None
    return _wrap(raw, op.share_is_object, op.share_wrap_key, op.share_hint)


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


def make_add_entry(op: ListOp, child_id: str, bill_payer_id: str, share) -> dict:
    entry = {op.child_id_field: child_id, op.bill_payer_field: bill_payer_id}
    if op.share_field and share is not None:
        entry[op.share_field] = share
    return entry


def make_delete_entry(op: ListOp, child_id: str, bill_payer_id: str) -> dict:
    return {op.child_id_field: child_id, op.bill_payer_field: bill_payer_id}


# --------------------------------------------------------------------------- #
# Scanning for the probe bill payer (in memory; nothing is printed but booleans)
# --------------------------------------------------------------------------- #
def probe_scan(client, site_id, shape, include_deleted, email_key, external_id, child_id,
               bill_payer_id=None) -> dict:
    """One full scan with `includeDeleted` passed EXPLICITLY (a bool, never omitted).

    Returns counts and, for the probe's own bill payer (matched by externalId, or by id
    when `bill_payer_id` is given), its child entries as (child id, multiplier)."""
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
        entries: list = []
        id_seen = False
        id_probe_ok = False
        found_deleted = None
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
                ext_text = str(node.get("externalId") or "").strip()
                is_ext = ext_text == external_id
                is_id = bool(bill_payer_id) and str(node.get("billPayerId")) == bill_payer_id
                by_email += is_email
                by_ext += is_ext
                if is_id:
                    id_seen = True
                    id_probe_ok = ext_text.startswith("probe-") and is_email
                if is_email or is_ext:
                    if found_id is None:
                        found_id = str(node.get("billPayerId"))
                        found_deleted = bool(node.get("deletedAt"))
                if (is_ext or is_id) and shape["children_field"]:
                    entries = runner._child_entries(node, shape)
                    attached = child_id in {c for c, _m in entries}
            cursor = listing.get("next") or None
            if not cursor:
                break
        return {"byEmail": by_email, "byExternalId": by_ext, "attached": attached,
                "id": found_id, "deleted": found_deleted, "entries": entries, "idSeen": id_seen,
                "idProbeOk": id_probe_ok}
    finally:
        os.unlink(handle.name)


def _yes(value) -> str:
    return "unknown" if value is None else "yes" if value else "no"


# --------------------------------------------------------------------------- #
# The probes
# --------------------------------------------------------------------------- #
@dataclass
class WriteResult:
    bill_payer_created: bool = False
    bill_payer_deleted: bool = False
    shares_deleted: bool = False
    children_added: bool = False
    test_child_created: bool = False
    test_child_deleted: bool = False
    stopped: str | None = None


@dataclass
class _Run:
    """What one run has created, so the clean-up knows what to remove."""
    plan: WritePlan
    shape: dict
    site_id: str
    email_key: str
    external_id: str
    session: object = None
    bill_payer_id: str | None = None
    owns_bill_payer: bool = True
    share_children: list = field(default_factory=list)      # child ids with a share to remove
    test_children: list = field(default_factory=list)       # child ids created by this run
    deleted_children: set = field(default_factory=set)


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

    def _rejection(self, label: str, exc) -> None:
        """Say HOW a request was rejected. A validation failure (the document itself was
        invalid) means nothing was executed; an API refusal means Famly looked at the
        request and said no. Either way it is never retried automatically."""
        text = self._error(exc)
        if "pass validation" in text.lower():
            self.say(f"{label} rejected at validation, nothing was executed (not retried, no variants "
                     f"tried): {text}")
        else:
            self.say(f"{label} REFUSED by the API (sent once, not retried): {text}")

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

    # -- the guards and the schema, shared by both modes ------------------------- #
    def _prepare(self, site_code, test_email, confirm):
        self._email = (test_email or "").strip()
        if not confirm:
            raise ProbeRefused("refusing without --confirm; nothing was written")
        if not site_code or not self._email or "@" not in self._email:
            raise ProbeRefused("refusing: --site and a --test-email are required")
        site_code = site_code.strip().upper()
        entry = catalogue.load_sites().get(site_code)
        site_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if not site_id:
            raise ProbeRefused(f"refusing: no institutionId for site {site_code}")
        return site_code, site_id

    def _verify_test_child(self, child_id: str) -> None:
        try:
            summary = self.rest.get(f"/v2/children/{child_id}/summary")
        except RestHTTPError as exc:
            raise ProbeRefused(f"could not verify the child (HTTP {exc.status_code})") from None
        if not child_probe.is_test_child(summary):
            raise ProbeRefused("refusing: that child is not a test child (isTestChild is not true)")

    def _read_schema(self, need_add_children: bool):
        schema_body = runner.fetch_schema(self.public)
        shape = runner.discover_shape(schema_body)
        plan = plan_from_schema(schema_body, shape)
        try:
            runner.require_include_deleted(shape)
        except runner.AuditRefused as exc:
            raise ProbeRefused(str(exc)) from None
        blockers = list(plan.blockers)
        if plan.delete_children is None:
            blockers.append(f"clean-up is impossible: {plan.delete_children_unusable}")
        if need_add_children and plan.add_children is None:
            blockers.append(plan.add_children_unusable or "billPayers.addChildren is unusable")
        return plan, shape, blockers

    def _new_test_child(self, run: _Run, site_code: str, label: str, stamp: str):
        group_id = derive.group_id_for(site_code, "Hatchlings")
        spec = child_probe.ChildSpec(
            label, TEST_CHILD_NAME, TEST_CHILD_BIRTHDAY, f"AAPROBE-BP-{stamp}-{label}",
            f"AAPROBE-BP-DEAL-{stamp}-{label}", "2027-01-04", "2027-12-31")
        payload = child_probe.make_child_input(spec, run.site_id, group_id, "%Y-%m-%d")
        child_id = run.session.create_child(payload, f"test child {label}")
        if not child_id:
            return None
        run.test_children.append(child_id)
        self.result.test_child_created = True
        self.say(f"TEST CHILD {label} id (terminal only): {child_id}")
        return child_id

    # ======================================================================== #
    # write: create one bill payer, measure the lag, clean up
    # ======================================================================== #
    def run(self, site_code: str, test_email: str, child_id: str | None, create_test_child: bool,
            share, confirm: bool, probe_delete_after_child_delete: bool = False) -> WriteResult:
        site_code, site_id = self._prepare(site_code, test_email, confirm)
        if bool(child_id) == bool(create_test_child):
            raise ProbeRefused("refusing: give exactly one of --child-id or --create-test-child")
        if probe_delete_after_child_delete and not create_test_child:
            raise ProbeRefused("refusing: --probe-delete-after-child-delete needs --create-test-child "
                               "(only a child this run created may be deleted)")
        if child_id:
            self._verify_test_child(child_id)        # BEFORE anything is scanned or written
        if probe_delete_after_child_delete:
            self.budget = Budget(delete_cap=2)

        plan, shape, blockers = self._read_schema(need_add_children=False)
        self.say(self.budget.text() + ("; test child created and deleted" if create_test_child else ""))
        self.say("Mutations are sent once and never retried, including on a 400 Transient.")
        if probe_delete_after_child_delete:
            self.say("EXPERIMENT: the test child is deleted FIRST, then billPayers.delete is tried once "
                     "(a second delete is allowed only as the fallback clean-up).")
        if blockers:
            for blocker in blockers:
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

        stamp = self.utcnow().strftime("%Y%m%dT%H%M%SZ")
        run = _Run(plan=plan, shape=shape, site_id=site_id,
                   email_key=normalise_email(self._email), external_id=f"probe-{stamp}")

        before = probe_scan(self.public, site_id, shape, True, run.email_key, run.external_id, "")
        if before["byEmail"] or before["byExternalId"]:
            self.say("STOPPED: the test email or the probe externalId already exists at this site; "
                     "nothing was written")
            self.result.stopped = "exists"
            return self.result
        self.say("PRE-SCAN: test email and probe externalId are absent")

        try:
            if create_test_child:
                run.session = child_probe.ProbeSession(self.rest, self.public, None, self.say)
                child_id = self._new_test_child(run, site_code, "1", stamp)
                if not child_id:
                    self.say("STOPPED: the test child was rejected; nothing else was written")
                    self.result.stopped = "child"
                    return self.result
            started = self._create_bill_payer(run, child_id, share)
            if started is not None:
                self._measure(run, child_id, started, SCAN_OFFSETS, "T+{:>2}s")
        except ProbeRefused as exc:
            self.say(f"STOPPED: {exc}")
            self.result.stopped = self.result.stopped or "refused"
        except Exception as exc:  # noqa: BLE001 - the clean-up below must still run
            self.say(f"ERROR ({type(exc).__name__}); going on to the clean-up")
        finally:
            if probe_delete_after_child_delete:
                self._cleanup_delete_after_child_delete(run)
            else:
                self._cleanup(run)
        self.say("=== DONE. Ids for a manual clean-up are above (terminal only; nothing was stored) ===")
        return self.result

    # ======================================================================== #
    # attach: a second test child onto the probe bill payer (the sibling path)
    # ======================================================================== #
    def run_attach(self, site_code: str, test_email: str, create_test_child: bool,
                   existing_bill_payer_id: str | None, share, second_share, confirm: bool) -> WriteResult:
        site_code, site_id = self._prepare(site_code, test_email, confirm)
        if not create_test_child:
            raise ProbeRefused("refusing: attach needs --create-test-child (it creates the test "
                               "children it uses, and deletes only those)")
        plan, shape, blockers = self._read_schema(need_add_children=True)
        if plan.delete_children is not None:
            # One request carries every share when the argument is a list.
            self.budget = Budget(delete_children_cap=1 if plan.delete_children.is_list else 2)
        self.say(self.budget.text() + ("; one test child created and deleted" if existing_bill_payer_id
                                       else "; two test children created and deleted"))
        self.say("Mutations are sent once and never retried, including on a 400 Transient.")
        if blockers:
            for blocker in blockers:
                self.say(f"STOPPED before any write: {blocker}")
            self.result.stopped = "schema"
            return self.result
        add = plan.add_children
        if (plan.share_required or add.share_required) and share is None:
            self.say("STOPPED before any write: a share is required; give --share")
            self.result.stopped = "share"
            return self.result
        raw_second = second_share if second_share is not None else share   # same unless --second-share
        share = build_share(plan, share)
        second = build_add_share(add, raw_second)
        self.say(f"addChildren input: {add.arg}: {add.arg_type} "
                 f"[{add.child_id_field}, {add.bill_payer_field}"
                 + (f", {add.share_hint}" if add.share_field else "") + "]")

        stamp = self.utcnow().strftime("%Y%m%dT%H%M%SZ")
        run = _Run(plan=plan, shape=shape, site_id=site_id, email_key=normalise_email(self._email),
                   external_id=f"probe-{stamp}", owns_bill_payer=not existing_bill_payer_id)

        # a) the pre-scan, includeDeleted false.
        before = probe_scan(self.public, site_id, shape, False, run.email_key, run.external_id, "",
                            bill_payer_id=existing_bill_payer_id)
        if existing_bill_payer_id:
            if not before["idSeen"] or not before["idProbeOk"]:
                self.say("STOPPED: that bill payer is not a probe bill payer with the test email "
                         "(externalId must start with probe- and the email must match); "
                         "nothing was written")
                self.result.stopped = "not_probe"
                return self.result
            run.bill_payer_id = existing_bill_payer_id
            self.say("PRE-SCAN: the existing bill payer is a probe bill payer with the test email")
        elif before["byEmail"] or before["byExternalId"]:
            self.say("STOPPED: the test email or the probe externalId already exists at this site; "
                     "nothing was written")
            self.result.stopped = "exists"
            return self.result
        else:
            self.say("PRE-SCAN: test email and probe externalId are absent")

        first_child = second_child = None
        try:
            run.session = child_probe.ProbeSession(self.rest, self.public, None, self.say)
            if run.owns_bill_payer:
                first_child = self._new_test_child(run, site_code, "1", stamp)
                if not first_child:
                    self.say("STOPPED: the first test child was rejected; nothing else was written")
                    self.result.stopped = "child"
                    return self.result
                started = self._create_bill_payer(run, first_child, share)
                if started is None:
                    return self.result            # rejected: the clean-up below still runs
                if not run.bill_payer_id:         # an unknown outcome: settle it by scanning
                    self._measure(run, first_child, started, ATTACH_OFFSETS, "T+{:>2}s")
            # b) the second test child
            second_child = self._new_test_child(run, site_code, "2", stamp)
            if not second_child:
                self.say("STOPPED: the second test child was rejected")
                self.result.stopped = "child"
                return self.result
            if not run.bill_payer_id:
                self.say("STOPPED: no bill payer id to attach to (the create was not confirmed)")
                self.result.stopped = "no_bill_payer"
                return self.result
            # c) addChildren, once
            self._add_children(run, second_child, second)
            # d) read it back
            self._measure_attach(run, first_child, second_child)
        except ProbeRefused as exc:
            self.say(f"STOPPED: {exc}")
            self.result.stopped = self.result.stopped or "refused"
        except Exception as exc:  # noqa: BLE001
            self.say(f"ERROR ({type(exc).__name__}); going on to the clean-up")
        finally:
            self._cleanup(run)
        self.say("=== DONE. Ids for a manual clean-up are above (terminal only; nothing was stored) ===")
        return self.result

    # -- create the bill payer ------------------------------------------------------ #
    def _create_bill_payer(self, run: _Run, child_id, share):
        """ONE billPayers.create. Returns the clock value it started at, or None if it
        was rejected. An unknown outcome is settled by the scans that follow."""
        payload = make_input(run.plan, run.site_id, self._email, run.external_id, child_id, share)
        variables = {"input": [payload] if "[" in run.plan.create_arg_type else payload}
        self.say("VARIABLES TO SEND: " + json.dumps(variables))      # the email is redacted by `say`
        self.budget.spend("billpayer_create")
        self.say(f"SENDING billPayers.create (externalId {run.external_id}) ...")
        started = self.clock()
        run.share_children.append(child_id)          # it has a share if the create landed
        try:
            body = self._send(create_document(run.plan), variables, CREATE_OP, "create")
        except (GraphQLError, GraphQLHTTPError) as exc:
            # An error status or an `errors` array does NOT mean the mutation did not run.
            self._rejection("create", exc)
            salvaged = self._data_id(exc)
            if salvaged:
                self.say("create: the response carried errors AND data with a billPayerId, so the "
                         "mutation RAN; treating it as created (never resent)")
                run.bill_payer_id = salvaged
                self.result.bill_payer_created = True
                self.say(f"BILL PAYER id (terminal only): {run.bill_payer_id}")
                return started
            self._settle(run, child_id)
            self.result.stopped = "rejected"
            return None
        except (requests.RequestException, TimeoutError, ConnectionError) as exc:
            self.say(f"UNKNOWN OUTCOME ({type(exc).__name__}); not resent; settling with a scan")
            self._settle(run, child_id)
        else:
            self.say(f"RAW RESPONSE: {json.dumps(body)}")
            run.bill_payer_id = self._created_id(body)
            if run.bill_payer_id:
                self.say(f"BILL PAYER id (terminal only): {run.bill_payer_id}")
                self.result.bill_payer_created = True
            else:
                self.say("create returned no billPayerId; settling with a scan")
                self._settle(run, child_id)
        return started

    def _settle(self, run: _Run, child_id) -> None:
        """The create was not a clean success, so look. Scan with includeDeleted TRUE by the
        probe externalId and the test email: if a record exists the create ran, and the
        clean-up must run on it. The create is never sent again."""
        found = probe_scan(self.public, run.site_id, run.shape, True, run.email_key,
                           run.external_id, child_id)
        self.say(f"SETTLE scan (includeDeleted=true): found by email={_yes(found['byEmail'] > 0)} "
                 f"(count {found['byEmail']}), found by externalId={_yes(found['byExternalId'] > 0)} "
                 f"(count {found['byExternalId']})")
        if not found["id"]:
            if child_id in run.share_children:
                run.share_children.remove(child_id)         # nothing was created: no share to remove
            self.say("SETTLED: no record exists; the create did not run")
            return
        if found["deleted"]:
            self.say("SETTLED: a record exists but is already archived; nothing to clean up")
            run.share_children.clear()
            return
        run.bill_payer_id = found["id"]
        self.result.bill_payer_created = True
        self.say(f"SETTLED: the create DID run. BILL PAYER id (terminal only, found by scan): "
                 f"{run.bill_payer_id}; the clean-up will remove it")

    @staticmethod
    def _data_id(exc) -> str | None:
        """The billPayerId in `data` of an error response, if there is one. A GraphQLError
        carries the parsed body; an HTTP error carries it as text."""
        body = getattr(exc, "body", None)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                return None
        if not isinstance(body, dict):
            return None
        return Probe._created_id({"data": body.get("data")}) if body.get("data") else None

    def _measure(self, run: _Run, child_id, started, offsets, label_format) -> None:
        """Read-after-write lag: scan at each offset after `started`."""
        for offset in offsets:
            wait = offset - (self.clock() - started)
            if wait > 0:
                self.sleep(wait)
            found = probe_scan(self.public, run.site_id, run.shape, False, run.email_key,
                               run.external_id, child_id)
            self.say(f"{label_format.format(offset)} scan: found by email={_yes(found['byEmail'] > 0)} "
                     f"(count {found['byEmail']}), found by externalId={_yes(found['byExternalId'] > 0)} "
                     f"(count {found['byExternalId']}), child attached={_yes(found['attached'])}")
            if found["id"] and not run.bill_payer_id:
                run.bill_payer_id = found["id"]
                self.result.bill_payer_created = True
                self.say(f"BILL PAYER id (terminal only, found by scan): {run.bill_payer_id}")

    # -- addChildren, once ----------------------------------------------------------- #
    def _add_children(self, run: _Run, second_child, share) -> None:
        op = run.plan.add_children
        variables = {"input": [make_add_entry(op, second_child, run.bill_payer_id, share)]
                     if op.is_list else make_add_entry(op, second_child, run.bill_payer_id, share)}
        self.say("VARIABLES TO SEND (addChildren): " + json.dumps(variables))
        self.budget.spend("billpayer_addchildren")
        self.say("SENDING billPayers.addChildren ...")
        run.share_children.append(second_child)       # remove it in the clean-up either way
        try:
            body = self._send(add_children_document(op), variables, ADD_CHILDREN_OP, "addChildren")
        except (GraphQLError, GraphQLHTTPError) as exc:
            self._rejection("addChildren", exc)
            self.result.stopped = "add_rejected"
            return
        except (requests.RequestException, TimeoutError, ConnectionError) as exc:
            self.say(f"addChildren OUTCOME UNKNOWN ({type(exc).__name__}); not resent")
            return
        self.result.children_added = True
        self.say(f"addChildren RAW RESPONSE: {json.dumps(body)}")
        returned = ((body or {}).get("data") or {}).get("billPayers", {}).get("addChildren")
        entries = returned if isinstance(returned, list) else [returned] if isinstance(returned, dict) else []
        shares = [(e.get("childId"), (e.get("share") or {}).get("multiplier")) for e in entries
                  if isinstance(e, dict) and e.get("childId")]
        if shares:
            self.say(f"addChildren RETURNED {len(shares)} share entr{'y' if len(shares) == 1 else 'ies'}; "
                     f"includes the second child: {_yes(second_child in {c for c, _m in shares})}")
        else:
            self.say("addChildren returned no share entries to inspect")

    def _measure_attach(self, run: _Run, first_child, second_child) -> None:
        started = self.clock()
        for offset in ATTACH_OFFSETS:
            wait = offset - (self.clock() - started)
            if wait > 0:
                self.sleep(wait)
            found = probe_scan(self.public, run.site_id, run.shape, False, run.email_key,
                               run.external_id, second_child, bill_payer_id=run.bill_payer_id)
            by_child = {c: m for c, m in found["entries"]}
            parts = []
            for label, child in (("child 1", first_child), ("child 2", second_child)):
                if child is None:
                    continue
                m = by_child.get(child)
                parts.append(f"{label} listed={_yes(child in by_child)}"
                             + (f" multiplier={m:.3f}" if m is not None else ""))
            both = all(c in by_child for c in (first_child, second_child) if c)
            self.say(f"T+{offset:>2}s after addChildren: the payer lists {len(found['entries'])} "
                     f"child entr{'y' if len(found['entries']) == 1 else 'ies'}; "
                     f"both test children listed={_yes(both)}; " + "; ".join(parts))

    @staticmethod
    def _created_id(body) -> str | None:
        data = ((body or {}).get("data") or {}).get("billPayers") or {}
        created = data.get("create")
        if isinstance(created, list):
            created = created[0] if created else None
        if isinstance(created, dict):
            return str(created.get("billPayerId") or "") or None
        return str(created) if created else None

    # -- the clean-up: shares, then the bill payer, then the children --------------- #
    def _cleanup(self, run: _Run) -> None:
        shares_gone = self._delete_shares(run)
        if run.bill_payer_id and run.owns_bill_payer:
            if shares_gone:
                self._delete_bill_payer(run)
            else:
                self.say("NOT DELETING the bill payer: its shares could not be removed and Famly "
                         "refuses to archive a bill payer with shares")
                self.say(f"DELETE THIS BILL PAYER BY HAND (remove its shares first): {run.bill_payer_id}")
        elif run.bill_payer_id:
            self.say(f"The existing bill payer was left in place: {run.bill_payer_id}")
        self._delete_test_children(run)

    def _delete_shares(self, run: _Run) -> bool:
        """billPayers.deleteChildren for every share this run made. One request when the
        argument is a list. True if there is nothing left to remove."""
        if not run.bill_payer_id or not run.share_children:
            return True
        op = run.plan.delete_children
        if op is None:
            self.say(f"NO DELETECHILDREN ({run.plan.delete_children_unusable}); remove the shares by hand")
            return False
        entries = [make_delete_entry(op, c, run.bill_payer_id) for c in run.share_children]
        batches = [entries] if op.is_list else [[e] for e in entries]
        for batch in batches:
            variables = {"input": batch if op.is_list else batch[0]}
            try:
                self.budget.spend("billpayer_deletechildren")
            except ProbeRefused as exc:
                self.say(f"STOPPED: {exc}")
                return False
            self.say("SENDING billPayers.deleteChildren (the probe's shares only) ...")
            try:
                body = self._send(delete_children_document(op), variables, DELETE_CHILDREN_OP,
                                  "deleteChildren")
            except (GraphQLError, GraphQLHTTPError) as exc:
                self._rejection("deleteChildren", exc)
                return False
            except (requests.RequestException, TimeoutError, ConnectionError) as exc:
                self.say(f"deleteChildren OUTCOME UNKNOWN ({type(exc).__name__}); not resent")
                return False
            self.say(f"deleteChildren RAW RESPONSE: {json.dumps(body)}")
        self.result.shares_deleted = True
        # deleteChildren answers with entries even for shares that never existed, so look.
        found = probe_scan(self.public, run.site_id, run.shape, False, run.email_key,
                           run.external_id, "", bill_payer_id=run.bill_payer_id)
        count = len(found["entries"])
        self.say(f"AFTER deleteChildren: the payer lists {count} child entr{'y' if count == 1 else 'ies'}"
                 + ("" if count == 0 else " (shares remain: the archive will probably be refused)"))
        return True

    def _delete_test_children(self, run: _Run) -> None:
        deleted_all = True
        for child in run.test_children:
            if child in run.deleted_children:
                continue
            try:
                ok = run.session.delete_child(child)
            except ProbeRefused as exc:
                self.say(f"TEST CHILD NOT DELETED: {exc}")
                ok = False
            except Exception as exc:  # noqa: BLE001
                self.say(f"TEST CHILD NOT DELETED ({type(exc).__name__})")
                ok = False
            if ok:
                run.deleted_children.add(child)
            else:
                deleted_all = False
                self.say(f"DELETE THIS TEST CHILD BY HAND: {child}")
        self.result.test_child_deleted = bool(run.test_children) and deleted_all

    def _delete_bill_payer(self, run: _Run) -> bool:
        plan = run.plan
        if plan.delete_arg is None:
            self.say(f"NO SAFE DELETE ({plan.delete_unusable}). DELETE THIS BILL PAYER IN THE "
                     f"FAMLY UI: {run.bill_payer_id}")
            return False
        variables = {"id": [run.bill_payer_id] if plan.delete_is_list else run.bill_payer_id}
        assert set(variables) == {"id"}
        try:
            self.budget.spend("billpayer_delete")
        except ProbeRefused as exc:
            self.say(f"STOPPED: {exc}")
            return False
        self.say("SENDING billPayers.delete (the bill payer id only) ...")
        try:
            body = self._send(delete_document(plan), variables, DELETE_OP, "delete")
        except (GraphQLError, GraphQLHTTPError) as exc:
            self._rejection("delete", exc)
            self.say(f"DELETE THIS BILL PAYER BY HAND: {run.bill_payer_id}")
            return False
        except (requests.RequestException, TimeoutError, ConnectionError) as exc:
            self.say(f"DELETE OUTCOME UNKNOWN ({type(exc).__name__}); not resent")
            self.say(f"CHECK / DELETE THIS BILL PAYER BY HAND: {run.bill_payer_id}")
            return False
        self.say(f"DELETE RAW RESPONSE: {json.dumps(body)}")
        self.result.bill_payer_deleted = True
        for flag in (False, True):
            found = probe_scan(self.public, run.site_id, run.shape, flag, run.email_key,
                               run.external_id, "")
            label = "includeDeleted=" + str(flag).lower()
            hidden = found["byExternalId"] == 0 and found["byEmail"] == 0
            self.say(f"AFTER DELETE ({label}): still returned = {_yes(not hidden)}"
                     f" (by email {found['byEmail']}, by externalId {found['byExternalId']})")
        return True

    # -- the experiment: does deleting a child release its share? ---------------------- #
    def _cleanup_delete_after_child_delete(self, run: _Run) -> None:
        if not run.bill_payer_id:
            self._delete_test_children(run)
            return
        self.say("=== EXPERIMENT: delete the test child first, then try billPayers.delete once ===")
        self._delete_test_children(run)
        child_deleted = self.result.test_child_deleted
        if not child_deleted:
            self.say("The test child was not deleted, so the experiment cannot be read; "
                     "falling back to the normal clean-up")
        deleted = self._delete_bill_payer(run) if run.owns_bill_payer else False
        if child_deleted:
            self.say("EXPERIMENT RESULT: after the test child was deleted, billPayers.delete was "
                     + ("ACCEPTED, so deleting a child RELEASES its share" if deleted else
                        "REFUSED or not completed, so deleting a child does NOT release its share "
                        "(see the line above)"))
        if deleted:
            return
        # The fallback: remove the share, then the second (and last) delete.
        if self._delete_shares(run):
            self._delete_bill_payer(run)
        else:
            self.say(f"DELETE THIS BILL PAYER BY HAND (remove its shares first): {run.bill_payer_id}")
