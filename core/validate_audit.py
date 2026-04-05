#!/usr/bin/env python3
"""
Audit Validator Tool

Validates YAML audit definitions against the schema and optionally
tests them with a sample LLM request.

Usage:
    python validate_audit.py prompts/my_audit.yaml
    python validate_audit.py prompts/my_audit.yaml --test
    python validate_audit.py --all

Author: Website LLM Analyzer Team
Created: 2026-02-12
"""

import argparse
import json
import os
import sys
import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.logger import get_logger, setup_logging

# Initialize logger
logger = get_logger(__name__)


# ============================================================================
# VALIDATION RULES
# ============================================================================

REQUIRED_METADATA = ['name', 'description', 'version']
OPTIONAL_METADATA = ['author', 'category', 'language_hint']
VALID_CATEGORIES = ['generic', 'compliance', 'brand', 'technical', 'custom']
VALID_FIELD_TYPES = ['integer', 'string', 'enum', 'boolean', 'array']
VALID_SAVE_CONDITIONS = ['always', 'has_issues']  # Also: score_below:N, score_above:N


class ValidationResult:
    """Result of a validation check."""
    
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: List[str] = []
    
    def add_error(self, message: str):
        self.errors.append(f"ERROR: {message}")
    
    def add_warning(self, message: str):
        self.warnings.append(f"WARNING: {message}")
    
    def add_info(self, message: str):
        self.info.append(f"INFO: {message}")
    
    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0
    
    def print_report(self, verbose: bool = False):
        """Print the validation report."""
        if self.errors:
            print("\n❌ Validation Errors:")
            for error in self.errors:
                print(f"   {error}")
        
        if self.warnings:
            print("\n⚠️  Warnings:")
            for warning in self.warnings:
                print(f"   {warning}")
        
        if verbose and self.info:
            print("\nℹ️  Info:")
            for info in self.info:
                print(f"   {info}")
        
        if self.is_valid:
            print("\n✅ Validation passed!")
        else:
            print(f"\n❌ Validation failed with {len(self.errors)} error(s)")


# ============================================================================
# VALIDATORS
# ============================================================================

def validate_yaml_structure(yaml_path: str) -> Tuple[Optional[dict], ValidationResult]:
    """
    Validate basic YAML structure and parse the file.
    
    Returns:
        Tuple of (parsed_data, validation_result)
    """
    result = ValidationResult()
    
    # Check file exists
    if not os.path.isfile(yaml_path):
        result.add_error(f"File not found: {yaml_path}")
        return None, result
    
    # Try to parse YAML
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.add_error(f"Invalid YAML syntax: {e}")
        return None, result
    except Exception as e:
        result.add_error(f"Failed to read file: {e}")
        return None, result
    
    if not data:
        result.add_error("Empty YAML file")
        return None, result
    
    if not isinstance(data, dict):
        result.add_error("YAML root must be a dictionary")
        return None, result
    
    result.add_info("YAML syntax valid")
    return data, result


def validate_metadata(data: dict, result: ValidationResult):
    """Validate metadata fields."""
    # Required fields
    for field in REQUIRED_METADATA:
        if field not in data:
            result.add_error(f"Missing required field: {field}")
        elif not data[field]:
            result.add_error(f"Empty required field: {field}")
        elif not isinstance(data[field], str):
            result.add_error(f"Field '{field}' must be a string")
    
    # Optional fields
    if 'category' in data:
        if data['category'] not in VALID_CATEGORIES:
            result.add_warning(
                f"Unknown category '{data['category']}'. "
                f"Valid: {', '.join(VALID_CATEGORIES)}"
            )
    
    if 'language_hint' in data:
        if data['language_hint'] not in ['auto', 'en', 'nl', 'de', 'fr', 'es', 'it']:
            result.add_warning(f"Unknown language_hint: {data['language_hint']}")
    
    result.add_info("Metadata validation complete")


def validate_prompt_components(data: dict, result: ValidationResult):
    """Validate role, task, and criteria."""
    # Role
    if 'role' not in data:
        result.add_warning("Missing 'role' field - using empty role")
    elif not isinstance(data['role'], str):
        result.add_error("'role' must be a string")
    elif len(data['role'].strip()) < 20:
        result.add_warning("'role' is very short - consider adding more detail")
    
    # Task
    if 'task' not in data:
        result.add_warning("Missing 'task' field - using empty task")
    elif not isinstance(data['task'], str):
        result.add_error("'task' must be a string")
    elif len(data['task'].strip()) < 20:
        result.add_warning("'task' is very short - consider adding more detail")
    
    # Criteria
    if 'criteria' in data:
        if not isinstance(data['criteria'], list):
            result.add_error("'criteria' must be a list")
        else:
            for i, criterion in enumerate(data['criteria']):
                if not isinstance(criterion, dict):
                    result.add_error(f"Criteria item {i} must be a dictionary")
                    continue
                
                if 'section' not in criterion:
                    result.add_error(f"Criteria item {i} missing 'section' field")
                
                if 'items' not in criterion:
                    result.add_error(f"Criteria item {i} missing 'items' field")
                elif not isinstance(criterion['items'], list):
                    result.add_error(f"Criteria item {i} 'items' must be a list")
                elif len(criterion['items']) == 0:
                    result.add_warning(f"Criteria section '{criterion.get('section', i)}' has no items")


def validate_output_schema(data: dict, result: ValidationResult):
    """Validate output schema definition."""
    if 'output_schema' not in data:
        result.add_error("Missing 'output_schema' field")
        return
    
    schema = data['output_schema']
    
    # Handle legacy string format
    if isinstance(schema, str):
        if len(schema.strip()) < 10:
            result.add_error("'output_schema' string is too short")
        else:
            result.add_info("Using legacy string format for output_schema")
            # Try to validate as JSON
            try:
                json.loads(schema.replace('\n', ''))
                result.add_info("Output schema JSON is valid")
            except json.JSONDecodeError:
                result.add_warning("Output schema is not valid JSON (may use placeholders)")
        return
    
    if not isinstance(schema, dict):
        result.add_error("'output_schema' must be a dict or string")
        return
    
    # Validate structured schema
    if 'root_key' not in schema:
        result.add_warning("Missing 'root_key' - using 'audit' as default")
    
    # Validate fields
    if 'fields' not in schema:
        result.add_warning("No 'fields' defined in output_schema")
    else:
        if not isinstance(schema['fields'], list):
            result.add_error("'output_schema.fields' must be a list")
        else:
            _validate_field_definitions(schema['fields'], "fields", result)
    
    # Validate issues schema
    if 'issues_key' in schema:
        if 'issues_schema' not in schema:
            result.add_warning("'issues_key' defined but no 'issues_schema'")
        else:
            if not isinstance(schema['issues_schema'], list):
                result.add_error("'output_schema.issues_schema' must be a list")
            else:
                _validate_field_definitions(schema['issues_schema'], "issues_schema", result)


def _validate_field_definitions(fields: List[dict], context: str, result: ValidationResult):
    """Validate a list of field definitions."""
    seen_names = set()
    
    for i, field in enumerate(fields):
        if not isinstance(field, dict):
            result.add_error(f"{context}[{i}] must be a dictionary")
            continue
        
        # Name required
        if 'name' not in field:
            result.add_error(f"{context}[{i}] missing 'name'")
            continue
        
        name = field['name']
        
        # Check for duplicates
        if name in seen_names:
            result.add_error(f"{context}: Duplicate field name '{name}'")
        seen_names.add(name)
        
        # Type validation
        if 'type' not in field:
            result.add_warning(f"{context}.{name}: Missing 'type', assuming 'string'")
        else:
            field_type = field['type']
            if field_type not in VALID_FIELD_TYPES:
                result.add_error(
                    f"{context}.{name}: Invalid type '{field_type}'. "
                    f"Valid: {', '.join(VALID_FIELD_TYPES)}"
                )
            
            # Type-specific validation
            if field_type == 'integer' and 'range' in field:
                range_val = field['range']
                if not isinstance(range_val, list) or len(range_val) != 2:
                    result.add_error(f"{context}.{name}: 'range' must be [min, max]")
                elif range_val[0] > range_val[1]:
                    result.add_error(f"{context}.{name}: Invalid range [{range_val[0]}, {range_val[1]}]")
            
            if field_type == 'enum':
                if 'values' not in field:
                    result.add_error(f"{context}.{name}: Enum type requires 'values' list")
                elif not isinstance(field['values'], list):
                    result.add_error(f"{context}.{name}: 'values' must be a list")
                elif len(field['values']) == 0:
                    result.add_error(f"{context}.{name}: 'values' list is empty")


def validate_scoring(data: dict, result: ValidationResult):
    """Validate scoring configuration."""
    if 'scoring' not in data:
        result.add_warning("No 'scoring' configuration - using defaults")
        return
    
    scoring = data['scoring']
    
    # Handle legacy list format
    if isinstance(scoring, list):
        result.add_info("Using legacy list format for scoring")
        return
    
    if not isinstance(scoring, dict):
        result.add_error("'scoring' must be a dict or list")
        return
    
    # Validate primary metric
    if 'primary_metric' not in scoring:
        result.add_warning("Missing 'primary_metric' - using 'overall_score'")
    
    # Validate prefix format
    if 'prefix_format' in scoring:
        prefix_format = scoring['prefix_format']
        if not isinstance(prefix_format, str):
            result.add_error("'scoring.prefix_format' must be a string")
        elif '{' in prefix_format:
            # Try to validate format string
            try:
                prefix_format.format(score=50)
            except KeyError as e:
                result.add_warning(f"Prefix format uses unknown key: {e}")
    
    # Validate save condition
    if 'save_condition' in scoring:
        condition = scoring['save_condition']
        if condition not in VALID_SAVE_CONDITIONS:
            if not (condition.startswith('score_below:') or condition.startswith('score_above:')):
                result.add_error(f"Invalid save_condition: '{condition}'")
            else:
                # Validate threshold is a number
                try:
                    threshold = int(condition.split(':')[1])
                except (ValueError, IndexError):
                    result.add_error(f"Invalid threshold in save_condition: '{condition}'")
    
    # Validate buckets
    if 'buckets' in scoring:
        buckets = scoring['buckets']
        if not isinstance(buckets, list):
            result.add_error("'scoring.buckets' must be a list")
        else:
            for i, bucket in enumerate(buckets):
                if not isinstance(bucket, dict):
                    result.add_error(f"Bucket {i} must be a dictionary")
                    continue
                
                if 'range' not in bucket:
                    result.add_error(f"Bucket {i} missing 'range'")
                elif not isinstance(bucket['range'], list) or len(bucket['range']) != 2:
                    result.add_error(f"Bucket {i} 'range' must be [min, max]")
                
                if 'label' not in bucket:
                    result.add_error(f"Bucket {i} missing 'label'")


def validate_full(yaml_path: str, verbose: bool = False) -> ValidationResult:
    """
    Run full validation on an audit definition file.
    
    Args:
        yaml_path: Path to YAML file
        verbose: Include info messages
        
    Returns:
        ValidationResult with all findings
    """
    # Parse YAML
    data, result = validate_yaml_structure(yaml_path)
    if data is None:
        return result
    
    # Run all validators
    validate_metadata(data, result)
    validate_prompt_components(data, result)
    validate_output_schema(data, result)
    validate_scoring(data, result)
    
    return result


# ============================================================================
# LLM TESTING
# ============================================================================

def test_with_llm(yaml_path: str, sample_content: str = None) -> Tuple[bool, str]:
    """
    Test the audit definition with a sample LLM request.
    
    Args:
        yaml_path: Path to YAML file
        sample_content: Optional sample content to analyze
        
    Returns:
        Tuple of (success, message)
    """
    try:
        from core.audit_builder import load_custom_audit, build_system_prompt, validate_audit_result
        from core import config
    except ImportError as e:
        return False, f"Import error: {e}"
    
    # Load definition
    try:
        definition = load_custom_audit(yaml_path)
    except Exception as e:
        return False, f"Failed to load audit: {e}"
    
    # Build prompt
    system_prompt = build_system_prompt(definition)
    if not system_prompt:
        return False, "Failed to build system prompt"
    
    # Use sample content
    if sample_content is None:
        sample_content = """
        Welcome to Example Company
        
        We are a leading provider of innovative solutions for businesses.
        Our team of experts has over 20 years of experience in the industry.
        
        Contact us today to learn more about our services!
        
        © 2026 Example Company. All rights reserved.
        """
    
    print(f"\nTesting audit with LLM...")
    print(f"  Provider: {config.get_provider()}")
    print(f"  Model: {config.get_model_name()}")
    
    # Send to LLM
    try:
        client = config.get_client()
        provider = config.get_provider()
        model = config.get_model_name()
        
        if provider == "ANTHROPIC":
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": f"CONTENT: {sample_content}"}]
            )
            response_text = response.content[0].text
        
        elif provider == "OPENAI":
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"CONTENT: {sample_content}"}
                ],
                response_format={"type": "json_object"}
            )
            response_text = response.choices[0].message.content
        
        else:  # MISTRAL
            response = client.chat.complete(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"CONTENT: {sample_content}"}
                ],
                response_format={"type": "json_object"}
            )
            response_text = response.choices[0].message.content
        
    except Exception as e:
        return False, f"LLM API error: {e}"
    
    # Parse JSON response
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        return False, f"LLM did not return valid JSON: {e}\nResponse: {response_text[:500]}"
    
    # Validate response schema
    validation_errors = validate_audit_result(definition, result)
    
    if validation_errors:
        error_msg = "Response schema validation errors:\n" + "\n".join(validation_errors)
        return False, error_msg
    
    # Test prefix extraction
    try:
        from core.audit_builder import get_score_prefix
        prefix = get_score_prefix(definition, result)
        print(f"  ✓ Score prefix extracted: {prefix}")
    except Exception as e:
        return False, f"Prefix extraction failed: {e}"
    
    return True, "LLM test passed!"


# ============================================================================
# CLI
# ============================================================================

def validate_all(prompts_dir: str, verbose: bool = False) -> int:
    """Validate all YAML files in the prompts directory."""
    if not os.path.isdir(prompts_dir):
        print(f"Directory not found: {prompts_dir}")
        return 1
    
    yaml_files = [
        f for f in os.listdir(prompts_dir)
        if f.endswith('.yaml') and not f.startswith('.') and f != 'schema.yaml'
    ]
    
    if not yaml_files:
        print(f"No YAML files found in {prompts_dir}")
        return 0
    
    print(f"\nValidating {len(yaml_files)} audit definitions in {prompts_dir}...")
    print("=" * 60)
    
    total_errors = 0
    results = []
    
    for filename in sorted(yaml_files):
        yaml_path = os.path.join(prompts_dir, filename)
        result = validate_full(yaml_path, verbose=verbose)
        results.append((filename, result))
        total_errors += len(result.errors)
    
    # Print summary
    print("\nValidation Summary:")
    print("-" * 60)
    
    for filename, result in results:
        status = "✅" if result.is_valid else "❌"
        error_count = len(result.errors)
        warning_count = len(result.warnings)
        
        details = []
        if error_count > 0:
            details.append(f"{error_count} error(s)")
        if warning_count > 0:
            details.append(f"{warning_count} warning(s)")
        
        detail_str = f" [{', '.join(details)}]" if details else ""
        print(f"  {status} {filename}{detail_str}")
    
    print("-" * 60)
    if total_errors == 0:
        print(f"✅ All {len(yaml_files)} files passed validation!")
    else:
        print(f"❌ {total_errors} total error(s) found")
    
    return 0 if total_errors == 0 else 1


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Validate YAML audit definitions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Validate a single file
  python validate_audit.py prompts/my_audit.yaml
  
  # Validate with verbose output
  python validate_audit.py prompts/my_audit.yaml -v
  
  # Test with LLM
  python validate_audit.py prompts/my_audit.yaml --test
  
  # Validate all audits
  python validate_audit.py --all
        '''
    )
    
    parser.add_argument(
        'yaml_file',
        nargs='?',
        help='Path to YAML audit definition file'
    )
    
    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Validate all YAML files in prompts directory'
    )
    
    parser.add_argument(
        '--test', '-t',
        action='store_true',
        help='Test the audit with a sample LLM request'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Include info messages in output'
    )
    
    parser.add_argument(
        '--prompts-dir',
        type=str,
        help='Directory for prompt files (default: ./prompts/)'
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    # Determine prompts directory
    if args.prompts_dir:
        prompts_dir = args.prompts_dir
    else:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        prompts_dir = os.path.join(current_dir, "prompts")
    
    # Validate all
    if args.all:
        sys.exit(validate_all(prompts_dir, verbose=args.verbose))
    
    # Validate single file
    if not args.yaml_file:
        print("Error: Please provide a YAML file path or use --all")
        print("Usage: python validate_audit.py prompts/my_audit.yaml")
        sys.exit(1)
    
    yaml_path = args.yaml_file
    if not os.path.isabs(yaml_path):
        # Try relative to prompts dir
        if not os.path.exists(yaml_path):
            alt_path = os.path.join(prompts_dir, yaml_path)
            if os.path.exists(alt_path):
                yaml_path = alt_path
    
    print(f"\nValidating: {yaml_path}")
    print("=" * 60)
    
    result = validate_full(yaml_path, verbose=args.verbose)
    result.print_report(verbose=args.verbose)
    
    # Run LLM test if requested
    if args.test and result.is_valid:
        success, message = test_with_llm(yaml_path)
        if success:
            print(f"\n✅ {message}")
        else:
            print(f"\n❌ LLM Test Failed:\n   {message}")
            sys.exit(1)
    
    sys.exit(0 if result.is_valid else 1)


if __name__ == "__main__":
    main()
