"""
Pins the fix from Etapa 5.1 of the consolidation (docs/CONSOLIDATION_PLAN.md):
the single biggest defect found in this whole effort. call_llm_for_summary()
(api/utils/llm_json_client.py, formerly api/routes/summary.py) has always
returned a 3-tuple (text, input_tokens, output_tokens) -- since the initial
commit. Six call sites across five files assigned the whole tuple to one
variable and then called string methods on it (clean_json_response(),
.strip()), crashing every time:

  - api/routes/action_cards.py      (1 call site) -- response.strip()
  - api/routes/content_briefs.py    (2 call sites) -- clean_json_response(tuple)
  - api/routes/benchmarks.py        (1 call site) -- clean_json_response(tuple)
  - api/routes/content_gaps.py      (2 call sites) -- ALSO passed content=
    instead of user_content= (TypeError before even reaching the tuple issue)
  - api/routes/gap_analysis.py      (1 call site) -- clean_json_response(tuple)

Only api/workers/draft_optimizer.py and summary.py's own generate_summary_task
unpacked it correctly. Confirmed live (DB timeline): all 40 action_cards and
all 25 content_briefs rows date from Feb 26 - Mar 9, 2026, nothing since --
these features have been silently broken for ~6 months. Live-tested every
fixed call site with a real Anthropic API call after the fix; all produced
real, substantive output for the first time.

This test pins the *shape* of the return value so a future refactor can't
silently reintroduce the mismatch without a caller-side test failing.
"""
import unittest
import ast


class TestCallLlmForSummaryReturnShape(unittest.TestCase):
    def test_source_declares_three_tuple_return(self):
        import inspect
        from api.utils.llm_json_client import call_llm_for_summary
        sig = inspect.signature(call_llm_for_summary)
        # tuple[str, int, int] is only a hint, not enforced at runtime --
        # this at least catches someone deleting the annotation/return shape
        # entirely without also checking real caller behavior below.
        src = inspect.getsource(call_llm_for_summary)
        self.assertIn("-> tuple[str, int, int]", src)


def _uses_tuple_unpacking(file_path: str, func_name_substring: str = "call_llm_for_summary") -> list:
    """
    Parse a route file's source and return every `await call_llm_for_summary(...)`
    call site's assignment target shape: True if it unpacks into >=2 names,
    False if it assigns to a single name (the bug pattern).
    """
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
            call = node.value.value
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == func_name_substring:
                target = node.targets[0]
                is_tuple_unpack = isinstance(target, (ast.Tuple, ast.List))
                results.append(is_tuple_unpack)
    return results


class TestNoSingleVariableAssignmentRegression(unittest.TestCase):
    """Regression guard: every call site must unpack into >=2 names, never one."""

    def _assert_all_unpacked(self, file_path: str):
        results = _uses_tuple_unpacking(file_path)
        self.assertTrue(results, f"no call_llm_for_summary call sites found in {file_path} -- test may be stale")
        for is_unpacked in results:
            self.assertTrue(
                is_unpacked,
                f"{file_path} assigns call_llm_for_summary's tuple return to a single "
                f"variable -- this is the exact bug fixed in Etapa 5.1 (crashes when "
                f"clean_json_response/.strip() is called on the tuple)",
            )

    def test_action_cards(self):
        self._assert_all_unpacked("api/routes/action_cards.py")

    def test_content_briefs(self):
        self._assert_all_unpacked("api/routes/content_briefs.py")

    def test_benchmarks(self):
        self._assert_all_unpacked("api/routes/benchmarks.py")

    def test_content_gaps(self):
        self._assert_all_unpacked("api/routes/content_gaps.py")

    def test_gap_analysis(self):
        self._assert_all_unpacked("api/routes/gap_analysis.py")


if __name__ == "__main__":
    unittest.main()
