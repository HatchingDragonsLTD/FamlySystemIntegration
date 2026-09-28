"""Institution x funded-variant x time-slot -> Famly session UUID.

FUNCTIONALLY LOAD-BEARING. Unlike `integrations/catalogue.py` (display names
only, safe to leave blank), this catalogue CHOOSES which session gets booked.
HubSpot no longer sends a pre-resolved session UUID -- it sends a raw slot
string plus institution/funded/meals/activities, and this module resolves the
UUID. There is no safe fallback for a gap: an empty or guessed UUID would
book the wrong session (or none) against a real child, so a missing entry
raises rather than returning None, naming exactly which part of the path was
missing.

Structure (see reference/session_catalogue.json):

    institutions.<code>.funded.<variant>.<slot>   -> uuid
    institutions.<code>.non_funded.<slot>          -> uuid

`variant` describes what is MISSING from the funded session, not what is
present -- an easy naming trap:

    has_meals=True,  has_activities=True  -> with_meals_and_activities
    has_meals=True,  has_activities=False -> no_activities   (meals only)
    has_meals=False, has_activities=True  -> no_meals        (activities only)
    has_meals=False, has_activities=False -> neither

The file is re-read on every call: it is a few hundred bytes and a preview is
a rare event, so an edit takes effect without a server restart. Matches the
style of `integrations/catalogue.py`, except that module degrades to a safe
default on a bad file and this one cannot -- there is nothing safe to fall
back to.

Environment:
    SESSION_CATALOGUE_FILE  optional path override. Defaults to
                            reference/session_catalogue.json.
"""

import json
import os
from pathlib import Path
from typing import Any

# reference/session_catalogue.json, resolved relative to the project root
# (this file lives in integrations/, one level down).
DEFAULT_CATALOGUE_PATH = (
    Path(__file__).resolve().parent.parent / "reference" / "session_catalogue.json"
)

# The funded variant keys, and the (has_meals, has_activities) each answers.
VARIANT_BOTH = "with_meals_and_activities"
VARIANT_MEALS_ONLY = "no_activities"
VARIANT_ACTIVITIES_ONLY = "no_meals"
VARIANT_NEITHER = "neither"

VALID_SLOTS = ("morning", "afternoon", "full_day")


class SessionCatalogueError(RuntimeError):
    """The catalogue has no usable UUID for the requested path.

    Raised rather than returning None: a caller resolving a session for a real
    booking must not be able to silently treat a gap as "no session".
    """


def catalogue_path() -> Path:
    """The catalogue file in use."""
    override = os.environ.get("SESSION_CATALOGUE_FILE", "").strip()
    return Path(override) if override else DEFAULT_CATALOGUE_PATH


def _read() -> dict:
    """Parse the catalogue file. Raises SessionCatalogueError on any problem.

    Unlike `integrations/catalogue.py`, there is no safe degraded mode here --
    a missing or malformed file cannot fall back to "show a UUID instead of a
    name", so it is itself a hard failure.
    """
    path = catalogue_path()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SessionCatalogueError(
            f"session_catalogue: file not found at {path}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise SessionCatalogueError(
            f"session_catalogue: could not read {path} ({exc})"
        ) from exc

    if not isinstance(raw, dict):
        raise SessionCatalogueError(f"session_catalogue: {path} is not a JSON object")

    return raw


def _variant_key(has_meals: bool, has_activities: bool) -> str:
    """The funded variant key for a (has_meals, has_activities) pair.

    NAMING TRAP: the variant name describes what is MISSING, not what is
    present -- `no_activities` is the meals-only variant, `no_meals` is the
    activities-only one.
    """
    if has_meals and has_activities:
        return VARIANT_BOTH
    if has_meals and not has_activities:
        return VARIANT_MEALS_ONLY
    if not has_meals and has_activities:
        return VARIANT_ACTIVITIES_ONLY
    return VARIANT_NEITHER


def normalise_slot(slot: Any) -> str:
    """`"full day"` / `"Full Day"` / `"full_day"` all normalise the same way.

    Public so any other producer of a slot alias (e.g. the session-catalogue
    pull script parsing Famly's own session titles) reuses this exact rule
    rather than re-implementing it.
    """
    if not isinstance(slot, str):
        return ""
    return slot.strip().lower().replace(" ", "_")


def _find_institution(institutions: dict, institution: str) -> Any:
    """Case-insensitive, trimmed lookup by institution code."""
    code = (institution or "").strip().upper()
    for key, value in institutions.items():
        if isinstance(key, str) and key.strip().upper() == code:
            return value
    return None


def resolve_session(
    institution: str,
    funded: bool,
    slot: str,
    has_meals: bool = False,
    has_activities: bool = False,
) -> str:
    """Look up the Famly session UUID for one booked day.

    Args:
        institution: the institution code HubSpot sends, e.g. "HDCITY".
            Matched case-insensitively and trimmed.
        funded: whether the deal is a funded deal. Non-funded looks up
            directly in the institution's `non_funded` branch.
        slot: "morning", "afternoon" or "full_day" (a "full day" with a space
            is accepted and normalised).
        has_meals: only meaningful when `funded` is True; ignored otherwise.
        has_activities: only meaningful when `funded` is True; ignored
            otherwise.

    Returns:
        The session UUID.

    Raises:
        SessionCatalogueError: naming exactly which part of the path --
            the institution, the funded/non_funded branch, the variant, or
            the slot -- was missing or had no UUID filled in. There is no
            safe fallback: an empty or guessed session UUID would book the
            wrong thing (or nothing) against a real child.
    """
    raw = _read()

    institutions = raw.get("institutions")
    if not isinstance(institutions, dict):
        raise SessionCatalogueError("session_catalogue: no 'institutions' section")

    entry = _find_institution(institutions, institution)
    if not isinstance(entry, dict):
        raise SessionCatalogueError(f"session_catalogue: no institution {institution!r}")

    slot_key = normalise_slot(slot)
    if slot_key not in VALID_SLOTS:
        raise SessionCatalogueError(
            f"session_catalogue: {slot!r} is not a valid slot (expected one of "
            f"{', '.join(VALID_SLOTS)})"
        )

    if funded:
        variant = _variant_key(bool(has_meals), bool(has_activities))
        funded_node = entry.get("funded")
        if not isinstance(funded_node, dict):
            raise SessionCatalogueError(
                f"session_catalogue: no UUID for institution {institution!r} > funded"
            )
        variant_node = funded_node.get(variant)
        if not isinstance(variant_node, dict):
            raise SessionCatalogueError(
                f"session_catalogue: no UUID for institution {institution!r} > "
                f"funded > {variant}"
            )
        uuid = variant_node.get(slot_key)
        path_label = f"institution {institution!r} > funded > {variant} > {slot_key}"
    else:
        non_funded_node = entry.get("non_funded")
        if not isinstance(non_funded_node, dict):
            raise SessionCatalogueError(
                f"session_catalogue: no UUID for institution {institution!r} > "
                f"non_funded"
            )
        uuid = non_funded_node.get(slot_key)
        path_label = f"institution {institution!r} > non_funded > {slot_key}"

    if not isinstance(uuid, str) or not uuid.strip():
        raise SessionCatalogueError(f"session_catalogue: no UUID for {path_label}")

    return uuid.strip()
