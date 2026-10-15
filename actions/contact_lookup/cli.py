"""CLI wiring for contact-lookup: check an email's Famly ids from the server.

Exposes the standard interface main.py discovers: NAME, HELP, add_args, handle.
Runs the same handler the web action uses, so it reports exactly what HubSpot
would receive. Read-only.
"""

import argparse

from . import web

NAME = "contact-lookup"
HELP = (
    "Look up the Famly contact id and bill payer id for a contact's email "
    "(read-only; roster cache first, live Famly fallback)"
)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("email", help="The contact's email address")


def handle(args: argparse.Namespace):
    data, status = web.handle({"action": web.ACTION_NAME, "email": args.email})
    return {"status": status, **data}
