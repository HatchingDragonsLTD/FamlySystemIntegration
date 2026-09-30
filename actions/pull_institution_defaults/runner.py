"""Pull Famly's live pricing rule group + billing-profile defaults per
institution into `reference/institution_defaults.json` -- the file
`integrations/institution_defaults.py` resolves at plan-build time (see its
module docstring for why this replaced HubSpot sending these as flat,
institution-wide constants).

Two internal-API queries per institution (both hit FAMLY_GRAPHQL_URL, the same
endpoint every OTHER action but pull_roster/urn_lookup uses):

    GetInstitutionSingleRuleGroupQuery -- Site.ruleGroups(namespace: "pricing"),
        batched in ONE call for every institution (see rule_group_query.graphql).
        Expects EXACTLY ONE result per institution; zero or more than one is a
        hard, reportable anomaly.

    GetBillingProfiles -- finance.billingProfiles.list(institutionSetId), ONE
        CALL PER INSTITUTION (institutionSetId is a single id, not a list --
        see billing_profiles_query.graphql). Each non-deleted profile is
        bucketed by its attendanceSchedule.fullYear: true -> all_year_round,
        false -> term_only. Exactly one profile per bucket is required; zero
        or more than one is a hard, reportable anomaly. If the two buckets'
        billing schemes differ from each other, that is ALSO a hard,
        reportable anomaly (a structural inconsistency, not a cosmetic one) --
        see `_billing_schemes_match`.

MONEY-RELEVANT, NO GUESSING: an institution either passes every one of these
checks and gets a COMPLETE, freshly-written entry (ruleGroupId + both schedule
buckets), or it fails entirely and its previous entry (if any) is left
untouched -- there is no partial write for one institution. This differs from
pull_sessions/pull_groups, which merge at a finer grain (slot/group), because a
partial institution_defaults entry here (say, a fresh term_only bucket sitting
next to a stale all_year_round one) would be actively misleading rather than
just incomplete.

Institutions come from reference/catalogue.json's `sites` section, same as
every other pull. Same conventions otherwise: --institution filter, --dry-run,
timestamped backup-before-write, per-institution failure isolation.
"""

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.client import GraphQLClient, GraphQLError, GraphQLHTTPError
from integrations import catalogue, institution_defaults
from integrations.catalogue import UnknownInstitutionError  # re-exported; see there
from integrations.institution_defaults import SCHEDULE_ALL_YEAR_ROUND, SCHEDULE_TERM_ONLY

logger = logging.getLogger(__name__)

RULE_GROUP_QUERY_PATH = Path(__file__).with_name("rule_group_query.graphql")
BILLING_PROFILES_QUERY_PATH = Path(__file__).with_name("billing_profiles_query.graphql")

RULE_GROUP_OPERATION_NAME = "GetInstitutionSingleRuleGroupQuery"
BILLING_PROFILES_OPERATION_NAME = "GetBillingProfiles"

PRICING_NAMESPACE = "pricing"


class InstitutionDefaultsAnomaly(RuntimeError):
    """A hard, reportable data problem for one institution -- see module docstring.

    Distinct from GraphQLError/GraphQLHTTPError: this is not a transport or API
    failure, it is Famly returning DATA that fails this pull's money-relevant
    validation (zero/multiple rule groups, zero/multiple profiles in a
    schedule bucket, or mismatched billing schemes across buckets).
    """


@dataclass
class ScheduleDefaults:
    billing_profile_id: str | None = None
    attendance_schedule_id: str | None = None
    weeks_of_care: float | None = None
    billing_id: str | None = None
    billing_title: str | None = None
    billing_invoices: float | None = None

    def to_json(self) -> dict:
        return {
            "billingProfileId": self.billing_profile_id,
            "attendanceScheduleId": self.attendance_schedule_id,
            "weeksOfCare": self.weeks_of_care,
            "billingId": self.billing_id,
            "billingTitle": self.billing_title,
            "billingInvoices": self.billing_invoices,
        }


@dataclass
class InstitutionDefaultsEntry:
    rule_group_id: str
    # "all_year_round" / "term_only" -> that bucket's defaults.
    schedules: dict[str, ScheduleDefaults]

    def to_json(self) -> dict:
        return {
            "ruleGroupId": self.rule_group_id,
            "schedules": {key: value.to_json() for key, value in self.schedules.items()},
        }


@dataclass
class PullResult:
    # {"institutions": {...}} in the institution_defaults.json shape, merged
    # with whatever the file already had. This is what gets written.
    catalogue: dict
    # Preserves a leading "_comment" the existing file had, if any.
    comment: Any = None
    institutions_pulled: list[str] = field(default_factory=list)
    institutions_skipped: dict[str, str] = field(default_factory=dict)
    institutions_failed: dict[str, str] = field(default_factory=dict)


def _normalise_institution_code(code: str) -> str:
    return (code or "").strip().upper()


def fetch_rule_groups(
    institution_ids: list[str], client: GraphQLClient | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Every institution's single 'pricing' ruleGroupId, in ONE call.

    Returns:
        (rule_group_by_id, failures_by_id). Each requested institution id
        lands in exactly one of the two: `rule_group_by_id` when Famly
        returned EXACTLY ONE pricing rule group for it, `failures_by_id` (with
        a human-readable reason) otherwise.

    Raises:
        GraphQLError / GraphQLHTTPError: propagated deliberately -- a failure
            of the single batched call means NOTHING was fetched this run for
            ANY institution, which the caller must treat as every institution
            failing, not as zero found.
    """
    client = client or GraphQLClient()

    body = client.execute(
        query_path=RULE_GROUP_QUERY_PATH,
        variables={"institutionIds": institution_ids},
        operation_name=RULE_GROUP_OPERATION_NAME,
    )

    data = body.get("data") if isinstance(body, dict) else None
    sites = data.get("institutions") if isinstance(data, dict) else None

    rule_group_by_id: dict[str, str] = {}
    failures_by_id: dict[str, str] = {}
    seen_ids: set = set()

    for site in sites or []:
        if not isinstance(site, dict):
            continue
        institution_id = site.get("institutionId")
        if not institution_id:
            continue
        seen_ids.add(institution_id)

        rule_groups = site.get("ruleGroups")
        if not isinstance(rule_groups, list) or len(rule_groups) != 1:
            count = len(rule_groups) if isinstance(rule_groups, list) else 0
            failures_by_id[institution_id] = (
                f"expected exactly one {PRICING_NAMESPACE!r} rule group, found {count}"
            )
            continue

        first = rule_groups[0] if isinstance(rule_groups[0], dict) else {}
        rule_group_id = first.get("ruleGroupId")
        if not rule_group_id:
            failures_by_id[institution_id] = (
                f"the {PRICING_NAMESPACE!r} rule group has no ruleGroupId"
            )
            continue

        rule_group_by_id[institution_id] = rule_group_id

    for institution_id in institution_ids:
        if institution_id not in seen_ids and institution_id not in failures_by_id:
            failures_by_id[institution_id] = "Famly returned no institution for this id"

    return rule_group_by_id, failures_by_id


def _billing_schemes_match(a: ScheduleDefaults, b: ScheduleDefaults) -> bool:
    return a.billing_id == b.billing_id and a.billing_title == b.billing_title


def _parse_schedule_defaults(profile: dict) -> ScheduleDefaults:
    scheme = profile.get("billingScheme")
    scheme = scheme if isinstance(scheme, dict) else {}
    annualization = profile.get("annualization")
    annualization = annualization if isinstance(annualization, dict) else {}
    schedule = profile.get("attendanceSchedule")
    schedule = schedule if isinstance(schedule, dict) else {}

    return ScheduleDefaults(
        billing_profile_id=profile.get("billingProfileId"),
        attendance_schedule_id=schedule.get("id"),
        weeks_of_care=annualization.get("weeksOfCare"),
        billing_id=scheme.get("id"),
        billing_title=scheme.get("title"),
        billing_invoices=annualization.get("invoices"),
    )


def fetch_billing_defaults(
    institution_id: str, client: GraphQLClient | None = None
) -> dict[str, ScheduleDefaults]:
    """One institution's billing profiles, bucketed by attendanceSchedule.fullYear.

    Returns:
        {"all_year_round": ScheduleDefaults, "term_only": ScheduleDefaults} --
        ALWAYS both keys when this returns at all (see Raises otherwise).

    Raises:
        InstitutionDefaultsAnomaly: a deleted-filtered bucket has zero or more
            than one profile, a non-deleted profile is missing its
            attendanceSchedule entirely (nothing to bucket it by), or the two
            buckets' billing schemes disagree with each other -- all hard,
            reportable data problems, never guessed around.
        GraphQLError / GraphQLHTTPError: propagated deliberately.
    """
    client = client or GraphQLClient()

    body = client.execute(
        query_path=BILLING_PROFILES_QUERY_PATH,
        variables={"institutionSetId": institution_id},
        operation_name=BILLING_PROFILES_OPERATION_NAME,
    )

    data = body.get("data") if isinstance(body, dict) else None
    finance = (data or {}).get("finance") if isinstance(data, dict) else None
    billing_profiles = finance.get("billingProfiles") if isinstance(finance, dict) else None
    profiles = billing_profiles.get("list") if isinstance(billing_profiles, dict) else None
    if not isinstance(profiles, list):
        profiles = []

    buckets: dict[str, list[dict]] = {SCHEDULE_ALL_YEAR_ROUND: [], SCHEDULE_TERM_ONLY: []}
    unbucketable = 0

    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("isDeleted"):
            continue

        schedule = profile.get("attendanceSchedule")
        if not isinstance(schedule, dict) or schedule.get("fullYear") is None:
            unbucketable += 1
            continue

        key = SCHEDULE_ALL_YEAR_ROUND if schedule.get("fullYear") else SCHEDULE_TERM_ONLY
        buckets[key].append(profile)

    if unbucketable:
        raise InstitutionDefaultsAnomaly(
            f"{unbucketable} non-deleted billing profile(s) have no "
            f"attendanceSchedule to bucket them by"
        )

    result: dict[str, ScheduleDefaults] = {}
    for key, entries in buckets.items():
        if len(entries) != 1:
            raise InstitutionDefaultsAnomaly(
                f"expected exactly one {key!r} billing profile, found {len(entries)}"
            )
        result[key] = _parse_schedule_defaults(entries[0])

    if not _billing_schemes_match(
        result[SCHEDULE_ALL_YEAR_ROUND], result[SCHEDULE_TERM_ONLY]
    ):
        raise InstitutionDefaultsAnomaly(
            "the all_year_round and term_only billing profiles have DIFFERENT "
            f"billing schemes ("
            f"{result[SCHEDULE_ALL_YEAR_ROUND].billing_id!r}/"
            f"{result[SCHEDULE_ALL_YEAR_ROUND].billing_title!r} vs "
            f"{result[SCHEDULE_TERM_ONLY].billing_id!r}/"
            f"{result[SCHEDULE_TERM_ONLY].billing_title!r}) -- this is a "
            f"structural anomaly, not a cosmetic one"
        )

    return result


def _read_existing_catalogue(path: Path) -> dict:
    """The current institution_defaults.json, or {} when there is none yet.

    A file that EXISTS but is not valid JSON is a hard failure here (raises),
    not treated as empty -- same reasoning as every other pull here.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"pull_institution_defaults: {path} exists but could not be read as "
            f"JSON ({exc}); refusing to overwrite it -- fix or remove it by hand "
            f"first"
        ) from exc

    return raw if isinstance(raw, dict) else {}


def pull_all(
    client: GraphQLClient | None = None, institutions: list[str] | None = None
) -> PullResult:
    """Pull ruleGroupId + billing defaults for every institution in
    catalogue.json's sites.

    Nothing is written to disk here -- see `write_catalogue`. An institution
    only ever ends up FULLY pulled or FULLY failed -- see the module docstring.

    Args:
        client: optional GraphQLClient, mainly for tests.
        institutions: optional site codes restricting the pull, same
            case-insensitive matching and UnknownInstitutionError-before-any-
            call behaviour as every other pull.

    Returns:
        A PullResult ready to hand to `write_catalogue`.

    Raises:
        UnknownInstitutionError: `institutions` named a code catalogue.json's
            sites do not have.
    """
    client = client or GraphQLClient()

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

    existing_raw = _read_existing_catalogue(institution_defaults.catalogue_path())
    existing_institutions = existing_raw.get("institutions")
    if not isinstance(existing_institutions, dict):
        existing_institutions = {}
    merged_institutions = json.loads(json.dumps(existing_institutions))

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
        # Nothing with an institutionId to ask for at all.
        return PullResult(
            catalogue={"institutions": merged_institutions},
            comment=existing_raw.get("_comment"),
            institutions_skipped=skipped,
        )

    try:
        rule_group_by_id, rule_group_failures = fetch_rule_groups(
            list(id_to_code), client
        )
    except (GraphQLError, GraphQLHTTPError) as exc:
        # The one batched call failed: every institution that would have been
        # asked about fails, nothing merged, existing entries left untouched.
        failed = {code: str(exc) for code in id_to_code.values()}
        return PullResult(
            catalogue={"institutions": merged_institutions},
            comment=existing_raw.get("_comment"),
            institutions_skipped=skipped,
            institutions_failed=failed,
        )

    pulled: list[str] = []
    failed = {}

    for institution_id, code in id_to_code.items():
        if institution_id in rule_group_failures:
            failed[code] = rule_group_failures[institution_id]
            continue

        try:
            schedules = fetch_billing_defaults(institution_id, client)
        except (GraphQLError, GraphQLHTTPError, InstitutionDefaultsAnomaly) as exc:
            failed[code] = str(exc)
            continue

        entry = InstitutionDefaultsEntry(
            rule_group_id=rule_group_by_id[institution_id], schedules=schedules
        )
        merged_institutions[code] = entry.to_json()
        pulled.append(code)

    return PullResult(
        catalogue={"institutions": merged_institutions},
        comment=existing_raw.get("_comment"),
        institutions_pulled=pulled,
        institutions_skipped=skipped,
        institutions_failed=failed,
    )


def backup_path(path: Path) -> Path:
    """A timestamped sibling path, e.g. institution_defaults.2026-09-30T120000Z.bak.json."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")


def write_catalogue(result: PullResult, path: Path | None = None) -> Path | None:
    """Back up the existing file, then write the merged catalogue.

    The backup happens BEFORE any write, unconditionally, whenever the file
    already exists.

    Returns:
        The backup path, or None when there was no existing file to back up.
    """
    path = path or institution_defaults.catalogue_path()
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
    """A CLI-friendly summary: which institutions got a complete, fresh entry."""
    return {
        "dryRun": dry_run,
        "institutionsPulled": result.institutions_pulled,
        "institutionsSkipped": result.institutions_skipped,
        "institutionsFailed": result.institutions_failed,
        "backupFile": str(backup) if backup else None,
    }
