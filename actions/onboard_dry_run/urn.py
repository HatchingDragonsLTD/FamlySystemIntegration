"""Child URNs for deals that have none (ONBOARD_URN_GENERATION=on).

Format: "HD" + 4 digits, zero-padded, + a 2-letter code of the ISSUING site, uppercase
(HD0001CW). Each site's sequence starts at 0001. Legacy URNs (ending CD, from EY Man) and
any other child_urn already on a deal are never touched or parsed here: a deal that has a
child_urn simply uses it. One URN per child for life; a sibling gets their own.
"""

import os
import re
from typing import Any

# Catalogue site code -> the 2-letter code in a URN. The ISSUING site's code. TO EXTEND:
# add the site here (Shoreditch is to be added by Drew); a site not listed holds the deal
# (`urn_site_code_unknown`): a code is never guessed.
SITE_URN_CODES = {"HDCW": "CW", "HDCITY": "CL", "HDWEST": "WM"}

MAX_NUMBER = 9999
_NEW_FORMAT = re.compile(r"^HD(\d{4})([A-Z]{2})$")

# plan `urn.status` values
EXISTING = "existing"
WOULD_ALLOCATE = "would_allocate"
ALLOCATED = "allocated"                      # reserved for this deal (never shown by a dry run that reserves nothing new)
CANDIDATE_RETURNING = "candidate_returning"
SITE_UNKNOWN = "site_unknown"
GENERATION_OFF_MISSING = "generation_off_missing"
CHILD_EXISTS = "child_exists"                # the child is already in Famly: no URN is needed

SITE_CODE_UNKNOWN = "urn_site_code_unknown"
POSSIBLY_RETURNING = "child_possible_returning"


def enabled() -> bool:
    """ONBOARD_URN_GENERATION=on (default off): off means a deal with no URN behaves as before."""
    return os.environ.get("ONBOARD_URN_GENERATION", "off").strip().lower() == "on"


def site_suffix(site_code: Any) -> str | None:
    return SITE_URN_CODES.get(str(site_code or "").strip().upper())


def format_urn(suffix: str, number: int) -> str:
    if not 1 <= number <= MAX_NUMBER:
        raise ValueError("urn number out of range")
    return f"HD{number:04d}{suffix}"


def parse_urn(value: Any) -> tuple[str, int] | None:
    """(site suffix, number) for a NEW-format URN, else None (legacy and every other
    format are ignored)."""
    match = _NEW_FORMAT.match(str(value or "").strip().upper())
    return (match.group(2), int(match.group(1))) if match else None
