"""READ-ONLY probe for the bill payer phase. No mutation of any kind, anywhere.

Two reports, both printing counts, ids, field names and type names ONLY (an email
is held in memory to be counted and never printed, logged or stored):

  * `schema_lines`: what the live schema says about bill payers: the BillPayer
    type, BillPayerInput, the query and mutation fields, the input of every
    mutation (create, update, addChildren), and every field of another type that
    points at a bill payer (how a bill payer links to a contact or a child).
  * `audit`: per site, how many bill payers, how many pages and seconds a full
    scan takes, how many have an email, how many canonical emails map to two or
    more bill payer ids (the duplicate audit that was never run), and how many
    emails appear at more than one site.

Famly's GraphQL endpoint takes queries by POST; "read-only" here means every
document sent is checked to contain no `mutation`.
"""

import re
import time
from collections import defaultdict
from pathlib import Path

from actions.onboard_dry_run.famly_reads import normalise_email

HERE = Path(__file__).parent
SCHEMA_QUERY = HERE / "introspect.graphql"
SCHEMA_OP = "ProbeBillPayerSchema"
BILL_PAYERS_QUERY = HERE.parent / "pull_roster" / "bill_payers_query.graphql"
BILL_PAYERS_OP = "PullRosterBillPayers"
MAX_PAGES = 500          # a runaway cursor must end


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


def _fields(type_node) -> list[str]:
    out = []
    for field in type_node.get("fields") or []:
        args = ", ".join(f"{a['name']}: {type_str(a['type'])}" for a in field.get("args") or [])
        out.append(f"    {field['name']}({args}) -> {type_str(field['type'])}")
    for field in type_node.get("inputFields") or []:
        out.append(f"    {field['name']}: {type_str(field['type'])}")
    for value in type_node.get("enumValues") or []:
        out.append(f"    {value['name']}")
    return out


def schema_lines(body: dict) -> list[str]:
    """The bill payer parts of an introspection response, as printable lines."""
    schema = ((body or {}).get("data") or {}).get("__schema") or {}
    types = [t for t in schema.get("types") or [] if isinstance(t, dict)]
    roots = {(schema.get("queryType") or {}).get("name"),
             (schema.get("mutationType") or {}).get("name")}
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


def run_schema(client) -> list[str]:
    assert_read_only(SCHEMA_QUERY)
    body = client.execute(query_path=SCHEMA_QUERY, variables={}, operation_name=SCHEMA_OP)
    return schema_lines(body)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def scan_site(client, site_id: str, clock=time.monotonic) -> dict:
    """Every bill payer at one site: ([(id, canonical email or "")], pages, seconds)."""
    assert_read_only(BILL_PAYERS_QUERY)
    started = clock()
    rows: list[tuple[str, str]] = []
    cursor = None
    pages = 0
    while pages < MAX_PAGES:
        body = client.execute(
            query_path=BILL_PAYERS_QUERY,
            variables={"siteIds": [site_id], "next": cursor},
            operation_name=BILL_PAYERS_OP,
        )
        pages += 1
        listing = (((body or {}).get("data") or {}).get("billPayers") or {}).get("listBySiteIds") or {}
        for node in listing.get("result") or []:
            if isinstance(node, dict) and node.get("billPayerId"):
                rows.append((str(node["billPayerId"]), normalise_email(node.get("email"))))
        cursor = listing.get("next") or None
        if not cursor:
            break
    return {"rows": rows, "pages": pages, "seconds": round(clock() - started, 2)}


def audit(client, sites: dict[str, str], clock=time.monotonic) -> dict:
    """Counts only. `sites` maps a site code to its institution id."""
    per_site = {}
    site_emails: dict[str, set] = defaultdict(set)
    total_pages, total_seconds = 0, 0.0
    for code, site_id in sites.items():
        scan = scan_site(client, site_id, clock)
        rows = scan["rows"]
        ids_by_email = defaultdict(set)
        for bill_payer_id, email in rows:
            if email:
                ids_by_email[email].add(bill_payer_id)
        duplicated = {e for e, ids in ids_by_email.items() if len(ids) >= 2}
        per_site[code] = {
            "billPayers": len({i for i, _ in rows}),
            "pages": scan["pages"],
            "seconds": scan["seconds"],
            "withEmail": sum(1 for _i, e in rows if e),
            "withoutEmail": sum(1 for _i, e in rows if not e),
            "emailsWith2PlusBillPayerIds": len(duplicated),
            "billPayerIdsInThoseEmails": sum(len(ids_by_email[e]) for e in duplicated),
        }
        for email in ids_by_email:
            site_emails[email].add(code)
        total_pages += scan["pages"]
        total_seconds += scan["seconds"]
    return {
        "sites": per_site,
        "emailsAtMoreThanOneSite": sum(1 for s in site_emails.values() if len(s) > 1),
        "totalPages": total_pages,
        "totalSeconds": round(total_seconds, 2),
    }
