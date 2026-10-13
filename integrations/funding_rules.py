"""Funding settings per institution: method, weekly funded hours, and the cap.

FUNCTIONALLY LOAD-BEARING, same standing as `integrations/institution_defaults.py`.
The funding settings on a funded plan decide what the government funding covers
and so what the family is billed, so they come from THE SERVER, per institution,
and never from whatever HubSpot happens to send.

`reference/funding_rules.json` is HAND-MAINTAINED and git-tracked. No pull
command reads Famly and writes it (unlike the other files in `reference/`), so
nothing can overwrite a figure someone typed in. Structure:

    fundingMethod                                          -> string, shared
    institutions.<code>.maxFundedMinutes                   -> number
    institutions.<code>.fundedWeeklyHours                  -> null, or
        <entitlement>.<schedule>                           -> number

`<entitlement>` is the funded-hours entitlement as a string ("30" or "15");
`<schedule>` is `all_year_round` or `term_only` (see
`institution_defaults.schedule_key`).

A `fundedWeeklyHours` of null means "not set up yet" and is a HARD ERROR when a
funded plan needs it. So is any missing entry, naming the exact path, e.g.

    funding_rules: no fundedWeeklyHours for HDCITY > 30 > term_only

There is NEVER a fallback to another institution's numbers, a default, or a
guess: a wrong figure here silently misbills a real family.

The file is re-read on every call, so an edit takes effect without a restart.

Environment:
    FUNDING_RULES_FILE  optional path override. Defaults to
                        reference/funding_rules.json.
"""

import json
import os
from pathlib import Path
from typing import Any

from . import institution_defaults

DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parent.parent / "reference" / "funding_rules.json"
)


class FundingRulesError(RuntimeError):
    """No usable funding settings for the requested institution/entitlement."""


def rules_path() -> Path:
    """The funding rules file in use."""
    override = os.environ.get("FUNDING_RULES_FILE", "").strip()
    return Path(override) if override else DEFAULT_RULES_PATH


def _read() -> dict:
    """Parse the rules file. Raises FundingRulesError on any problem."""
    path = rules_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FundingRulesError(f"funding_rules: file not found at {path}") from exc
    except (OSError, ValueError) as exc:
        raise FundingRulesError(f"funding_rules: could not read {path} ({exc})") from exc

    if not isinstance(raw, dict):
        raise FundingRulesError(f"funding_rules: {path} is not a JSON object")
    return raw


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entitlement_text(entitlement: Any) -> str:
    """The entitlement as the string key it is stored under, or ''."""
    if isinstance(entitlement, bool):
        return ""
    if isinstance(entitlement, int):
        return str(entitlement)
    return entitlement.strip() if isinstance(entitlement, str) else ""


def resolve_funding(institution: str, entitlement: Any, term_only: Any) -> dict:
    """The funding settings for one institution, entitlement and schedule.

    Args:
        institution: the institution code, e.g. "HDCW". Matched
            case-insensitively and trimmed.
        entitlement: "30" or "15" (a string; a whole number is accepted as its
            string).
        term_only: the plan's `termTimeOnly`, picking `term_only` over
            `all_year_round`.

    Returns:
        {"fundingMethod", "fundingHours", "maxFundedMinutes"}.

    Raises:
        FundingRulesError: naming exactly what is missing -- the file, the
            institution, its cap, the shared method, or the exact
            `fundedWeeklyHours` path. Never falls back to anything else.
    """
    raw = _read()
    code = (institution or "").strip().upper()

    institutions = raw.get("institutions")
    if not isinstance(institutions, dict):
        raise FundingRulesError("funding_rules: no 'institutions' section")

    key, entry = next(
        (
            (k, v)
            for k, v in institutions.items()
            if isinstance(k, str) and k.strip().upper() == code
        ),
        (None, None),
    )
    if key is None or not isinstance(entry, dict):
        raise FundingRulesError(f"funding_rules: no institution {code or '(none)'}")
    name = key.strip()

    method = raw.get("fundingMethod")
    if not isinstance(method, str) or not method.strip():
        raise FundingRulesError("funding_rules: no fundingMethod")

    cap = entry.get("maxFundedMinutes")
    if not _number(cap):
        raise FundingRulesError(f"funding_rules: no maxFundedMinutes for {name}")

    wanted = _entitlement_text(entitlement)
    if not wanted:
        raise FundingRulesError(
            "funding_rules: funding_entitlement is required for a funded plan "
            "(a string such as \"30\" or \"15\")"
        )
    schedule = institution_defaults.schedule_key(term_only)
    path = f"funding_rules: no fundedWeeklyHours for {name} > {wanted} > {schedule}"

    weekly = entry.get("fundedWeeklyHours")
    by_entitlement = weekly.get(wanted) if isinstance(weekly, dict) else None
    hours = by_entitlement.get(schedule) if isinstance(by_entitlement, dict) else None
    if not _number(hours):
        raise FundingRulesError(path)

    return {
        "fundingMethod": method.strip(),
        "fundingHours": hours,
        "maxFundedMinutes": cap,
    }
