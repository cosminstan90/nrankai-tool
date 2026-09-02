"""
Pins the fix from Etapa 4.7 of the consolidation (docs/CONSOLIDATION_PLAN.md):
GET /api/mention-seeding/configs/{id}/latest computed coverage_score as
covered / len(by_platform), but by_platform is built only from platforms
that already had at least one persisted hit -- so covered always equals
len(by_platform) by construction, and coverage_score was mathematically
guaranteed to report 100% regardless of how many platforms the config
actually monitors. api/workers/mention_seeder_worker.get_active_platforms()
is the shared source of truth for "which platforms does this config
monitor" (used by the worker to decide what to scan, and now by the route
to compute the true denominator).
"""
import unittest
from types import SimpleNamespace

from api.workers.mention_seeder_worker import get_active_platforms


class TestGetActivePlatforms(unittest.TestCase):
    def test_all_flags_true_returns_all_six_platforms(self):
        cfg = SimpleNamespace(monitor_reddit=True, monitor_quora=True, monitor_review_sites=True, monitor_press=True)
        self.assertEqual(
            set(get_active_platforms(cfg)),
            {"reddit", "quora", "g2", "capterra", "trustpilot", "press"},
        )

    def test_only_reddit_returns_one_platform(self):
        cfg = SimpleNamespace(monitor_reddit=True, monitor_quora=False, monitor_review_sites=False, monitor_press=False)
        self.assertEqual(set(get_active_platforms(cfg)), {"reddit"})

    def test_review_sites_expands_to_three_platforms(self):
        cfg = SimpleNamespace(monitor_reddit=False, monitor_quora=False, monitor_review_sites=True, monitor_press=False)
        self.assertEqual(set(get_active_platforms(cfg)), {"g2", "capterra", "trustpilot"})

    def test_no_flags_falls_back_to_all_platforms(self):
        cfg = SimpleNamespace(monitor_reddit=False, monitor_quora=False, monitor_review_sites=False, monitor_press=False)
        self.assertEqual(
            set(get_active_platforms(cfg)),
            {"reddit", "quora", "g2", "capterra", "trustpilot", "press"},
        )


if __name__ == "__main__":
    unittest.main()
