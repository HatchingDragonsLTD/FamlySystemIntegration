"""probe-reference-schema: ONE-OFF, READ-ONLY. DELETE THIS FOLDER (and its main.py line) once
the answer is in docs/status.md.

Question: can a HubSpot id be stored on Famly contacts and bill payers as an external
reference and found again by a lookup? Prints TYPE AND FIELD NAMES ONLY from the live
schema (reusing probe_bill_payers' introspection), plus, with --bill-payer-id, counts and
booleans about that one payer's invoice recipients. Every document sent is checked to
contain no mutation. Nothing is written; no ids or personal data are printed.
"""

import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from actions.probe_bill_payers import cli as bp_cli
from actions.probe_bill_payers import runner
from integrations import catalogue

NAME = "probe-reference-schema"
HELP = "ONE-OFF READ-ONLY: can contacts / bill payers carry an external reference and be looked up by it?"
HERE = Path(__file__).parent
RECIPIENTS_QUERY = HERE / "payer_recipients.graphql"
RECIPIENTS_OP = "ProbePayerRecipients"
CONTACTS_QUERY = HERE.parent / "onboard_dry_run" / "child_contacts.graphql"
CONTACTS_OP = "OnboardChildContacts"
MAX_PAGES = 500

REF_WORDS = re.compile(r"external|foreign|systemsref|byreference|reference", re.I)
TYPES = {
    "public": ("ContactInput", "ContactUpdate", "ContactsMutation", "ContactQueries", "ForeignContact",
               "ForeignIdReferenceInput", "BillPayerInput", "BillPayerUpdateInput", "BillPayerMutations",
               "BillPayerQueries", "BillPayerReferenceInput", "InvoiceRecipient", "InvoiceRecipientInput",
               "InvoiceRecipientDeleteInput"),
    "internal": ("ContactsMutation", "ContactInvitationInput", "ContactUpdate", "BillPayerMutations",
                 "BillPayerExternalSystemReference", "BillpayerExternalSystemReferenceInput",
                 "ExternalSystemName", "BillPayers"),
}


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", metavar="PATH")
    parser.add_argument("--sites", default=bp_cli.DEFAULT_SITES)
    parser.add_argument("--bill-payer-id", metavar="ID",
                        help="Report that payer's invoice-recipient counts and booleans (never printed)")


def reference_fields(body: dict) -> list[str]:
    types, _roots = runner._types(body)
    lines = []
    for node in sorted(types, key=lambda t: t.get("name") or ""):
        if (node.get("name") or "").startswith("__"):
            continue
        for field in (node.get("fields") or []) + (node.get("inputFields") or []):
            if REF_WORDS.search(field["name"]):
                args = ", ".join(runner._arg_text(a) for a in field.get("args") or [])
                lines.append(f"  {node['kind']} {node['name']}.{field['name']}({args}) -> "
                             f"{runner.type_str(field['type'])}")
    return lines


def schema_report(api: str) -> list[str]:
    body = runner.fetch_schema(bp_cli._client(api))
    lines = [f"===== {api.upper()} API: fields whose name suggests an external reference"]
    lines += reference_fields(body)
    lines.append(f"===== {api.upper()} API: the types that matter, in full")
    lines += runner.type_lines(body, TYPES[api])
    return lines


def _pages(client, query: Path, op: str, variables: dict, path: list[str], cursor_var: str):
    runner.assert_read_only(query)
    cursor = None
    for _ in range(MAX_PAGES):
        body = client.execute(query_path=query, variables={**variables, cursor_var: cursor},
                              operation_name=op)
        node = body
        for key in path:
            node = (node or {}).get(key)
        for row in (node or {}).get("result") or []:
            yield row
        cursor = (node or {}).get("next")
        if not cursor:
            return


def payer_report(client, sites: dict, payer_id: str) -> dict:
    """Counts and booleans only. `contactIdIsOnALinkedChild[i]` is whether the i-th invoice
    recipient's contactId is the id of a contact listed on one of the payer's children."""
    found = None
    for institution_id in sites.values():
        for row in _pages(client, RECIPIENTS_QUERY, RECIPIENTS_OP,
                          {"siteIds": [institution_id], "includeDeleted": True},
                          ["data", "billPayers", "listBySiteIds"], "next"):
            if row.get("billPayerId") == payer_id:
                found = row
                break
        if found:
            break
    if not found:
        return {"found": False}
    child_ids = [c["childId"] for c in found.get("children") or []]
    on_child: set[str] = set()
    for child_id in child_ids:
        for row in _pages(client, CONTACTS_QUERY, CONTACTS_OP, {"childIds": [child_id]},
                          ["data", "contacts", "list"], "next"):
            on_child.add(row["id"])
    recipients = [r["contactId"] for r in found.get("invoiceRecipients") or []]
    return {"found": True, "linkedChildren": len(child_ids), "invoiceRecipients": len(recipients),
            "contactsOnLinkedChildren": len(on_child),
            "contactIdIsOnALinkedChild": [cid in on_child for cid in recipients]}


def handle(args: argparse.Namespace):
    if args.env:
        load_dotenv(args.env, override=False)
    for api in ("public", "internal"):
        for line in schema_report(api):
            print(line)
    if not args.bill_payer_id:
        return {"report": "reference-schema"}
    sites = catalogue.load_sites()
    wanted = [c.strip().upper() for c in args.sites.split(",") if c.strip()]
    try:
        chosen = {c: sites[c]["institutionId"] for c in wanted}
    except KeyError:
        print("unknown site code", file=sys.stderr)
        raise SystemExit(3) from None
    return payer_report(bp_cli._client("public"), chosen, args.bill_payer_id)
