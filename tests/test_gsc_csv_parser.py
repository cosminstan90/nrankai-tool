"""
Pins the fix from Etapa 5.3 of the consolidation (docs/CONSOLIDATION_PLAN.md):
api/routes/gsc/properties.py's upload_csv() called _parse_gsc_csv(content),
a function that was never defined anywhere in the repo -- a casualty of the
2026-04-05 refactor that split the old monolithic gsc.py into the gsc/
subpackage (git log -p on properties.py shows the old file's definition
being dropped while the call site survived). Every call to
POST /api/gsc/properties/{id}/upload raised NameError, 100% of the time.

Confirmed via a read-only DB query before any fix: the app's one real
GscProperty (site_url='sc-domain:conso.ro', sync_type='api') and all
25,000/11,832 rows in gsc_query_rows/gsc_page_rows arrived exclusively via
the live OAuth API sync in oauth_sync.py -- this bug never caused any data
loss, since nothing had ever written through the broken CSV path.

_parse_gsc_csv() was written fresh, matched to GSC's real "Performance"
report CSV export format and the existing 0.0-1.0 CTR scale convention
already used by the working OAuth sync path (the Search Console API
returns ctr as a raw fraction; CSV upload must normalise "12.34%" strings
to the same 0.1234 scale so the two ingestion paths don't silently
disagree on units).
"""
import unittest

from api.routes.gsc.properties import _parse_gsc_csv


class TestParseGscCsvQueries(unittest.TestCase):
    def test_detects_queries_report_type(self):
        csv_bytes = (
            b'Top queries,Clicks,Impressions,CTR,Position\n'
            b'"best crm software",120,5000,2.4%,8.3\n'
        )
        report_type, rows = _parse_gsc_csv(csv_bytes)
        self.assertEqual(report_type, "queries")
        self.assertEqual(len(rows), 1)

    def test_ctr_percentage_converted_to_fraction(self):
        """CTR must land on the same 0.0-1.0 scale the OAuth sync path
        already uses -- '2.4%' must become 0.024, not 2.4."""
        csv_bytes = (
            b'Top queries,Clicks,Impressions,CTR,Position\n'
            b'"best crm software",120,5000,2.4%,8.3\n'
        )
        _, rows = _parse_gsc_csv(csv_bytes)
        self.assertAlmostEqual(rows[0]["ctr"], 0.024)

    def test_row_fields_correct(self):
        csv_bytes = (
            b'Top queries,Clicks,Impressions,CTR,Position\n'
            b'"crm pricing",45,1200,3.75%,12.1\n'
        )
        _, rows = _parse_gsc_csv(csv_bytes)
        row = rows[0]
        self.assertEqual(row["key"], "crm pricing")
        self.assertEqual(row["clicks"], 45)
        self.assertEqual(row["impressions"], 1200)
        self.assertAlmostEqual(row["ctr"], 0.0375)
        self.assertAlmostEqual(row["position"], 12.1)


class TestParseGscCsvPages(unittest.TestCase):
    def test_detects_pages_report_type(self):
        csv_bytes = (
            b'Top pages,Clicks,Impressions,CTR,Position\n'
            b'"https://example.com/page1",300,10000,3%,5.2\n'
        )
        report_type, rows = _parse_gsc_csv(csv_bytes)
        self.assertEqual(report_type, "pages")
        self.assertEqual(rows[0]["key"], "https://example.com/page1")


class TestParseGscCsvErrors(unittest.TestCase):
    def test_unrecognized_header_raises_value_error(self):
        csv_bytes = b"Something,Else\nfoo,bar\n"
        with self.assertRaises(ValueError):
            _parse_gsc_csv(csv_bytes)

    def test_empty_file_raises_value_error(self):
        with self.assertRaises(ValueError):
            _parse_gsc_csv(b"")

    def test_headers_only_no_data_rows_raises_value_error(self):
        csv_bytes = b"Top queries,Clicks,Impressions,CTR,Position\n"
        with self.assertRaises(ValueError):
            _parse_gsc_csv(csv_bytes)


if __name__ == "__main__":
    unittest.main()
