"""onboard-phone-audit: READ-ONLY. For a file of HubSpot deal ids, read each deal's
associated contacts' phone properties IN MEMORY and print how many numbers the
normaliser rejects, by reason code, with the SHAPE of each rejected value (every digit
replaced by N, letters by A, `+ ( ) - . ` and spaces kept, anything else `?`).

It never prints, logs or stores a number, a name or an email. The only HubSpot calls are
GETs (HubSpotClient has no other transport) and nothing is sent to Famly.
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from actions.onboard_dry_run import links
from core.hubspot import HubSpotClient, HubSpotError

from . import phone

NAME = "onboard-phone-audit"
HELP = ("READ-ONLY: count phone numbers the normaliser rejects, by reason code and shape "
        "(no numbers printed), for the deal ids in a file")

_KEEP = set("+()-. ")


def shape(value: str) -> str:
    """The value with digits as N and letters as A; only `+ ( ) - .` and spaces kept."""
    out = []
    for ch in str(value):
        if ch.isdigit():
            out.append("N")
        elif ch.isalpha():
            out.append("A")
        elif ch in _KEEP:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", required=True, metavar="PATH",
                        help="One deal id per line (digits only); blank lines and # comments are ignored")


def read_ids(path: str) -> list[str]:
    ids: list[str] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if not re.fullmatch(r"[0-9]{1,20}", text):
            print(f"line {number}: not a deal id (digits only); nothing was read", file=sys.stderr)
            raise SystemExit(3)
        ids.append(text)
    return list(dict.fromkeys(ids))


def audit(client, deal_ids: list[str]) -> dict:
    labels = links.load_labels(client)
    reasons: Counter = Counter()
    shapes: dict[str, Counter] = defaultdict(Counter)
    failed: dict[str, list[str]] = {}
    totals = Counter()
    for deal_id in deal_ids:
        try:
            contacts = links.deal_contacts(client, labels, deal_id)
        except HubSpotError:
            failed[deal_id] = ["read_error"]
            continue
        found = []
        for contact in contacts:
            totals["contacts"] += 1
            source, _type, number, reason, prop = phone.pick_phone_detail(contact.props)
            if source == "none":
                totals["no_phone"] += 1
            elif number:
                totals["accepted"] += 1
            else:
                totals["rejected"] += 1
                reasons[reason] += 1
                shapes[reason][shape(contact.props.get(prop) or "")] += 1
                found.append(reason)
        if found:
            failed[deal_id] = sorted(found)
    return {
        "mode": "read_only",
        "dealsRead": len(deal_ids),
        "contacts": totals["contacts"],
        "accepted": totals["accepted"],
        "noPhone": totals["no_phone"],
        "rejected": totals["rejected"],
        "byReason": dict(sorted(reasons.items())),
        "shapesByReason": {r: dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))
                           for r, c in sorted(shapes.items())},
        "dealsWithRejections": dict(sorted(failed.items())),
    }


def handle(args: argparse.Namespace):
    ids = read_ids(args.file)
    print(f"{len(ids)} deal id(s) read", file=sys.stderr)
    return audit(HubSpotClient(), ids)
