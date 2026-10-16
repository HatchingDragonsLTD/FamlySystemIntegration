"""Build the ChildInput and ContactInput for Famly, exactly as proven by
probe-child-create and probe-contact-create.

Personal data (names, emails, phones, addresses, dates of birth) is read from
the in-memory HubSpot context, put in the payload, sent, and dropped. These
functions return payloads and codes; nothing here logs or stores a value.

CHILD: name as a plain "First Last" string; birthday `%Y-%m-%d`; currentGroupId
from the derivation; `records: []`, `sensitiveRecords: []`; gender OMITTED;
sitesRelation [{siteId, firstDay, lastDay}] (lastDay is ALWAYS sent: the deal's
`end_date`, or the first 31 August on or after the 4th birthday); externalId and externalSystems
References for HUBSPOT (deal id) and URN (child_urn, if present). `isTestChild`
is added ONLY for a test deal; otherwise the field is not sent. No nationality,
language, birth place or extra info.

CONTACT: name "First Last"; email trimmed and lower-cased; childIds [child];
`roles: []` ALWAYS (empty, so no login and no invitation is created); records
RELATION = "Parent"; emergencyContact true only for the bill payer; ONE phone
(see phone.py); an address only if street, zip and city are all present.
"""

from datetime import date

from actions.onboard import phone as phone_mod
from actions.onboard_dry_run import derive


class InputIncomplete(Exception):
    """A required value is missing; `code` says which."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def full_name(first, last) -> str | None:
    """"First Last", or None if either part is missing."""
    first, last = _clean(first), _clean(last)
    return f"{first} {last}" if first and last else None


def build_child_input(deal: dict, deal_id: str, plan: dict, is_test_child: bool) -> dict:
    """The ChildInput for a child that would be created.

    Raises:
        InputIncomplete: the name, birth date, start date, group or site is
            missing (the plan holds on these first; this is the last line).
    """
    name = full_name(deal.get("child_first_name"), deal.get("child_last_name"))
    if name is None:
        raise InputIncomplete("child_name_incomplete")
    try:
        birthday = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
        start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
        # The deal's end date, or the 4th-birthday default: lastDay is ALWAYS sent.
        end, _defaulted = derive.resolve_end_date(deal)
    except ValueError:
        raise InputIncomplete("date_invalid") from None
    if birthday is None:
        raise InputIncomplete("dob_missing")
    if start is None:
        raise InputIncomplete("start_date_missing")
    if not plan.get("groupId") or not plan.get("siteId"):
        raise InputIncomplete("group_missing")

    if end is None:
        raise InputIncomplete("end_date_missing")     # no end date and no birth date to default from
    if end < start:
        raise InputIncomplete("end_date_before_start")
    relation = {
        "siteId": plan["siteId"],
        "firstDay": start.strftime("%Y-%m-%d"),
        "lastDay": end.strftime("%Y-%m-%d"),
    }

    payload = {
        "name": name,
        "birthday": birthday.strftime("%Y-%m-%d"),
        "currentGroupId": plan["groupId"],
        "records": [],
        "sensitiveRecords": [],
        "sitesRelation": [relation],
    }
    references = [{"foreignId": str(deal_id), "system": "HUBSPOT"}]
    urn = _clean(deal.get("child_urn"))
    if urn:
        payload["externalId"] = urn
        references.append({"foreignId": urn, "system": "URN"})
    payload["externalSystemsReferences"] = references
    if is_test_child:
        payload["isTestChild"] = True   # only ever for a test deal
    return payload


def canonical_email(value) -> str:
    """Trimmed and lower-cased: the form sent to Famly."""
    return str(value or "").strip().lower()


def build_contact_input(contact, child_id: str) -> tuple[dict, list[str]]:
    """(ContactInput, warning codes) for one DealContact.

    Raises:
        InputIncomplete: `contact_name_incomplete` if the first or last name is
            missing (the contact is held; nothing is sent), `contact_no_email`.
    """
    props = contact.props
    name = full_name(props.get("firstname"), props.get("lastname"))
    if name is None:
        raise InputIncomplete("contact_name_incomplete")
    email = canonical_email(contact.email)
    if not email:
        raise InputIncomplete("contact_no_email")

    warnings: list[str] = []

    source, famly_type, number = phone_mod.pick_phone(props)
    phones = []
    if number:
        phones.append({"value": number, "phoneType": famly_type})
    elif source != "none":
        warnings.append("phone_unnormalised")
    else:
        warnings.append("phone_missing")

    payload = {
        "name": name,
        "email": email,
        "phoneNumbers": phones,
        "records": [{"key": "RELATION", "value": "Parent"}],
        "childIds": [child_id],
        "roles": [],
        "emergencyContact": bool(contact.is_bill_payer),
    }

    street, zip_code, city = (_clean(props.get(k)) for k in ("address", "zip", "city"))
    if street and zip_code and city:
        address = {"street": street, "zip": zip_code, "city": city}
        for key in ("state", "country"):
            value = _clean(props.get(key))
            if value:
                address[key] = value   # as given: no defaulting of the country
        payload["address"] = address
    else:
        warnings.append("address_incomplete")

    return payload, warnings
