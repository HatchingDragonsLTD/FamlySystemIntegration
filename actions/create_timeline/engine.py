"""The create_timeline flow: build a starter's plans as a chain, one approval each.

The pieces, in the order a chain lives:

  1. `parse_request`      the flat HubSpot payload plus the timeline fields
  2. `plan_write.timeline.build_segments`   the dated stretches (pure)
  3. `validate_segments`  every segment's payload parsed and validated up
                          front, with NO Famly call, so a chain cannot get
                          three plans in and then fail on the fourth's input
  4. `present_segment`    preview ONE segment through the existing path
                          (`plan_write.web.run_preview_and_post`) and post it
  5. `record_commit` / `continue_after_commit`   after its approval commits:
                          mark it done, read the plans back, check Famly trimmed
                          the previous plan, then present the next segment
  6. `stop_for_reject` / `stop_for_failure` / `stop_chain`   end the chain and
                          tell Slack exactly what exists

HOW THE PLANS JOIN. Every plan except the last is created with NO end date, and
Famly sets the previous plan's `to` EQUAL to the new plan's `from`: Famly's end
date is EXCLUSIVE (the first day NOT covered), so "Plan 1 to 2026-09-01" ends
the day before 2026-09-01 and meets Plan 2 starting that day with no gap and no
overlap. Confirmed by a real run. Only the final plan gets an explicit end: the
leave date, if there is one. That is why a stopped chain always leaves its last
committed plan open-ended, and why every stop message says so.

TWO DATE CONVENTIONS. The segments (`plan_write/timeline.py`), the chain record
and the Slack headings are INCLUSIVE: a segment "2026-09-01 to 2026-10-31"
includes 31 October. Famly's own `to` is exclusive. The only place the two meet
is the read-back check below, which compares Famly's `to` with the next
segment's start.

THE COMMIT PATH IS UNTOUCHED. Nothing here writes to Famly. Every plan is
committed by the ordinary Slack Approve click (`plan_write/approval.py`) with
every guard it already has; this module only previews, and reacts to the
outcome the approval hands back. Nothing is ever retried automatically.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date

from actions.plan_write import (
    hubspot_flatten,
    hubspot_intake,
    input_schema,
)
from actions.plan_write import web as plan_write_web
from actions.read_child_plans import runner as read_child_plans
from integrations import funding_rules, slack

from . import chains

logger = logging.getLogger(__name__)

# Same env var every plan action reads, so they all talk to one plans version.
PLAN_VERSION = int(os.environ.get("INTAKE_PLAN_VERSION", "3"))

# After a commit the plan is read back. Famly's read model can lag the write, so
# a plan that is not there YET is looked for again -- this many more times, this
# many seconds apart -- before the chain stops. Only "not found" is retried; a
# failed read, a bad join or a wrong latest plan stop the chain at once.
READBACK_RETRIES = 3
READBACK_DELAY_SECONDS = 5
_sleep = time.sleep

# The advanced-fee discount rides in the LAST of the ten custom discount slots,
# so it can never collide with the discounts HubSpot fills from slot 1 upwards.
# It is a FIXED amount in pounds per week (flag "fixed"), never a percentage.
ADVANCED_FEE_DISCOUNT_TITLE = "Advanced Fee Discount"
ADVANCED_FEE_DISCOUNT_SLOT = 10

# Slot 9 is reserved for HubSpot's booked-days discount, which is a NON-FUNDED
# feature: a funded segment never carries it (see `segment_payload`).
BOOKED_DAYS_DISCOUNT_SLOT = 9

# --------------------------------------------------------------------------- #
# What the chain keeps of the original request.
#
# An ALLOW-LIST, not a "strip the names" rule: the HubSpot payload is a flat bag
# that may carry anything (a child's or parent's name under any key), and
# `plan_write` passes unknown fields through harmlessly. Keeping only the fields
# the plan builder actually reads means no name can be stored by accident.
#
# `note` is kept: it is plan content that every stored plan body already
# carries, and dropping it would lose it from every plan after the first.
# --------------------------------------------------------------------------- #
_PLAN_FIELDS = (
    "childId",
    "from",
    "to",
    "note",
    "termTimeOnly",
    "institution",
    "has_meals",
    "has_activities",
    *(day_key for day_key, _ in hubspot_flatten._DAYS),
    "funded",
    "fundingMethod",
    "fundingHours",
    "maxFundedMinutes",
    "funding_entitlement",
    "site_code",
    "addon_quantity",
)
_DISCOUNT_FIELDS = tuple(
    f"discount_{slot}_{part}"
    for slot in range(1, ADVANCED_FEE_DISCOUNT_SLOT + 1)
    for part in ("name", "amount", "flag")
)

# The fields that describe the timeline itself. Kept in the stored payload (the
# segments are built from them) and removed from every per-segment payload.
TIMELINE_FIELDS = (
    "start_date",
    "leave_date",
    "has_funding",
    "funding_start",
    "funding_end",
    "has_advanced_fee",
    "advanced_start",
    "advanced_end",
    "advanced_discount",
)

ALLOWED_FIELDS = frozenset(_PLAN_FIELDS + _DISCOUNT_FIELDS + TIMELINE_FIELDS)

# The funding settings a funded segment carries and a non-funded one must not.
_FUNDING_SETTING_FIELDS = ("fundingMethod", "fundingHours", "maxFundedMinutes")


class PayloadError(ValueError):
    """A segment's payload cannot be built."""


class FundingPayloadError(PayloadError):
    """A funded segment's funding settings cannot be resolved from the rules."""


@dataclass
class TimelineRequest:
    """A create_timeline request, parsed."""

    child_id: str
    payload: dict
    start: str
    leave: str | None
    funding: tuple[str, str] | None
    advanced: tuple[str, str] | None


@dataclass
class ParsedRequest:
    request: TimelineRequest | None = None
    errors: list[str] = field(default_factory=list)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def clean_payload(raw: dict) -> dict:
    """The allow-listed fields of `raw`, and nothing else."""
    return {key: raw[key] for key in ALLOWED_FIELDS if key in raw}


def parse_request(raw: dict) -> ParsedRequest:
    """Read the timeline fields out of the (already unwrapped) payload.

    Dates may arrive as ISO strings or HubSpot epoch milliseconds, exactly as
    `from`/`to` do. A flag that is true but is missing its dates is an error --
    never "no funding" -- because quietly dropping a period would price the
    wrong plans.
    """
    errors: list[str] = []
    to_date = hubspot_flatten._to_iso_date
    as_bool = hubspot_flatten._as_bool

    child_id = _text(raw.get("childId"))
    if not child_id:
        errors.append("childId: required, but missing or empty")

    start = to_date(raw.get("start_date"))
    if not start:
        errors.append("start_date: required, but missing or empty")
    leave = to_date(raw.get("leave_date"))

    def period(flag, first, last, extra=()):
        if not as_bool(raw.get(flag)):
            return None
        pair = (to_date(raw.get(first)), to_date(raw.get(last)))
        missing = [name for name, value in zip((first, last), pair) if not value]
        missing += [name for name in extra if not _text(raw.get(name))]
        if missing:
            errors.append(f"{flag} is true but {', '.join(missing)} is missing")
            return None
        return pair

    funding = period("has_funding", "funding_start", "funding_end")
    advanced = period(
        "has_advanced_fee",
        "advanced_start",
        "advanced_end",
        extra=("advanced_discount",),
    )

    if errors:
        return ParsedRequest(errors=errors)

    return ParsedRequest(
        request=TimelineRequest(
            child_id=child_id,
            payload=clean_payload(raw),
            start=start,
            leave=leave,
            funding=funding,
            advanced=advanced,
        )
    )


# --------------------------------------------------------------------------- #
# Per-segment payloads
# --------------------------------------------------------------------------- #
def segment_payload(base: dict, segment: dict, *, is_last: bool) -> dict:
    """The flat HubSpot payload for ONE segment, built from the original.

      * `from` is the segment's start. `to` is left EMPTY for every segment but
        the last (Famly trims the previous plan when the next one arrives); the
        last gets the leave date, or none when there is no leave date.
      * a FUNDED segment books the funded session variants (`funded` true) and
        gets its three funding settings -- method, weekly hours and the cap --
        from the SERVER's per-institution rules (`integrations.funding_rules`),
        using the payload's `institution`, `funding_entitlement` and
        `termTimeOnly`. Anything HubSpot sent for them is overridden. It books
        no add-on products and carries NO booked-days discount (slot 9): both
        are non-funded features.
      * a NON-FUNDED segment gets `funded` false, NO funding settings, and keeps
        `addon_quantity` (so it books the add-on products) and the booked-days
        discount in slot 9.
      * an ADVANCED segment adds the Advanced Fee Discount in slot 10, flagged
        "fixed": `advanced_discount` is POUNDS PER WEEK, so it builds a
        weekly, non-percentage discount. A slot 10 already in use is an error,
        never overwritten.

    Never mutates `base`.
    """
    payload = {k: v for k, v in base.items() if k not in TIMELINE_FIELDS}
    payload["from"] = segment["from"]
    payload["to"] = segment["to"] if is_last else None

    if segment["funded"]:
        payload["funded"] = "true"
        payload.pop("addon_quantity", None)
        for part in ("name", "amount", "flag"):
            payload.pop(f"discount_{BOOKED_DAYS_DISCOUNT_SLOT}_{part}", None)
        try:
            payload.update(
                funding_rules.resolve_funding(
                    _text(base.get("institution")),
                    base.get("funding_entitlement"),
                    hubspot_flatten._as_bool(base.get("termTimeOnly")),
                )
            )
        except funding_rules.FundingRulesError as exc:
            raise FundingPayloadError(str(exc)) from exc
    else:
        payload["funded"] = "false"
        for name in _FUNDING_SETTING_FIELDS:
            payload.pop(name, None)

    if segment["advanced"]:
        slot = ADVANCED_FEE_DISCOUNT_SLOT
        if _text(payload.get(f"discount_{slot}_name")) or _text(
            payload.get(f"discount_{slot}_amount")
        ):
            raise PayloadError(
                f"discount slot {slot} is already in use, so the "
                f"{ADVANCED_FEE_DISCOUNT_TITLE} cannot be added"
            )
        payload[f"discount_{slot}_name"] = ADVANCED_FEE_DISCOUNT_TITLE
        payload[f"discount_{slot}_amount"] = base.get("advanced_discount")
        payload[f"discount_{slot}_flag"] = "fixed"  # pounds per week, not a percent

    return payload


def validate_segments(base: dict, segments: list[dict]) -> list[str]:
    """Every problem with any segment's payload, found WITHOUT calling Famly.

    Runs the same parse and validation the preview will, so a catalogue gap, a
    missing funding method or a bad discount in plan 4 is found before plan 1 is
    ever posted. A bad Advanced Fee Discount is an error here even though a bad
    discount slot is only advisory in an ordinary preview: dropping it would
    quietly bill the family too much.

    The advanced fee is a FIXED amount in pounds per week. Besides the fixed
    range the discount builder enforces (above 0, at most 5000), an amount
    BELOW 1 is refused as "looks like a fraction": a percentage sent by mistake
    (0.05) would otherwise become a 5p weekly discount.
    """
    errors: list[str] = []
    total = len(segments)

    # Funding rules are the same for every funded plan, so a gap is reported
    # once, not once per plan.
    reported_funding: set[str] = set()

    if any(segment["advanced"] for segment in segments):
        try:
            amount = float(_text(base.get("advanced_discount")))
        except ValueError:
            amount = None  # the discount builder reports a non-number below
        if amount is not None and 0 < amount < 1:
            errors.append(
                f"advanced_discount {base.get('advanced_discount')!r} looks like a "
                f"fraction, expected £ per week (a fixed amount of at least £1)"
            )

    for index, segment in enumerate(segments):
        label = f"Plan {index + 1} of {total}"
        try:
            payload = segment_payload(base, segment, is_last=index == total - 1)
        except FundingPayloadError as exc:
            if str(exc) not in reported_funding:
                reported_funding.add(str(exc))
                errors.append(f"{label}: {exc}")
            continue
        except PayloadError as exc:
            errors.append(f"{label}: {exc}")
            continue

        plan_input = hubspot_intake.parse_hubspot_payload(payload)
        errors.extend(f"{label}: {e}" for e in input_schema.validate(plan_input))

        if segment["advanced"]:
            slot = ADVANCED_FEE_DISCOUNT_SLOT
            problems = [
                p for p in plan_input.problems if p.startswith(f"Discount {slot} ")
            ]
            booked = any(
                isinstance(d, dict)
                and d.get("title") == ADVANCED_FEE_DISCOUNT_TITLE
                and d.get("ordering") == slot - 1
                and d.get("isPercent") is False
                and d.get("period") == "WEEKLY"
                for part in plan_input.plan_parts
                for d in part.discounts
            )
            if problems or not booked:
                detail = "; ".join(problems) or "it was not built"
                errors.append(f"{label}: {ADVANCED_FEE_DISCOUNT_TITLE} invalid: {detail}")

    return errors


# --------------------------------------------------------------------------- #
# Labels and messages
# --------------------------------------------------------------------------- #
def heading_text(segments: list[dict], index: int) -> str:
    """e.g. "Plan 2 of 4 (funded, 2026-11-01 to 2026-12-31)"."""
    segment = segments[index]
    kind = ["funded" if segment["funded"] else "base"]
    if segment["advanced"]:
        kind.append("advanced")

    end = segment["to"]
    span = f"{segment['from']} to {end}" if end else f"{segment['from']} onwards"
    return f"Plan {index + 1} of {len(segments)} ({', '.join(kind)}, {span})"


def _short_name(chain: chains.Chain, index: int) -> str:
    """e.g. "Plan 2 of 4"."""
    return f"Plan {index + 1} of {len(chain.segments)}"


def label_for(chain_id: str | None, index: int | None) -> str | None:
    """The heading text of a stored preview's chain position, or None."""
    if not chain_id or index is None:
        return None
    chain = chains.get(chain_id)
    if chain is None or not 0 <= index < len(chain.segments):
        return None
    return heading_text(chain.segments, index)


def _plan_line(chain: chains.Chain, index: int, plan_id=None) -> str:
    line = f"• {heading_text(chain.segments, index)}"
    return f"{line} — plan `{plan_id}`" if plan_id else line


def stop_message(
    chain: chains.Chain,
    reason: str,
    *,
    uncertain_index: int | None = None,
    extra_lines: list[str] | None = None,
    title: str = "Timeline stopped",
) -> str:
    """What Slack is told when a chain stops: why, what exists, what does not.

    Whenever a non-final plan has been committed it ALSO says that this plan is
    open-ended -- every plan but the last is created without an end date, so the
    last committed one stays open until someone gives it an end date or creates
    its successor.
    """
    lines = [
        f":octagonal_sign: *{title}* — child `{chain.child_id}`, "
        f"chain `{chain.chain_id}`",
        f"Reason: {reason}",
    ]
    lines += extra_lines or []

    committed = chain.committed()
    lines.append("")
    if committed:
        lines.append("*Plans that exist in Famly:*")
        lines += [_plan_line(chain, i, s.get("plan_id")) for i, s in committed]
    else:
        lines.append("*Plans that exist in Famly:* none — nothing was created.")

    pending = chain.not_committed()
    if pending:
        lines.append("*Plans NOT created:*")
        for i, _segment in pending:
            line = _plan_line(chain, i)
            if i == uncertain_index:
                line += (
                    " (the commit reported an error — check Famly in case it "
                    "was written before the error)"
                )
            lines.append(line)

    if committed:
        last_index, last_segment = committed[-1]
        if last_index < chain.last_index:
            lines += [
                "",
                f":warning: The last committed plan ({_short_name(chain, last_index)}, "
                f"`{last_segment.get('plan_id')}`) is *open-ended*: it has no end "
                f"date. It needs either an end date or its successor plan.",
            ]

    lines += ["", "_Nothing further will be created automatically._"]
    return "\n".join(lines)


def stop_chain(
    chain_id: str,
    reason: str,
    *,
    uncertain_index: int | None = None,
    extra_lines: list[str] | None = None,
    status: str = chains.STATUS_STOPPED,
    title: str = "Timeline stopped",
) -> bool:
    """Finish an active chain and tell Slack. True when this call finished it.

    `status`/`title` let expiry and cancellation reuse the same message.

    A chain that is already stopped or complete is left alone and Slack is not
    told twice. Never raises.
    """
    try:
        if not chains.finish(chain_id, status):
            return False
        chain = chains.get(chain_id)
        logger.warning("CHAIN STOPPED chain_id=%s: %s", chain_id, reason)
        if chain is not None:
            slack.post_notice(
                stop_message(
                    chain,
                    reason,
                    uncertain_index=uncertain_index,
                    extra_lines=extra_lines,
                    title=title,
                )
            )
        return True
    except Exception:  # noqa: BLE001 - stopping must never raise at the caller
        logger.exception("Could not stop chain_id=%s cleanly", chain_id)
        return False


def stop_for_reject(stored, user) -> bool:
    """The approver rejected a chain preview: the chain ends there."""
    chain = chains.get(stored.chain_id)
    if chain is None:
        return False
    return stop_chain(
        chain.chain_id,
        f"{heading_text(chain.segments, stored.chain_index)} was rejected "
        f"by {user or 'unknown'}.",
    )


def stop_for_failure(stored, detail: str) -> bool:
    """A chain preview's commit failed: the chain ends there. No retry."""
    chain = chains.get(stored.chain_id)
    if chain is None:
        return False
    return stop_chain(
        chain.chain_id,
        f"{heading_text(chain.segments, stored.chain_index)} could not be "
        f"committed: {detail}",
        uncertain_index=stored.chain_index,
    )


# --------------------------------------------------------------------------- #
# Presenting a segment
# --------------------------------------------------------------------------- #
def present_segment(
    chain: chains.Chain, index: int, old_plan_id: str | None
) -> tuple[dict, int]:
    """Preview segment `index` through the ordinary path and post it to Slack.

    The first segment has no `oldPlanId`; each later one names the plan before
    it, with `replaceOldPlan` false. Returns `run_preview_and_post`'s
    (data, status). The chain is STOPPED, and Slack told, when the preview
    fails to validate, Famly errors, or Slack cannot take the post -- a
    segment nobody can see could never be approved.

    A Famly error is re-raised after the chain is stopped, for a caller (the
    request handler) whose router classifies it.
    """
    payload = segment_payload(
        chain.payload, chain.segments[index], is_last=index == chain.last_index
    )
    where = heading_text(chain.segments, index)

    try:
        data, status = plan_write_web.run_preview_and_post(
            payload,
            old_plan_id=old_plan_id,
            replace_old_plan=False,
            chain_id=chain.chain_id,
            chain_index=index,
            heading=f"*{where}*",
        )
    except Exception as exc:
        stop_chain(
            chain.chain_id, f"the preview of {where} failed: {type(exc).__name__}: {exc}"
        )
        raise

    if status != 200:
        problems = "; ".join(str(e) for e in data.get("errors") or []) or f"HTTP {status}"
        stop_chain(chain.chain_id, f"the preview of {where} was refused: {problems}")
    elif not data.get("slack_posted"):
        stop_chain(chain.chain_id, f"the preview of {where} could not be posted to Slack.")
        return data, 502

    return data, status


# --------------------------------------------------------------------------- #
# After a commit
# --------------------------------------------------------------------------- #
def record_commit(chain_id: str, index: int, plan_id: str | None, child_id) -> bool:
    """Mark segment `index` committed. Called right after the commit succeeds.

    Synchronous and cheap on purpose: the plan IS in Famly now, so the chain's
    record must say so before anything slower is attempted. Returns False when
    the chain could not take it (it is not active, or `index` is not its next
    segment) -- the plan exists regardless, so Slack is told.
    """
    chain = chains.mark_committed(chain_id, index, plan_id or "")
    if chain is not None:
        return True

    logger.error(
        "CHAIN OUT OF SYNC chain_id=%s: plan %s for segment %s was committed but "
        "the chain could not record it",
        chain_id,
        plan_id,
        index,
    )
    slack.post_notice(
        f":warning: A timeline plan was committed (plan `{plan_id}`, child "
        f"`{child_id}`) but chain `{chain_id}` is no longer expecting it "
        f"(stopped, or out of order), so nothing further will be created for "
        f"that chain. Please check the child's plans in Famly."
    )
    return False


def _day(value) -> date | None:
    if not isinstance(value, str) or len(value.strip()) < 10:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _find(plans: list, plan_id):
    return next((p for p in plans if plan_id and p.id == plan_id), None)


def _plan_description(chain: chains.Chain, index: int, plan) -> str:
    return (
        f"• {_short_name(chain, index)} `{plan.id}`: "
        f"from {plan.from_ or 'unknown'} to {plan.to or 'open-ended'}"
    )


def continue_after_commit(chain_id: str, index: int) -> None:
    """Verify the commit of segment `index`, then present the next one.

    Reads the child's plans back (ONE read, no retry) and checks that:

      * the plan just committed is there;
      * if there is a previous plan, its `to` EQUALS this plan's `from` --
        Famly's end date is exclusive, so that is the trim it does. Strict: any
        other value, the day before included, means a gap or an overlap;
      * this plan is the child's latest-starting plan, which is what the NEXT
        segment will name as its `oldPlanId`.

    Any miss stops the chain. After the last plan it posts "All N plans
    created". Never raises: it runs off the request thread, so an escaping
    exception would leave the approver waiting for a message that never comes.
    """
    chain = chains.get(chain_id)
    if chain is None or not chain.is_active:
        logger.info("Chain chain_id=%s is not active; nothing to continue", chain_id)
        return

    try:
        _verify_and_advance(chain, index)
    except Exception as exc:  # noqa: BLE001 - reported to Slack, never raised
        logger.exception("CHAIN CONTINUE FAILED chain_id=%s index=%s", chain_id, index)
        stop_chain(
            chain_id,
            f"after {_short_name(chain, index)} was committed: "
            f"{type(exc).__name__}: {exc}",
        )


def _verify_and_advance(chain: chains.Chain, index: int) -> None:
    n = len(chain.segments)
    name = _short_name(chain, index)
    plan_id = chain.segments[index].get("plan_id")

    # Read the plans the child has NOW. A failed read must stop the chain, not
    # be taken as "nothing wrong".
    plans = read_child_plans.run(chain.child_id, version=PLAN_VERSION).plans

    this = _find(plans, plan_id)
    for _attempt in range(READBACK_RETRIES):
        if this is not None:
            break
        _sleep(READBACK_DELAY_SECONDS)
        plans = read_child_plans.run(chain.child_id, version=PLAN_VERSION).plans
        this = _find(plans, plan_id)

    if this is None:
        stop_chain(
            chain.chain_id,
            f"{name} was committed (plan `{plan_id}`) but is not in the child's "
            f"plans when read back (looked {READBACK_RETRIES + 1} times, "
            f"{READBACK_DELAY_SECONDS}s apart), so it could not be verified.",
        )
        return

    if index > 0:
        previous_id = chain.segments[index - 1].get("plan_id")
        previous = _find(plans, previous_id)
        if previous is None:
            stop_chain(
                chain.chain_id,
                f"the previous plan (`{previous_id}`) is not in the child's plans "
                f"when read back after {name} was committed.",
            )
            return

        expected = _day(this.from_)
        found = _day(previous.to)
        if expected is None or found != expected:
            stop_chain(
                chain.chain_id,
                f"after {name} was committed, the previous plan does not meet it "
                f"(a gap or an overlap). Famly's end date is exclusive: expected "
                f"previous plan to = {expected.isoformat() if expected else 'unknown'}, "
                f"found {previous.to or 'open-ended'}.",
                extra_lines=[
                    _plan_description(chain, index - 1, previous),
                    _plan_description(chain, index, this),
                ],
            )
            return

    latest = max(plans, key=lambda p: _day(p.from_) or date.min)
    if latest.id != plan_id:
        stop_chain(
            chain.chain_id,
            f"{name} (`{plan_id}`) is not the child's latest-starting plan "
            f"(`{latest.id}` starts {latest.from_}), so the next plan cannot "
            f"safely name it as the plan it follows.",
        )
        return

    if index == chain.last_index:
        if chains.finish(chain.chain_id, chains.STATUS_COMPLETE):
            ids = ", ".join(f"`{s.get('plan_id')}`" for s in chain.segments)
            slack.post_notice(
                f":white_check_mark: All {n} plans created for child "
                f"`{chain.child_id}` (chain `{chain.chain_id}`): {ids}"
            )
        return

    # Re-read so the next segment is built from the chain as it now stands.
    chain = chains.get(chain.chain_id) or chain
    present_segment(chain, index + 1, old_plan_id=latest.id)
