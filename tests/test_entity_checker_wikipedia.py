"""
Pins the fix from Etapa 4.8 of the consolidation (docs/CONSOLIDATION_PLAN.md):
_check_wikipedia() used to look up en.wikipedia.org/api/rest_v1/page/summary/
{brand} by name alone, with no disambiguation. For a brand whose name
collides with an unrelated common-word article -- "Asana" the yoga posture,
not "Asana, Inc." the software company -- this silently returned a false
positive: found=True, +25 points toward entity_authority_score, and a
description/url pointing at the wrong topic entirely.

The fix verifies each candidate title's external links (via the MediaWiki
extlinks API) actually reference target_domain before accepting a match, and
tries common disambiguation patterns ("{brand} (company)", "{brand}, Inc.",
"{brand} (software)") so a real company article is still discovered rather
than just suppressing the false positive.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from api.workers.entity_checker import _check_wikipedia


def _summary_response(status_code: int, title: str = "", extract: str = "", page_url: str = ""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = {
        "title": title,
        "extract": extract,
        "content_urls": {"desktop": {"page": page_url}},
    }
    return resp


def _extlinks_response(links: list):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "query": {"pages": {"1": {"extlinks": [{"*": link} for link in links]}}}
    }
    return resp


def _not_found_response():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 404
    return resp


class TestCheckWikipediaDisambiguation(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_name_collision_and_finds_disambiguated_article(self):
        """Regression test for the Asana (yoga) vs Asana, Inc. false positive."""
        session = AsyncMock()

        def get_side_effect(url, **kwargs):
            if "w/api.php" in url:
                # extlinks lookups: only "Asana, Inc." actually links asana.com
                title = kwargs["params"]["titles"]
                if title == "Asana, Inc.":
                    return _extlinks_response(["https://asana.com/"])
                return _extlinks_response([])  # yoga article: no company link
            if "Inc" in url:
                return _summary_response(200, title="Asana, Inc.", extract="American software company")
            if "company" in url or "software" in url:
                return _not_found_response()
            if "summary/Asana" in url:
                return _summary_response(200, title="Asana", extract="A yoga posture")
            return _not_found_response()

        session.get.side_effect = get_side_effect

        result = await _check_wikipedia("Asana", "asana.com", session)

        self.assertTrue(result.found)
        self.assertEqual(result.description, "American software company")

    async def test_no_match_anywhere_returns_not_found(self):
        session = AsyncMock()
        session.get.side_effect = lambda url, **kwargs: (
            _extlinks_response([]) if "w/api.php" in url else _not_found_response()
        )

        result = await _check_wikipedia("ThisBrandDoesNotExistXyz123", "nonexistent-xyz.com", session)

        self.assertFalse(result.found)

    async def test_unambiguous_brand_matches_on_first_candidate(self):
        session = AsyncMock()

        def get_side_effect(url, **kwargs):
            if "w/api.php" in url:
                return _extlinks_response(["https://salesforce.com/"])
            if url.endswith("/summary/Salesforce"):
                return _summary_response(200, title="Salesforce", extract="American cloud company")
            return _not_found_response()

        session.get.side_effect = get_side_effect

        result = await _check_wikipedia("Salesforce", "salesforce.com", session)

        self.assertTrue(result.found)
        self.assertEqual(result.description, "American cloud company")


if __name__ == "__main__":
    unittest.main()
