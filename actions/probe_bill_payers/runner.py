"""READ-ONLY probe for the bill payer phase. No mutation of any kind, anywhere.

Reports, all printing counts, field names and type names ONLY (an email, a name
and every id are held in memory to be counted and never printed, logged or stored):

  * `schema_lines`: what the live schema says about bill payers: the BillPayer
    type, BillPayerInput, the query and mutation fields (with argument defaults),
    and every field of another type that points at a bill payer. `type_lines`
    prints named types in full (ChildShare, ChildShareInput, ...).
  * `audit`: per site, with `includeDeleted` false and true: how many bill payers,
    pages and seconds for a full scan, with/without an email, canonical emails
    mapped to two or more bill payer ids; for those, how many ids have no
    children, how many share an identical child set, how many have a different
    one; how many bill payers have an externalId or no children; and how many
    emails appear at more than one site.

Famly's GraphQL endpoint takes queries by POST; "read-only" here means every
document sent is checked to contain no `mutation`.

The BillPayer children field and its shape are DISCOVERED from the live schema
(the repo has no record of them) and the choice is reported by name, so a wrong
guess is visible instead of silent.
"""

import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from actions.onboard_dry_run.famly_reads import normalise_email

HERE = Path(__file__).parent
SCHEMA_QUERY = HERE / "introspect.graphql"
SCHEMA_OP = "ProbeBillPayerSchema"
BILL_PAYERS_QUERY = HERE.parent / "pull_roster" / "bill_payers_query.graphql"
BILL_PAYERS_OP = "ProbeBillPayerAudit"
MAX_PAGES = 500          # a runaway cursor must end

DEFAULT_TYPES = ("ChildShare", "ChildShareInput", "ChildSharesUpdateInput", "BillPayerChildInput")


def assert_read_only(path: Path) -> None:
    text = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ).lower()
    if re.search(r"\bmutation\b\s*[\w{(]", text.replace("mutationtype", "")):
        raise AssertionError(f"{path.name} is not a read-only document")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def type_str(node) -> str:
    """A type reference as text: `[Foo!]!`."""
    if not isinstance(node, dict):
        return "?"
    kind = node.get("kind")
    if kind == "NON_NULL":
        return type_str(node.get("ofType")) + "!"
    if kind == "LIST":
        return "[" + type_str(node.get("ofType")) + "]"
    return node.get("name") or "?"


def named_type(node) -> str | None:
    """The innermost named type of a type reference."""
    while isinstance(node, dict) and not node.get("name"):
        node = node.get("ofType")
    return node.get("name") if isinstance(node, dict) else None


def _arg_text(arg) -> str:
    default = arg.get("defaultValue")
    return f"{arg['name']}: {type_str(arg['type'])}" + (f" = {default}" if default is not None else "")


def _fields(type_node) -> list[str]:
    out = []
    for field in type_node.get("fields") or []:
        args = ", ".join(_arg_text(a) for a in field.get("args") or [])
        out.append(f"    {field['name']}({args}) -> {type_str(field['type'])}")
    for field in type_node.get("inputFields") or []:
        default = field.get("defaultValue")
        out.append(f"    {field['name']}: {type_str(field['type'])}"
                   + (f" = {default}" if default is not None else ""))
    for value in type_node.get("enumValues") or []:
        out.append(f"    {value['name']}")
    return out


def _types(body: dict) -> tuple[list[dict], set]:
    schema = ((body or {}).get("data") or {}).get("__schema") or {}
    types = [t for t in schema.get("types") or [] if isinstance(t, dict)]
    roots = {(schema.get("queryType") or {}).get("name"),
             (schema.get("mutationType") or {}).get("name")}
    return types, roots


def type_lines(body: dict, names) -> list[str]:
    """The named types in full (fields or input fields with `!` for required,
    enum values). A name the schema does not have is reported as missing."""
    types, _roots = _types(body)
    by_name = {t.get("name"): t for t in types}
    lines = []
    for name in names:
        node = by_name.get(name)
        if node is None:
            lines.append(f"{name}: NOT IN THE SCHEMA")
            continue
        lines.append(f"{node.get('kind')} {name}")
        lines += _fields(node)
    return lines


def schema_lines(body: dict) -> list[str]:
    """The bill payer parts of an introspection response, as printable lines."""
    types, roots = _types(body)
    mentions = re.compile("bill", re.IGNORECASE)
    points_at = re.compile(r"bill\s*payer", re.IGNORECASE)

    lines = []
    for node in sorted(types, key=lambda t: t.get("name") or ""):
        name = node.get("name") or ""
        if name.startswith("__") or name in roots or not mentions.search(name):
            continue
        lines.append(f"{node.get('kind')} {name}")
        lines += _fields(node)

    lines.append("ROOT fields that mention bill payers:")
    for node in types:
        if node.get("name") in roots:
            for field in node.get("fields") or []:
                if mentions.search(field["name"]) or mentions.search(type_str(field["type"])):
                    lines.append(f"  {node['name']}.{field['name']} -> {type_str(field['type'])}")

    lines.append("OTHER types with a field that points at a bill payer:")
    for node in sorted(types, key=lambda t: t.get("name") or ""):
        name = node.get("name") or ""
        if name.startswith("__") or name in roots or mentions.search(name):
            continue
        for field in (node.get("fields") or []) + (node.get("inputFields") or []):
            if points_at.search(type_str(field["type"])) or points_at.search(field["name"]):
                lines.append(f"  {name}.{field['name']} -> {type_str(field['type'])}")
    return lines


def fetch_schema(client) -> dict:
    assert_read_only(SCHEMA_QUERY)
    return client.execute(query_path=SCHEMA_QUERY, variables={}, operation_name=SCHEMA_OP)


def run_schema(client, type_names=()) -> list[str]:
    body = fetch_schema(client)
    lines = schema_lines(body)
    if type_names:
        lines.append("REQUESTED types:")
        lines += type_lines(body, type_names)
    return lines


# --------------------------------------------------------------------------- #
# Discovering the shape of a bill payer's children (names only, from the schema)
# --------------------------------------------------------------------------- #
def _list_by_site_ids_field(by_name: dict, schema: dict) -> dict | None:
    """The `listBySiteIds` field of the bill payers query object.

    Found through the root query's `billPayers` field, whatever the object type is
    called (the old detector matched a type name that began "BillPayers" and so
    missed the real one). Falls back to any object whose `listBySiteIds` returns a
    bill payer page."""
    root = by_name.get((schema.get("queryType") or {}).get("name") or "") or {}
    entry = next((f for f in root.get("fields") or [] if f["name"] == "billPayers"), None)
    holder = by_name.get(named_type(entry["type"]) if entry else "") or {}
    for field in holder.get("fields") or []:
        if field["name"] == "listBySiteIds":
            return field
    for node in by_name.values():
        for field in (node or {}).get("fields") or []:
            if field["name"] == "listBySiteIds" and re.search(
                    r"bill\s*payer", named_type(field["type"]) or "", re.IGNORECASE):
                return field
    return None


def discover_shape(body: dict) -> dict:
    """How to ask for a bill payer's children, share, deletedAt, externalId and
    `includeDeleted`.

    Returns {"children_field", "child_path", "multiplier_field", "external_id",
    "deleted_at", "include_deleted_type", "include_deleted_default", "notes"}.
    `child_path` is the selection for one child id inside the children field ("" when
    the field is a plain list of ids, "childId" or "child { id }" otherwise).
    Anything that cannot be worked out is None/False and explained in notes.
    """
    types, roots = _types(body)
    schema = ((body or {}).get("data") or {}).get("__schema") or {}
    by_name = {t.get("name"): t for t in types}
    notes: list[str] = []
    shape = {"children_field": None, "child_path": None, "multiplier_field": None,
             "external_id": False, "deleted_at": False, "include_deleted_type": None,
             "include_deleted_default": None, "notes": notes}

    # The list query: `includeDeleted` (NON_NULL or not, with or without a default).
    listing = _list_by_site_ids_field(by_name, schema)
    if listing is None:
        notes.append("the bill payers listBySiteIds field was not found")
    else:
        for arg in listing.get("args") or []:
            if arg["name"] == "includeDeleted":
                shape["include_deleted_type"] = type_str(arg["type"])
                shape["include_deleted_default"] = arg.get("defaultValue")
        if shape["include_deleted_type"] is None:
            notes.append("listBySiteIds has no includeDeleted argument")

    bill_payer = by_name.get("BillPayer")
    if bill_payer is None:
        notes.append("BillPayer type not found")
        return shape
    fields = {f["name"]: f for f in bill_payer.get("fields") or []}
    shape["external_id"] = "externalId" in fields
    shape["deleted_at"] = "deletedAt" in fields
    if not shape["deleted_at"]:
        notes.append("BillPayer has no deletedAt field")

    candidates = []
    for name, field in fields.items():
        target = named_type(field["type"]) or ""
        required_args = [a["name"] for a in field.get("args") or []
                         if (a.get("type") or {}).get("kind") == "NON_NULL" and a.get("defaultValue") is None]
        if required_args:
            continue
        if target == "ChildShare":
            candidates.append((0, name, target))
        elif target == "Child":
            candidates.append((1, name, target))
        elif "child" in name.lower():
            candidates.append((2, name, target))
    if not candidates:
        notes.append("no children field found on BillPayer")
        return shape

    _rank, name, target = sorted(candidates)[0]
    shape["children_field"] = name
    target_type = by_name.get(target)
    sub = {f["name"]: f for f in (target_type or {}).get("fields") or []}
    if target_type is None or target_type.get("kind") in ("SCALAR", "ENUM"):
        shape["child_path"] = ""
    elif "childId" in sub:
        shape["child_path"] = "childId"
    elif "child" in sub:
        inner = by_name.get(named_type(sub["child"]["type"]) or "") or {}
        inner_fields = {f["name"] for f in inner.get("fields") or []}
        pick = "childId" if "childId" in inner_fields else "id" if "id" in inner_fields else None
        shape["child_path"] = f"child {{ {pick} }}" if pick else None
    elif "id" in sub:
        shape["child_path"] = "id"
    if shape["child_path"] is None:
        notes.append(f"cannot find a child id inside {target}")
    if "multiplier" in sub and shape["child_path"]:
        shape["multiplier_field"] = "multiplier"
    elif shape["child_path"]:
        notes.append(f"no multiplier field inside {target}")
    return shape


def build_query(shape: dict, with_include_deleted: bool) -> str:
    selection = ["billPayerId", "email"]
    if shape["external_id"]:
        selection.append("externalId")
    if shape.get("deleted_at"):
        selection.append("deletedAt")
    if shape["children_field"] and shape["child_path"] is not None:
        parts = [shape["child_path"]] if shape["child_path"] else []
        if shape.get("multiplier_field"):
            parts.append(shape["multiplier_field"])
        inner = f" {{ {' '.join(parts)} }}" if parts else ""
        selection.append(shape["children_field"] + inner)
    declared = "$siteIds: [SiteId!]!, $next: BillPayerCursor"
    passed = "siteIds: $siteIds, nextToken: $next"
    if with_include_deleted:
        declared += f", $includeDeleted: {shape['include_deleted_type']}"
        passed += ", includeDeleted: $includeDeleted"
    return (f"query {BILL_PAYERS_OP}({declared}) {{ billPayers {{ listBySiteIds({passed}) "
            f"{{ result {{ {' '.join(selection)} }} next }} }} }}\n")


def _child_entries(node: dict, shape: dict) -> list[tuple[str, float | None]]:
    """(child id, multiplier or None) for each child entry of a bill payer node."""
    field = shape["children_field"]
    if not field:
        return []
    out = []
    for item in node.get(field) or []:
        value = item
        if isinstance(item, dict):
            for part in re.findall(r"\w+", shape["child_path"] or ""):
                value = value.get(part) if isinstance(value, dict) else None
        if not value:
            continue
        multiplier = None
        if isinstance(item, dict) and shape.get("multiplier_field"):
            try:
                multiplier = float(item.get(shape["multiplier_field"]))
            except (TypeError, ValueError):
                multiplier = None
        out.append((str(value), multiplier))
    return out


def _child_ids(node: dict, shape: dict) -> set[str]:
    return {child for child, _m in _child_entries(node, shape)}


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
class AuditRefused(Exception):
    """The audit cannot run as designed (for example `includeDeleted` was not
    detected). It never falls back to the server default."""


def scan_site(client, site_id: str, shape: dict, include_deleted, clock=time.monotonic) -> dict:
    """Every bill payer at one site. `include_deleted` None = do not pass the argument
    (the server's default); only the audit's refusal-free callers use that. Rows are
    held in memory only."""
    with_flag = include_deleted is not None
    text = build_query(shape, with_flag)
    handle = tempfile.NamedTemporaryFile("w", suffix=".graphql", delete=False, encoding="utf-8")
    try:
        handle.write(text)
        handle.close()
        path = Path(handle.name)
        assert_read_only(path)
        started = clock()
        rows = []
        cursor = None
        pages = 0
        while pages < MAX_PAGES:
            variables = {"siteIds": [site_id], "next": cursor}
            if with_flag:
                variables["includeDeleted"] = bool(include_deleted)
            body = client.execute(query_path=path, variables=variables, operation_name=BILL_PAYERS_OP)
            pages += 1
            listing = (((body or {}).get("data") or {}).get("billPayers") or {}).get("listBySiteIds") or {}
            for node in listing.get("result") or []:
                if isinstance(node, dict) and node.get("billPayerId"):
                    entries = _child_entries(node, shape)
                    rows.append({
                        "id": str(node["billPayerId"]),
                        "email": normalise_email(node.get("email")),
                        "children": {c for c, _m in entries},
                        "multipliers": [m for _c, m in entries if m is not None],
                        "entries": len(entries),
                        "ext": bool(str(node.get("externalId") or "").strip()),
                        "deleted": bool(node.get("deletedAt")),
                    })
            cursor = listing.get("next") or None
            if not cursor:
                break
        return {"rows": rows, "pages": pages, "seconds": round(clock() - started, 2)}
    finally:
        os.unlink(handle.name)


DEFINITIONS = {
    "duplicate email group": "a canonical email (trimmed, lower case, googlemail.com = gmail.com) "
                             "that maps to two or more bill payer ids",
    "idsInGroups": "bill payer ids inside duplicate email groups",
    "idsDeleted / idsLiveWithChildren / idsLiveWithNoChildren": "of those ids: deletedAt set; not "
                             "deleted with at least one child; not deleted with no children",
    "emailsNoneHaveChildren": "groups where no id has a child",
    "emailsOneHasChildren": "groups where exactly one id has children",
    "emailsAllIdentical": "groups with two or more ids that have children, all with the SAME child set",
    "emailsAllDifferent": "groups with two or more ids that have children, every child set DIFFERENT",
    "emailsMixed": "groups with two or more ids that have children where some share a child set "
                   "and others differ (for example two identical sets and a third that differs)",
    "multiplierEntries": "count of child entries by multiplier value, rounded to 3 decimal places",
    "liveWith2PlusChildren": "bill payers not deleted that have two or more child entries",
}


def _duplicate_stats(rows) -> dict:
    """Counts for the canonical emails that map to two or more bill payer ids."""
    by_email = defaultdict(list)
    for row in rows:
        if row["email"]:
            by_email[row["email"]].append(row)
    duplicated = {e: members for e, members in by_email.items() if len({m["id"] for m in members}) >= 2}
    stats = Counter()
    for members in duplicated.values():
        for member in members:
            stats["idsInGroups"] += 1
            if member["deleted"]:
                stats["idsDeleted"] += 1
            elif member["children"]:
                stats["idsLiveWithChildren"] += 1
            else:
                stats["idsLiveWithNoChildren"] += 1
        non_empty = [frozenset(m["children"]) for m in members if m["children"]]
        stats["idsWithZeroChildren"] += sum(1 for m in members if not m["children"])
        groups = Counter(non_empty)
        shared = sum(n for n in groups.values() if n >= 2)
        stats["idsSharingAnIdenticalChildSet"] += shared
        stats["idsWithADifferentChildSet"] += len(non_empty) - shared
        if not non_empty:
            stats["emailsNoneHaveChildren"] += 1
        elif len(non_empty) == 1:
            stats["emailsOneHasChildren"] += 1
        elif len(groups) == 1:
            stats["emailsAllIdentical"] += 1
        elif all(n == 1 for n in groups.values()):
            stats["emailsAllDifferent"] += 1
        else:
            stats["emailsMixed"] += 1
    keys = ("idsInGroups", "idsDeleted", "idsLiveWithChildren", "idsLiveWithNoChildren",
            "idsWithZeroChildren", "idsSharingAnIdenticalChildSet", "idsWithADifferentChildSet",
            "emailsNoneHaveChildren", "emailsOneHasChildren", "emailsAllIdentical",
            "emailsAllDifferent", "emailsMixed")
    out = {"emailsWith2PlusBillPayerIds": len(duplicated)}
    out.update({k: stats[k] for k in keys})
    return out


def _site_counts(scan: dict) -> dict:
    rows = scan["rows"]
    multipliers = Counter(f"{m:.3f}" for r in rows for m in r["multipliers"])
    counts = {
        "billPayers": len({r["id"] for r in rows}),
        "pages": scan["pages"],
        "seconds": scan["seconds"],
        "withEmail": sum(1 for r in rows if r["email"]),
        "withoutEmail": sum(1 for r in rows if not r["email"]),
        "withExternalId": sum(1 for r in rows if r["ext"]),
        "withNoChildren": sum(1 for r in rows if not r["children"]),
        "withDeletedAtSet": sum(1 for r in rows if r["deleted"]),
        "liveWith2PlusChildren": sum(1 for r in rows if not r["deleted"] and r["entries"] >= 2),
        "multiplierEntries": dict(sorted(multipliers.items())),
    }
    counts.update(_duplicate_stats(rows))
    return counts


def require_include_deleted(shape: dict) -> None:
    """Refuse (never fall back to the server default) when `includeDeleted` was not
    detected on the list query."""
    if shape.get("include_deleted_type") is None:
        raise AuditRefused(
            "refusing: the includeDeleted argument of listBySiteIds was not detected "
            f"({'; '.join(shape['notes']) or 'no detail'}); the server default would "
            "silently include deleted bill payers")


def audit(client, sites: dict[str, str], clock=time.monotonic) -> dict:
    """Counts only. `sites` maps a site code to its institution id."""
    shape = discover_shape(fetch_schema(client))
    require_include_deleted(shape)
    per_site: dict[str, dict] = {}
    site_emails: dict[str, set] = defaultdict(set)
    total_pages, total_seconds = 0, 0.0
    for code, site_id in sites.items():
        per_site[code] = {}
        for label, flag in (("includeDeletedFalse", False), ("includeDeletedTrue", True)):
            scan = scan_site(client, site_id, shape, flag, clock)
            per_site[code][label] = _site_counts(scan)
            total_pages += scan["pages"]
            total_seconds += scan["seconds"]
            if flag is False:
                for row in scan["rows"]:
                    if row["email"]:
                        site_emails[row["email"]].add(code)
    return {
        # The pipeline's existing scan (pull_roster) passes NO includeDeleted at all,
        # so it gets the server default (`shape.include_deleted_default`).
        "existingScanPassesIncludeDeleted": "includedeleted" in BILL_PAYERS_QUERY.read_text(
            encoding="utf-8").lower(),
        "shape": {k: v for k, v in shape.items() if k != "notes"} | {"notes": list(shape["notes"])},
        "definitions": DEFINITIONS,
        "sites": per_site,
        "emailsAtMoreThanOneSite": sum(1 for s in site_emails.values() if len(s) > 1),
        "totalPages": total_pages,
        "totalSeconds": round(total_seconds, 2),
    }
