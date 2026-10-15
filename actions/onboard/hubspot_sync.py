"""HubSpot write-back for phase 2: LIVE MODE ONLY, never in dry.

Writes ONLY: `famly_childid` (the Famly child id), `famly_sync_status`,
`famly_sync_note` and `famly_sync_gaps`. The note and gaps are made of CODES,
never data. `HubSpotDealWriter.write_deal_properties` refuses any other name.

FAIL CLOSED. Before any Famly write, `check_properties` lists the deal's
properties and requires all four to exist (and, if `famly_sync_status` is a
dropdown, to offer the status values used). What is actually there is reported:
the exact internal names of the `famly_*` properties found, so a missing one (or
one named differently from what Drew created) is visible. If a property is
missing the job fails before it writes anything to Famly.

IDEMPOTENT. Writing the same values again changes nothing, so a write-back can
be re-run on its own. A failed write-back never causes anything to be created in
Famly again.
"""

from dataclasses import dataclass, field

from core import hubspot
from core.hubspot import HubSpotDealWriter, HubSpotError

STATUS_QUEUED = "queued"
STATUS_CHILD_READY = "child_ready"
STATUS_CONTACTS_LINKED = "contacts_linked"
STATUS_NEEDS_ATTENTION = "needs_attention"
STATUS_VALUES = (STATUS_QUEUED, STATUS_CHILD_READY, STATUS_CONTACTS_LINKED, STATUS_NEEDS_ATTENTION)

REQUIRED = tuple(sorted(hubspot.WRITABLE_DEAL_PROPERTIES))


@dataclass
class PropertyCheck:
    ok: bool
    missing: list[str] = field(default_factory=list)
    bad_options: list[str] = field(default_factory=list)
    found_famly: list[str] = field(default_factory=list)  # every famly_* property found


def check_properties(writer: HubSpotDealWriter) -> PropertyCheck:
    """List the deal properties and check the four sync properties exist.

    Raises:
        HubSpotError: the listing failed (the caller fails closed).
    """
    definitions = writer.deal_property_definitions()
    found = sorted(name for name in definitions if name.startswith("famly_"))
    missing = [name for name in REQUIRED if name not in definitions]

    bad = []
    status = definitions.get(hubspot.SYNC_STATUS_PROPERTY)
    if status and status["options"]:   # a dropdown: our values must be offered
        bad = [v for v in STATUS_VALUES[1:] if v not in status["options"]]

    return PropertyCheck(ok=not missing and not bad, missing=missing,
                         bad_options=bad, found_famly=found)


def gaps_value(codes) -> str:
    """Warning/gap codes as one value: sorted, de-duplicated, ';' separated (the
    format a HubSpot multiple-checkbox property takes)."""
    return ";".join(sorted({str(c) for c in codes if c}))


def write_back(
    writer: HubSpotDealWriter,
    deal_id: str,
    *,
    status: str,
    note: str,
    gaps,
    child_id: str | None = None,
) -> None:
    """Write the sync properties. Raises HubSpotError (or ValueError) on failure."""
    properties = {
        hubspot.SYNC_STATUS_PROPERTY: status,
        hubspot.SYNC_NOTE_PROPERTY: note,
        hubspot.SYNC_GAPS_PROPERTY: gaps_value(gaps),
    }
    if child_id:
        properties[hubspot.FAMLY_CHILDID_PROPERTY] = child_id
    writer.write_deal_properties(deal_id, properties)


__all__ = ["check_properties", "write_back", "gaps_value", "PropertyCheck", "HubSpotError"]
