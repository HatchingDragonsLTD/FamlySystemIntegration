"""Pure derivations from a HubSpot deal's properties: site, group, stage.

No I/O except reading the two reference files. Every failure is a HOLD reason
code, never a guess: a wrong room or wrong site misprices a plan.

AGE RULE (design doc, section 14): the child's age in COMPLETED MONTHS at the
START date (`actual_start_date_1`), not today. Under 24 = Hatchlings, 24 to
under 36 = Littlewings, 36 or more = Voyagers.

"Completed month" convention (an assumption, flagged to Drew): the monthly
anniversary of a birth day that does not exist in a shorter month falls on that
month's LAST day, as `EDATE` and most date libraries do. So a child born on the
31st completes a month on 28 February (29 in a leap year), and a child born on
29 February completes a year on 28 February. The alternative (anniversary on
the 1st of the next month) would keep such a child in the younger room a day
or two longer.
"""

import calendar
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from integrations import catalogue

# location_of_interest -> site code. Anything else (Shoreditch, Head office,
# Nannying, Other, empty) is a HOLD.
LOCATION_TO_SITE = {
    "westminster": "HDWEST",
    "city": "HDCITY",
    "canada water": "HDCW",
}

HATCHLINGS = "Hatchlings"
LITTLEWINGS = "Littlewings"
VOYAGERS = "Voyagers"

DEFAULT_GROUPS_PATH = Path(__file__).resolve().parents[2] / "reference" / "groups_catalogue.json"

# Hold reason codes.
SITE_UNMAPPED = "site_unmapped"
SITE_ID_MISSING = "site_id_missing"
GROUP_MISSING = "group_missing"
GROUP_AMBIGUOUS = "group_ambiguous"
DOB_MISSING = "dob_missing"
START_DATE_MISSING = "start_date_missing"
DATE_INVALID = "date_invalid"
DATES_INCONSISTENT = "dates_inconsistent"


class Hold(Exception):
    """A derivation could not be made safely. `reason` is the hold code."""

    def __init__(self, reason: str, **detail: Any):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def parse_hubspot_date(value: Any) -> date | None:
    """A HubSpot date property as a `date`, or None when empty.

    HubSpot returns date properties as "YYYY-MM-DD" (or an ISO datetime) through
    the API, and as epoch milliseconds (midnight UTC) for some legacy
    properties. Raises ValueError for anything else.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(text))).date()
    return date.fromisoformat(text[:10])


def _add_months(start: date, months: int) -> date:
    """`start` plus `months`, clamping the day to the target month's length."""
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def completed_months(dob: date, on: date) -> int:
    """Whole months the child has completed on `on`. Requires dob <= on."""
    months = (on.year - dob.year) * 12 + (on.month - dob.month)
    if _add_months(dob, months) > on:
        months -= 1
    return months


def group_title_for_age(months: int) -> str:
    if months < 24:
        return HATCHLINGS
    if months < 36:
        return LITTLEWINGS
    return VOYAGERS


def group_title(dob_value: Any, start_value: Any) -> str:
    """The room title for a child, by age at the start date.

    Raises:
        Hold: a missing, unparseable or inconsistent date.
    """
    try:
        dob = parse_hubspot_date(dob_value)
        start = parse_hubspot_date(start_value)
    except ValueError:
        raise Hold(DATE_INVALID) from None
    if dob is None:
        raise Hold(DOB_MISSING)
    if start is None:
        raise Hold(START_DATE_MISSING)
    if start < dob:
        raise Hold(DATES_INCONSISTENT)
    return group_title_for_age(completed_months(dob, start))


# --------------------------------------------------------------------------- #
# Site and group id
# --------------------------------------------------------------------------- #
def site_for_location(location: Any) -> tuple[str, str]:
    """(site code, Famly institution id) for `location_of_interest`.

    Raises:
        Hold: SITE_UNMAPPED for any value that is not Westminster, City or
            Canada Water; SITE_ID_MISSING if catalogue.json has no id for it.
    """
    key = str(location).strip().casefold() if isinstance(location, str) else ""
    code = LOCATION_TO_SITE.get(key)
    if code is None:
        raise Hold(SITE_UNMAPPED)

    entry = catalogue.load_sites().get(code)
    institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
    if not institution_id:
        raise Hold(SITE_ID_MISSING, site=code)
    return code, institution_id


def groups_path() -> Path:
    override = os.environ.get("GROUPS_CATALOGUE_FILE", "").strip()
    return Path(override) if override else DEFAULT_GROUPS_PATH


def group_id_for(site_code: str, title: str) -> str:
    """The Famly group id titled `title` at `site_code`, matched by title.

    Raises:
        Hold: GROUP_MISSING if the site has no such group (or the file cannot
            be read); GROUP_AMBIGUOUS if two groups share the title.
    """
    try:
        raw = json.loads(groups_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Hold(GROUP_MISSING, site=site_code, title=title) from None

    institutions = raw.get("institutions") if isinstance(raw, dict) else None
    site = institutions.get(site_code) if isinstance(institutions, dict) else None
    groups = site.get("groups") if isinstance(site, dict) else None
    if not isinstance(groups, dict):
        raise Hold(GROUP_MISSING, site=site_code, title=title)

    wanted = title.strip().casefold()
    found = [
        group_id
        for group_id, group_name in groups.items()
        if isinstance(group_name, str) and group_name.strip().casefold() == wanted
    ]
    if not found:
        raise Hold(GROUP_MISSING, site=site_code, title=title)
    if len(found) > 1:
        raise Hold(GROUP_AMBIGUOUS, site=site_code, title=title)
    return found[0]


# --------------------------------------------------------------------------- #
# Stage (reported only; nothing is built on it in this phase)
# --------------------------------------------------------------------------- #
DEPOSIT_UNPAID = "unpaid"
DEPOSIT_PAID = "paid"
DEPOSIT_NONE = "none"
DEPOSIT_UNKNOWN = "unknown"

_UNPAID_STAGES = {"deposit pending"}
_NONE_STAGES = {"left nursery"}
_PAID_STAGES = {
    "deposit paid",
    "eyman onboarded",
    "settling in booked",
    "active in school",
    "frozen account",
    "gave notice",
}


def deposit_state(pipeline_label: str, stage_label: str) -> str:
    """Whether a deposit would be unpaid (Deposit Pending), paid (a later
    Operations stage) or none (Left Nursery). `unknown` for any other stage,
    including every Sales stage."""
    if (pipeline_label or "").strip().casefold() != "operations":
        return DEPOSIT_UNKNOWN
    stage = (stage_label or "").strip().casefold()
    if stage in _UNPAID_STAGES:
        return DEPOSIT_UNPAID
    if stage in _NONE_STAGES:
        return DEPOSIT_NONE
    if stage in _PAID_STAGES:
        return DEPOSIT_PAID
    return DEPOSIT_UNKNOWN
