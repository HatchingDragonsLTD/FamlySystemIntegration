"""ONE-OFF PROBE: create TWO test children in Famly, inspect how Famly treats
them, then delete them.

DELETE THIS PACKAGE (`actions/probe_child_create/`) and its line in main.py once
the questions in docs/famly_onboarding_pipeline_design.md section 9 are answered.

THIS IS AN EXPLICIT, NARROW EXCEPTION to "never call a write against Famly
outside tests" (CLAUDE.md), the same as probe-contact-create: it may CREATE test
children and DELETE the ones it created, nothing else. It never runs from a web
action, a queue or any other command.

THE DANGEROUS PART IS DELETE. Famly's `children.delete(childIds, siteIds)`
"soft-deletes the given children, OR EVERY CHILD IN THE GIVEN SITES". So:

  * the delete mutation document declares `childIds` only; `siteIds` cannot be
    sent, and `ProbeSession.delete_child` asserts the variables are exactly one
    id under `childIds`;
  * a child is deleted ONLY if this run created it, and ONLY after its internal
    summary (GET /v2/children/{id}/summary) is re-read and says isTestChild true;
  * if the schema has no delete, the ids are printed for manual deletion.

Every guard, all before the first write:
  1. `--confirm` is required.
  2. Every payload has isTestChild true, hard-coded. `assert_test_payload` runs
     inside the single create path and refuses anything else.
  3. At most MAX_CHILDREN (2) children are created and MAX_WRITES (8) write
     requests (creates, accepted or rejected, plus deletes) are made. Both caps
     are printed.
  4. The site is HDCW and the group is its "Hatchlings" group, both from the
     reference files; if either is missing nothing is written.
"""

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from actions.onboard_dry_run import derive
from core.client import GraphQLError, GraphQLHTTPError
from core.rest_client import RestHTTPError
from integrations import catalogue

HERE = Path(__file__).parent
CREATE_QUERY = HERE / "create_child.graphql"
DELETE_QUERY = HERE / "delete_child.graphql"
INTROSPECT_QUERY = HERE / "introspect.graphql"
LOOKUP_QUERY = HERE / "lookup_by_ref.graphql"
LIST_QUERY = HERE / "children_list.graphql"

CREATE_OP = "ProbeChildCreate"
DELETE_OP = "ProbeChildDelete"
INTROSPECT_OP = "ProbeChildIntrospect"
LOOKUP_OP = "ProbeChildByRef"
LIST_OP = "ProbeChildrenList"

MAX_CHILDREN = 2
MAX_WRITES = 8

SITE_CODE = "HDCW"
GROUP_TITLE = "Hatchlings"
NAME_PREFIX = "AAProbe"
EXTERNAL_PREFIX = "AAPROBE-"

# LocalDate formats to try for the birthday, in order (first is plain ISO).
BIRTHDAY_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT00:00:00Z", "%d/%m/%Y", "%Y%m%d")


class ProbeRefused(Exception):
    """A guard failed. Nothing further was written."""


@dataclass(frozen=True)
class ChildSpec:
    label: str
    name: str
    birthday: date
    external_id: str
    deal_ref: str
    first_day: str
    last_day: str


# Child B's birthday is not in the brief ("same fields"), but 2025-06-01 would
# make it start nursery before it was born, so it is born in 2022.
CHILD_A = ChildSpec("A", "AAProbe ChildOne", date(2025, 6, 1), "AAPROBE-URN-001",
                    "AAPROBE-DEAL-001", "2027-01-04", "2027-12-31")
CHILD_B = ChildSpec("B", "AAProbe ChildTwo", date(2022, 6, 1), "AAPROBE-URN-002",
                    "AAPROBE-DEAL-002", "2024-09-02", "2025-07-31")


@dataclass
class ProbeResult:
    created: list[dict] = field(default_factory=list)  # {"label", "id"}
    deleted: list[str] = field(default_factory=list)
    undeleted: list[str] = field(default_factory=list)
    writes: int = 0
    birthday_format: str | None = None


# --------------------------------------------------------------------------- #
# Payload and its guard
# --------------------------------------------------------------------------- #
def make_child_input(spec: ChildSpec, site_id: str, group_id: str, birthday_format: str) -> dict:
    """One ChildInput. `isTestChild` is hard-coded true; gender is omitted."""
    return {
        "name": spec.name,
        "birthday": spec.birthday.strftime(birthday_format),
        "currentGroupId": group_id,
        "records": [],
        "sensitiveRecords": [],
        "isTestChild": True,
        "externalId": spec.external_id,
        "externalSystemsReferences": [
            {"foreignId": spec.deal_ref, "system": "HUBSPOT"},
            {"foreignId": spec.external_id, "system": "URN"},
        ],
        "sitesRelation": [
            {"siteId": site_id, "firstDay": spec.first_day, "lastDay": spec.last_day}
        ],
    }


def assert_test_payload(payload: Any) -> None:
    """Refuse any payload that is not unmistakably a test child."""
    if not isinstance(payload, dict) or payload.get("isTestChild") is not True:
        raise ProbeRefused("refusing: payload does not have isTestChild true")
    if not str(payload.get("name", "")).startswith(NAME_PREFIX):
        raise ProbeRefused("refusing: payload name is not an AAProbe name")
    if not str(payload.get("externalId", "")).startswith(EXTERNAL_PREFIX):
        raise ProbeRefused("refusing: payload externalId is not an AAPROBE id")


def is_test_child(summary: Any) -> bool:
    """True only if the summary names isTestChild and EVERY occurrence is true."""
    values = _find_all(summary, "isTestChild")
    return bool(values) and all(v is True for v in values)


def _find_all(node: Any, key: str) -> list:
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found.extend(_find_all(v, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_all(item, key))
    return found


def error_text(exc: Exception) -> str:
    if isinstance(exc, GraphQLError):
        return json.dumps(exc.errors)
    if isinstance(exc, GraphQLHTTPError):
        return f"HTTP {exc.status_code}: {exc.body[:2000]}"
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# The session: the ONLY create and delete paths
# --------------------------------------------------------------------------- #
class ProbeSession:
    def __init__(self, rest, public, internal, out: Callable[[str], None] = print):
        self.rest, self.public, self.internal, self.out = rest, public, internal, out
        self.result = ProbeResult()
        self._created_ids: set[str] = set()

    # -- the write budget -------------------------------------------------- #
    def _spend_write(self) -> None:
        if self.result.writes >= MAX_WRITES:
            raise ProbeRefused(f"write cap ({MAX_WRITES}) reached")
        self.result.writes += 1

    # -- create ------------------------------------------------------------- #
    def create_child(self, payload: dict, label: str):
        """One create request. Returns the new child id, or None if rejected.

        Raises:
            ProbeRefused: the payload is not a test child, or a cap was hit.
        """
        assert_test_payload(payload)
        if len(self._created_ids) >= MAX_CHILDREN:
            raise ProbeRefused(f"child cap ({MAX_CHILDREN}) reached")
        self._spend_write()
        try:
            body = self.public.execute(
                query_path=CREATE_QUERY,
                variables={"children": [payload]},
                operation_name=CREATE_OP,
            )
        except (GraphQLError, GraphQLHTTPError) as exc:
            self.out(f"[{label}] REJECTED: {error_text(exc)}")
            return None
        created = body["data"]["children"]["create"]
        self.out(f"[{label}] RAW RESPONSE: {json.dumps(body)}")
        child_id = created[0]["id"]
        self._created_ids.add(child_id)
        self.result.created.append({"label": label, "id": child_id})
        return child_id

    # -- reads -------------------------------------------------------------- #
    def summary(self, child_id: str) -> dict:
        return self.rest.get(f"/v2/children/{child_id}/summary")

    def lookup(self, foreign_id: str, system: str) -> list[str]:
        body = self.public.execute(
            query_path=LOOKUP_QUERY,
            variables={"foreignId": foreign_id, "system": system},
            operation_name=LOOKUP_OP,
        )
        rows = body["data"]["children"]["listByExternalSystemReference"]["result"]
        return [row["id"] for row in rows]

    def in_children_list(self, site_id: str, statuses: list[str], child_id: str):
        """(present, that child's own row) in the site's internal childrenList
        under these enrollment statuses. Only this test child is reported."""
        cursor = None
        while True:
            body = self.internal.execute(
                query_path=LIST_QUERY,
                variables={"siteId": site_id, "filter": {"enrollmentStatuses": statuses},
                           "limit": 100, "cursor": cursor},
                operation_name=LIST_OP,
            )
            page = body["data"]["childrenList"]
            for row in page["children"]:
                if row["id"] == child_id:
                    return True, row
            cursor = page.get("next")
            if not cursor:
                return False, None

    # -- delete -------------------------------------------------------------- #
    def delete_child(self, child_id: str) -> bool:
        """Delete ONE child created by this run, after re-verifying it.

        Returns True if it was deleted. Raises ProbeRefused (before any request)
        if the child was not created by this run; returns False if the re-read
        does not say isTestChild true.
        """
        if child_id not in self._created_ids:
            raise ProbeRefused("refusing to delete a child this run did not create")
        try:
            summary = self.summary(child_id)
        except RestHTTPError as exc:
            self.out(f"NOT DELETING {child_id}: summary unreadable (HTTP {exc.status_code})")
            return False
        if not is_test_child(summary):
            self.out(f"NOT DELETING {child_id}: summary does not say isTestChild true")
            return False

        variables = {"childIds": [child_id]}
        # The delete mutation also accepts `siteIds`, which would delete EVERY
        # child at a site. It must never be present, and only one id is sent.
        assert set(variables) == {"childIds"} and len(variables["childIds"]) == 1
        self._spend_write()
        try:
            body = self.public.execute(
                query_path=DELETE_QUERY, variables=variables, operation_name=DELETE_OP
            )
        except (GraphQLError, GraphQLHTTPError) as exc:
            self.out(f"DELETE REJECTED for {child_id}: {error_text(exc)}")
            return False
        self.out(f"DELETE RAW RESPONSE for {child_id}: {json.dumps(body)}")
        self.result.deleted.append(child_id)
        return True


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
def resolve_site_and_group() -> tuple[str, str]:
    """HDCW's institution id and its Hatchlings group id, or refuse."""
    entry = catalogue.load_sites().get(SITE_CODE)
    site_id = entry.get("institutionId") if isinstance(entry, dict) else None
    if not site_id:
        raise ProbeRefused(f"no institutionId for {SITE_CODE} in reference/catalogue.json")
    try:
        group_id = derive.group_id_for(SITE_CODE, GROUP_TITLE)
    except derive.Hold as hold:
        raise ProbeRefused(
            f"no {GROUP_TITLE!r} group for {SITE_CODE} in reference/groups_catalogue.json "
            f"({hold.reason})"
        ) from None
    return site_id, group_id


def run_probe(confirm: bool, rest, public, internal, out: Callable[[str], None] = print):
    """Run the probe. Raises ProbeRefused if a guard fails before any write."""
    if not confirm:
        raise ProbeRefused("refusing without --confirm; nothing was written")
    site_id, group_id = resolve_site_and_group()

    out(f"CAPS: at most {MAX_CHILDREN} children created, at most {MAX_WRITES} write requests "
        f"in total (creates, accepted or rejected, plus deletes)")
    out(f"SITE {SITE_CODE} = {site_id}; GROUP {GROUP_TITLE} = {group_id}")

    session = ProbeSession(rest, public, internal, out)
    delete_available = _print_schema(public, out)

    try:
        _create_and_inspect(session, site_id, group_id, out)
    except ProbeRefused as exc:
        out(f"STOPPED: {exc}")
    except Exception as exc:  # noqa: BLE001 - never skip the clean-up below
        out(f"ERROR while inspecting ({type(exc).__name__}): {error_text(exc)}; "
            f"continuing to the delete step")

    _delete_all(session, delete_available, out)
    try:
        _final_lookups(session, out)
    except Exception as exc:  # noqa: BLE001
        out(f"ERROR in the final lookups ({type(exc).__name__}): {error_text(exc)}")
    return session.result


def _create_and_inspect(session: ProbeSession, site_id: str, group_id: str, out) -> None:
    result = session.result

    # Child A, trying birthday formats until one is accepted.
    child_id = None
    for fmt in BIRTHDAY_FORMATS:
        payload = make_child_input(CHILD_A, site_id, group_id, fmt)
        out(f"[A] SENDING birthday={payload['birthday']!r}")
        child_id = session.create_child(payload, "A")
        if child_id:
            result.birthday_format = fmt
            break
    if not child_id:
        out("NO BIRTHDAY FORMAT WAS ACCEPTED for child A; stopping (nothing to delete)")
        return
    _inspect(session, CHILD_A, child_id, site_id, out)

    payload = make_child_input(CHILD_B, site_id, group_id, result.birthday_format)
    out(f"[B] SENDING birthday={payload['birthday']!r}")
    child_id = session.create_child(payload, "B")
    if child_id:
        _inspect(session, CHILD_B, child_id, site_id, out)


def _inspect(session: ProbeSession, spec: ChildSpec, child_id: str, site_id: str, out) -> None:
    out(f"=== INSPECTING child {spec.label} ({child_id}) ===")
    try:
        summary = session.summary(child_id)
    except RestHTTPError as exc:
        out(f"FULL INTERNAL SUMMARY unreadable: HTTP {exc.status_code}: {exc.body[:500]}")
        summary = None
    else:
        out(f"FULL INTERNAL SUMMARY: {json.dumps(summary)}")
        out(f"  isTestChild per summary: {is_test_child(summary)}")

    def safe(description: str, fn) -> None:
        """A failing read is printed, never allowed to stop the probe."""
        try:
            fn()
        except (GraphQLError, GraphQLHTTPError, RestHTTPError) as exc:
            out(f"  {description} FAILED: {error_text(exc)}")

    for system, foreign_id, note in [
        ("HUBSPOT", spec.deal_ref, "exact"),
        ("HUBSPOT", spec.deal_ref.upper(), "upper case"),
        ("HUBSPOT", spec.deal_ref.lower(), "lower case"),
        ("HUBSPOT", f"  {spec.deal_ref}  ", "surrounding spaces"),
        ("URN", spec.external_id, "exact"),
    ]:
        def look(system=system, foreign_id=foreign_id, note=note):
            ids = session.lookup(foreign_id, system)
            out(f"  listByExternalSystemReference system={system} foreignId={foreign_id!r} "
                f"({note}) -> {json.dumps(ids)}")

        safe(f"lookup {system} {note}", look)

    for statuses in (["CURRENT", "UPCOMING"], ["UPCOMING"], ["CURRENT"]):
        def listing(statuses=statuses):
            present, row = session.in_children_list(site_id, statuses, child_id)
            out(f"  internal childrenList {'+'.join(statuses)}: child {spec.label} present = "
                f"{present}" + (f" (its own row: {json.dumps(row)})" if row else ""))

        safe(f"childrenList {'+'.join(statuses)}", listing)


def _delete_all(session: ProbeSession, delete_available: bool, out) -> None:
    ids = [item["id"] for item in session.result.created]
    if not ids:
        return
    if not delete_available:
        out("The public schema has no children delete. DELETE THESE TEST CHILDREN BY HAND:")
        for child_id in ids:
            out(f"  {child_id}")
        session.result.undeleted = list(ids)
        return
    for child_id in ids:
        try:
            deleted = session.delete_child(child_id)
        except ProbeRefused as exc:
            out(f"STOPPED DELETING: {exc}")
            deleted = False
        if not deleted:
            session.result.undeleted.append(child_id)
    if session.result.undeleted:
        out("NOT DELETED, DELETE THESE TEST CHILDREN BY HAND:")
        for child_id in session.result.undeleted:
            out(f"  {child_id}")


def _final_lookups(session: ProbeSession, out) -> None:
    out("=== FINAL LOOKUPS (should no longer return the deleted children) ===")
    for spec, item in zip((CHILD_A, CHILD_B), session.result.created):
        ids = session.lookup(spec.deal_ref, "HUBSPOT")
        still = item["id"] in ids
        out(f"  HUBSPOT {spec.deal_ref} -> {json.dumps(ids)} "
            f"(test child {spec.label} still returned: {still})")


# --------------------------------------------------------------------------- #
# Read-only schema introspection
# --------------------------------------------------------------------------- #
def _type_name(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return node.get("name") or f"{node.get('kind')}<{_type_name(node.get('ofType'))}>"


def _print_schema(public, out) -> bool:
    """Print the children create/delete mutations and the input types. Returns
    whether a `delete` mutation exists."""
    body = public.execute(query_path=INTROSPECT_QUERY, variables={}, operation_name=INTROSPECT_OP)
    data = body.get("data") or {}
    has_delete = False
    out("SCHEMA: children mutations:")
    for field_ in (data.get("mutation") or {}).get("fields") or []:
        if field_["name"] not in ("create", "delete"):
            continue
        has_delete = has_delete or field_["name"] == "delete"
        args = ", ".join(f"{a['name']}: {_type_name(a['type'])}" for a in field_.get("args") or [])
        out(f"  {field_['name']}({args}) -> {_type_name(field_['type'])}")
        if field_.get("description"):
            out(f"    {field_['description']}")
    for key, title in (("input", "ChildInput"), ("siteRelation", "SiteRelationInput"),
                       ("reference", "ForeignIdReferenceInput")):
        out(f"SCHEMA: {title}:")
        for field_ in (data.get(key) or {}).get("inputFields") or []:
            out(f"  {field_['name']}: {_type_name(field_['type'])}")
    return has_delete
