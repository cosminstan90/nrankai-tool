"""
Pasul 9 of docs/superpowers/plans/2026-09-30-next-steps.md.

A visibility scan asked each provider a tracking query exactly once, so 1/5
vs 2/5 citations was mostly noise (LLM answers vary between runs -- the real
ing.ro tracker's one unreproduced "absent" run was probably exactly this).
samples_per_query lets a tracker ask N times per query and reports a Wilson
confidence interval instead of a single over-precise percentage.
"""
import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CitationScan, CitationTracker
from api.routes.visibility import _run_visibility_scan


class _TrackerCase(unittest.IsolatedAsyncioTestCase):
    website = "ing.ro"
    queries = ["q1"]
    samples_per_query = 3

    async def asyncSetUp(self):
        self.tracker_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(CitationTracker(
                id=self.tracker_id, name="samples test", website=self.website,
                url_patterns=json.dumps([self.website]), tracking_queries=json.dumps(self.queries),
                providers_config=json.dumps({"perplexity": True}),
                samples_per_query=self.samples_per_query,
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            t = (await db.execute(select(CitationTracker).where(CitationTracker.id == self.tracker_id))).scalar_one_or_none()
            if t:
                await db.delete(t)
                await db.commit()

    async def _run(self, query_fn):
        with patch("api.routes.visibility._query_provider", query_fn), \
             patch("api.routes.visibility.track_cost", AsyncMock()), \
             patch("api.routes.visibility.asyncio.sleep", AsyncMock()):
            await _run_visibility_scan(self.tracker_id)

    async def _breakdown(self):
        async with AsyncSessionLocal() as db:
            scan = (await db.execute(
                select(CitationScan).where(CitationScan.tracker_id == self.tracker_id)
            )).scalar_one()
        return json.loads(scan.provider_breakdown), scan


class TestSamplesPerQueryCallCount(_TrackerCase):
    async def test_n_calls_are_made_per_query(self):
        query_fn = AsyncMock(return_value=("plain answer, no link", 5, 5, "sonar"))
        await self._run(query_fn)
        self.assertEqual(query_fn.await_count, self.samples_per_query * len(self.queries))

    async def test_all_samples_are_kept_in_results_json(self):
        query_fn = AsyncMock(return_value=("plain answer, no link", 5, 5, "sonar"))
        await self._run(query_fn)
        _, scan = await self._breakdown()
        results = json.loads(scan.results_json)
        self.assertEqual(len(results[0]["providers"]["perplexity"]["samples"]), self.samples_per_query)


class TestCitationRateUsesSamplesAsDenominator(_TrackerCase):
    async def test_partial_citation_across_samples(self):
        """1 of 3 samples cites the site -- the rate must be 1/3, not 1/1 (queries) or 1/anything else."""
        responses = iter([
            ("See https://ing.ro/oferta for details", 5, 5, "sonar"),
            ("no link here", 5, 5, "sonar"),
            ("still nothing", 5, 5, "sonar"),
        ])

        async def fake_query(provider, query):
            return next(responses)

        await self._run(fake_query)
        breakdown, _ = await self._breakdown()
        stats = breakdown["perplexity"]

        self.assertEqual(stats["responses"], 3)
        self.assertEqual(stats["citations"], 1)
        self.assertEqual(stats["queries"], 1)   # one distinct tracking query, answered
        self.assertAlmostEqual(stats["citation_rate"], 100 / 3, places=1)

    async def test_wilson_ci_is_present_and_matches_the_core_function(self):
        from core.wilson import wilson_interval

        responses = iter([
            ("See https://ing.ro/oferta for details", 5, 5, "sonar"),
            ("no link here", 5, 5, "sonar"),
            ("still nothing", 5, 5, "sonar"),
        ])

        async def fake_query(provider, query):
            return next(responses)

        await self._run(fake_query)
        breakdown, _ = await self._breakdown()
        stats = breakdown["perplexity"]

        expected_low, expected_high = wilson_interval(1, 3)
        self.assertAlmostEqual(stats["citation_rate_ci"]["low"], expected_low, places=4)
        self.assertAlmostEqual(stats["citation_rate_ci"]["high"], expected_high, places=4)


class TestFailedSamplesAreExcludedFromTheDenominator(_TrackerCase):
    async def test_a_failed_sample_does_not_count_as_a_measured_response(self):
        """2 succeed (1 citing), 1 fails outright -- rate must be 1/2, and the failure is tracked separately."""
        responses = iter([
            ("See https://ing.ro/oferta for details", 5, 5, "sonar"),
            ("", 0, 0, "sonar"),          # empty response -- a failed call
            ("no link here", 5, 5, "sonar"),
        ])

        async def fake_query(provider, query):
            return next(responses)

        await self._run(fake_query)
        breakdown, _ = await self._breakdown()
        stats = breakdown["perplexity"]

        self.assertEqual(stats["responses"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["citations"], 1)
        self.assertAlmostEqual(stats["citation_rate"], 50.0, places=1)

    async def test_all_samples_failing_is_a_gap_not_a_measured_zero(self):
        from api.routes.visibility import _provider_point

        query_fn = AsyncMock(return_value=("", 0, 0, "sonar"))
        await self._run(query_fn)
        breakdown, _ = await self._breakdown()
        stats = breakdown["perplexity"]

        self.assertEqual(stats["responses"], 0)
        self.assertEqual(stats["failed"], self.samples_per_query)
        self.assertIsNone(_provider_point(breakdown, "perplexity", "citation_rate"))


class TestDefaultIsOneSampleUnchanged(_TrackerCase):
    samples_per_query = None   # NULL on the tracker -- must behave exactly like samples_per_query=1

    async def test_null_samples_per_query_makes_exactly_one_call(self):
        query_fn = AsyncMock(return_value=("plain answer, no link", 5, 5, "sonar"))
        await self._run(query_fn)
        self.assertEqual(query_fn.await_count, len(self.queries))

    async def test_old_shaped_provider_breakdown_is_still_readable(self):
        """A scan written before this feature has no citation_rate_ci / samples keys at all."""
        from api.routes.visibility import _provider_point

        old_breakdown = {"perplexity": {"citations": 2, "mentions": 3, "queries": 5,
                                        "responses": 5, "failed": 0, "citation_rate": 40.0}}
        self.assertEqual(_provider_point(old_breakdown, "perplexity", "citation_rate"), 40.0)


if __name__ == "__main__":
    unittest.main()
