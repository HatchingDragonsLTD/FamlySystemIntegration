"""Pull Famly's live product ids for every institution into
`reference/products_catalogue.json` (the flat id -> title display map, used
only for Slack summaries), AND resolve each institution's two fixed add-on
product ids into `reference/institution_defaults.json` as `addonProducts`.

REST ENDPOINT CONFIRMED LIVE (2026-09-30), replacing an earlier GraphQL
workaround (`finance.overview(...).filtersContext.products`): the FIRST build
of this action never actually tried the REST API -- it searched GraphQL
introspection and docs.famly.co, found nothing named "products", and built
against `filtersContext.products` instead, without trying
`GET v2/products` even though `pull_sessions` sits right next to it using the
equivalent `GET v2/sessions`. That was an oversight, not a rejected option:
once tried, `v2/products` works exactly like `v2/sessions` --

    GET {rest_base}/v2/products?institutionId=<id>&validOn=<today>
        &includeDiscontinued=false&behaviors=1
        &includePricesForAllPricingGroups=true
    -> 200 {"products": [{"id", "title", ...}, ...], "behaviors": [...]}

-- confirmed for all three configured institutions. `includeDiscontinued=false`
matches `pull_sessions`' deliberate choice, for the same reason: a discontinued
product must never be matched into `addonProducts` for a new booking. (None of
the three institutions currently has a discontinued product, so this made no
observable difference in testing -- it is a forward-looking safeguard, same as
`pull_sessions`' identical choice.)

TWO SEPARATE CONCERNS, not to be confused:

  1. `pull_all` / `write_catalogue` -- the flat id -> title map for EVERY
     product Famly returns, unconditionally (informational only, same as
     before). Untouched by concern 2 below; a title-matching failure there
     never blocks this.

  2. `resolve_addon_products` / `write_addon_products` -- for each
     institution, find the ONE product whose title is EXACTLY "Meals &
     Snacks" -> mealsProductId, and the ONE whose title is EXACTLY
     "Educational Activities & Extras" -> activitiesProductId. Case-sensitive,
     no normalization: these are fixed, known titles, and normalizing could
     mask a real naming drift -- which happened for real during this
     feature's build-out (titles were briefly inconsistent across
     institutions while being corrected by hand in Famly). Zero or 2+ matches
     for either title is a hard, reportable anomaly for that institution;
     nothing is guessed or written for it. Written into
     `reference/institution_defaults.json` under `addonProducts`, NOT into
     products_catalogue.json.

SHARED FILE, SEPARATE OWNERSHIP: `institution_defaults.json` is also written
by `actions/pull_institution_defaults` (ruleGroupId + schedules). Each pull
merges only ITS OWN top-level key(s) into an institution's existing entry,
preserving whatever the other pull already wrote there -- see
`write_addon_products` here and `pull_institution_defaults.runner.pull_all`'s
matching merge. Neither pull replaces the other's data.

Institutions come from reference/catalogue.json's `sites` section, same as
every other pull. Same conventions otherwise: --institution filter, --dry-run,
timestamped backup-before-write, per-institution failure isolation.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from core.rest_client import RestClient, RestHTTPError
from integrations import catalogue, institution_defaults
from integrations.catalogue import UnknownInstitutionError  # re-exported; see there

logger = logging.getLogger(__name__)

PRODUCTS_PATH = "v2/products"

DEFAULT_CATALOGUE_PATH = (
    Path(__file__).resolve().parents[2] / "reference" / "products_catalogue.json"
)

# The two fixed, known titles this pull resolves into addonProducts. EXACT,
# case-sensitive match only -- see the module docstring for why normalizing
# these would be actively wrong.
MEALS_TITLE = "Meals & Snacks"
ACTIVITIES_TITLE = "Educational Activities & Extras"


def catalogue_path() -> Path:
    """The products catalogue file in use."""
    override = os.environ.get("PRODUCTS_CATALOGUE_FILE", "").strip()
    return Path(override) if override else DEFAULT_CATALOGUE_PATH


class AddonProductAnomaly(RuntimeError):
    """A hard, reportable data problem resolving one institution's addon
    products -- either fixed title matched zero or 2+ products. Never guessed
    around: see the module docstring.
    """


@dataclass
class ProductRow:
    id: str | None = None
    title: str | None = None


@dataclass
class UnmatchedProduct:
    """A product returned by Famly but missing an id or a title."""

    institution: str
    product_id: str | None
    title: str
    reason: str


@dataclass
class PullResult:
    catalogue: dict
    comment: Any = None
    written_counts: dict[str, int] = field(default_factory=dict)
    unmatched: list[UnmatchedProduct] = field(default_factory=list)
    institutions_pulled: list[str] = field(default_factory=list)
    institutions_skipped: dict[str, str] = field(default_factory=dict)
    institutions_failed: dict[str, str] = field(default_factory=dict)
    # code -> {"mealsProductId", "activitiesProductId"}, for institutions
    # whose product list resolved both fixed titles cleanly.
    addon_products: dict[str, dict] = field(default_factory=dict)
    # code -> reason, for institutions where the flat product list was
    # fetched fine but the addon-title resolution failed (see
    # AddonProductAnomaly). Independent of institutions_failed: a fetch
    # failure lands there instead, and skips addon resolution entirely.
    addon_products_failed: dict[str, str] = field(default_factory=dict)


def _normalise_institution_code(code: str) -> str:
    return (code or "").strip().upper()


def _parse_product(node: Any) -> ProductRow | None:
    if not isinstance(node, dict):
        return None
    return ProductRow(id=node.get("id"), title=node.get("title"))


def _parse_products_response(body: Any) -> list[dict]:
    """The response's product list, defensively.

    Confirmed live to be `{"products": [...], "behaviors": [...]}`, same
    envelope shape as `pull_sessions`' `v2/sessions` response -- this also
    accepts a bare list or a couple of other plausible keys, matching that
    module's defensive style, so an unexpected but non-error shape degrades
    to zero products pulled rather than raising.
    """
    if isinstance(body, list):
        candidates: Any = body
    elif isinstance(body, dict):
        candidates = body.get("products") or body.get("data") or body.get("results") or []
    else:
        candidates = []

    if not isinstance(candidates, list):
        return []
    return [p for p in candidates if isinstance(p, dict)]


def fetch_institution_products(
    institution_id: str, client: RestClient | None = None, *, today: str | None = None
) -> list[ProductRow]:
    """Every non-discontinued product configured for one institution, in ONE
    call -- mirrors `pull_sessions.fetch_institution_sessions`.

    `includeDiscontinued=false`: matches `pull_sessions`' deliberate choice --
    a discontinued product must never be matched into `addonProducts` for a
    new booking. No pagination on this endpoint.

    Raises:
        RestHTTPError: propagated deliberately, uncaught.
    """
    client = client or RestClient()

    params = {
        "institutionId": institution_id,
        "validOn": today or date.today().isoformat(),
        "includeDiscontinued": "false",
        "behaviors": 1,
        "includePricesForAllPricingGroups": "true",
    }
    body = client.get(PRODUCTS_PATH, params=params)
    products = _parse_products_response(body)

    return [row for row in (_parse_product(node) for node in products) if row is not None]


def resolve_addon_products(products: list[ProductRow]) -> dict:
    """Find the two fixed add-on products by EXACT title match.

    Args:
        products: one institution's full product list (as fetched by
            `fetch_institution_products`).

    Returns:
        {"mealsProductId": ..., "activitiesProductId": ...}.

    Raises:
        AddonProductAnomaly: either title matched zero or 2+ products --
            named explicitly, including every duplicate id when there is
            more than one match. Never guessed at: see the module docstring.
    """

    def _match(title: str) -> str:
        matches = [p for p in products if p.id and p.title == title]
        if not matches:
            raise AddonProductAnomaly(f"no product titled {title!r} was found")
        if len(matches) > 1:
            ids = ", ".join(p.id for p in matches)
            raise AddonProductAnomaly(
                f"{len(matches)} products are titled {title!r} (ids: {ids}) -- "
                f"a duplicate title, not guessed at"
            )
        return matches[0].id

    return {
        "mealsProductId": _match(MEALS_TITLE),
        "activitiesProductId": _match(ACTIVITIES_TITLE),
    }


def _read_existing_catalogue(path: Path) -> dict:
    """The current products_catalogue.json, or {} when there is none yet."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"pull_products: {path} exists but could not be read as JSON "
            f"({exc}); refusing to overwrite it -- fix or remove it by hand "
            f"first"
        ) from exc

    return raw if isinstance(raw, dict) else {}


def pull_all(
    client: RestClient | None = None, institutions: list[str] | None = None
) -> PullResult:
    """Pull products for every institution in catalogue.json's sites.

    For each successfully fetched institution, this ALSO attempts addon-
    product resolution (see `resolve_addon_products`); that is an independent
    outcome from the flat catalogue write -- a title-matching anomaly lands
    in `addon_products_failed` and never blocks or rolls back the flat
    catalogue entry, which is written exactly as before.

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
        sites = {code: sites[code] for code in dict.fromkeys(requested)}

    existing_raw = _read_existing_catalogue(catalogue_path())
    existing_institutions = existing_raw.get("institutions")
    if not isinstance(existing_institutions, dict):
        existing_institutions = {}
    merged_institutions = json.loads(json.dumps(existing_institutions))

    written_counts: dict[str, int] = {}
    unmatched: list[UnmatchedProduct] = []
    pulled: list[str] = []
    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}
    addon_products: dict[str, dict] = {}
    addon_products_failed: dict[str, str] = {}

    for code, entry in sites.items():
        institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if not institution_id:
            skipped[code] = "no institutionId configured in catalogue.json"
            continue

        try:
            products = fetch_institution_products(institution_id, client)
        except RestHTTPError as exc:
            failed[code] = str(exc)
            continue

        new_products: dict[str, str] = {}
        for product in products:
            if not product.id or not product.title:
                unmatched.append(
                    UnmatchedProduct(
                        institution=code,
                        product_id=product.id,
                        title=product.title or "untitled",
                        reason="missing id or title",
                    )
                )
                continue
            new_products[product.id] = product.title

        existing_entry = merged_institutions.get(code)
        merged_products = (
            dict(existing_entry.get("products", {}))
            if isinstance(existing_entry, dict)
            else {}
        )
        merged_products.update(new_products)
        merged_institutions[code] = {"products": merged_products}
        written_counts[code] = len(new_products)
        pulled.append(code)

        try:
            addon_products[code] = resolve_addon_products(products)
        except AddonProductAnomaly as exc:
            addon_products_failed[code] = str(exc)

    return PullResult(
        catalogue={"institutions": merged_institutions},
        comment=existing_raw.get("_comment"),
        written_counts=written_counts,
        unmatched=unmatched,
        institutions_pulled=pulled,
        institutions_skipped=skipped,
        institutions_failed=failed,
        addon_products=addon_products,
        addon_products_failed=addon_products_failed,
    )


def backup_path(path: Path) -> Path:
    """A timestamped sibling path, e.g. products_catalogue.2026-09-30T120000Z.bak.json."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")


def write_catalogue(result: PullResult, path: Path | None = None) -> Path | None:
    """Back up the existing file, then write the merged FLAT catalogue.

    This is the id -> title display map only -- see `write_addon_products`
    for the separate institution_defaults.json write.
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


def _read_existing_institution_defaults(path: Path) -> dict:
    """The current institution_defaults.json, or {} when there is none yet."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"pull_products: {path} exists but could not be read as JSON "
            f"({exc}); refusing to overwrite it -- fix or remove it by hand "
            f"first"
        ) from exc

    return raw if isinstance(raw, dict) else {}


def write_addon_products(result: PullResult, path: Path | None = None) -> Path | None:
    """Merge resolved addonProducts into institution_defaults.json.

    Backs up the existing file first, same convention as every other pull.
    Only the `addonProducts` key of each successfully-resolved institution is
    touched -- any `ruleGroupId`/`schedules` already there (written by
    `pull_institution_defaults`) is preserved untouched, and so is every
    OTHER institution's entire entry, whether or not it was in this run.

    Returns:
        The backup path, or None when there was no existing file to back up.
    """
    path = path or institution_defaults.catalogue_path()
    backup = None

    if path.exists():
        backup = backup_path(path)
        shutil.copy2(path, backup)

    existing_raw = _read_existing_institution_defaults(path)
    existing_institutions = existing_raw.get("institutions")
    existing_institutions = existing_institutions if isinstance(existing_institutions, dict) else {}
    merged_institutions = json.loads(json.dumps(existing_institutions))

    for code, addon in result.addon_products.items():
        existing_entry = merged_institutions.get(code)
        entry = dict(existing_entry) if isinstance(existing_entry, dict) else {}
        entry["addonProducts"] = addon
        merged_institutions[code] = entry

    payload: dict = {}
    if existing_raw.get("_comment") is not None:
        payload["_comment"] = existing_raw["_comment"]
    payload["institutions"] = merged_institutions

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return backup


def summary_payload(
    result: PullResult, *, backup: Path | None = None, dry_run: bool = False
) -> dict:
    """A CLI-friendly summary: counts per institution, and every unmatched product."""
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
                "productId": u.product_id,
                "title": u.title,
                "reason": u.reason,
            }
            for u in result.unmatched
        ],
        "addonProductsResolved": sorted(result.addon_products),
        "addonProductsFailed": result.addon_products_failed,
        "backupFile": str(backup) if backup else None,
    }
