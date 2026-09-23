"""Local names for Famly session and product UUIDs.

Famly's plans response identifies sessions and products by UUID only and
carries no titles, so the Slack plan-preview summary would otherwise list
`222faa9f-b12e-…` four times over. This maps those ids to readable names from a
small JSON file the setting maintains by hand.

INTERIM BY DESIGN. If a Famly endpoint that returns session/product names turns
up, swap the source here: `load_titles()` returns two plain dicts, which is all
`build_summary` takes, so nothing else changes.

The file is read on every call -- it is a few hundred bytes, and a preview is a
rare event, so an edit takes effect without restarting the server. Nothing here
raises: a missing, unreadable or malformed file yields empty maps and the
summary falls back to UUIDs.

Also holds the SITE registry: site_code -> {label, institutionId}. That is
metadata for the approver and for future site-aware queries -- `institution_id`
is never sent to Famly, and never decides which session, billing profile or
attendance schedule a plan uses. Those stay entirely determined by the UUIDs
HubSpot sends.

Environment:
    FAMLY_CATALOGUE_FILE  optional path override. Defaults to
                          reference/catalogue.json beside the project root.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# reference/catalogue.json, resolved relative to the project root (this file
# lives in integrations/, one level down).
DEFAULT_CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "reference" / "catalogue.json"


def catalogue_path() -> Path:
    """The catalogue file in use."""
    override = os.environ.get("FAMLY_CATALOGUE_FILE", "").strip()
    return Path(override) if override else DEFAULT_CATALOGUE_PATH


def _titles(node) -> dict:
    """One section as an id -> name map, keeping only usable entries.

    Blank names are dropped rather than kept as empty strings, so an unfilled
    placeholder behaves exactly like an absent id: the caller falls back to the
    UUID instead of printing nothing.
    """
    if not isinstance(node, dict):
        return {}

    titles = {}
    for key, value in node.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        name = value.strip()
        if name:
            titles[key] = name
    return titles


def _read() -> dict:
    """Parse the catalogue file. Returns {} on any problem -- never raises.

    Shared by every reader here, so a missing or malformed file degrades the
    same way throughout: names fall back to UUIDs, sites to unresolved.
    """
    path = catalogue_path()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "Catalogue file %s not found; the Slack summary will show raw IDs",
            path,
        )
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not read the catalogue file %s (%s); the Slack summary will "
            "show raw IDs",
            path,
            exc,
        )
        return {}

    if not isinstance(raw, dict):
        logger.warning("Catalogue file %s is not a JSON object", path)
        return {}

    return raw


def load_sites() -> dict:
    """Read the `sites` section: SITE_CODE -> {label, institutionId}.

    Keys are upper-cased so a lookup can be case-insensitive. Never raises: a
    missing or malformed file yields an empty registry, and the caller then
    treats every code as unrecognised rather than failing.
    """
    raw = _read()
    sites = raw.get("sites") if isinstance(raw, dict) else None
    if not isinstance(sites, dict):
        return {}

    registry = {}
    for code, entry in sites.items():
        if not isinstance(code, str) or not code.strip():
            continue
        entry = entry if isinstance(entry, dict) else {}
        # An explicit "code" wins over the key, for a site whose key is not the
        # code HubSpot sends.
        explicit = entry.get("code")
        key = explicit if isinstance(explicit, str) and explicit.strip() else code
        registry[key.strip().upper()] = entry

    return registry


def resolve_site(site_code: str) -> dict | None:
    """Look a site_code up in the catalogue.

    Args:
        site_code: the code HubSpot sends, e.g. "HDCITY". Matched
            case-insensitively and trimmed.

    Returns:
        {"site_code", "label", "institution_id"} for a known code, or None when
        the code is empty or unrecognised. A known code with an unfilled entry
        still resolves -- `label` falls back to the code itself and
        `institution_id` is None -- so a half-filled catalogue is safe.

    METADATA ONLY. `institution_id` is carried for future site-aware queries;
    nothing sends it to Famly today.
    """
    if not isinstance(site_code, str) or not site_code.strip():
        return None

    code = site_code.strip().upper()
    entry = load_sites().get(code)
    if entry is None:
        return None

    label = entry.get("label")
    label = label.strip() if isinstance(label, str) and label.strip() else None

    institution_id = entry.get("institutionId", entry.get("institution_id"))
    institution_id = (
        institution_id.strip()
        if isinstance(institution_id, str) and institution_id.strip()
        else None
    )

    return {
        "site_code": code,
        # Fall back to the code so the approver always sees something concrete.
        "label": label or code,
        "institution_id": institution_id,
    }


def load_titles() -> tuple[dict, dict]:
    """Read the session and product name maps.

    Returns:
        (session_titles, product_titles). Both empty when the file is missing,
        unreadable or malformed -- never raises, because a cosmetic lookup must
        not cost the approver the Slack message.
    """
    raw = _read()
    return _titles(raw.get("sessions")), _titles(raw.get("products"))
