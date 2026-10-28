"""The note codes for `famly_plan_note`: a short CODE per kind of problem, never the error text.

Raw error text can carry names, emails, response bodies and ids, so it never goes into HubSpot: every mapper
here returns one of a fixed set of codes (or a code plus an HTTP status class), chosen by matching the text,
and anything it does not recognise becomes the generic code for its kind. Codes use only a-z 0-9 _ : . , -
(`core.hubspot.PLAN_NOTE_PATTERN`), so a note can never hold free text.

  plan_rejected:<kind>     the plan_preview (or queue / chain request) was rejected, nothing was created;
  commit_failed*           the Approve commit failed (Famly said no, or an unexpected error);
  commit_unknown           the commit went through but the plan could not be confirmed by a live read;
  queue_job_failed:<kind>  a queued job failed;
  chain_failed:<kind>      a multi-plan chain stopped.
"""

import re

PLAN_REJECTED = "plan_rejected"
COMMIT_FAILED = "commit_failed"
COMMIT_FAILED_4XX = "commit_failed_http_4xx"
COMMIT_FAILED_5XX = "commit_failed_http_5xx"
COMMIT_UNKNOWN = "commit_unknown"
QUEUE_JOB_FAILED = "queue_job_failed"
CHAIN_FAILED = "chain_failed"

_HTTP = re.compile(r"\(HTTP (\d{3})\)")


def plan_rejected(errors) -> str:
    """The code for a plan_preview rejection from its error strings (the first that matches a kind)."""
    text = " ".join(str(e) for e in (errors or []))
    if "addon_quantity" in text or "addonProducts" in text or "add-on product" in text:
        return f"{PLAN_REJECTED}:addon_products"
    if "session_catalogue" in text:
        return f"{PLAN_REJECTED}:session_catalogue"
    if "institution_defaults" in text:
        return f"{PLAN_REJECTED}:institution_defaults"
    return f"{PLAN_REJECTED}:input_invalid"


def plan_rejected_by_famly(status_code) -> str:
    """A Famly rejection of the preview request: the HTTP status only."""
    try:
        return f"{PLAN_REJECTED}:famly_http_{int(status_code)}"
    except (TypeError, ValueError):
        return f"{PLAN_REJECTED}:famly_error"


def request_invalid(kind: str) -> str:
    """A queue or chain request refused at intake: `kind` is one of the fixed words the caller passes."""
    return f"{PLAN_REJECTED}:{kind}"


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
