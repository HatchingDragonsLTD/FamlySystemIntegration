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


# Why a number was not accepted: a small FIXED set of codes (never any part of the number).
REASON_EMPTY = "phone_empty"
REASON_LETTERS = "phone_has_letters"                 # letters (an extension, a note, two words)
REASON_CHARACTERS = "phone_bad_characters"           # other characters: / , ; a second +, invisible marks
REASON_LENGTH = "phone_wrong_length"                 # a recognised prefix, the wrong number of digits
REASON_PREFIX = "phone_not_uk_prefix"                # digits with no prefix we can trust
REASONS = (REASON_EMPTY, REASON_LETTERS, REASON_CHARACTERS, REASON_LENGTH, REASON_PREFIX)


def _uk(national: str) -> tuple[str | None, str | None]:
    if national.startswith("0"):
        national = national[1:]
    if re.fullmatch(r"\d{10}", national):
        return f"+44{national}", None
    return None, REASON_LENGTH


def explain(raw) -> tuple[str | None, str | None]:
    """(number, None) when `raw` normalises confidently, else (None, a REASONS code).
    The rules are exactly those of the module doc; only the reason is new."""
    if not isinstance(raw, str):
        return None, REASON_EMPTY
    text = raw.strip()
    if not text:
        return None, REASON_EMPTY

    # "+44 (0)7700 900123": the (0) after the country code is not dialled.
    text = re.sub(r"^(\+\s*44)\s*\(0\)", r"\1", text)
    cleaned = _STRIP.sub("", text)

    if not re.fullmatch(r"\+?\d+", cleaned):
        # letters, slashes, extensions, two numbers in one field
        return None, REASON_LETTERS if any(ch.isalpha() for ch in cleaned) else REASON_CHARACTERS

    if cleaned.startswith("+"):
        if cleaned.startswith("+44"):
            return _uk(cleaned[3:])
        if _E164.match(cleaned):
            return cleaned, None
        return None, REASON_LENGTH if re.fullmatch(r"\+[1-9]\d*", cleaned) else REASON_PREFIX

    if cleaned.startswith("0044"):
        return _uk(cleaned[4:])
    if cleaned.startswith("0"):
        national = cleaned[1:]
        if re.fullmatch(r"\d{10}", national):
            return f"+44{national}", None
        return None, REASON_LENGTH
    if cleaned.startswith("44"):
        if re.fullmatch(r"44\d{10}", cleaned):
            return f"+{cleaned}", None
        return None, REASON_LENGTH
    return None, REASON_PREFIX


def normalise_uk(raw) -> str | None:
    """`raw` as +44 E.164 (or another country's E.164 if already in that form),
    or None if it cannot be normalised confidently."""
    return explain(raw)[0]


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


def pick_phone_detail(properties: dict) -> tuple[str, str | None, str | None, str | None, str | None]:
    """`pick_phone` plus why it failed: (source, famly type, number, reason, property).

    `reason` is a REASONS code when a property was present but not accepted, else None;
    `property` is the HubSpot property name that was tried (None when none was present).
    The number itself is never part of the reason."""
    for source, name, famly_type in PHONE_ORDER:
        value = str(properties.get(name) or "").strip()
        if value:
            number, reason = explain(value)
            return source, famly_type, number, reason, name
    return "none", None, None, None, None
