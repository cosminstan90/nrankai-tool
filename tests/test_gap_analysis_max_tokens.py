"""
task_b3d32e2e: api/routes/gap_analysis.py's generate_gap_analysis_task()
called call_llm_for_summary(..., max_tokens=4096) for a JSON response that
routinely needs more. _empty_criteria() (gap_analysis.py:96-109) has up to
10 criteria slots, reused unmodified for SEO/GEO/content audit types alike;
each can become a criteria_gaps entry with 3 prose fields (details,
competitor_example, fix_action) per the system prompt's own schema
(build_gap_analysis_prompt, gap_analysis.py:638+), plus a strengths array
and recommendations grouped into quick_wins/medium_term/strategic -- a full
gap analysis across most criteria plausibly exceeds 4096 tokens and gets
cut off mid-JSON, failing the json.loads() a few lines below the LLM call.
Bumped to 8192, matching the value already used for comparably verbose
structured JSON elsewhere (content_briefs.py:286, keyword_research.py:630).

No CompetitorGapAnalysis rows existed in the real DB to check for a past
truncation failure directly (the feature had never been exercised in
production) -- this is a reasoned fix from reading the schema/prompt, not
a reproduced live failure. This test pins the max_tokens value itself
(mocking call_llm_for_summary, no real LLM call) so a future edit can't
silently revert it back to 4096.
"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.audit import Audit, AuditResult
from api.models.content import CompetitorGapAnalysis
from api.routes.gap_analysis import generate_gap_analysis_task

FAKE_GAP_RESPONSE = json.dumps({
    "criteria_gaps": [], "strengths": [],
    "recommendations": {"quick_wins": [], "medium_term": [], "strategic": []},
})


class TestGapAnalysisMaxTokens(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            audits = (await db.execute(
                select(Audit).where(Audit.website.in_([
                    "https://gap-target.example.com", "https://gap-competitor.example.com",
                ]))
            )).scalars().all()
            for a in audits:
                await db.delete(a)
            gaps = (await db.execute(
                select(CompetitorGapAnalysis).where(CompetitorGapAnalysis.name == "test-max-tokens-gap")
            )).scalars().all()
            for g in gaps:
                await db.delete(g)
            await db.commit()

    async def test_calls_llm_with_8192_max_tokens(self):
        async with AsyncSessionLocal() as db:
            target = Audit(
                id="test-gap-target-audit", website="https://gap-target.example.com",
                audit_type="SEO_AUDIT", status="completed", provider="anthropic", model="claude-test",
            )
            competitor = Audit(
                id="test-gap-competitor-audit", website="https://gap-competitor.example.com",
                audit_type="SEO_AUDIT", status="completed", provider="anthropic", model="claude-test",
            )
            db.add_all([target, competitor])
            await db.flush()
            db.add_all([
                AuditResult(audit_id=target.id, page_url="p1", filename="p1.json", score=50,
                            result_json=json.dumps({"meta_title": "x" * 40})),
                AuditResult(audit_id=competitor.id, page_url="p1", filename="p1.json", score=90,
                            result_json=json.dumps({"meta_title": "y" * 45})),
            ])
            gap = CompetitorGapAnalysis(
                id="test-max-tokens-gap-id", name="test-max-tokens-gap",
                target_audit_id=target.id,
                competitor_audit_ids=json.dumps([competitor.id]),
                status="pending",
            )
            db.add(gap)
            await db.commit()

        with patch("api.routes.gap_analysis.call_llm_for_summary",
                   new=AsyncMock(return_value=(FAKE_GAP_RESPONSE, 100, 50))) as mock_call:
            await generate_gap_analysis_task(
                gap_id="test-max-tokens-gap-id",
                target_audit_id="test-gap-target-audit",
                competitor_audit_ids=["test-gap-competitor-audit"],
                provider="anthropic", model="claude-test",
            )

        mock_call.assert_called_once()
        self.assertEqual(mock_call.call_args.kwargs["max_tokens"], 8192)


if __name__ == "__main__":
    unittest.main()
