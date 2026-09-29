"""Tests for the STUBBED pull-products maintenance command.

Run from the project root:

    python -m unittest discover -s tests -v

See actions/pull_products/runner.py's module docstring for status: there is
no confirmed products-listing query yet, so `_fetch_product_page` always
raises. These tests pin two things: the pagination LOOP itself
(`fetch_institution_products`) is complete and correct against a fake
page-fetcher, and `pull_all`'s surrounding plumbing (per-institution
isolation, --institution filter, merge, dry-run, backup) behaves exactly like
pull_sessions/pull_groups even though every institution currently fails.
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions.pull_products import runner as pull_products

logging.disable(logging.CRITICAL)

CITY_ID = "11111111-1111-1111-1111-111111111111"
CW_ID = "22222222-2222-2222-2222-222222222222"

CATALOGUE = {
    "sites": {
        "HDCITY": {"label": "City", "institutionId": CITY_ID},
        "HDCW": {"label": "Canada Water", "institutionId": CW_ID},
    }
}


class PullProductsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.catalogue_file = self.tmp / "catalogue.json"
        self.catalogue_file.write_text(json.dumps(CATALOGUE), encoding="utf-8")

        self.products_catalogue_file = self.tmp / "products_catalogue.json"

        env = mock.patch.dict(
            "os.environ",
            {
                "FAMLY_CATALOGUE_FILE": str(self.catalogue_file),
                "PRODUCTS_CATALOGUE_FILE": str(self.products_catalogue_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def write_existing_products_catalogue(self, data: dict):
        self.products_catalogue_file.write_text(json.dumps(data), encoding="utf-8")


class NotYetImplementedTests(PullProductsTestCase):
    def test_fetch_product_page_itself_raises(self):
        with self.assertRaises(pull_products.ProductsQueryNotImplemented):
            pull_products._fetch_product_page(client=None, institution_id=CITY_ID, cursor=None)

    def test_fetch_institution_products_propagates_it(self):
        with self.assertRaises(pull_products.ProductsQueryNotImplemented):
            pull_products.fetch_institution_products(CITY_ID, client=None)

    def test_pull_all_reports_every_institution_as_failed_not_guessed(self):
        result = pull_products.pull_all(client=object())

        self.assertEqual(sorted(result.institutions_failed), ["HDCITY", "HDCW"])
        for message in result.institutions_failed.values():
            self.assertIn("no confirmed products-listing query yet", message)
            self.assertNotIn("Traceback", message)
        # Nothing written, nothing guessed.
        self.assertEqual(result.written_counts, {})
        self.assertEqual(result.institutions_pulled, [])
        self.assertEqual(result.catalogue["institutions"], {})


class PaginationLoopTests(PullProductsTestCase):
    """The loop mechanics, proven against a fake page-fetcher -- this is the
    part the user asked to have ready ahead of the real query.
    """

    def test_a_single_page_needs_only_one_call(self):
        calls = []

        def fake_page(client, institution_id, cursor):
            calls.append(cursor)
            return [pull_products.ProductRow(id="p-1", title="Snack")], None

        with mock.patch.object(pull_products, "_fetch_product_page", fake_page):
            rows = pull_products.fetch_institution_products(CITY_ID, client=None)

        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0])
        self.assertEqual([r.id for r in rows], ["p-1"])

    def test_pagination_across_multiple_pages_is_followed_correctly(self):
        pages = [
            ([pull_products.ProductRow(id="p-1", title="Snack")], "page-2"),
            ([pull_products.ProductRow(id="p-2", title="Lunch")], "page-3"),
            ([pull_products.ProductRow(id="p-3", title="Trip")], None),
        ]
        calls = []

        def fake_page(client, institution_id, cursor):
            calls.append(cursor)
            return pages.pop(0)

        with mock.patch.object(pull_products, "_fetch_product_page", fake_page):
            rows = pull_products.fetch_institution_products(CITY_ID, client=None)

        self.assertEqual(calls, [None, "page-2", "page-3"])
        self.assertEqual([r.id for r in rows], ["p-1", "p-2", "p-3"])

    def test_an_empty_string_cursor_stops_pagination(self):
        def fake_page(client, institution_id, cursor):
            return [pull_products.ProductRow(id="p-1", title="Snack")], ""

        with mock.patch.object(pull_products, "_fetch_product_page", fake_page):
            rows = pull_products.fetch_institution_products(CITY_ID, client=None)

        self.assertEqual([r.id for r in rows], ["p-1"])

    def test_a_matched_page_would_be_written_under_the_right_institution(self):
        # Proves pull_all's merge/write logic works once _fetch_product_page
        # is real -- exercised here via the fake page-fetcher standing in for
        # the not-yet-implemented one.
        def fake_page(client, institution_id, cursor):
            if institution_id == CITY_ID:
                return [pull_products.ProductRow(id="p-1", title="Snack")], None
            return [pull_products.ProductRow(id="p-2", title="Lunch")], None

        with mock.patch.object(pull_products, "_fetch_product_page", fake_page):
            result = pull_products.pull_all(client=object())

        self.assertEqual(
            result.catalogue["institutions"]["HDCITY"]["products"], {"p-1": "Snack"}
        )
        self.assertEqual(
            result.catalogue["institutions"]["HDCW"]["products"], {"p-2": "Lunch"}
        )
        self.assertEqual(result.institutions_failed, {})

    def test_a_row_missing_id_or_title_is_unmatched_not_written(self):
        def fake_page(client, institution_id, cursor):
            return [
                pull_products.ProductRow(id="p-1", title="Snack"),
                pull_products.ProductRow(id=None, title="No id"),
                pull_products.ProductRow(id="p-2", title=None),
            ], None

        with mock.patch.object(pull_products, "_fetch_product_page", fake_page):
            result = pull_products.pull_all(client=object(), institutions=["HDCITY"])

        self.assertEqual(
            result.catalogue["institutions"]["HDCITY"]["products"], {"p-1": "Snack"}
        )
        self.assertEqual(len(result.unmatched), 2)


class InstitutionFilterTests(PullProductsTestCase):
    def test_an_unknown_institution_code_raises_with_zero_attempts(self):
        with self.assertRaises(pull_products.UnknownInstitutionError) as ctx:
            pull_products.pull_all(client=object(), institutions=["NOPE"])

        self.assertIn("NOPE", str(ctx.exception))

    def test_filtering_restricts_which_institutions_are_attempted(self):
        result = pull_products.pull_all(client=object(), institutions=["HDCITY"])

        self.assertEqual(list(result.institutions_failed), ["HDCITY"])


class NoInstitutionIdConfiguredTests(PullProductsTestCase):
    def test_a_site_with_no_institution_id_is_skipped_not_attempted(self):
        self.catalogue_file.write_text(
            json.dumps({"sites": {"HDNONE": {"label": "No institution yet"}}}),
            encoding="utf-8",
        )

        result = pull_products.pull_all(client=object())

        self.assertIn("HDNONE", result.institutions_skipped)
        self.assertEqual(result.institutions_failed, {})


class MergePreservationTests(PullProductsTestCase):
    def test_a_stubbed_run_never_disturbs_existing_entries(self):
        self.write_existing_products_catalogue(
            {"institutions": {"HDCITY": {"products": {"p-old": "Old Snack"}}}}
        )

        result = pull_products.pull_all(client=object())

        self.assertEqual(
            result.catalogue["institutions"]["HDCITY"]["products"],
            {"p-old": "Old Snack"},
        )


class WriteCatalogueTests(PullProductsTestCase):
    def test_a_backup_is_created_before_the_existing_file_is_overwritten(self):
        original = {"institutions": {"HDCITY": {"products": {}}}}
        self.write_existing_products_catalogue(original)

        result = pull_products.pull_all(client=object())
        backup = pull_products.write_catalogue(result, path=self.products_catalogue_file)

        self.assertIsNotNone(backup)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

    def test_no_backup_when_there_was_no_existing_file(self):
        result = pull_products.pull_all(client=object())

        self.assertFalse(self.products_catalogue_file.exists())
        backup = pull_products.write_catalogue(result, path=self.products_catalogue_file)

        self.assertIsNone(backup)
        self.assertTrue(self.products_catalogue_file.exists())


if __name__ == "__main__":
    unittest.main()
