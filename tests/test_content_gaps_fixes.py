"""
Pins two additional fixes found in api/routes/content_gaps.py while fixing
the call_llm_for_summary tuple-unpacking bug (Etapa 5.1,
docs/CONSOLIDATION_PLAN.md):

1. Both LLM call sites passed content=user_prompt, a keyword that doesn't
   exist on call_llm_for_summary's signature (it's user_content) -- this
   raised TypeError immediately, before even reaching the tuple-unpacking
   issue. Confirmed live: /api/content-gaps/{id}/generate-full-brief now
   produces a real content brief.

2. Every endpoint taking a gap_id path parameter
   (GET/{gap_id}, PATCH/{gap_id}, POST/{gap_id}/generate-full-brief,
   DELETE/{gap_id}) typed it as `int`, but ContentGap.id is a UUID string
   (String(36), default=str(uuid.uuid4())) -- so FastAPI's path-parameter
   coercion rejected every real gap_id with a 422, before the handler ever
   ran. Confirmed live: GET /api/content-gaps/{real-uuid} returned 422
   before the fix, 200 after.
"""
import ast
import unittest


class TestContentGapsCallSitesUseUserContent(unittest.TestCase):
    def test_no_call_site_uses_wrong_content_kwarg(self):
        with open("api/routes/content_gaps.py", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        found_call = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "call_llm_for_summary":
                found_call = True
                kwarg_names = {kw.arg for kw in node.keywords}
                self.assertNotIn(
                    "content", kwarg_names,
                    "call_llm_for_summary has no 'content' parameter (it's "
                    "'user_content') -- this exact typo caused a TypeError "
                    "on every content_gaps.py brief generation call",
                )
                self.assertIn("user_content", kwarg_names)

        self.assertTrue(found_call, "no call_llm_for_summary call sites found -- test may be stale")


class TestContentGapsGapIdIsString(unittest.TestCase):
    def test_no_endpoint_types_gap_id_as_int(self):
        with open("api/routes/content_gaps.py", encoding="utf-8") as f:
            source = f.read()

        self.assertNotIn(
            "gap_id: int", source,
            "ContentGap.id is a UUID string (String(36)) -- typing a path "
            "parameter as `gap_id: int` makes FastAPI reject every real "
            "gap_id with a 422 before the handler ever runs",
        )
        self.assertIn("gap_id: str", source)


if __name__ == "__main__":
    unittest.main()
