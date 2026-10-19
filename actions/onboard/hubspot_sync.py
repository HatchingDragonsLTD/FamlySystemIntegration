"""HubSpot write-back for the onboarding job: LIVE MODE ONLY, never in dry.

THE PRINCIPLE: HubSpot never blocks Famly. Nothing here can stop, delay or undo a
Famly write. The deal's properties are listed up front as a PRE-FLIGHT; a missing
property, or a value a dropdown/checkbox does not offer, is REPORTED (a log line
and a Slack notice, codes only) and that property's write is SKIPPED. It is never a
reason to refuse to create the child or a contact.

WHAT MAY BE WRITTEN (core/hubspot.py WRITABLE_DEAL_PROPERTIES, enforced by the
writer itself): `famly_childid`, `famly_sync_status`, `famly_sync_note`,
`famly_sync_gaps`, and `end_date` (only when it was empty and the pipeline
defaulted one, never overwritten: the worker checks the live value first).

ONE PROPERTY PER REQUEST. A value HubSpot rejects (a dropdown that does not offer
it) fails only that property, so it cannot take `famly_childid` down with it.
Writing the same value again changes nothing, so a write-back can be re-run alone.

TWO KINDS OF PROPERTY:
  * CRITICAL: `famly_childid` and a defaulted `end_date`. If one could not be
    written (skipped, missing, or rejected) the job ends `needs_review`
    (`hubspot_writeback_failed`) AFTER the Famly work is done.
  * everything else (status, note, gaps) is advisory: a failure is reported, no
    more.

Nothing is retried by the job. (The HubSpot client's own transport backoff on a
429 or 5xx is a separate, bounded thing inside one request.)
"""

import logging
from dataclasses import dataclass, field

from actions.onboard import messages
from core import hubspot
from core.hubspot import HubSpotDealWriter, HubSpotError

logger = logging.getLogger(__name__)

# `famly_sync_status` is a HubSpot DROPDOWN with exactly these internal values
# (labels in the comment): the only values this job ever writes.
STATUS_CHILD_READY = "child_ready"                # "Child created in Famly"
STATUS_CONTACTS_LINKED = "contacts_linked"        # "Child and contacts in Famly"
STATUS_NEEDS_ATTENTION = "needs_attention"        # "Needs attention"
STATUS_OPTIONS = (STATUS_CHILD_READY, STATUS_CONTACTS_LINKED, STATUS_NEEDS_ATTENTION)

# The properties the pre-flight looks for.
CHECKED = tuple(sorted(hubspot.WRITABLE_DEAL_PROPERTIES))
CRITICAL = frozenset({hubspot.FAMLY_CHILDID_PROPERTY, hubspot.END_DATE_PROPERTY})


@dataclass
class Preflight:
    """What the deal's property list says. Built by `preflight`, which never raises."""

    listed: bool = False
    found_famly: list[str] = field(default_factory=list)   # every famly_* property found
    missing: list[str] = field(default_factory=list)
    options: dict[str, list[str]] = field(default_factory=dict)  # property -> offered values
    warnings: list[str] = field(default_factory=list)      # codes, for the log and Slack

    def offers(self, prop: str, value: str) -> bool:
        """False if `prop` is a dropdown/checkbox that does not offer `value`."""
        offered = self.options.get(prop)
        return not offered or value in offered


def preflight(writer: HubSpotDealWriter) -> Preflight:
    """List the deal properties and report what is missing. NEVER raises: if the
    listing itself fails the result says so (`preflight_failed`) and writes are
    simply attempted."""
    try:
        definitions = writer.deal_property_definitions()
    except (HubSpotError, ValueError):
        return Preflight(listed=False, warnings=["preflight_failed"])

    result = Preflight(listed=True)
    result.found_famly = sorted(n for n in definitions if n.startswith("famly_"))
    for name in CHECKED:
        if name not in definitions:
            result.missing.append(name)
            result.warnings.append(f"missing:{name}")
        elif definitions[name].get("options"):
            result.options[name] = list(definitions[name]["options"])
    return result


def gaps_value(codes) -> str:
    """The things to check, as plain-English sentences, one per line, for the
    `famly_sync_gaps` TEXT property. Staff-facing items only: configuration (admin)
    codes are never written to the deal. Empty when there is nothing to check."""
    return "\n".join(messages.classify(codes)[messages.CHECK])


def _sentences(codes) -> str:
    shown = messages.classify(codes)
    return " ".join(shown[messages.NOT_DONE] + shown[messages.CHECK])


def note_done(summary: str) -> str:
    """The `famly_sync_note` text when everything was done."""
    return f"Done. {summary}"


def note_needs_attention(summary: str, codes) -> str:
    """The note when the Famly work is done but something was not."""
    detail = _sentences(codes)
    return f"Needs attention. {summary}" + (f" {detail}" if detail else "")


def note_not_started(codes) -> str:
    """The note when the deal was held and nothing was created."""
    detail = _sentences(codes)
    return "Not started. Nothing was created in Famly." + (f" {detail}" if detail else "")


class Syncer:
    """Writes the sync properties for one deal, one request per property.

    Every write returns whether it landed. Skips and failures are recorded in
    `reports` (codes) and, for a CRITICAL property, in `critical_failed`. Nothing
    here raises for a HubSpot problem.
    """

    def __init__(self, writer: HubSpotDealWriter, deal_id: str, pre: Preflight | None = None):
        self.writer = writer
        self.deal_id = str(deal_id)
        self.pre = pre or Preflight()
        self.reports: list[str] = []
        self.critical_failed: list[str] = []

    def _fail(self, prop: str, code: str) -> bool:
        if code not in self.reports:
            self.reports.append(code)
        if prop in CRITICAL and prop not in self.critical_failed:
            self.critical_failed.append(prop)
        return False

    def write(self, prop: str, value: str) -> bool:
        if prop in self.pre.missing:
            return self._fail(prop, f"skipped:{prop}")
        if not self.pre.offers(prop, str(value)):
            return self._fail(prop, f"value_not_offered:{prop}:{value}")
        try:
            self.writer.write_deal_properties(self.deal_id, {prop: str(value)})
        except (HubSpotError, ValueError):
            logger.warning("onboard: HubSpot write failed for property %s", prop)
            return self._fail(prop, f"write_failed:{prop}")
        return True

    def write_gaps(self, codes) -> bool:
        """The things to check, as plain-English sentences (see `gaps_value`)."""
        return self.write(hubspot.SYNC_GAPS_PROPERTY, gaps_value(codes))

    def write_status(self, status: str, note: str, gaps) -> None:
        """`status` is one of STATUS_OPTIONS; `note` and the gaps are plain English."""
        self.write(hubspot.SYNC_STATUS_PROPERTY, status)
        self.write(hubspot.SYNC_NOTE_PROPERTY, note)
        self.write_gaps(gaps)
