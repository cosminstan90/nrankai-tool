"""
Pins the fix from Etapa 4.9 of the consolidation (docs/CONSOLIDATION_PLAN.md):
SerpIQVerdictEngine's "top 3" analysis (avg_word_count_top3,
_top3_domains_bullet, _schema_rate_top_n) used to filter serp_items by
literal position in (1, 2, 3) / position <= n. Once serp_fetcher.py stopped
letting SERP feature blocks (AI Overview, People Also Ask) occupy those
exact position numbers as hollow rows, this filter started starving on
real-world SERPs -- when e.g. an AI Overview sits at position 1 and a
People Also Ask box at position 3, only ONE real organic item (position 2)
would ever match "position in (1, 2, 3)", even though 3+ real organic
competitors exist right after (positions 4, 5, 6, ...). Confirmed live for
"best crm software": the bullet showed only 1 domain instead of 3.

The fix uses list order (the already-rank-ordered, now junk-free list)
instead of literal position numbers.
"""
import unittest
from dataclasses import dataclass, field
from typing import Optional

from app.modules.serpiq.services.verdict_engine import SerpIQVerdictEngine, _schema_rate_top_n


@dataclass
class _FakeItem:
    position: int
    domain: Optional[str] = None
    word_count_estimated: Optional[int] = None
    has_schema: bool = False


def _gapped_items() -> list:
    """Simulates a real SERP where an AI Overview (pos 1) and People Also
    Ask (pos 3) box were already filtered out upstream, leaving organic
    results at positions 2, 4, 5 -- no item at position 1 or 3 at all."""
    return [
        _FakeItem(position=2, domain="a.com", word_count_estimated=900, has_schema=True),
        _FakeItem(position=4, domain="b.com", word_count_estimated=700, has_schema=False),
        _FakeItem(position=5, domain="c.com", word_count_estimated=800, has_schema=True),
        _FakeItem(position=6, domain="d.com", word_count_estimated=600, has_schema=False),
    ]


class TestTop3DomainsBullet(unittest.TestCase):
    def test_finds_three_domains_despite_position_gaps(self):
        bullet = SerpIQVerdictEngine._top3_domains_bullet(_gapped_items())
        self.assertIn("a.com", bullet)
        self.assertIn("b.com", bullet)
        self.assertIn("c.com", bullet)
        self.assertNotIn("d.com", bullet)


class TestAvgWordCountTop3(unittest.TestCase):
    def test_averages_first_three_by_list_order(self):
        engine = SerpIQVerdictEngine()
        diff = engine.analyze_serp_difficulty(_gapped_items())
        # (900 + 700 + 800) / 3 = 800
        self.assertEqual(diff.avg_word_count_top3, 800)


class TestSchemaRateTopN(unittest.TestCase):
    def test_uses_list_order_not_position_number(self):
        rate = _schema_rate_top_n(_gapped_items(), n=3)
        # a.com (True), b.com (False), c.com (True) -> 2/3
        self.assertAlmostEqual(rate, 2 / 3)


if __name__ == "__main__":
    unittest.main()
