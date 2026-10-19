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
def discover_shape(body: dict) -> dict:
    """How to ask for a bill payer's children, externalId and `includeDeleted`.

    Returns {"children_field", "child_path", "external_id", "include_deleted_type",
    "notes"}. `child_path` is the selection for one child id inside the children
    field ("" when the field is a plain list of ids, "childId" or "child { id }"
    otherwise). Anything that cannot be worked out is None and explained in notes.
    """
    types, roots = _types(body)
    by_name = {t.get("name"): t for t in types}
    notes: list[str] = []
    shape = {"children_field": None, "child_path": None, "external_id": False,
             "include_deleted_type": None, "include_deleted_default": None, "notes": notes}

    bill_payer = by_name.get("BillPayer")
    if bill_payer is None:
        notes.append("BillPayer type not found")
        return shape
    fields = {f["name"]: f for f in bill_payer.get("fields") or []}
    shape["external_id"] = "externalId" in fields

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
    else:
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

    # The list query: does it take `includeDeleted`, and what does the schema say
    # its default is?
    for node in types:
        if (node.get("name") or "").lower().startswith("billpayers") and node.get("fields"):
            for field in node["fields"]:
                if field["name"] == "listBySiteIds":
                    for arg in field.get("args") or []:
                        if arg["name"] == "includeDeleted":
                            shape["include_deleted_type"] = type_str(arg["type"])
                            shape["include_deleted_default"] = arg.get("defaultValue")
    if shape["include_deleted_type"] is None:
        notes.append("listBySiteIds has no includeDeleted argument")
    return shape


def build_query(shape: dict, with_include_deleted: bool) -> str:
    selection = ["billPayerId", "email"]
    if shape["external_id"]:
        selection.append("externalId")
    if shape["children_field"] and shape["child_path"] is not None:
        inner = f" {{ {shape['child_path']} }}" if shape["child_path"] else ""
        selection.append(shape["children_field"] + inner)
    declared = "$siteIds: [SiteId!]!, $next: BillPayerCursor"
    passed = "siteIds: $siteIds, nextToken: $next"
    if with_include_deleted:
        declared += f", $includeDeleted: {shape['include_deleted_type']}"
        passed += ", includeDeleted: $includeDeleted"
    return (f"query {BILL_PAYERS_OP}({declared}) {{ billPayers {{ listBySiteIds({passed}) "
            f"{{ result {{ {' '.join(selection)} }} next }} }} }}\n")


def _child_ids(node: dict, shape: dict) -> set[str]:
    field = shape["children_field"]
    if not field:
        return set()
    out = set()
    for item in node.get(field) or []:
        value = item
        if isinstance(item, dict):
            for part in re.findall(r"\w+", shape["child_path"] or ""):
                value = value.get(part) if isinstance(value, dict) else None
        if value:
            out.add(str(value))
    return out


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def scan_site(client, site_id: str, shape: dict, include_deleted, clock=time.monotonic) -> dict:
    """Every bill payer at one site. `include_deleted` None = do not pass the argument
    (the server's default). Rows are held in memory: (id, canonical email, child ids,
    has externalId)."""
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
                    rows.append((str(node["billPayerId"]), normalise_email(node.get("email")),
                                 _child_ids(node, shape), bool(str(node.get("externalId") or "").strip())))
            cursor = listing.get("next") or None
            if not cursor:
                break
        return {"rows": rows, "pages": pages, "seconds": round(clock() - started, 2)}
    finally:
        os.unlink(handle.name)


def _duplicate_stats(rows) -> dict:
    """Counts for the canonical emails that map to two or more bill payer ids."""
    by_email = defaultdict(dict)                     # email -> {id: child set}
    for bill_payer_id, email, children, _ext in rows:
        if email:
            by_email[email][bill_payer_id] = children
    duplicated = {e: ids for e, ids in by_email.items() if len(ids) >= 2}
    zero = identical = different = 0
    all_identical = all_different = mixed = 0
    for ids in duplicated.values():
        sets = list(ids.values())
        zero += sum(1 for s in sets if not s)
        non_empty = [frozenset(s) for s in sets if s]
        groups = Counter(non_empty)
        shared = sum(n for n in groups.values() if n >= 2)
        identical += shared
        different += len(non_empty) - shared
        if non_empty and len(groups) == 1 and len(non_empty) >= 2:
            all_identical += 1
        elif non_empty and all(n == 1 for n in groups.values()) and len(non_empty) >= 2:
            all_different += 1
        elif non_empty:
            mixed += 1
    return {
        "emailsWith2PlusBillPayerIds": len(duplicated),
        "billPayerIdsInThoseEmails": sum(len(i) for i in duplicated.values()),
        "idsWithZeroChildren": zero,
        "idsSharingAnIdenticalChildSet": identical,
        "idsWithADifferentChildSet": different,
        "emailsWhereEveryNonEmptySetIsIdentical": all_identical,
        "emailsWhereEveryNonEmptySetDiffers": all_different,
        "emailsMixed": mixed,
    }


def _site_counts(scan: dict) -> dict:
    rows = scan["rows"]
    counts = {
        "billPayers": len({r[0] for r in rows}),
        "pages": scan["pages"],
        "seconds": scan["seconds"],
        "withEmail": sum(1 for r in rows if r[1]),
        "withoutEmail": sum(1 for r in rows if not r[1]),
        "withExternalId": sum(1 for r in rows if r[3]),
        "withNoChildren": sum(1 for r in rows if not r[2]),
    }
    counts.update(_duplicate_stats(rows))
    return counts


def audit(client, sites: dict[str, str], clock=time.monotonic) -> dict:
    """Counts only. `sites` maps a site code to its institution id."""
    shape = discover_shape(fetch_schema(client))
    variants = [("includeDeletedFalse", False), ("includeDeletedTrue", True)]
    if shape["include_deleted_type"] is None:
        variants = [("serverDefault", None)]

    per_site: dict[str, dict] = {}
    site_emails: dict[str, set] = defaultdict(set)
    total_pages, total_seconds = 0, 0.0
    for code, site_id in sites.items():
        per_site[code] = {}
        for label, flag in variants:
            scan = scan_site(client, site_id, shape, flag, clock)
            per_site[code][label] = _site_counts(scan)
            total_pages += scan["pages"]
            total_seconds += scan["seconds"]
            if label in ("includeDeletedFalse", "serverDefault"):
                for _id, email, _children, _ext in scan["rows"]:
                    if email:
                        site_emails[email].add(code)
    return {
        # The scan the pipeline already has (pull_roster) passes no includeDeleted at
        # all, so it gets the server's default (`shape.include_deleted_default`).
        "existingScanPassesIncludeDeleted": "includedeleted" in BILL_PAYERS_QUERY.read_text(
            encoding="utf-8").lower(),
        "shape": {k: v for k, v in shape.items() if k != "notes"} | {"notes": list(shape["notes"])},
        "sites": per_site,
        "emailsAtMoreThanOneSite": sum(1 for s in site_emails.values() if len(s) > 1),
        "totalPages": total_pages,
        "totalSeconds": round(total_seconds, 2),
    }
