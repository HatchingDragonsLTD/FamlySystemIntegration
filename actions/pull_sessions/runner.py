"""Pull Famly's live session UUIDs for every institution and refresh
`reference/session_catalogue.json`, replacing the manual copy-paste process.

Institutions come from `reference/catalogue.json`'s `sites` section (the
site-code work): each site's stored `institutionId` is what gets queried.
Nothing here touches `catalogue.json` itself -- that file is display names
only; this script writes exclusively to `session_catalogue.json`, the
FUNCTIONALLY LOAD-BEARING file `integrations/session_catalogue.py` reads at
booking time.

For each institution, GET
    v2/sessions?institutionId=<id>&validOn=<today>&includeDiscontinued=false
               &behaviors=1&includePricesForAllPricingGroups=true
(discontinued sessions are deliberately excluded -- a discontinued session's
UUID must never end up chosen for a new booking).

Each returned session's `title` is classified by a fixed prefix convention:

    (F)    -> funded, variant "with_meals_and_activities"
    (FNM)  -> funded, variant "no_meals"
    (FNA)  -> funded, variant "no_activities"
    (FNE)  -> funded, variant "neither"
    (no parenthesised prefix at all) -> non_funded

Whatever remains after the prefix (or the whole title, when there is none) is
normalised into a slot key with `session_catalogue.normalise_slot` -- the SAME
function `resolve_session` itself uses, so slot-text handling lives in one
place. A title that does not cleanly resolve to a (funded, variant, slot)
triple -- an unrecognised prefix, or clean prefix but unmappable remainder --
is collected as an error and WRITES NOTHING for that session. There is no
partial or guessed entry: a wrong session UUID is worse than a gap the
existing hard-failure path in hubspot_flatten will catch instead.

Writing merges into the existing file (institutions/variants/slots this run
did not touch are left exactly as they were) and always backs the previous
file up first, timestamped alongside it, so a bad pull can be reverted by
hand.
"""

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from core.rest_client import RestClient, RestHTTPError
from integrations import catalogue, session_catalogue

logger = logging.getLogger(__name__)

SESSIONS_PATH = "v2/sessions"

# The exact prefix -> variant mapping. Order does not matter for matching: the
# four prefixes are distinct fixed strings, none a substring-match hazard of
# another once the surrounding parentheses are part of the comparison.
PREFIX_TO_VARIANT = {
    "F": "with_meals_and_activities",
    "FNM": "no_meals",
    "FNA": "no_activities",
    "FNE": "neither",
}

# A leading "(...)" token, captured for lookup against PREFIX_TO_VARIANT, plus
# whatever follows. `[^)]*` rather than letters-only so a genuinely wrong
# prefix (a typo, stray punctuation) still gets EXTRACTED and reported by name
# rather than silently falling through to "no prefix" (non_funded).
_PREFIX_RE = re.compile(r"^\(([^)]*)\)\s*(.*)$")


class TitleParseError(ValueError):
    """A session title did not cleanly match the prefix/slot convention."""


class UnknownInstitutionError(ValueError):
    """An `institutions` filter named a code not in catalogue.json's sites.

    Raised before any Famly call is made -- an unrecognised code is a mistake
    worth stopping for, not a silent no-op that quietly pulls nothing.
    """


@dataclass
class ParsedTitle:
    funded: bool
    variant: str | None  # None for non_funded
    slot: str


@dataclass
class UnmatchedSession:
    """A session whose title could not be classified. Nothing was written for it."""

    institution: str
    session_id: str | None
    title: str
    reason: str


@dataclass
class PullResult:
    # {"institutions": {...}} in the exact session_catalogue.json shape,
    # merged with whatever the file already had. This is what gets written.
    catalogue: dict
    # Preserves a leading "_comment" the existing file had, if any.
    comment: Any = None
    written_counts: dict[str, int] = field(default_factory=dict)
    unmatched: list[UnmatchedSession] = field(default_factory=list)
    institutions_pulled: list[str] = field(default_factory=list)
    institutions_skipped: dict[str, str] = field(default_factory=dict)
    institutions_failed: dict[str, str] = field(default_factory=dict)


def _split_title(title: str) -> tuple[str | None, str]:
    """(prefix_text, remainder). `prefix_text` is None when there was none."""
    match = _PREFIX_RE.match(title.strip())
    if not match:
        return None, title.strip()
    return match.group(1).strip(), match.group(2).strip()


def parse_title(title: Any) -> ParsedTitle:
    """Classify one session title into (funded, variant, slot).

    Raises:
        TitleParseError: the title is missing, its prefix is not one of the
            four recognised codes, or the text after the prefix (or the whole
            title, for a non-funded session) does not normalise to a known
            slot. The message names what went wrong, for the run's error list.
    """
    if not isinstance(title, str) or not title.strip():
        raise TitleParseError("empty or missing title")

    prefix, remainder = _split_title(title)

    if prefix is None:
        funded = False
        variant = None
    else:
        variant = PREFIX_TO_VARIANT.get(prefix)
        if variant is None:
            raise TitleParseError(
                f"unrecognised prefix '({prefix})' (expected one of "
                f"{', '.join(f'({p})' for p in PREFIX_TO_VARIANT)})"
            )
        funded = True

    slot = session_catalogue.normalise_slot(remainder)
    if slot not in session_catalogue.VALID_SLOTS:
        raise TitleParseError(
            f"{remainder!r} does not map to a known slot (expected one of "
            f"{', '.join(session_catalogue.VALID_SLOTS)})"
        )

    return ParsedTitle(funded=funded, variant=variant, slot=slot)


def _parse_sessions_response(body: Any) -> list[dict]:
    """The response's session list, defensively.

    Accepts either a bare list or a dict wrapping one under a plausible key --
    the exact envelope Famly's v2/sessions uses was not pinned down here, so
    this degrades to an empty list (reported as zero sessions pulled) rather
    than raising on an unexpected but non-error shape.
    """
    if isinstance(body, list):
        candidates: Any = body
    elif isinstance(body, dict):
        candidates = body.get("sessions") or body.get("data") or body.get("results") or []
    else:
        candidates = []

    if not isinstance(candidates, list):
        return []
    return [s for s in candidates if isinstance(s, dict)]


def fetch_institution_sessions(
    institution_id: str, client: RestClient, *, today: str | None = None
) -> list[dict]:
    """GET the live, non-discontinued sessions for one institution."""
    params = {
        "institutionId": institution_id,
        "validOn": today or date.today().isoformat(),
        "includeDiscontinued": "false",
        "behaviors": 1,
        "includePricesForAllPricingGroups": "true",
    }
    body = client.get(SESSIONS_PATH, params=params)
    return _parse_sessions_response(body)


def _read_existing_catalogue(path: Path) -> dict:
    """The current session_catalogue.json, or {} when there is none yet.

    A file that EXISTS but is not valid JSON is a hard failure here (raises),
    not treated as empty: silently starting from nothing would let a pull
    wipe out every hand-filled UUID other institutions still rely on. A
    missing file is fine -- there is nothing yet to merge with or lose.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"pull_sessions: {path} exists but could not be read as JSON ({exc}); "
            f"refusing to overwrite it -- fix or remove it by hand first"
        ) from exc

    return raw if isinstance(raw, dict) else {}


def _merge_institution(existing: Any, new_entries: dict) -> dict:
    """Merge this pull's slot UUIDs into one institution's existing entry.

    Deep-merges funded.<variant>.<slot> and non_funded.<slot>. Anything this
    pull did not touch -- a different variant, a different slot, or the whole
    institution when it errored out -- is left exactly as it was; nothing is
    ever removed by a merge.
    """
    merged = json.loads(json.dumps(existing)) if isinstance(existing, dict) else {}
    merged.setdefault("funded", {})
    merged.setdefault("non_funded", {})
    if not isinstance(merged["funded"], dict):
        merged["funded"] = {}
    if not isinstance(merged["non_funded"], dict):
        merged["non_funded"] = {}

    for variant, slots in new_entries.get("funded", {}).items():
        merged["funded"].setdefault(variant, {})
        if not isinstance(merged["funded"][variant], dict):
            merged["funded"][variant] = {}
        merged["funded"][variant].update(slots)

    merged["non_funded"].update(new_entries.get("non_funded", {}))

    return merged


def _normalise_institution_code(code: str) -> str:
    return (code or "").strip().upper()


def pull_all(
    client: RestClient | None = None, institutions: list[str] | None = None
) -> PullResult:
    """Pull live session UUIDs for every institution in catalogue.json's sites.

    Nothing is written to disk here -- see `write_catalogue`. A per-institution
    Famly failure (RestHTTPError) does not abort the run: it is recorded under
    `institutions_failed` and every other institution still gets pulled.

    Args:
        client: optional RestClient, mainly for tests.
        institutions: optional site codes (case-insensitive, matched the same
            way `catalogue.resolve_site` does) restricting the pull to just
            those institutions. None (the default) processes every site in
            catalogue.json's sites section, unchanged from an unfiltered run.
            An institution not found among those sites is not silently
            skipped -- it means the filter itself is probably wrong, so it
            raises UnknownInstitutionError before any Famly call is made.

    Returns:
        A PullResult ready to hand to `write_catalogue`.

    Raises:
        UnknownInstitutionError: `institutions` named a code catalogue.json's
            sites do not have.
    """
    client = client or RestClient()

    sites = catalogue.load_sites()

    if institutions is not None:
        requested = [_normalise_institution_code(code) for code in institutions]
        unknown = [code for code in requested if code not in sites]
        if unknown:
            raise UnknownInstitutionError(
                f"unknown institution code(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(sites)) or 'none configured'})"
            )
        # De-duplicate while keeping only the requested sites, in case the
        # same code was passed more than once.
        sites = {code: sites[code] for code in dict.fromkeys(requested)}

    existing_raw = _read_existing_catalogue(session_catalogue.catalogue_path())
    existing_institutions = existing_raw.get("institutions")
    if not isinstance(existing_institutions, dict):
        existing_institutions = {}

    merged_institutions = json.loads(json.dumps(existing_institutions))
    written_counts: dict[str, int] = {}
    unmatched: list[UnmatchedSession] = []
    pulled: list[str] = []
    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}

    for code, entry in sites.items():
        institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if not institution_id:
            skipped[code] = "no institutionId configured in catalogue.json"
            continue

        try:
            sessions = fetch_institution_sessions(institution_id, client)
        except RestHTTPError as exc:
            failed[code] = str(exc)
            continue

        new_entries: dict = {"funded": {}, "non_funded": {}}
        count = 0

        for session in sessions:
            title = session.get("title")
            session_id = session.get("id")
            readable_title = title if isinstance(title, str) else repr(title)

            if not session_id:
                unmatched.append(
                    UnmatchedSession(
                        institution=code,
                        session_id=None,
                        title=readable_title,
                        reason="session has no id",
                    )
                )
                continue

            try:
                parsed = parse_title(title)
            except TitleParseError as exc:
                unmatched.append(
                    UnmatchedSession(
                        institution=code,
                        session_id=session_id,
                        title=readable_title,
                        reason=str(exc),
                    )
                )
                continue

            if parsed.funded:
                new_entries["funded"].setdefault(parsed.variant, {})[parsed.slot] = (
                    session_id
                )
            else:
                new_entries["non_funded"][parsed.slot] = session_id
            count += 1

        merged_institutions[code] = _merge_institution(
            merged_institutions.get(code), new_entries
        )
        written_counts[code] = count
        pulled.append(code)

    return PullResult(
        catalogue={"institutions": merged_institutions},
        comment=existing_raw.get("_comment"),
        written_counts=written_counts,
        unmatched=unmatched,
        institutions_pulled=pulled,
        institutions_skipped=skipped,
        institutions_failed=failed,
    )


def backup_path(path: Path) -> Path:
    """A timestamped sibling path, e.g. session_catalogue.2026-09-28T120000Z.bak.json."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")


def write_catalogue(result: PullResult, path: Path | None = None) -> Path | None:
    """Back up the existing file, then write the merged catalogue.

    The backup happens BEFORE the write, unconditionally, whenever a file
    already exists -- a bad pull must always be revertable by hand.

    Returns:
        The backup path, or None when there was no existing file to back up
        (a first-ever run).
    """
    path = path or session_catalogue.catalogue_path()
    backup = None

    if path.exists():
        backup = backup_path(path)
        shutil.copy2(path, backup)

    payload: dict = {}
    if result.comment is not None:
        payload["_comment"] = result.comment
    payload.update(result.catalogue)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return backup


def summary_payload(
    result: PullResult, *, backup: Path | None = None, dry_run: bool = False
) -> dict:
    """A CLI-friendly summary: counts per institution, and every unmatched title."""
    return {
        "dryRun": dry_run,
        "institutionsPulled": result.institutions_pulled,
        "writtenPerInstitution": result.written_counts,
        "institutionsSkipped": result.institutions_skipped,
        "institutionsFailed": result.institutions_failed,
        "unmatchedCount": len(result.unmatched),
        "unmatched": [
            {
                "institution": u.institution,
                "sessionId": u.session_id,
                "title": u.title,
                "reason": u.reason,
            }
            for u in result.unmatched
        ],
        "backupFile": str(backup) if backup else None,
    }
