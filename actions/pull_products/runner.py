"""STUBBED pull for `reference/products_catalogue.json` -- awaiting a
confirmed products-listing query.

STATUS: NOT YET IMPLEMENTED. Checked against https://docs.famly.co on
2026-09-29: Famly's public GraphQL API has no products or billing-profile
root query at all -- a product only ever appears as an id nested inside an
invoice line item, never as a queryable, listable entity. The user has said
they will supply the real query shape separately.

Everything AROUND the actual fetch is built and tested exactly like
`actions/pull_sessions` and `actions/pull_groups` -- per-institution loop,
backup-before-write, merge-not-replace, `--institution` filter, dry-run,
per-institution failure isolation, unmatched-not-guessed reporting -- so that
once a real query is confirmed, only `_fetch_product_page` and
`query.graphql` need filling in. `fetch_institution_products`'s pagination
loop (follow a cursor until it is empty/null) is complete and covered by
tests/test_pull_products.py against a fake page-fetcher; only the real
GraphQL call and response parsing are missing.

Running this action today is safe but useless: `pull_all` attempts every
institution, each one fails with a `ProductsQueryNotImplemented` (a
`NotImplementedError`) from `_fetch_product_page`, and every failure is
recorded under `institutions_failed` -- exactly the same per-institution
isolation pull_sessions already gives a real, transient Famly failure. Nothing
is silently guessed or written.

TO FINISH THIS ACTION:
    1. Confirm the real query name/shape (the user has this) -- do not guess.
    2. Replace query.graphql with the real query, keeping a cursor-style
       argument if the API paginates.
    3. Replace `_fetch_product_page`'s body (currently `raise
       ProductsQueryNotImplemented`) with the real `client.execute` call and
       response parsing, returning `(rows, next_cursor)` like
       `pull_sessions.runner._fetch_page`.
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
OPERATION_NAME = "Products"

DEFAULT_CATALOGUE_PATH = (
    Path(__file__).resolve().parents[2] / "reference" / "products_catalogue.json"
)


def catalogue_path() -> Path:
    """The products catalogue file in use."""
    override = os.environ.get("PRODUCTS_CATALOGUE_FILE", "").strip()
    return Path(override) if override else DEFAULT_CATALOGUE_PATH


class ProductsQueryNotImplemented(NotImplementedError):
    """No confirmed products-listing query exists yet -- see the module docstring."""


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


def _normalise_institution_code(code: str) -> str:
    return (code or "").strip().upper()


def _fetch_product_page(
    client: GraphQLClient, institution_id: str, cursor: str | None
) -> tuple[list[ProductRow], str | None]:
    """One page of one institution's products.

    NOT YET IMPLEMENTED -- see the module docstring. Deliberately raises
    rather than returning an empty page: a run of this action must fail
    loudly and immediately, never silently produce an empty catalogue that
    looks like "this institution just has no products".
    """
    raise ProductsQueryNotImplemented(
        "pull_products: no confirmed products-listing query yet -- fill in "
        "actions/pull_products/query.graphql and _fetch_product_page in "
        "runner.py once the real API shape is confirmed (see the module "
        "docstring)."
    )


def fetch_institution_products(
    institution_id: str, client: GraphQLClient
) -> list[ProductRow]:
    """Fetch ALL pages of one institution's products, following a cursor
    until it comes back empty/null -- the same shape as
    `pull_sessions.runner.fetch_institution_sessions`.

    This loop itself is complete and tested; only `_fetch_product_page` needs
    a real implementation (see the module docstring).
    """
    rows: list[ProductRow] = []
    cursor: str | None = None

    while True:
        page, cursor = _fetch_product_page(client, institution_id, cursor)
        rows.extend(page)
        if not cursor:
            break

    return rows


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
    client: GraphQLClient | None = None, institutions: list[str] | None = None
) -> PullResult:
    """Pull products for every institution in catalogue.json's sites.

    Currently every institution ends up in `institutions_failed` via
    `ProductsQueryNotImplemented` -- see the module docstring. The mechanics
    (institution loop, --institution filter, merge, skip, isolation) are
    otherwise identical to `pull_sessions.runner.pull_all`.

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

    for code, entry in sites.items():
        institution_id = entry.get("institutionId") if isinstance(entry, dict) else None
        if not institution_id:
            skipped[code] = "no institutionId configured in catalogue.json"
            continue

        try:
            products = fetch_institution_products(institution_id, client)
        except (GraphQLError, GraphQLHTTPError, NotImplementedError) as exc:
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
    """A timestamped sibling path, e.g. products_catalogue.2026-09-29T120000Z.bak.json."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")


def write_catalogue(result: PullResult, path: Path | None = None) -> Path | None:
    """Back up the existing file, then write the merged catalogue."""
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
        "backupFile": str(backup) if backup else None,
    }
