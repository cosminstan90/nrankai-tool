"""
task_4baec8cd: core/direct_analyzer.py's DirectAnalyzer._process_single_page()
called `asyncio.create_task(record_cost_async(...))` -- the same
fire-and-forget-racing-a-shared-SQLite-connection anti-pattern already found
and fixed independently in api/routes/visibility.py (Etapa 3),
api/routes/content_briefs.py, api/routes/fanout.py, api/routes/schema_gen.py,
api/routes/summary.py (Etapa 5.6's rescued fix). record_cost_async() opens
its own AsyncSessionLocal(); firing it via create_task while the caller's own
db session is still open races on api/models/_base.py's shared StaticPool
SQLite connection.

Here the same pattern additionally caused a cross-test hang: an abandoned
create_task() left mid-flight when a test's event loop closes (every
unittest.IsolatedAsyncioTestCase test gets a fresh loop, closed at the end)
can wedge aiosqlite's single background worker thread -- every later test
sharing that same pooled connection then queues behind the wedged query and
hangs forever, since aiosqlite delivers results via
future.get_loop().call_soon_threadsafe(...), and that call either fails
silently in the background thread or never gets a chance to run once the
original loop is closed. Confirmed by pairing
tests/test_contentiq_crawler_concurrency.py (any real AsyncSessionLocal use)
with a second test calling DirectAnalyzer._process_single_page() twice with
a fake LLM client returning nonzero tokens -- hung indefinitely before this
fix, passes cleanly (and the full suite runs 3x in a row without hanging)
after switching to `await record_cost_async(...)`.

This is a codebase-wide AST scan (not just direct_analyzer.py) since the
same anti-pattern has recurred five separate times already -- guards against
a sixth reappearing anywhere under api/ or core/.
"""
import ast
import os
import unittest


def _find_create_task_wrapping_record_cost_async(file_path: str) -> list[int]:
    """Return line numbers where asyncio.create_task(record_cost_async(...)) appears."""
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=file_path)

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_create_task = (
            (isinstance(func, ast.Attribute) and func.attr == "create_task")
            or (isinstance(func, ast.Name) and func.id == "create_task")
        )
        if not is_create_task or not node.args:
            continue
        inner = node.args[0]
        if isinstance(inner, ast.Call):
            inner_func = inner.func
            name = getattr(inner_func, "id", None) or getattr(inner_func, "attr", None)
            if name == "record_cost_async":
                hits.append(node.lineno)
    return hits


class TestRecordCostAsyncNeverFireAndForget(unittest.TestCase):
    def test_no_create_task_wrapping_anywhere_in_api_or_core(self):
        offenders = {}
        for root_dir in ("api", "core"):
            for dirpath, _dirs, filenames in os.walk(root_dir):
                for filename in filenames:
                    if not filename.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, filename)
                    hits = _find_create_task_wrapping_record_cost_async(path)
                    if hits:
                        offenders[path] = hits

        self.assertEqual(
            offenders, {},
            f"asyncio.create_task(record_cost_async(...)) found -- this exact "
            f"fire-and-forget pattern races on the shared SQLite StaticPool "
            f"connection (silently drops commits) and can wedge aiosqlite's "
            f"worker thread across event loops (hangs). Await it directly "
            f"instead. Offending files:line: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
