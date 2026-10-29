"""plan-check: for each deal in a file, read it from HubSpot (GET only) and report, as sub-codes, which of the
plan_preview validation rules its inputs would fail. READ-ONLY: no Famly request, no HubSpot write, no Slack,
and the note codes are the same ones the server writes (`status_codes`). Ids and codes only.

IT IS PARTIAL, and says so in its output (`coverage`). The plan_preview body is built by a HubSpot custom-code
action that is NOT in this repo, so the body cannot be reproduced exactly from the deal. What this command
CAN do is rebuild, from the deal properties the repo already reads, the inputs the plan depends on:

    famly_childid            -> childId
    actual_start_date_1      -> start_date
    end_date                 -> leave_date (empty = open ended)
    location_of_interest     -> institution (the site code, as the audit does)
    monday .. friday         -> the weekday slots
    billable_fees            -> (plan-check only) fees_missing / fees_unparseable

and run the SAME functions the queue and chain intake run on them: `engine.parse_request`,
`timeline.build_segments` and `engine.validate_segments` (which parses the payload like the preview and runs
`input_schema.validate`, the session catalogue and the institution defaults, all local files). A deal that
passes here can still be rejected for an input this cannot see (NOT_CHECKED below); a deal that fails here
would be rejected by the intake for the same reason.
"""

from collections import Counter

from actions.create_timeline import engine
from actions.onboard_audit import runner as audit
from actions.onboard_dry_run import derive
from actions.plan_write import status_codes, timeline
from core.hubspot import HubSpotError

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
CHECKED = ["child id", "start date", "end date", "site", "weekday slots", "institution defaults and session catalogue",
           "fees (billable_fees present and numeric)"]
NOT_CHECKED = ["funded flag and funding settings", "meals and activities flags", "termTimeOnly",
               "addon_quantity", "discount slots", "advanced fee", "site_code and note",
               "(all come from the workflow's custom-code action, which is not in this repo)"]

DEAL_NOT_FOUND, DEAL_READ_FAILED = "deal_not_found", "deal_read_failed"


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def check_deal(deal: dict) -> list[str]:
    """The sub-codes the deal's plan inputs would fail, in first-seen order (empty = nothing found)."""
    codes: list[str] = []
    try:
        site_code = derive.site_for_location(deal.get("location_of_interest"))[0]
    except derive.Hold:
        site_code = ""
        codes.append("site_unknown")

    start = end = None
    try:
        start = derive.parse_hubspot_date(deal.get("actual_start_date_1"))
    except ValueError:
        codes.append("start_date_invalid")
    try:
        end = derive.parse_hubspot_date(deal.get("end_date"))
    except ValueError:
        codes.append("end_date_invalid")

    payload = {"childId": _clean(deal.get("famly_childid")), "institution": site_code,
               "start_date": start.isoformat() if start else "", "leave_date": end.isoformat() if end else "",
               **{day: _clean(deal.get(day)) for day in WEEKDAYS}}
    parsed = engine.parse_request(payload)
    if parsed.request is None:
        codes += status_codes.subcodes_of(parsed.errors)
    else:
        request = parsed.request
        try:
            segments, _warnings = timeline.build_segments(request.start, request.leave, funding=request.funding,
                                                          advanced=request.advanced)
        except timeline.TimelineError as exc:
            codes += status_codes.subcodes_of([str(exc)])
        else:
            codes += status_codes.subcodes_of(engine.validate_segments(request.payload, segments))

    fees = _clean(deal.get("billable_fees"))
    if not fees:
        codes.append("fees_missing")
    elif audit._number(fees) is None:
        codes.append("fees_unparseable")

    codes = list(dict.fromkeys(codes))
    if "start_date_invalid" in codes and "start_date_missing" in codes:
        codes.remove("start_date_missing")            # an unreadable date is not also "missing"
    if "site_unknown" in codes and "institution_missing" in codes:
        codes.remove("institution_missing")
    return codes


def run(hubspot, deal_ids: list[str]) -> dict:
    """Read each deal (GET only) and check it. Counts, ids and codes only."""
    per_deal: dict[str, list[str]] = {}
    unreadable: dict[str, str] = {}
    for deal_id in deal_ids:
        try:
            deal = hubspot.get_deal(deal_id)
        except HubSpotError as exc:
            unreadable[deal_id] = DEAL_NOT_FOUND if exc.status_code == 404 else DEAL_READ_FAILED
            continue
        per_deal[deal_id] = check_deal(deal)
    counts = Counter(code for codes in per_deal.values() for code in codes)
    problems = sum(1 for codes in per_deal.values() if codes)
    return {"mode": "read_only", "coverage": "partial", "checked": CHECKED, "notChecked": NOT_CHECKED,
            "deals": len(deal_ids), "dealsChecked": len(per_deal), "withProblems": problems,
            "clean": len(per_deal) - problems, "bySubcode": dict(sorted(counts.items())),
            "perDeal": per_deal, "unreadable": unreadable, "hubspotRequests": getattr(hubspot, "calls", None)}
