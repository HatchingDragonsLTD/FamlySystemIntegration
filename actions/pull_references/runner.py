"""Runs the sessions + groups + products + institution-defaults reference-data
pulls together, for a single weekly cron entry.

Each pull -- `actions/pull_sessions`, `actions/pull_groups`,
`actions/pull_products`, `actions/pull_institution_defaults` -- is still
individually runnable via its own `pull-sessions` / `pull-groups` /
`pull-products` / `pull-institution-defaults` CLI command; this module just
sequences all four under one command (`pull-references`).

ISOLATION: one pull's total failure (an `UnknownInstitutionError` from a bad
`--institution` filter, a connection error, or -- for pull_products today --
its stubbed "not implemented" state) is recorded and does NOT stop the others
from running. This generalises the SAME principle each individual pull already
applies to its own institutions: one bad institution/source must never take
down the rest of the run.
"""

from dataclasses import dataclass, field
from typing import Any

from actions.pull_groups import runner as pull_groups
from actions.pull_institution_defaults import runner as pull_institution_defaults
from actions.pull_products import runner as pull_products
from actions.pull_sessions import runner as pull_sessions

# name -> that pull's runner module. Every module here exposes the same
# interface: pull_all(client=None, institutions=None) -> PullResult,
# write_catalogue(result, path=None) -> backup path or None, and
# summary_payload(result, *, backup=None, dry_run=False) -> dict. A pull with a
# SECONDARY output (session titles, addon products) also exposes
# write_extras(result), which is called right after write_catalogue.
PULLS = {
    "sessions": pull_sessions,
    "groups": pull_groups,
    "products": pull_products,
    "institutionDefaults": pull_institution_defaults,
}


@dataclass
class ReferencePullResult:
    # name -> that pull's own PullResult, for every pull that completed
    # (successfully or not -- pull_all() itself only raises for a filter
    # problem or a total connection failure, both captured under `errors`).
    results: dict[str, Any] = field(default_factory=dict)
    # name -> the backup path write_catalogue returned (None on a first-ever
    # run, or when dry_run=True and nothing was written).
    backups: dict[str, Any] = field(default_factory=dict)
    # name -> "this whole pull could not run at all" (as opposed to a
    # per-institution failure INSIDE a pull, which lives on that pull's own
    # PullResult and is unaffected by this).
    errors: dict[str, str] = field(default_factory=dict)


def pull_all_references(
    institutions: list[str] | None = None,
    *,
    dry_run: bool = False,
) -> ReferencePullResult:
    """Run every reference pull, isolating one's total failure from the rest.

    Args:
        institutions: passed through to every pull's own `pull_all` as-is.
        dry_run: when True, no pull writes its catalogue file or a backup --
            every pull still runs and reports what it would have done.

    Returns:
        A ReferencePullResult; see `summary_payload` for the CLI-facing shape.
    """
    outcome = ReferencePullResult()

    for name, module in PULLS.items():
        try:
            result = module.pull_all(institutions=institutions)
        except Exception as exc:  # noqa: BLE001 - one pull's failure must not stop the rest
            outcome.errors[name] = str(exc)
            continue

        outcome.results[name] = result

        backup = None
        if not dry_run:
            backup = module.write_catalogue(result)
            write_extras = getattr(module, "write_extras", None)
            if callable(write_extras):
                write_extras(result)
        outcome.backups[name] = backup

    return outcome


def summary_payload(outcome: ReferencePullResult, *, dry_run: bool = False) -> dict:
    """A CLI-friendly summary: one entry per pull, in `PULLS`' order."""
    summary: dict[str, Any] = {"dryRun": dry_run, "pulls": {}}

    for name, module in PULLS.items():
        if name in outcome.errors:
            summary["pulls"][name] = {"failed": outcome.errors[name]}
            continue

        result = outcome.results.get(name)
        if result is None:
            continue

        summary["pulls"][name] = module.summary_payload(
            result, backup=outcome.backups.get(name), dry_run=dry_run
        )

    return summary
