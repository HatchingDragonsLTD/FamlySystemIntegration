"""Pull Famly's live room/group ids into `reference/groups_catalogue.json`,
mirroring `actions/pull_sessions`' backup/merge/dry-run/--institution
conventions.

Institutions come from `reference/catalogue.json`'s `sites` section, same as
pull_sessions: each site's stored `institutionId` decides which groups belong
to it.

QUERY SHAPE CONFIRMED AGAINST THE LIVE API (not just docs.famly.co, which
turned out wrong on two points -- see query.graphql for the full story):
`groups(institutionIds: [InstitutionId!])` REQUIRES an institution filter (one
of `institutionIds`/`groupIds`/`siteSetIds` -- this pull always supplies the
first) and has no pagination cursor. Unlike pull_sessions, this pull makes
exactly ONE GraphQL call carrying every in-scope institution's id at once
(rather than one call per institution), and partitions the single response
afterwards using each group's own `institutionId` field. A group whose
`institutionId` does not match any of the institutions asked for is reported
as unmatched -- never guessed into a site -- though in practice Famly should
never return one, since the call only ever asks for known institutions.

Writing merges into the existing file (institutions this run did not touch --
including every one skipped by an `--institution` filter, or all of them if
the single API call itself failed -- are left exactly as they were) and always
backs the previous file up first, timestamped alongside it.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.client import GraphQLClient, GraphQLError, GraphQLHTTPError
from integrations import catalogue
from integrations.catalogue import UnknownInstitutionError  # re-exported; see there

logger = logging.getLogger(__name__)

QUERY_PATH = Path(__file__).with_name("query.graphql")
OPERATION_NAME = "Groups"

DEFAULT_CATALOGUE_PATH = (
    Path(__file__).resolve().parents[2] / "reference" / "groups_catalogue.json"
)


def catalogue_path() -> Path:
    """The groups catalogue file in use."""
    override = os.environ.get("GROUPS_CATALOGUE_FILE", "").strip()
    return Path(override) if override else DEFAULT_CATALOGUE_PATH


@dataclass
class GroupRow:
    id: str | None = None
    title: str | None = None
    institution_id: str | None = None


@dataclass
class UnmatchedGroup:
    """A group Famly returned that could not be attributed to a requested site."""

    group_id: str | None
    title: str
    institution_id: str | None
    reason: str


@dataclass
class PullResult:
    # {"institutions": {...}} in the groups_catalogue.json shape, merged with
    # whatever the file already had. This is what gets written.
    catalogue: dict
    # Preserves a leading "_comment" the existing file had, if any.
    comment: Any = None
    written_counts: dict[str, int] = field(default_factory=dict)
    unmatched: list[UnmatchedGroup] = field(default_factory=list)
    institutions_pulled: list[str] = field(default_factory=list)
    institutions_skipped: dict[str, str] = field(default_factory=dict)
    # The SINGLE call's own failure (there is no independent per-institution
    # call to fail here -- see the module docstring).
    fetch_error: str | None = None


def _normalise_institution_code(code: str) -> str:
    return (code or "").strip().upper()


def _parse_group(node: Any) -> GroupRow | None:
    if not isinstance(node, dict):
        return None
    return GroupRow(
        id=node.get("id"),
        title=node.get("title"),
        institution_id=node.get("institutionId"),
    )


def fetch_groups(
    institution_ids: list[str], client: GraphQLClient | None = None
) -> list[GroupRow]:
    """Every group across the given institutions, in one call.

    `institution_ids` must be non-empty: the query requires one of
    `institutionIds`/`groupIds`/`siteSetIds` (Famly rejects a call supplying
    none of them). There is no pagination cursor on this query.

    Raises:
        GraphQLError / GraphQLHTTPError: propagated deliberately, uncaught --
            a failure here means nothing was fetched this run, and that must
            be surfaced clearly rather than treated as "zero groups".
    """
    client = client or GraphQLClient()

    body = client.execute(
        query_path=QUERY_PATH,
        variables={"institutionIds": institution_ids},
        operation_name=OPERATION_NAME,
    )

    data = body.get("data") if isinstance(body, dict) else None
    listing = data.get("groups") if isinstance(data, dict) else None

    if not isinstance(listing, list):
        return []

    return [row for row in (_parse_group(node) for node in listing) if row is not None]


def _read_existing_catalogue(path: Path) -> dict:
    """The current groups_catalogue.json, or {} when there is none yet.

    A file that EXISTS but is not valid JSON is a hard failure here (raises),
    not treated as empty -- see pull_sessions' identical reasoning.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"pull_groups: {path} exists but could not be read as JSON ({exc}); "
            f"refusing to overwrite it -- fix or remove it by hand first"
        ) from exc

    return raw if isinstance(raw, dict) else {}


def pull_all(
    client: GraphQLClient | None = None, institutions: list[str] | None = None
) -> PullResult:
    """Pull live group ids for every institution in catalogue.json's sites.

    Nothing is written to disk here -- see `write_catalogue`.

    Args:
        client: optional GraphQLClient, mainly for tests.
        institutions: optional site codes (case-insensitive, matched the same
            way `catalogue.resolve_site` does) restricting the pull to just
            those institutions -- passed straight through as the query's
            `institutionIds`, so (unlike pull_sessions' REST calls, but like
            it in spirit) this filter changes what gets FETCHED, not just what
            gets merged. None (the default) fetches every site in
            catalogue.json's sites section. An institution not found among
            those sites raises UnknownInstitutionError before any Famly call
            is made.

    Returns:
        A PullResult ready to hand to `write_catalogue`.

    Raises:
        UnknownInstitutionError: `institutions` named a code catalogue.json's
            sites do not have.
    """
    sites = catalogue.load_sites()

    if institutions is not None:
        requested = [_normalise_institution_code(code) for code in institutions]
        unknown = [code for code in requested if code not in sites]
        if unknown:
            raise UnknownInstitutionError(
                f"unknown institution code(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(sites)) or 'none configured'})"
            )
        sites = {code: sites[code] for code in dict.fromkeys(requested)}

    existing_raw = _read_existing_catalogue(catalogue_path())
    existing_institutions = existing_raw.get("institutions")
    if not isinstance(existing_institutions, dict):
        existing_institutions = {}
    merged_institutions = json.loads(json.dumps(existing_institutions))

    # institutionId (Famly's UUID) -> site code, for both the query's
    # argument and matching the response back to a code.
    id_to_code = {
        entry.get("institutionId"): code
        for code, entry in sites.items()
        if isinstance(entry, dict) and entry.get("institutionId")
    }

    skipped = {
        code: "no institutionId configured in catalogue.json"
        for code, entry in sites.items()
        if not (isinstance(entry, dict) and entry.get("institutionId"))
    }

    if not id_to_code:
        # Nothing with an institutionId to ask for at all -- calling the API
        # with an empty institutionIds list would itself be rejected (see the
        # module docstring), so there is nothing to fetch this run.
        return PullResult(
            catalogue={"institutions": merged_institutions},
            comment=existing_raw.get("_comment"),
            institutions_skipped=skipped,
        )

    try:
        groups = fetch_groups(list(id_to_code), client)
    except (GraphQLError, GraphQLHTTPError) as exc:
        # The one call failed: nothing fetched, nothing merged, existing
        # entries left exactly as they were.
        return PullResult(
            catalogue={"institutions": merged_institutions},
            comment=existing_raw.get("_comment"),
            institutions_skipped=skipped,
            fetch_error=str(exc),
        )

    new_groups_by_code: dict[str, dict[str, str]] = {}
    unmatched: list[UnmatchedGroup] = []

    for group in groups:
        code = id_to_code.get(group.institution_id)
        if code is None:
            # Should not happen -- the call only ever asked for known
            # institutions -- but never guessed into one if it somehow does.
            unmatched.append(
                UnmatchedGroup(
                    group_id=group.id,
                    title=group.title or "untitled",
                    institution_id=group.institution_id,
                    reason=(
                        f"institutionId {group.institution_id!r} was not among "
                        f"the institutions requested"
                    ),
                )
            )
            continue

        if not group.id or not group.title:
            unmatched.append(
                UnmatchedGroup(
                    group_id=group.id,
                    title=group.title or "untitled",
                    institution_id=group.institution_id,
                    reason="missing id or title",
                )
            )
            continue

        new_groups_by_code.setdefault(code, {})[group.id] = group.title

    written_counts: dict[str, int] = {}
    pulled: list[str] = []

    for code, new_groups in new_groups_by_code.items():
        existing_entry = merged_institutions.get(code)
        merged_groups = (
            dict(existing_entry.get("groups", {}))
            if isinstance(existing_entry, dict)
            else {}
        )
        merged_groups.update(new_groups)
        merged_institutions[code] = {"groups": merged_groups}
        written_counts[code] = len(new_groups)
        pulled.append(code)

    return PullResult(
        catalogue={"institutions": merged_institutions},
        comment=existing_raw.get("_comment"),
        written_counts=written_counts,
        unmatched=unmatched,
        institutions_pulled=pulled,
        institutions_skipped=skipped,
    )


def backup_path(path: Path) -> Path:
    """A timestamped sibling path, e.g. groups_catalogue.2026-09-29T120000Z.bak.json."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")


def write_catalogue(result: PullResult, path: Path | None = None) -> Path | None:
    """Back up the existing file, then write the merged catalogue.

    Returns:
        The backup path, or None when there was no existing file to back up.
    """
    path = path or catalogue_path()
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
    """A CLI-friendly summary: counts per institution, and every unmatched group."""
    return {
        "dryRun": dry_run,
        "institutionsPulled": result.institutions_pulled,
        "writtenPerInstitution": result.written_counts,
        "institutionsSkipped": result.institutions_skipped,
        "fetchError": result.fetch_error,
        "unmatchedCount": len(result.unmatched),
        "unmatched": [
            {
                "groupId": u.group_id,
                "title": u.title,
                "institutionId": u.institution_id,
                "reason": u.reason,
            }
            for u in result.unmatched
        ],
        "backupFile": str(backup) if backup else None,
    }
