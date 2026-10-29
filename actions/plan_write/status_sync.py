"""Write `famly_plan_status` and `famly_plan_note` on a HubSpot deal, with the same rules as the onboarding
write-back (`onboard/hubspot_sync.py`): ONE PATCH per property, never retried, HubSpot never blocks Famly.

  * a property that does not exist, or a dropdown that does not offer the value, is REPORTED (a log line and
    one Slack line, codes only) and that write is SKIPPED: `skipped:<property>`,
    `value_not_offered:<property>:<value>`, `write_failed:<property>`;
  * the note is written FIRST and the status second, because a workflow triggers on the status and should
    find the note already there; a note is a code (`status_codes`), "" clears it;
  * `needs_attention` is written with a note code; `plan_created` clears the note, and a later success
    overwrites an earlier `needs_attention`;
  * nothing here raises: a HubSpot problem is only ever reported.

`attention_async` is the hook the failure points use: it runs off the calling thread (a daemon thread, like the
plan flag) so a webhook or a click never waits for HubSpot. Ids and codes only, never names or error text.
"""

import logging
import threading

from core import hubspot
from core.hubspot import HubSpotDealWriter, HubSpotError
from integrations import slack

logger = logging.getLogger(__name__)

NEEDS_ATTENTION = hubspot.PLAN_STATUS_NEEDS_ATTENTION
PLAN_CREATED = hubspot.PLAN_STATUS_CREATED


def _spawn(work) -> None:
    """Run `work` on a daemon thread (tests replace this with an inline call)."""
    threading.Thread(target=work, daemon=True).start()


class PlanStatusSyncer:
    """Writes the status and the note for one HubSpot deal. Every method returns the report codes (a list)."""

    def __init__(self, writer=None, notify=None):
        self.writer = writer or HubSpotDealWriter()
        self.notify = notify

    # -- pre-flight --------------------------------------------------------- #
    def _definitions(self) -> dict | None:
        try:
            return self.writer.deal_property_definitions()
        except (HubSpotError, ValueError):
            return None                                    # unknown: writes are simply attempted

    def _skip_code(self, definitions: dict | None, prop: str, value: str) -> str | None:
        if definitions is None:
            return None
        definition = definitions.get(prop)
        if definition is None:
            return f"skipped:{prop}"
        offered = definition.get("options") or []
        if prop == hubspot.PLAN_STATUS_PROPERTY and offered and value not in offered:
            return f"value_not_offered:{prop}:{value}"
        return None

    # -- one property ------------------------------------------------------- #
    def _write(self, definitions, prop: str, value: str, send) -> str | None:
        """None when written; else the report code (skipped, not offered, or failed)."""
        code = self._skip_code(definitions, prop, value)
        if code:
            return code
        try:
            send()
        except (HubSpotError, ValueError):
            return f"write_failed:{prop}"
        return None

    def _report(self, deal_id, codes: list[str]) -> list[str]:
        if not codes:
            return codes
        logger.warning("PLAN STATUS deal=%s %s", deal_id, ",".join(codes))
        try:
            (self.notify or slack.post_notice)(
                f"famly_plan_status was not fully written for deal {deal_id} ({', '.join(codes)}). "
                "Check the two deal properties in HubSpot.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("PLAN STATUS notice failed (%s)", type(exc).__name__)
        return codes

    def set(self, deal_id, status: str, note: str) -> list[str]:
        """Write the note, then the status. Returns the report codes; [] when both were written."""
        deal_id = str(deal_id)
        definitions = self._definitions()
        codes = []
        for prop, value, send in (
                (hubspot.PLAN_NOTE_PROPERTY, note, lambda: self.writer.write_plan_note(deal_id, note)),
                (hubspot.PLAN_STATUS_PROPERTY, status, lambda: self.writer.write_plan_status(deal_id, status))):
            code = self._write(definitions, prop, value, send)
            if code:
                codes.append(code)
        return self._report(deal_id, codes)

    # -- the two outcomes --------------------------------------------------- #
    def mark_needs_attention(self, deal_id, note_code: str) -> list[str]:
        return self.set(deal_id, NEEDS_ATTENTION, note_code)

    def mark_plan_created(self, deal_id) -> tuple[bool, list[str]]:
        """plan_created with the note cleared, unless the deal already says exactly that. Returns
        (whether a write was attempted, report codes). A read that fails is treated as "differs"."""
        try:
            _child, status, note = self.writer.read_plan_status_state(str(deal_id))
        except (HubSpotError, ValueError):
            status, note = None, "unreadable"
        if status == PLAN_CREATED and not note:
            return False, []
        return True, self.set(deal_id, PLAN_CREATED, "")


def attention_now(deal_id, note_code: str, writer=None, notify=None) -> list[str]:
    """Write needs_attention + the note code for a deal, in the calling thread. Never raises; no deal id = nothing."""
    if not deal_id:
        return []
    try:
        return PlanStatusSyncer(writer, notify).mark_needs_attention(deal_id, note_code)
    except Exception as exc:  # noqa: BLE001 - nothing here may reach the caller
        logger.warning("PLAN STATUS attention write failed deal=%s error=%s", deal_id, type(exc).__name__)
        return ["failed"]


def rejection_text(deal_id, note_code: str) -> str:
    """The ONE Slack line for a rejected preview or queue / chain request: the deal id and the note code (which
    holds the sub-codes), nothing else. No names, values or error text can be in it: a note is a code."""
    return (f"Plan not created for deal {deal_id or 'unknown'} ({note_code}). Nothing was written to Famly. "
            "Fix the deal in HubSpot and send it again.")


def rejection_now(deal_id, note_code: str, writer=None, notify=None) -> list[str]:
    """A rejected preview or request: post ONE Slack line (the deal id and the sub-codes), then write
    needs_attention + the note to the deal when there is a deal id. Never raises."""
    try:
        (notify or slack.post_notice)(rejection_text(deal_id, note_code))
    except Exception as exc:  # noqa: BLE001 - a Slack problem never hides the HubSpot write
        logger.warning("PLAN STATUS rejection notice failed deal=%s error=%s", deal_id, type(exc).__name__)
    return attention_now(deal_id, note_code, writer, notify)


def rejection_async(deal_id, note_code: str) -> None:
    """The rejection hook: `rejection_now` off the calling thread. Never raises, never blocks the caller."""
    try:
        _spawn(lambda: rejection_now(deal_id, note_code))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PLAN STATUS rejection hook not started deal=%s error=%s", deal_id, type(exc).__name__)


def held_text(deal_id, job_id, note_code: str) -> str:
    """The ONE Slack line for a held queue job: the deal id, the job id and the reason code, nothing else."""
    return (f"Queue job held for deal {deal_id or 'unknown'}: job {job_id} ({note_code}). The held plan is not "
            f"created. Approve or reject it in Slack, or run `queue-cancel {job_id}` and send the deal again.")


def held_now(deal_id, job_id, note_code: str, post_slack: bool = True, writer=None, notify=None) -> list[str]:
    """A held queue job: post ONE Slack line (when `post_slack`), then write needs_attention + the note code to
    the deal when there is a deal id. Never raises."""
    if post_slack:
        try:
            (notify or slack.post_notice)(held_text(deal_id, job_id, note_code))
        except Exception as exc:  # noqa: BLE001 - a Slack problem never hides the HubSpot write
            logger.warning("PLAN STATUS held notice failed deal=%s error=%s", deal_id, type(exc).__name__)
    return attention_now(deal_id, note_code, writer, notify)


def held_async(deal_id, job_id, note_code: str, post_slack: bool = True) -> None:
    """The hold hook: `held_now` off the calling thread. Never raises, never blocks the caller."""
    try:
        _spawn(lambda: held_now(deal_id, job_id, note_code, post_slack))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PLAN STATUS held hook not started deal=%s error=%s", deal_id, type(exc).__name__)


def attention_async(deal_id, note_code: str) -> None:
    """The failure hook: write needs_attention off the calling thread. Never raises, never blocks the caller."""
    if not deal_id:
        return
    try:
        _spawn(lambda: attention_now(deal_id, note_code))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PLAN STATUS hook not started deal=%s error=%s", deal_id, type(exc).__name__)
