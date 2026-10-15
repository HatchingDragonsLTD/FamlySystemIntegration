"""Web handler for the contact_lookup action (read-only).

Takes a contact's email from a HubSpot workflow and returns the matching Famly
contact id and bill payer id, for the workflow to write onto the contact. See
`runner.py` for the lookup rules and why this is not an existence check.

Request body:

    {"action": "contact_lookup", "email": "..."}

Response `data`, via the standard envelope (ids only -- no names, no emails):

    {"contactId", "contactMatched", "billPayerId", "billPayerMatched", "source"}

    200  both lookups completed without ambiguity, including not-found. A
         contact with no bill payer (or the reverse) is normal -- their
         billing email can differ -- and gets a warning.
    409  EITHER side has 2+ distinct ids: both ids are null so HubSpot writes
         nothing half-done; the counts and conflicting ids are in `errors`.
    422  missing, non-string or malformed email. No Famly call is made.
    503  the live fetch failed: the search was incomplete, never "not found".

The email address is never logged.
"""

import logging

from . import runner

logger = logging.getLogger(__name__)

ACTION_NAME = "contact_lookup"


def _data(**overrides) -> dict:
    data = {
        "contactId": None,
        "contactMatched": False,
        "billPayerId": None,
        "billPayerMatched": False,
        "source": None,
        "warnings": [],
        "errors": [],
    }
    data.update(overrides)
    return data


def handle(payload: dict) -> tuple[dict, int]:
    email = payload.get("email") if isinstance(payload, dict) else None

    if not isinstance(email, str) or "@" not in runner.normalise_email(email):
        return _data(errors=["missing or invalid 'email'"]), 422

    try:
        result = runner.lookup(email)
    except runner.LiveFetchError as exc:
        logger.error("contact_lookup: live fetch failed: %s", exc)
        return _data(errors=[f"search incomplete, not reporting 'not found': {exc}"]), 503

    contacts, bill_payers = result.contact_ids, result.bill_payer_ids

    if len(contacts) > 1 or len(bill_payers) > 1:
        errors = []
        if len(contacts) > 1:
            errors.append(
                f"contactMatchCount={len(contacts)}: ambiguous contact ids "
                f"{', '.join(contacts)}"
            )
        if len(bill_payers) > 1:
            errors.append(
                f"billPayerMatchCount={len(bill_payers)}: ambiguous bill payer ids "
                f"{', '.join(bill_payers)}"
            )
        logger.error(
            "contact_lookup: ambiguous source=%s contacts=%s bill_payers=%s",
            result.source,
            contacts,
            bill_payers,
        )
        return _data(source=result.source, errors=errors), 409

    warnings = []
    if not contacts:
        warnings.append("no Famly contact found for this email")
    if not bill_payers:
        warnings.append("no Famly bill payer found for this email")

    data = _data(
        contactId=contacts[0] if contacts else None,
        contactMatched=bool(contacts),
        billPayerId=bill_payers[0] if bill_payers else None,
        billPayerMatched=bool(bill_payers),
        source=result.source,
        warnings=warnings,
    )
    logger.info(
        "contact_lookup: source=%s contactId=%s billPayerId=%s",
        result.source,
        data["contactId"],
        data["billPayerId"],
    )
    return data, 200
