"""
Etapa 5.6 of the consolidation (docs/CONSOLIDATION_PLAN.md): recovers
core/validate_audit.py's schema-validation logic as a regression test before
that file is deleted as part of the legacy CLI toolchain cleanup. The tool
itself is unused (no importer anywhere in api/ or app/), but its YAML
structure/metadata/output_schema/scoring checks are the only thing in the
repo that validates prompts/*.yaml against the shape core/audit_builder.py
and core/direct_analyzer.py actually expect -- worth keeping as a test given
prompts/ changes the output of every audit (see CLAUDE.md).

Two files intentionally fail the legacy 'output_schema' check and that is
NOT a bug: content_brief.yaml and draft_optimizer.yaml are not dispatched
through the generic 19-audit-type engine (core.prompt_loader.
list_available_audits() + core.output_schemas.get_output_schema(), which
looks up AUDIT_OUTPUT_SCHEMAS by audit_type -- a Python dict, independent of
any YAML field) -- they're consumed by their own dedicated routes
(content_briefs.py, draft_optimizer.py) via call_llm_for_summary, so the
YAML-embedded 'output_schema' field this validator checks for was never
required for them. This test pins that as a known, accounted-for exception
rather than silently ignoring it -- if a third file starts failing, or one
of these two starts passing, that's worth noticing.
"""
import glob
import os
import unittest

from core.validate_audit import validate_full

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")

# Consumed by dedicated routes outside the generic 19-audit dispatch engine --
# see module docstring above for why "Missing 'output_schema' field" here is
# expected, not a defect.
KNOWN_NO_EMBEDDED_OUTPUT_SCHEMA = {"content_brief.yaml", "draft_optimizer.yaml"}


class TestPromptYamlsValidate(unittest.TestCase):
    def test_all_prompt_yamls_present(self):
        files = sorted(os.path.basename(f) for f in glob.glob(os.path.join(PROMPTS_DIR, "*.yaml")))
        self.assertGreaterEqual(len(files), 18, f"Expected the real prompts/ directory, found: {files}")

    def test_every_prompt_yaml_validates_cleanly_except_known_exceptions(self):
        failures = {}
        for path in sorted(glob.glob(os.path.join(PROMPTS_DIR, "*.yaml"))):
            filename = os.path.basename(path)
            result = validate_full(path)
            if not result.is_valid:
                failures[filename] = list(result.errors)

        unexpected = {f: errs for f, errs in failures.items() if f not in KNOWN_NO_EMBEDDED_OUTPUT_SCHEMA}
        self.assertEqual(unexpected, {}, f"Unexpected prompt YAML validation errors: {unexpected}")

        for filename in KNOWN_NO_EMBEDDED_OUTPUT_SCHEMA:
            self.assertIn(
                filename, failures,
                f"{filename} was expected to fail the legacy output_schema check -- "
                "if it now passes, core/audit_builder.py or the YAML gained an "
                "embedded output_schema and KNOWN_NO_EMBEDDED_OUTPUT_SCHEMA can shrink.",
            )
            self.assertEqual(
                failures[filename], ["ERROR: Missing 'output_schema' field"],
                f"{filename} failed validation for a NEW reason, not just the known one: {failures[filename]}",
            )


if __name__ == "__main__":
    unittest.main()
