"""
Etapa 5.6 of the consolidation (docs/CONSOLIDATION_PLAN.md): recovers
core/validate_audit.py's schema-validation logic as a regression test, since
that file itself is deleted as part of the legacy CLI toolchain cleanup (it
had zero importers anywhere in api/ or app/). The validation logic is
inlined below rather than imported, precisely because the source file is
gone -- this IS the recovery, not a wrapper around it.

The checks (YAML structure/metadata/output_schema/scoring shape) are the
only thing in the repo that validates prompts/*.yaml against the shape
core/audit_builder.py and core/direct_analyzer.py actually expect -- worth
keeping given prompts/ changes the output of every audit (see CLAUDE.md).

Two files intentionally fail the legacy 'output_schema' check and that is
NOT a bug: content_brief.yaml and draft_optimizer.yaml are not dispatched
through the generic 19-audit-type engine (core.prompt_loader.
list_available_audits() + core.output_schemas.get_output_schema(), which
looks up AUDIT_OUTPUT_SCHEMAS by audit_type -- a Python dict, independent of
any YAML field) -- they're consumed by their own dedicated routes
(content_briefs.py, draft_optimizer.py) via call_llm_for_summary, so the
YAML-embedded 'output_schema' field this check looks for was never required
for them. This test pins that as a known, accounted-for exception rather
than silently ignoring it -- if a third file starts failing, or one of
these two starts passing, that's worth noticing.
"""
import glob
import os
import unittest
from typing import Any, Dict, List, Optional, Tuple

import yaml

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")

# Consumed by dedicated routes outside the generic 19-audit dispatch engine --
# see module docstring above for why "Missing 'output_schema' field" here is
# expected, not a defect.
KNOWN_NO_EMBEDDED_OUTPUT_SCHEMA = {"content_brief.yaml", "draft_optimizer.yaml"}

VALID_FIELD_TYPES = ["integer", "string", "enum", "boolean", "array"]


class ValidationResult:
    """Result of a validation check. Ported from the deleted core/validate_audit.py."""

    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def add_error(self, message: str):
        self.errors.append(f"ERROR: {message}")

    def add_warning(self, message: str):
        self.warnings.append(f"WARNING: {message}")

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def _validate_yaml_structure(yaml_path: str) -> Tuple[Optional[dict], ValidationResult]:
    result = ValidationResult()
    if not os.path.isfile(yaml_path):
        result.add_error(f"File not found: {yaml_path}")
        return None, result
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.add_error(f"Invalid YAML syntax: {e}")
        return None, result
    if not data or not isinstance(data, dict):
        result.add_error("Empty YAML file or root is not a dictionary")
        return None, result
    return data, result


def _validate_metadata(data: dict, result: ValidationResult):
    for field in ["name", "description", "version"]:
        if field not in data:
            result.add_error(f"Missing required field: {field}")
        elif not data[field]:
            result.add_error(f"Empty required field: {field}")
        elif not isinstance(data[field], str):
            result.add_error(f"Field '{field}' must be a string")


def _validate_field_definitions(fields: List[dict], context: str, result: ValidationResult):
    seen_names = set()
    for i, field in enumerate(fields):
        if not isinstance(field, dict):
            result.add_error(f"{context}[{i}] must be a dictionary")
            continue
        if "name" not in field:
            result.add_error(f"{context}[{i}] missing 'name'")
            continue
        name = field["name"]
        if name in seen_names:
            result.add_error(f"{context}: Duplicate field name '{name}'")
        seen_names.add(name)
        field_type = field.get("type")
        if field_type is not None and field_type not in VALID_FIELD_TYPES:
            result.add_error(f"{context}.{name}: Invalid type '{field_type}'")
        if field_type == "enum" and not field.get("values"):
            result.add_error(f"{context}.{name}: Enum type requires non-empty 'values' list")


def _validate_output_schema(data: dict, result: ValidationResult):
    if "output_schema" not in data:
        result.add_error("Missing 'output_schema' field")
        return
    schema = data["output_schema"]
    if isinstance(schema, str):
        return
    if not isinstance(schema, dict):
        result.add_error("'output_schema' must be a dict or string")
        return
    if "fields" in schema:
        if not isinstance(schema["fields"], list):
            result.add_error("'output_schema.fields' must be a list")
        else:
            _validate_field_definitions(schema["fields"], "fields", result)
    if "issues_key" in schema and "issues_schema" in schema:
        if not isinstance(schema["issues_schema"], list):
            result.add_error("'output_schema.issues_schema' must be a list")
        else:
            _validate_field_definitions(schema["issues_schema"], "issues_schema", result)


def validate_full(yaml_path: str) -> ValidationResult:
    data, result = _validate_yaml_structure(yaml_path)
    if data is None:
        return result
    _validate_metadata(data, result)
    _validate_output_schema(data, result)
    return result


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
                "if it now passes, the YAML gained an embedded output_schema and "
                "KNOWN_NO_EMBEDDED_OUTPUT_SCHEMA can shrink.",
            )
            self.assertEqual(
                failures[filename], ["ERROR: Missing 'output_schema' field"],
                f"{filename} failed validation for a NEW reason, not just the known one: {failures[filename]}",
            )


if __name__ == "__main__":
    unittest.main()
