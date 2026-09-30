"""Institution x schedule -> Famly's billing/pricing defaults for a plan.

FUNCTIONALLY LOAD-BEARING, same standing as `integrations/session_catalogue.py`.
HubSpot used to send `ruleGroupId`, `attendanceScheduleId`, `weeksOfCare`,
`billingId`, `billingTitle` and `billingInvoices` as flat, institution-wide
constants on every webhook -- values that never actually varied per deal, only
per institution (and, for the billing-profile fields, per term-time-only vs
full-year schedule). `billingProfileId` was never sent at all and reached
Famly as a bare `null`, relying on Famly's own default for the plan part.

None of that is true anymore. HubSpot no longer sends any of those six fields;
`actions/plan_write/hubspot_flatten.py` resolves all of them from
`reference/institution_defaults.json` via `resolve_defaults` below, keyed by
the deal's `institution` and a `schedule` derived from its `termTimeOnly` flag
(see `schedule_key`). This also means `billingProfileId` gets a REAL,
institution-correct value for the first time, rather than always being null.

Structure (see reference/institution_defaults.json):

    institutions.<code>.ruleGroupId                                -> ULID
    institutions.<code>.schedules.<schedule>.billingProfileId      -> UUID
    institutions.<code>.schedules.<schedule>.attendanceScheduleId  -> UUID
    institutions.<code>.schedules.<schedule>.weeksOfCare           -> number
    institutions.<code>.schedules.<schedule>.billingId             -> billing
                                               scheme enum string (e.g.
                                               "ANNUALIZED_V2")
    institutions.<code>.schedules.<schedule>.billingTitle          -> string
    institutions.<code>.schedules.<schedule>.billingInvoices       -> number
    institutions.<code>.addonProducts.mealsProductId               -> UUID
    institutions.<code>.addonProducts.activitiesProductId          -> UUID

`schedule` is one of SCHEDULE_ALL_YEAR_ROUND / SCHEDULE_TERM_ONLY -- see
`schedule_key`. `ruleGroupId` and `addonProducts` sit at the institution
level, not inside a schedule bucket: neither varies by attendance schedule the
way a billing profile does. `addonProducts` is written by
`actions/pull_products` (exact product-title match, see its module
docstring), NOT by `actions/pull_institution_defaults` -- each pull owns a
different top-level key and merges without disturbing the other's (see both
runners' "shared file, separate ownership" notes). `resolve_addon_products`
below is deliberately a SEPARATE accessor from `resolve_defaults`: addon
products only matter for a non-funded deal with `addon_quantity` set (see
`hubspot_flatten.build_product_bookings`), so a plan that never touches that
must not be forced to have `addonProducts` configured.

The file is re-read on every call, matching `session_catalogue`'s reasoning:
it is small, a preview is a rare event, and an edit then takes effect without
a server restart. Also like `session_catalogue`, there is NO safe degraded
mode -- a wrong or guessed billing profile silently misbills a real family, so
a missing entry raises rather than returning None or a default.

Environment:
    INSTITUTION_DEFAULTS_FILE  optional path override. Defaults to
                               reference/institution_defaults.json.
"""

import json
import os
from pathlib import Path
from typing import Any

# reference/institution_defaults.json, resolved relative to the project root
# (this file lives in integrations/, one level down).
DEFAULT_CATALOGUE_PATH = (
    Path(__file__).resolve().parent.parent / "reference" / "institution_defaults.json"
)

# The two schedule buckets a billing profile is bucketed into, by its
# attendanceSchedule.fullYear boolean (see actions/pull_institution_defaults).
SCHEDULE_ALL_YEAR_ROUND = "all_year_round"
SCHEDULE_TERM_ONLY = "term_only"
VALID_SCHEDULES = (SCHEDULE_ALL_YEAR_ROUND, SCHEDULE_TERM_ONLY)

# The fields a schedule bucket must have every one of, for resolve_defaults to
# consider it usable. ruleGroupId is checked separately -- it lives at the
# institution level, not inside a bucket.
_SCHEDULE_FIELDS = (
    "billingProfileId",
    "attendanceScheduleId",
    "weeksOfCare",
    "billingId",
    "billingTitle",
    "billingInvoices",
)


class InstitutionDefaultsError(RuntimeError):
    """No usable default for the requested institution/schedule.

    Raised rather than returning None -- same reasoning as
    `session_catalogue.SessionCatalogueError`: a caller resolving billing
    defaults for a real plan must not be able to silently treat a gap as "no
    defaults", since that would either crash later with a less useful error or
    (worse) let Famly apply its own default against the wrong institution.
    """


def catalogue_path() -> Path:
    """The institution_defaults file in use."""
    override = os.environ.get("INSTITUTION_DEFAULTS_FILE", "").strip()
    return Path(override) if override else DEFAULT_CATALOGUE_PATH


def schedule_key(term_time_only: Any) -> str:
    """The schedule bucket a plan's `termTimeOnly` flag resolves to.

    Mirrors how `actions/pull_institution_defaults` buckets Famly's own
    `attendanceSchedule.fullYear`: term-time-only -> term_only, otherwise (the
    full year) -> all_year_round. Any truthy value counts as term-time-only,
    same convention as `hubspot_flatten._as_bool`.
    """
    return SCHEDULE_TERM_ONLY if term_time_only else SCHEDULE_ALL_YEAR_ROUND


def _read() -> dict:
    """Parse the catalogue file. Raises InstitutionDefaultsError on any problem.

    Unlike `integrations/catalogue.py`, there is no safe degraded mode here --
    see the module docstring.
    """
    path = catalogue_path()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InstitutionDefaultsError(
            f"institution_defaults: file not found at {path}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise InstitutionDefaultsError(
            f"institution_defaults: could not read {path} ({exc})"
        ) from exc

    if not isinstance(raw, dict):
        raise InstitutionDefaultsError(f"institution_defaults: {path} is not a JSON object")

    return raw


def _find_institution(institutions: dict, institution: str) -> Any:
    """Case-insensitive, trimmed lookup by institution code."""
    code = (institution or "").strip().upper()
    for key, value in institutions.items():
        if isinstance(key, str) and key.strip().upper() == code:
            return value
    return None


def resolve_defaults(institution: str, schedule: str) -> dict:
    """Look up one institution's ruleGroupId and one schedule's billing defaults.

    Args:
        institution: the institution code HubSpot sends, e.g. "HDCITY".
            Matched case-insensitively and trimmed, same as
            `session_catalogue.resolve_session`.
        schedule: SCHEDULE_ALL_YEAR_ROUND or SCHEDULE_TERM_ONLY (see
            `schedule_key`).

    Returns:
        {"ruleGroupId", "billingProfileId", "attendanceScheduleId",
         "weeksOfCare", "billingId", "billingTitle", "billingInvoices"}.

    Raises:
        InstitutionDefaultsError: naming exactly what was missing -- the
            institution itself, the requested schedule, or which field(s) in
            that schedule's bucket were empty. There is no safe fallback: a
            wrong or guessed billing profile would misbill a real family.
    """
    raw = _read()

    institutions = raw.get("institutions")
    if not isinstance(institutions, dict):
        raise InstitutionDefaultsError("institution_defaults: no 'institutions' section")

    entry = _find_institution(institutions, institution)
    if not isinstance(entry, dict):
        raise InstitutionDefaultsError(
            f"institution_defaults: no institution {institution!r}"
        )

    rule_group_id = entry.get("ruleGroupId")
    if not isinstance(rule_group_id, str) or not rule_group_id.strip():
        raise InstitutionDefaultsError(
            f"institution_defaults: no ruleGroupId for institution {institution!r}"
        )

    if schedule not in VALID_SCHEDULES:
        raise InstitutionDefaultsError(
            f"institution_defaults: {schedule!r} is not a valid schedule (expected "
            f"one of {', '.join(VALID_SCHEDULES)})"
        )

    schedules = entry.get("schedules")
    if not isinstance(schedules, dict):
        raise InstitutionDefaultsError(
            f"institution_defaults: no 'schedules' for institution {institution!r}"
        )

    bucket = schedules.get(schedule)
    if not isinstance(bucket, dict):
        raise InstitutionDefaultsError(
            f"institution_defaults: no {schedule!r} schedule for institution "
            f"{institution!r}"
        )

    result: dict[str, Any] = {"ruleGroupId": rule_group_id.strip()}
    missing = []
    for field_name in _SCHEDULE_FIELDS:
        value = bucket.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field_name)
        result[field_name] = value

    if missing:
        raise InstitutionDefaultsError(
            f"institution_defaults: institution {institution!r} > schedules > "
            f"{schedule!r} is missing {', '.join(missing)}"
        )

    return result


# The two addon-product fields resolve_addon_products checks for.
_ADDON_PRODUCT_FIELDS = ("mealsProductId", "activitiesProductId")


def resolve_addon_products(institution: str) -> dict:
    """The institution's two fixed add-on product ids.

    Written by `actions/pull_products` via an exact (case-sensitive) title
    match against "Meals & Snacks" and "Educational Activities & Extras" --
    see that module's docstring. Kept separate from `resolve_defaults` (see
    the module docstring) since not every plan needs these.

    Args:
        institution: the institution code HubSpot sends, e.g. "HDCITY".
            Matched case-insensitively and trimmed, same as
            `resolve_defaults`.

    Returns:
        {"mealsProductId": ..., "activitiesProductId": ...}.

    Raises:
        InstitutionDefaultsError: naming exactly what was missing -- the
            institution, or which of the two ids. There is no safe fallback:
            a wrong or guessed product id would book/bill the wrong item.
    """
    raw = _read()

    institutions = raw.get("institutions")
    if not isinstance(institutions, dict):
        raise InstitutionDefaultsError("institution_defaults: no 'institutions' section")

    entry = _find_institution(institutions, institution)
    if not isinstance(entry, dict):
        raise InstitutionDefaultsError(
            f"institution_defaults: no institution {institution!r}"
        )

    addon_products = entry.get("addonProducts")
    if not isinstance(addon_products, dict):
        raise InstitutionDefaultsError(
            f"institution_defaults: no addonProducts for institution {institution!r}"
        )

    result: dict[str, Any] = {}
    missing = []
    for field_name in _ADDON_PRODUCT_FIELDS:
        value = addon_products.get(field_name)
        if not isinstance(value, str) or not value.strip():
            missing.append(field_name)
        result[field_name] = value

    if missing:
        raise InstitutionDefaultsError(
            f"institution_defaults: institution {institution!r} > addonProducts "
            f"is missing {', '.join(missing)}"
        )

    return result
