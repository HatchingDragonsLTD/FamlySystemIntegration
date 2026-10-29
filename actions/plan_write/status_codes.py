"""The note codes for `famly_plan_note`: a short CODE per kind of problem, never the error text.

Raw error text can carry names, emails, response bodies and ids, so it never goes into HubSpot: every mapper
here returns one of a fixed set of codes (or a code plus an HTTP status class), chosen by matching the text,
and anything it does not recognise becomes the generic code for its kind. Codes use only a-z 0-9 _ : . , -
(`core.hubspot.PLAN_NOTE_PATTERN`), so a note can never hold free text.

  plan_rejected:<kind>[:<sub-codes>]
                           the plan_preview (or queue / chain request) was rejected, nothing was created.
                           The sub-codes say WHICH validation rule failed (see SUBCODE_RULES): up to three,
                           comma-joined, then `,+N` for any more, e.g.
                           `plan_rejected:input_invalid:start_date_missing,no_bookings,+2`;
  commit_failed*           the Approve commit failed (Famly said no, or an unexpected error);
  commit_unknown           the commit went through but the plan could not be confirmed by a live read;
  queue_job_failed:<kind>  a queued job failed;
  queue_job_held:<reasons> a queued job was held for a human (see HELD_WARNING_CODES and HELD_TEXT_CODES);
  chain_failed:<kind>      a multi-plan chain stopped.
"""

import re

PLAN_REJECTED = "plan_rejected"
QUEUE_JOB_HELD = "queue_job_held"
COMMIT_FAILED = "commit_failed"
COMMIT_FAILED_4XX = "commit_failed_http_4xx"
COMMIT_FAILED_5XX = "commit_failed_http_5xx"
COMMIT_UNKNOWN = "commit_unknown"
QUEUE_JOB_FAILED = "queue_job_failed"
CHAIN_FAILED = "chain_failed"

_HTTP = re.compile(r"\(HTTP (\d{3})\)")


NOTE_MAX = 100                  # the longest note (core.hubspot.PLAN_NOTE_PATTERN)
MAX_SUBCODES = 3                # sub-codes shown; the rest are counted as ",+N"
OTHER = "other"                 # the catch-all sub-code: a rejection no rule below recognises

# One sub-code per validation rule, in priority order: the FIRST rule whose pattern matches an error gives it
# its sub-code. The patterns only classify the error text; the text itself is never kept or sent. Sources:
# input_schema.validate (the `planParts` and `childId` rules), the flatten errors (institution, session
# catalogue, institution defaults, add-on products), timeline.build_segments (the dates), engine.parse_request
# and engine.validate_segments (the queue and chain intake).
SUBCODE_RULES = (
    # the request itself (queue / chain intake) and the plan's identity and dates
    ("child_id_missing", r"^childId: required"),
    ("child_id_invalid", r"^childId: .* is not a UUID"),
    ("start_date_missing", r"^(from|start_date): required"),
    ("start_date_invalid", r"^from: .* is not an ISO date"),
    ("end_date_invalid", r"^to: .* is not an ISO date"),
    ("funding_dates_missing", r"^has_funding is true but"),
    ("advanced_fee_missing", r"^has_advanced_fee is true but"),
    ("date_not_iso", r"must be an ISO date \(YYYY-MM-DD\)"),
    ("date_not_real", r"is not a real date"),
    ("period_not_pair", r"must be a \(from, to\) pair"),
    ("period_one_date", r"has only one date"),
    ("period_reversed", r"ends \(.*\) before it starts"),
    ("leave_before_start", r"^leave \(.*\) is before start"),
    # input_schema.validate
    ("rule_group_invalid", r"^ruleGroupId:"),
    ("no_plan_parts", r"^planParts: at least one plan part"),
    ("billing_profile_invalid", r"billingProfileId"),
    ("attendance_schedule_invalid", r"attendanceScheduleId"),
    ("term_schedule_invalid", r"termScheduleId"),
    ("term_time_flag_invalid", r"termTimeOnly"),
    ("billing_scheme_unknown", r"billing\.id: NOTE"),
    ("billing_missing", r"\.billing(\.id)?: required"),
    ("discount_invalid", r"\.discounts\[\d+\]:"),
    ("no_bookings", r"has neither session nor product bookings"),
    ("session_id_invalid", r"sessionBookings\[\d+\]\.sessionId"),
    ("booking_day_invalid", r"\.day: .* is not one of"),
    ("fundable_invalid", r"\.fundable:"),
    ("product_id_invalid", r"productBookings\[\d+\]\.productId"),
    ("product_amount_invalid", r"\.amount:"),
    ("funding_type_invalid", r"publicFundingSettings\.fundingTypeIds"),
    # the flatten errors
    ("institution_missing", r"^institution: required"),
    ("day_slot_invalid", r"session_catalogue: .* is not a valid slot"),
    ("session_institution_unknown", r"session_catalogue: no institution"),
    ("session_not_in_catalogue", r"session_catalogue: no UUID for"),
    ("session_catalogue_unreadable",
     r"session_catalogue: (file not found|could not read|.* is not a JSON object|no 'institutions')"),
    ("defaults_institution_unknown", r"institution_defaults: no institution"),
    ("defaults_rule_group_missing", r"institution_defaults: no ruleGroupId"),
    ("defaults_schedule_invalid", r"institution_defaults: .* is not a valid schedule"),
    ("defaults_schedule_missing", r"institution_defaults: no ('schedules'|'.*' schedule)"),
    ("defaults_incomplete", r"institution_defaults: institution .* > schedules > .* is missing"),
    ("addon_products_missing", r"institution_defaults: no addonProducts|addonProducts is missing"),
    ("defaults_unreadable",
     r"institution_defaults: (file not found|could not read|.* is not a JSON object|no 'institutions')"),
    ("addon_not_a_number", r"^addon_quantity: .* is not a number"),
    ("addon_negative", r"^addon_quantity \(.*\) cannot be negative"),
    ("addon_exceeds_days", r"exceeds the number of booked days"),
    ("addon_half_missing", r"needs a half day of add-ons"),
    ("funding_invalid", r"^funding_rules:"),
    ("discount_slot_in_use", r"discount slot \d+ is already in use"),
    ("advanced_discount_out_of_range", r"advanced_discount .* is out of range"),
    ("advanced_fee_invalid", r"Advanced Fee Discount invalid"),
)
# Sub-codes only `plan-check` can report (they read the deal's own properties; plan_preview has no such rule).
PLAN_CHECK_ONLY = ("fees_missing", "fees_unparseable", "site_unknown")
ALL_SUBCODES = tuple(code for code, _pattern in SUBCODE_RULES) + (OTHER,) + PLAN_CHECK_ONLY

BOOKING_FAILURES = ("day_slot_invalid", "session_institution_unknown", "session_not_in_catalogue",
                    "session_catalogue_unreadable", "institution_missing")

_COMPILED = tuple((code, re.compile(pattern)) for code, pattern in SUBCODE_RULES)
_PLAN_PREFIX = re.compile(r"^Plan \d+ of \d+: ")


def subcodes_of(errors) -> list[str]:
    """The sub-codes for a list of error strings, de-duplicated in order of first appearance. An error no rule
    recognises is `other`. Only fixed codes come out: never the error text, a value or a name."""
    out: list[str] = []
    for error in errors or []:
        text = _PLAN_PREFIX.sub("", str(error))
        code = next((name for name, pattern in _COMPILED if pattern.search(text)), OTHER)
        if code not in out:
            out.append(code)
    # A session that cannot be resolved leaves the plan with no bookings, so `no_bookings` is then a symptom
    # of the cause already listed, not a second problem: it is left out.
    if "no_bookings" in out and any(cause in out for cause in BOOKING_FAILURES):
        out.remove("no_bookings")
    return out


def with_subcodes(top: str, subs) -> str:
    """`top:sub1,sub2,sub3,+N`: at most MAX_SUBCODES shown and `+N` for the rest, within NOTE_MAX characters
    (codes are dropped into the `+N` count until it fits). No sub-codes: `top` alone."""
    subs = list(dict.fromkeys(subs or []))
    if not subs:
        return top

    def build(shown: int) -> str:
        extra = len(subs) - shown
        body = ",".join(subs[:shown])
        if extra:
            body += ("," if shown else "") + f"+{extra}"
        return f"{top}:{body}"
    shown = min(MAX_SUBCODES, len(subs))
    code = build(shown)
    while len(code) > NOTE_MAX and shown > 0:
        shown -= 1
        code = build(shown)
    return code


def plan_rejected(errors) -> str:
    """The code for a plan_preview rejection: the kind (the first that matches, as before) plus the sub-codes
    of every error."""
    text = " ".join(str(e) for e in (errors or []))
    if "addon_quantity" in text or "addonProducts" in text or "add-on product" in text:
        top = f"{PLAN_REJECTED}:addon_products"
    elif "session_catalogue" in text:
        top = f"{PLAN_REJECTED}:session_catalogue"
    elif "institution_defaults" in text:
        top = f"{PLAN_REJECTED}:institution_defaults"
    else:
        top = f"{PLAN_REJECTED}:input_invalid"
    return with_subcodes(top, subcodes_of(errors))


def plan_rejected_by_famly(status_code) -> str:
    """A Famly rejection of the preview request: the HTTP status only."""
    try:
        return f"{PLAN_REJECTED}:famly_http_{int(status_code)}"
    except (TypeError, ValueError):
        return f"{PLAN_REJECTED}:famly_error"


def request_invalid(kind: str, errors=None) -> str:
    """A queue or chain request refused at intake: `kind` is one of the fixed words the caller passes
    (input_invalid, timeline_invalid, segments_invalid); `errors` adds the sub-codes of what was wrong."""
    return with_subcodes(f"{PLAN_REJECTED}:{kind}", subcodes_of(errors))


def commit_failed(detail) -> str:
    """The code for a failed direct commit, from approval's detail string: the HTTP class when Famly answered
    ("Famly rejected the plan (HTTP 4xx)" / "Famly upstream error (HTTP 5xx)"), else the generic code."""
    match = _HTTP.search(str(detail or ""))
    if match:
        return COMMIT_FAILED_5XX if match.group(1).startswith("5") else COMMIT_FAILED_4XX
    return COMMIT_FAILED


def queue_job_failed(reason) -> str:
    """The code for a failed queue job, from the worker's reason string."""
    text = str(reason or "")
    for needle, kind in (("was not created", "plan_not_created"), ("unexpected error", "unexpected_error"),
                         ("could not be resumed", "resume_failed"), ("not the child's latest", "join_problem"),
                         ("read back", "unverified"), ("after ", "join_problem")):
        if needle in text:
            return f"{QUEUE_JOB_FAILED}:{kind}"
    return QUEUE_JOB_FAILED


# A warning that holds a queue job, by its `error` code (Famly's own, or the local kinds in warnings.py). The
# warning's title and message are never used: a code outside this table is `warning_other`.
HELD_WARNING_CODES = {
    "1614173107": "plan_past_child_last_day",
    "DiscountExcluded": "discount_excluded",
    "SiteUnresolved": "site_unresolved",
    "FundingMismatch": "funding_mismatch",
}
WARNING_OTHER = "warning_other"
HELD_OTHER = "other"

# The other reasons a plan is held, matched on the worker's own reason text (never Famly's).
HELD_TEXT_CODES = (
    ("rounding was not applied", "rounding_not_applied"),
    ("the preview has no pricing periods", "no_pricing_periods"),
    ("is 0 and no funding is applied", "zero_estimate_no_funding"),
    ("is not above 0", "estimate_not_above_zero"),
    ("QUEUE_MAX_MONTHLY", "estimate_over_ceiling"),
    ("QUEUE_ALLOWED_INSTITUTIONS", "institution_not_allowed"),
    ("not allowed to auto-commit", "institution_not_allowed"),
)


def warning_reason_code(error) -> str:
    """The reason code for one warning, from its error code only."""
    return HELD_WARNING_CODES.get(str(error or "").strip(), WARNING_OTHER)


def held_code_of(reason) -> str:
    """The reason code for one hold reason string (the worker's text). `warning:` reasons here have no error
    code to go on, so they are `warning_other`; the worker maps warnings by their code itself."""
    text = str(reason or "")
    if text.startswith("warning:"):
        return WARNING_OTHER
    return next((code for needle, code in HELD_TEXT_CODES if needle in text), HELD_OTHER)


def queue_job_held(codes) -> str:
    """The note for a held queue job: `queue_job_held:<reason>[,<reason>...]`, at most NOTE_MAX characters."""
    return with_subcodes(QUEUE_JOB_HELD, codes or [HELD_OTHER])


def chain_failed(reason) -> str:
    """The code for a stopped chain, from the engine's reason string."""
    text = str(reason or "")
    if "could not be committed" in text:
        return f"{CHAIN_FAILED}:commit_failed"
    if "could not be verified" in text or "when read back" in text:
        return f"{CHAIN_FAILED}:commit_unknown"
    if "was refused" in text:
        return f"{CHAIN_FAILED}:preview_refused"
    if "could not be posted to Slack" in text:
        return f"{CHAIN_FAILED}:slack_not_posted"
    if "preview of" in text and "failed" in text:
        return f"{CHAIN_FAILED}:preview_failed"
    if "after " in text:
        return f"{CHAIN_FAILED}:join_problem"
    return CHAIN_FAILED
