"""Readable names for Famly session and product titles, derived at read time.

Famly's own titles are terse and prefix-coded -- "(F) Morning", "(FNM) Full
Day", "(1/2) Meals & Snacks" -- which reads badly in a Slack approval message.
This module turns them into plain wording, and nothing it produces is ever
stored: the pulled files hold the RAW Famly titles, and the readable form is
computed whenever a name is needed, so a wording change here applies
everywhere on the next preview with no re-pull.

SESSIONS. The raw title is classified by the EXISTING
`actions.pull_sessions.runner.parse_title` (reused, not re-implemented, so the
prefix table lives in exactly one place). The readable name is

    ["Funded "] + slot label + variant suffix

with slot labels Morning / Afternoon / Full Day, and, for a funded session
only, a suffix naming what is MISSING from it:

    with_meals_and_activities  ->  (none)
    no_meals                   ->  " (No Meals)"
    no_activities              ->  " (No Activities)"
    neither                    ->  " (No Extras)"

so "(FNM) Full Day" is "Funded Full Day (No Meals)", "(F) Morning" is "Funded
Morning", and a non-funded "Afternoon" is just "Afternoon".

PRODUCTS. A leading "(F) " becomes "Funded ", "(1/2) " becomes "Half Day ",
and "(1/2,F) " becomes "Half Day Funded "; a plain title is unchanged.

FALLBACK. Anything that does not parse -- a prefix that is not one of the four
known codes, a slot that is not morning/afternoon/full day, an empty title --
returns the raw Famly title untouched. A readable name is a nicety; it must
never hide what Famly actually calls the thing.

This module also owns where the two pulled files live, so the pulls that write
them and `integrations.catalogue.load_titles` that reads them cannot disagree:

    SESSION_TITLES_FILE       reference/session_titles.json
    PRODUCTS_CATALOGUE_FILE   reference/products_catalogue.json
"""

import os
from pathlib import Path
from typing import Any

_REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"

DEFAULT_SESSION_TITLES_PATH = _REFERENCE_DIR / "session_titles.json"
DEFAULT_PRODUCTS_CATALOGUE_PATH = _REFERENCE_DIR / "products_catalogue.json"

SLOT_LABELS = {
    "morning": "Morning",
    "afternoon": "Afternoon",
    "full_day": "Full Day",
}

# What is MISSING from a funded session, as a suffix. The variant keys are the
# ones `pull_sessions.PREFIX_TO_VARIANT` produces.
VARIANT_SUFFIXES = {
    "with_meals_and_activities": "",
    "no_meals": " (No Meals)",
    "no_activities": " (No Activities)",
    "neither": " (No Extras)",
}

# Product title prefix -> readable replacement. Matched exactly, at the start
# only, including the trailing space.
PRODUCT_PREFIXES = (
    ("(1/2,F) ", "Half Day Funded "),
    ("(1/2) ", "Half Day "),
    ("(F) ", "Funded "),
)


def session_titles_path() -> Path:
    """The pulled raw session titles file in use."""
    override = os.environ.get("SESSION_TITLES_FILE", "").strip()
    return Path(override) if override else DEFAULT_SESSION_TITLES_PATH


def products_catalogue_path() -> Path:
    """The pulled products catalogue file in use."""
    override = os.environ.get("PRODUCTS_CATALOGUE_FILE", "").strip()
    return Path(override) if override else DEFAULT_PRODUCTS_CATALOGUE_PATH


def session_display_name(raw_title: Any) -> str:
    """The readable name for a raw Famly session title (the raw title itself
    when it does not classify)."""
    if not isinstance(raw_title, str):
        return ""

    # Imported here, not at module level: the runner imports
    # `integrations.catalogue`, which imports this module, so a top-level
    # import would be circular. The function is the existing one, unchanged.
    from actions.pull_sessions import runner as pull_sessions

    try:
        parsed = pull_sessions.parse_title(raw_title)
    except pull_sessions.TitleParseError:
        return raw_title

    slot = SLOT_LABELS.get(parsed.slot)
    if slot is None:
        return raw_title

    if not parsed.funded:
        return slot

    suffix = VARIANT_SUFFIXES.get(parsed.variant)
    if suffix is None:
        return raw_title
    return f"Funded {slot}{suffix}"


def product_display_name(raw_title: Any) -> str:
    """The readable name for a raw Famly product title (unchanged when it has
    none of the known prefixes)."""
    if not isinstance(raw_title, str):
        return ""

    for prefix, replacement in PRODUCT_PREFIXES:
        if raw_title.startswith(prefix):
            return replacement + raw_title[len(prefix):]
    return raw_title
