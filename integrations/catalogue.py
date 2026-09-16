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


def load_titles() -> tuple[dict, dict]:
    """Read the catalogue.

    Returns:
        (session_titles, product_titles). Both empty when the file is missing,
        unreadable or malformed -- never raises, because a cosmetic lookup must
        not cost the approver the Slack message.
    """
    path = catalogue_path()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "Catalogue file %s not found; the Slack summary will show raw IDs",
            path,
        )
        return {}, {}
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not read the catalogue file %s (%s); the Slack summary will "
            "show raw IDs",
            path,
            exc,
        )
        return {}, {}

    if not isinstance(raw, dict):
        logger.warning("Catalogue file %s is not a JSON object", path)
        return {}, {}

    return _titles(raw.get("sessions")), _titles(raw.get("products"))
