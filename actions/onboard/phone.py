"""Phone number normalisation for Famly contacts.

UK numbers become +44 E.164: strip spaces and punctuation; a leading 0 becomes
+44; `0044` and `44` prefixes and a `(0)` after +44 are understood. A number
that is already E.164 for another country is kept as it is. Anything that cannot
be normalised CONFIDENTLY returns None (the caller omits the phone and warns
`phone_unnormalised`): a wrong number on a parent's record is worse than none.

A UK national number is exactly 10 digits after the leading 0 (7700 900123 for
a mobile, 20 7946 0000 for a London landline). Anything else is not guessed at.
"""

import re

from core import hubspot

_STRIP = re.compile(r"[\s\-\.\(\)]")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# Properties in the order a phone is chosen, with the Famly type each becomes.
# (source code, HubSpot property, Famly PhoneType)
PHONE_ORDER = (
    ("mobile", "mobilephone", "MOBILE"),
    ("work", "phone", "WORK"),
    ("whatsapp", hubspot.WHATSAPP_PROPERTY, "MOBILE"),
)


def normalise_uk(raw) -> str | None:
    """`raw` as +44 E.164 (or another country's E.164 if already in that form),
    or None if it cannot be normalised confidently."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    # "+44 (0)7700 900123": the (0) after the country code is not dialled.
    text = re.sub(r"^(\+\s*44)\s*\(0\)", r"\1", text)
    cleaned = _STRIP.sub("", text)

    if not re.fullmatch(r"\+?\d+", cleaned):
        return None  # letters, slashes, extensions, two numbers in one field

    if cleaned.startswith("+"):
        if cleaned.startswith("+44"):
            national = cleaned[3:]
            if national.startswith("0"):
                national = national[1:]
            return f"+44{national}" if re.fullmatch(r"\d{10}", national) else None
        return cleaned if _E164.match(cleaned) else None

    if cleaned.startswith("0044"):
        national = cleaned[4:]
        if national.startswith("0"):
            national = national[1:]
        return f"+44{national}" if re.fullmatch(r"\d{10}", national) else None
    if cleaned.startswith("0"):
        national = cleaned[1:]
        return f"+44{national}" if re.fullmatch(r"\d{10}", national) else None
    if cleaned.startswith("44") and re.fullmatch(r"44\d{10}", cleaned):
        return f"+{cleaned}"
    return None


def pick_phone(properties: dict) -> tuple[str, str | None, str | None]:
    """(source, famly phone type, E.164 number or None).

    The FIRST present property in PHONE_ORDER is used. If it cannot be
    normalised the result is (source, type, None): it is NOT replaced by a later
    property, so the caller can warn `phone_unnormalised`. No property present
    gives ("none", None, None).
    """
    for source, name, famly_type in PHONE_ORDER:
        value = str(properties.get(name) or "").strip()
        if value:
            return source, famly_type, normalise_uk(value)
    return "none", None, None
