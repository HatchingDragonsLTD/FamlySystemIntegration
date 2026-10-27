"""HubSpot association logic: which contact is the bill payer, which deal is the
Sales deal, and the quotes on the deal. Read-only (see core/hubspot.py).

DO NOT TRUST TYPE IDS BLINDLY. The labels endpoints are called first; a label
is recognised by its LABEL text or INTERNAL NAME (case-insensitive), and its
type id is taken from what the endpoint actually returns. The configured ids
(91 Bill Payer, 138 Operations) are only compared and reported, so a drifted id
shows up in the output instead of silently mis-marking a bill payer.

DIRECTION. Which side carries "Bill Payer" (deal to contact, or contact to
deal) is not confirmed, so both are supported and the one found is reported.
A contact is the bill payer when its link to the deal carries the label, read
from whichever direction defines it.
"""

from dataclasses import dataclass, field
from typing import Any

from core import hubspot
from core.hubspot import HubSpotClient

DEAL_TO_CONTACT = "deal_to_contact"
CONTACT_TO_DEAL = "contact_to_deal"
NOT_FOUND = "not_found"
BOTH = "both"


def _same(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.strip().casefold() == b.strip().casefold()


def matches_label(item: dict, label: str, internal_name: str) -> bool:
    """A label definition or a link type matches by label text or internal name."""
    return _same(item.get("label"), label) or _same(item.get("name"), internal_name)


def _type_ids(items: list[dict]) -> set[int]:
    return {int(i["typeId"]) for i in items if isinstance(i.get("typeId"), int)}


@dataclass
class LabelMap:
    """The labels actually found, and what they resolve to."""

    deal_to_contact: list[dict] = field(default_factory=list)
    contact_to_deal: list[dict] = field(default_factory=list)
    deal_to_deal: list[dict] = field(default_factory=list)

    @property
    def bill_payer_deal_side(self) -> list[dict]:
        return [
            i
            for i in self.deal_to_contact
            if matches_label(i, hubspot.BILL_PAYER_LABEL, hubspot.BILL_PAYER_INTERNAL_NAME)
        ]

    @property
    def bill_payer_contact_side(self) -> list[dict]:
        return [
            i
            for i in self.contact_to_deal
            if matches_label(i, hubspot.BILL_PAYER_LABEL, hubspot.BILL_PAYER_INTERNAL_NAME)
        ]

    @property
    def sales_labels(self) -> list[dict]:
        return [
            i
            for i in self.deal_to_deal
            if matches_label(i, hubspot.SALES_LABEL, hubspot.SALES_INTERNAL_NAME)
        ]

    @property
    def bill_payer_direction(self) -> str:
        deal, contact = bool(self.bill_payer_deal_side), bool(self.bill_payer_contact_side)
        if deal and contact:
            return BOTH
        if deal:
            return DEAL_TO_CONTACT
        if contact:
            return CONTACT_TO_DEAL
        return NOT_FOUND

    def report(self) -> dict:
        """What was found, for the printed output (labels and ids only)."""

        def rows(items):
            return [
                {
                    "typeId": i.get("typeId"),
                    "label": i.get("label"),
                    **({"name": i["name"]} if i.get("name") else {}),
                    "category": i.get("category"),
                }
                for i in items
            ]

        bill_ids = _type_ids(self.bill_payer_deal_side + self.bill_payer_contact_side)
        sales_ids = _type_ids(self.sales_labels)
        return {
            "dealToContact": rows(self.deal_to_contact),
            "contactToDeal": rows(self.contact_to_deal),
            "dealToDeal": rows(self.deal_to_deal),
            "billPayerDirection": self.bill_payer_direction,
            "billPayerTypeIds": sorted(bill_ids),
            "billPayerTypeIdAsConfigured": hubspot.BILL_PAYER_TYPE_ID in bill_ids,
            "salesTypeIds": sorted(sales_ids),
            "operationsTypeIdAsConfigured": hubspot.SALES_OPERATIONS_TYPE_ID
            in _type_ids(
                [
                    i
                    for i in self.deal_to_deal
                    if _same(i.get("label"), "Operations")
                    or _same(i.get("name"), hubspot.SALES_INTERNAL_NAME)
                ]
            ),
        }


def load_labels(client: HubSpotClient) -> LabelMap:
    return LabelMap(
        deal_to_contact=client.association_labels("deal", "contact"),
        contact_to_deal=client.association_labels("contact", "deal"),
        deal_to_deal=client.association_labels("deal", "deal"),
    )


@dataclass
class DealContact:
    hubspot_id: str
    email: str  # held in memory only; never put in a plan
    is_bill_payer: bool
    # The labels on this contact's link to the deal: names and type ids only.
    labels: list[dict] = field(default_factory=list)
    # Derived codes and booleans only (the values themselves are not kept).
    phone_source: str = "none"
    has_complete_address: bool = False
    has_first_and_last_name: bool = False
    # The raw allow-listed contact properties, IN MEMORY ONLY: used to build a
    # Famly contact. repr=False so it can never leak into a log line by accident.
    props: dict = field(default_factory=dict, repr=False)
    # Every email address of the contact: the primary plus each additional one, canonical (trimmed,
    # lower-cased, googlemail = gmail), de-duplicated. IN MEMORY ONLY, repr=False. `email` stays the
    # primary: a Famly contact is created with that one only. Filled from `props` when not given.
    emails: frozenset = field(default_factory=frozenset, repr=False)

    def __post_init__(self):
        if not self.emails:
            self.emails = email_set(self.email, (self.props or {}).get("hs_additional_emails"))


def email_set(primary, additional) -> frozenset:
    """The canonical set of a contact's addresses: the primary and the semicolon-separated additional
    ones (HubSpot's `hs_additional_emails`). Blank entries are dropped."""
    from actions.onboard_dry_run.famly_reads import normalise_email

    found = {normalise_email(primary)}
    found |= {normalise_email(part) for part in str(additional or "").split(";")}
    found.discard("")
    return frozenset(found)


# Phone order: first one present wins. (source code, HubSpot property, Famly type)
PHONE_ORDER = (
    ("mobile", "mobilephone", "MOBILE"),
    ("work", "phone", "WORK"),
    ("whatsapp", hubspot.WHATSAPP_PROPERTY, "MOBILE"),
)


def _present(properties: dict, name: str) -> bool:
    return bool(str(properties.get(name) or "").strip())


def phone_source(properties: dict) -> str:
    """Which phone would be used: mobile, work, whatsapp, or none."""
    for source, name, _ in PHONE_ORDER:
        if _present(properties, name):
            return source
    return "none"


def phone_type(source: str) -> str | None:
    """The Famly phone type for a phone source (WhatsApp counts as MOBILE)."""
    for code, _, famly_type in PHONE_ORDER:
        if code == source:
            return famly_type
    return None


def has_complete_address(properties: dict) -> bool:
    """Street, zip and city all present (state and country are not required)."""
    return all(_present(properties, name) for name in ("address", "zip", "city"))


def has_first_and_last_name(properties: dict) -> bool:
    return _present(properties, "firstname") and _present(properties, "lastname")


def id_sort_key(value) -> tuple:
    """Numeric ids in numeric order, anything else after them in text order. HubSpot
    and Famly return lists in no guaranteed order; the plan must not depend on it."""
    text = str(value)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def _label_sort_key(row: dict) -> tuple:
    type_id = row.get("typeId")
    return (
        type_id if isinstance(type_id, int) else -1,
        str(row.get("label") or ""),
        str(row.get("direction") or ""),
    )


def _label_rows(types: list[dict], direction: str) -> list[dict]:
    return [
        {"typeId": t.get("typeId"), "label": t.get("label"), "direction": direction}
        for t in types
    ]


def _link_is_bill_payer(types: list[dict], deal_side_ids: set[int]) -> bool:
    return any(
        matches_label(t, hubspot.BILL_PAYER_LABEL, hubspot.BILL_PAYER_INTERNAL_NAME)
        or (isinstance(t.get("typeId"), int) and t["typeId"] in deal_side_ids)
        for t in types
    )


def deal_contacts(client: HubSpotClient, labels: LabelMap, deal_id: str) -> list[DealContact]:
    """Every contact associated with the deal, flagged as bill payer by the
    label on its link to the deal."""
    deal_side_ids = _type_ids(labels.bill_payer_deal_side)
    contact_side_ids = _type_ids(labels.bill_payer_contact_side)

    out: list[DealContact] = []
    for link in client.associations("deals", deal_id, "contacts"):
        is_bill_payer = _link_is_bill_payer(link["types"], deal_side_ids)
        link_labels = _label_rows(link["types"], DEAL_TO_CONTACT)

        if not is_bill_payer and labels.bill_payer_contact_side:
            # Defined from the contact's side: read the contact's links to deals.
            for back in client.associations("contacts", link["id"], "deals"):
                if back["id"] == str(deal_id):
                    link_labels += _label_rows(back["types"], CONTACT_TO_DEAL)
                if back["id"] == str(deal_id) and any(
                    matches_label(t, hubspot.BILL_PAYER_LABEL, hubspot.BILL_PAYER_INTERNAL_NAME)
                    or (isinstance(t.get("typeId"), int) and t["typeId"] in contact_side_ids)
                    for t in back["types"]
                ):
                    is_bill_payer = True

        props = client.get_contact(link["id"])
        out.append(
            DealContact(
                link["id"],
                (props.get("email") or "").strip(),
                is_bill_payer,
                sorted(link_labels, key=_label_sort_key),
                phone_source=phone_source(props),
                has_complete_address=has_complete_address(props),
                has_first_and_last_name=has_first_and_last_name(props),
                props=dict(props),
            )
        )
    # CANONICAL ORDER: by contact id, whatever order HubSpot answered in. The plan
    # (and so its hash, and the worker's tie order) must be the same on every run.
    return sorted(out, key=lambda c: id_sort_key(c.hubspot_id))


def sales_deal_ids(client: HubSpotClient, labels: LabelMap, deal_id: str) -> list[str]:
    """Deals associated with this (Operations) deal whose link carries the
    Sales label."""
    sales_ids = _type_ids(labels.sales_labels)
    found = []
    for link in client.associations("deals", deal_id, "deals"):
        if any(
            matches_label(t, hubspot.SALES_LABEL, hubspot.SALES_INTERNAL_NAME)
            or (isinstance(t.get("typeId"), int) and t["typeId"] in sales_ids)
            for t in link["types"]
        ):
            found.append(link["id"])
    return list(dict.fromkeys(found))


SIGNED_ESIGN_STATUSES = {"completed", "signed"}


def is_signed(properties: dict) -> bool:
    """Signed = manually signed, or e-sign complete. (The exact HubSpot status
    strings are an assumption; see the summary.)"""
    manual = str(properties.get("hs_manually_signed") or "").strip().casefold() == "true"
    esign = str(properties.get("hs_quote_esign_status") or "").strip().casefold()
    return manual or esign in SIGNED_ESIGN_STATUSES


def quote_summary(client: HubSpotClient, labels: LabelMap, deal_id: str) -> tuple[dict, list[str]]:
    """({"count", "exactlyOneSigned", "source", "salesDealCount"}, warnings).

    Quotes on the deal itself; for an older deal with none, the quotes on its
    Sales deal. Count and signed state only. `count`/`exactlyOneSigned` are None
    when the Sales deal is ambiguous, so nothing is guessed.
    """
    own = client.associations("deals", deal_id, "quotes")
    sales = sales_deal_ids(client, labels, deal_id)
    warnings: list[str] = []

    if own:
        source, quote_ids = "deal", [q["id"] for q in own]
    elif len(sales) == 1:
        source = "sales_deal"
        quote_ids = [q["id"] for q in client.associations("deals", sales[0], "quotes")]
    elif len(sales) > 1:
        warnings.append("sales_deal_ambiguous")
        return (
            {"count": None, "exactlyOneSigned": None, "source": "sales_deal_ambiguous",
             "salesDealCount": len(sales)},
            warnings,
        )
    else:
        source, quote_ids = "none", []

    signed = sum(1 for qid in quote_ids if is_signed(client.get_quote(qid)))
    return (
        {
            "count": len(quote_ids),
            "exactlyOneSigned": signed == 1,
            "source": source,
            "salesDealCount": len(sales),
        },
        warnings,
    )
