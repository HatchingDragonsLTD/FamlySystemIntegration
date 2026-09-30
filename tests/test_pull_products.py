"""Tests for the pull-products maintenance command.

Run from the project root:

    python -m unittest discover -s tests -v

No real Famly, no network: `RestClient` is stubbed throughout, mirroring
test_pull_sessions.py's FakeRest (this pull hits `GET v2/products`, the same
REST API `pull_sessions` hits at `GET v2/sessions`). `FAMLY_CATALOGUE_FILE` /
`PRODUCTS_CATALOGUE_FILE` / `INSTITUTION_DEFAULTS_FILE` all point at temporary
files for every test, so the real reference files are never read or written.

This pull writes TWO files (see runner.py's module docstring): the flat
products_catalogue.json display map (unconditional), and
institution_defaults.json's `addonProducts` (gated on an exact, case-sensitive
title match against "Meals & Snacks" / "Educational Activities & Extras" --
zero or 2+ matches for either is a hard, reportable anomaly, independent of
the flat catalogue write).
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.pull_products import runner as pull_products
from core.rest_client import RestHTTPError

logging.disable(logging.CRITICAL)

CITY_ID = "11111111-1111-1111-1111-111111111111"
CW_ID = "22222222-2222-2222-2222-222222222222"

CATALOGUE = {
    "sites": {
        "HDCITY": {"label": "City", "institutionId": CITY_ID},
        "HDCW": {"label": "Canada Water", "institutionId": CW_ID},
    }
}


def product_node(product_id, title):
    """A raw REST response item -- for building FakeRest responses."""
    return {"id": product_id, "title": title}


def row(product_id, title) -> pull_products.ProductRow:
    """A parsed ProductRow -- for calling resolve_addon_products directly."""
    return pull_products.ProductRow(id=product_id, title=title)


def products_body(products: list) -> dict:
    return {"products": products, "behaviors": []}


def clean_product_nodes():
    """Raw REST items for a cleanly-resolvable institution: both fixed
    titles once each, plus their funded ("(F) ...") counterparts.
    """
    return [
        product_node("p-meals", pull_products.MEALS_TITLE),
        product_node("p-activities", pull_products.ACTIVITIES_TITLE),
        product_node("p-meals-f", f"(F) {pull_products.MEALS_TITLE}"),
        product_node("p-activities-f", f"(F) {pull_products.ACTIVITIES_TITLE}"),
    ]


def clean_products():
    """The same set as `clean_product_nodes`, but as parsed ProductRow
    objects -- for calling resolve_addon_products directly.
    """
    return [row(n["id"], n["title"]) for n in clean_product_nodes()]


class FakeRest:
    """Serves a canned body per institutionId (or raises). Records every
    call's params. Mirrors test_pull_sessions.py's FakeRest.
    """

    def __init__(self, bodies: dict | None = None, errors: dict | None = None):
        self._bodies = bodies or {}
        self._errors = errors or {}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(dict(params or {}))
        institution_id = (params or {}).get("institutionId")
        if institution_id in self._errors:
            raise self._errors[institution_id]
        return self._bodies.get(institution_id, products_body([]))


class PullProductsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.catalogue_file = self.tmp / "catalogue.json"
        self.catalogue_file.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        self.products_catalogue_file = self.tmp / "products_catalogue.json"
        self.institution_defaults_file = self.tmp / "institution_defaults.json"

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(self.catalogue_file),
                "PRODUCTS_CATALOGUE_FILE": str(self.products_catalogue_file),
                "INSTITUTION_DEFAULTS_FILE": str(self.institution_defaults_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def write_existing_products_catalogue(self, data: dict):
        self.products_catalogue_file.write_text(json.dumps(data), encoding="utf-8")

    def write_existing_institution_defaults(self, data: dict):
        self.institution_defaults_file.write_text(json.dumps(data), encoding="utf-8")

    def clean_client(self, extra: dict | None = None):
        bodies = {
            CITY_ID: products_body(clean_product_nodes()),
            CW_ID: products_body(clean_product_nodes()),
        }
        if extra:
            bodies.update(extra)
        return FakeRest(bodies=bodies)


class FetchInstitutionProductsTests(PullProductsTestCase):
    def test_the_query_carries_the_requested_institution_id(self):
        client = FakeRest(bodies={CITY_ID: products_body(clean_product_nodes())})
        pull_products.fetch_institution_products(CITY_ID, client=client)

        self.assertEqual(client.calls[0]["institutionId"], CITY_ID)

    def test_includes_discontinued_is_false(self):
        # Matches pull_sessions' deliberate choice: a discontinued product
        # must never be matched into addonProducts for a new booking.
        client = FakeRest(bodies={CITY_ID: products_body(clean_product_nodes())})
        pull_products.fetch_institution_products(CITY_ID, client=client)

        self.assertEqual(client.calls[0]["includeDiscontinued"], "false")

    def test_one_call_returns_the_full_list_no_pagination(self):
        client = FakeRest(bodies={CITY_ID: products_body(clean_product_nodes())})
        rows = pull_products.fetch_institution_products(CITY_ID, client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(rows), 4)

    def test_a_rest_error_is_surfaced_not_swallowed(self):
        error = RestHTTPError("boom", 502, "boom")
        client = FakeRest(errors={CITY_ID: error})

        with self.assertRaises(RestHTTPError):
            pull_products.fetch_institution_products(CITY_ID, client=client)


class ResolveAddonProductsTests(unittest.TestCase):
    def test_both_fixed_titles_resolve_to_their_ids(self):
        result = pull_products.resolve_addon_products(clean_products())

        self.assertEqual(result, {"mealsProductId": "p-meals", "activitiesProductId": "p-activities"})

    def test_a_missing_meals_title_is_a_hard_failure_naming_it(self):
        products = [p for p in clean_products() if p.title != pull_products.MEALS_TITLE]

        with self.assertRaises(pull_products.AddonProductAnomaly) as ctx:
            pull_products.resolve_addon_products(products)

        self.assertIn(pull_products.MEALS_TITLE, str(ctx.exception))
        self.assertIn("no product titled", str(ctx.exception))

    def test_a_missing_activities_title_is_a_hard_failure_naming_it(self):
        products = [p for p in clean_products() if p.title != pull_products.ACTIVITIES_TITLE]

        with self.assertRaises(pull_products.AddonProductAnomaly) as ctx:
            pull_products.resolve_addon_products(products)

        self.assertIn(pull_products.ACTIVITIES_TITLE, str(ctx.exception))

    def test_a_duplicate_title_is_a_hard_failure_naming_the_duplicate_ids(self):
        products = clean_products() + [
            row("p-meals-dup", pull_products.MEALS_TITLE)
        ]

        with self.assertRaises(pull_products.AddonProductAnomaly) as ctx:
            pull_products.resolve_addon_products(products)

        message = str(ctx.exception)
        self.assertIn(pull_products.MEALS_TITLE, message)
        self.assertIn("p-meals", message)
        self.assertIn("p-meals-dup", message)

    def test_matching_is_exact_and_case_sensitive_no_normalization(self):
        # Different case and a trailing space must NOT match -- the whole
        # point is to catch drift, not paper over it.
        products = [
            row("p-1", pull_products.MEALS_TITLE.lower()),
            row("p-2", pull_products.ACTIVITIES_TITLE + " "),
        ]

        with self.assertRaises(pull_products.AddonProductAnomaly):
            pull_products.resolve_addon_products(products)

    def test_a_funded_prefixed_title_does_not_collide_with_the_non_funded_one(self):
        # "(F) Meals & Snacks" != "Meals & Snacks": exact match only.
        result = pull_products.resolve_addon_products(clean_products())
        self.assertNotEqual(result["mealsProductId"], "p-meals-f")


class PullAllFlatCatalogueTests(PullProductsTestCase):
    """The existing, unconditional id -> title map -- unaffected by addon
    resolution succeeding or failing.
    """

    def test_matched_products_are_written_under_the_right_institution(self):
        result = pull_products.pull_all(client=self.clean_client())

        city = result.catalogue["institutions"]["HDCITY"]["products"]
        self.assertEqual(len(city), 4)
        self.assertEqual(city["p-meals"], pull_products.MEALS_TITLE)

    def test_a_row_missing_id_or_title_is_unmatched_not_written(self):
        client = self.clean_client(
            {CITY_ID: products_body(clean_product_nodes() + [product_node(None, "No id")])}
        )
        result = pull_products.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(len(result.unmatched), 1)
        self.assertEqual(result.unmatched[0].reason, "missing id or title")

    def test_a_fetch_failure_reports_nothing_guessed(self):
        client = FakeRest(errors={CITY_ID: RestHTTPError("down", 502, "nope")})
        result = pull_products.pull_all(client=client, institutions=["HDCITY"])

        self.assertIn("HDCITY", result.institutions_failed)
        self.assertNotIn("HDCITY", result.catalogue["institutions"])
        self.assertNotIn("HDCITY", result.addon_products)


class PullAllAddonResolutionTests(PullProductsTestCase):
    def test_a_clean_institution_resolves_addon_products(self):
        result = pull_products.pull_all(client=self.clean_client())

        self.assertEqual(
            result.addon_products["HDCITY"],
            {"mealsProductId": "p-meals", "activitiesProductId": "p-activities"},
        )
        self.assertEqual(result.addon_products_failed, {})

    def test_an_anomalous_institution_fails_addon_resolution_only(self):
        broken = [n for n in clean_product_nodes() if n["title"] != pull_products.MEALS_TITLE]
        client = self.clean_client({CW_ID: products_body(broken)})

        result = pull_products.pull_all(client=client)

        # HDCW's flat catalogue still gets written...
        self.assertIn("HDCW", result.catalogue["institutions"])
        self.assertEqual(result.written_counts["HDCW"], 3)
        # ...but its addon resolution failed, naming what was missing.
        self.assertIn("HDCW", result.addon_products_failed)
        self.assertIn(pull_products.MEALS_TITLE, result.addon_products_failed["HDCW"])
        self.assertNotIn("HDCW", result.addon_products)
        # HDCITY is unaffected.
        self.assertEqual(
            result.addon_products["HDCITY"],
            {"mealsProductId": "p-meals", "activitiesProductId": "p-activities"},
        )


class InstitutionFilterTests(PullProductsTestCase):
    def test_an_unknown_institution_code_raises_with_zero_attempts(self):
        client = self.clean_client()

        with self.assertRaises(pull_products.UnknownInstitutionError) as ctx:
            pull_products.pull_all(client=client, institutions=["NOPE"])

        self.assertIn("NOPE", str(ctx.exception))
        self.assertEqual(client.calls, [])

    def test_filtering_restricts_which_institutions_are_attempted(self):
        client = self.clean_client()
        result = pull_products.pull_all(client=client, institutions=["HDCITY"])

        self.assertEqual(result.institutions_pulled, ["HDCITY"])
        self.assertNotIn("HDCW", result.addon_products)


class NoInstitutionIdConfiguredTests(PullProductsTestCase):
    def test_a_site_with_no_institution_id_is_skipped_not_attempted(self):
        self.catalogue_file.write_text(
            json.dumps({"sites": {"HDNONE": {"label": "No institution yet"}}}),
            encoding="utf-8",
        )
        client = FakeRest()

        result = pull_products.pull_all(client=client)

        self.assertIn("HDNONE", result.institutions_skipped)
        self.assertEqual(client.calls, [])


class WriteCatalogueTests(PullProductsTestCase):
    def test_a_backup_is_created_before_the_existing_file_is_overwritten(self):
        original = {"institutions": {"HDCITY": {"products": {}}}}
        self.write_existing_products_catalogue(original)

        result = pull_products.pull_all(client=self.clean_client())
        backup = pull_products.write_catalogue(result, path=self.products_catalogue_file)

        self.assertIsNotNone(backup)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

    def test_no_backup_when_there_was_no_existing_file(self):
        result = pull_products.pull_all(client=self.clean_client())

        self.assertFalse(self.products_catalogue_file.exists())
        backup = pull_products.write_catalogue(result, path=self.products_catalogue_file)

        self.assertIsNone(backup)
        self.assertTrue(self.products_catalogue_file.exists())


class WriteAddonProductsTests(PullProductsTestCase):
    def test_addon_products_are_written_into_institution_defaults(self):
        result = pull_products.pull_all(client=self.clean_client())
        pull_products.write_addon_products(result, path=self.institution_defaults_file)

        written = json.loads(self.institution_defaults_file.read_text(encoding="utf-8"))
        self.assertEqual(
            written["institutions"]["HDCITY"]["addonProducts"],
            {"mealsProductId": "p-meals", "activitiesProductId": "p-activities"},
        )

    def test_existing_rule_group_and_schedules_survive_untouched(self):
        self.write_existing_institution_defaults(
            {
                "institutions": {
                    "HDCITY": {
                        "ruleGroupId": "rg-existing",
                        "schedules": {"all_year_round": {"billingProfileId": "bp-1"}},
                    }
                }
            }
        )

        result = pull_products.pull_all(client=self.clean_client())
        pull_products.write_addon_products(result, path=self.institution_defaults_file)

        written = json.loads(self.institution_defaults_file.read_text(encoding="utf-8"))
        entry = written["institutions"]["HDCITY"]
        self.assertEqual(entry["ruleGroupId"], "rg-existing")
        self.assertEqual(entry["schedules"]["all_year_round"]["billingProfileId"], "bp-1")
        self.assertEqual(entry["addonProducts"]["mealsProductId"], "p-meals")

    def test_an_anomalous_institution_leaves_its_existing_addon_products_untouched(self):
        self.write_existing_institution_defaults(
            {
                "institutions": {
                    "HDCW": {"addonProducts": {"mealsProductId": "old-meals"}},
                }
            }
        )
        broken = [n for n in clean_product_nodes() if n["title"] != pull_products.MEALS_TITLE]
        client = self.clean_client({CW_ID: products_body(broken)})

        result = pull_products.pull_all(client=client)
        pull_products.write_addon_products(result, path=self.institution_defaults_file)

        written = json.loads(self.institution_defaults_file.read_text(encoding="utf-8"))
        self.assertEqual(
            written["institutions"]["HDCW"]["addonProducts"]["mealsProductId"], "old-meals"
        )

    def test_other_institutions_are_left_completely_untouched(self):
        self.write_existing_institution_defaults(
            {"institutions": {"HDOTHER": {"ruleGroupId": "unrelated"}}}
        )

        result = pull_products.pull_all(client=self.clean_client(), institutions=["HDCITY"])
        pull_products.write_addon_products(result, path=self.institution_defaults_file)

        written = json.loads(self.institution_defaults_file.read_text(encoding="utf-8"))
        self.assertEqual(written["institutions"]["HDOTHER"], {"ruleGroupId": "unrelated"})

    def test_a_backup_is_created_before_the_existing_file_is_overwritten(self):
        original = {"institutions": {"HDCITY": {"ruleGroupId": "rg-1", "schedules": {}}}}
        self.write_existing_institution_defaults(original)

        result = pull_products.pull_all(client=self.clean_client())
        backup = pull_products.write_addon_products(result, path=self.institution_defaults_file)

        self.assertIsNotNone(backup)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

    def test_no_backup_when_there_was_no_existing_file(self):
        result = pull_products.pull_all(client=self.clean_client())

        self.assertFalse(self.institution_defaults_file.exists())
        backup = pull_products.write_addon_products(result, path=self.institution_defaults_file)

        self.assertIsNone(backup)
        self.assertTrue(self.institution_defaults_file.exists())


class MergePreservationTests(PullProductsTestCase):
    def test_a_filtered_run_never_disturbs_other_institutions_flat_catalogue(self):
        self.write_existing_products_catalogue(
            {"institutions": {"HDCW": {"products": {"p-old": "Old Snack"}}}}
        )

        result = pull_products.pull_all(client=self.clean_client(), institutions=["HDCITY"])

        self.assertEqual(
            result.catalogue["institutions"]["HDCW"]["products"], {"p-old": "Old Snack"}
        )


if __name__ == "__main__":
    unittest.main()
