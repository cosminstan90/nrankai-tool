"""
Audit Builder Module

Provides functionality to load, validate, and process custom audit definitions from YAML files.
This enables users to create new audit types without writing Python code.

Author: Refactored by Claude
Created: 2026-02-12
"""

import os
import re
import yaml
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
from pathlib import Path

from logger import get_logger

# Initialize module logger
logger = get_logger(__name__)


# ============================================================================
# EXCEPTIONS
# ============================================================================

class AuditBuilderError(Exception):
    """Base exception for audit builder errors."""
    pass


class AuditSchemaError(AuditBuilderError):
    """Raised when audit YAML has invalid schema."""
    pass


class AuditNotFoundError(AuditBuilderError):
    """Raised when audit definition file is not found."""
    pass


class AuditValidationError(AuditBuilderError):
    """Raised when audit data fails validation."""
    pass


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class FieldDefinition:
    """Definition for a single field in the output schema."""
    name: str
    type: str  # integer, string, enum, boolean, array
    description: str = ""
    range: Optional[Tuple[int, int]] = None  # For integer types
    values: Optional[List[str]] = None  # For enum types
    default: Any = None
    required: bool = True


@dataclass
class ScoreBucket:
    """Score bucket for categorization."""
    min_value: int
    max_value: int
    label: str


@dataclass
class ScoringConfig:
    """Scoring configuration for an audit."""
    primary_metric: str
    prefix_format: str = "{score:03d}"
    save_condition: str = "always"  # always, has_issues, score_below:N, score_above:N
    buckets: List[ScoreBucket] = field(default_factory=list)
    # Optional additional fields for prefix
    secondary_field: Optional[str] = None


@dataclass
class AuditDefinition:
    """Complete definition of a custom audit type."""
    # Metadata
    name: str
    description: str
    version: str
    author: str = "Unknown"
    category: str = "generic"  # generic, compliance, brand, technical
    
    # Prompt components
    role: str = ""
    task: str = ""
    criteria: List[Dict[str, Any]] = field(default_factory=list)
    
    # Output schema
    root_key: str = "audit"
    fields: List[FieldDefinition] = field(default_factory=list)
    issues_key: Optional[str] = None
    issues_schema: List[FieldDefinition] = field(default_factory=list)
    
    # Scoring
    scoring: Optional[ScoringConfig] = None
    
    # Additional
    language_hint: str = "auto"
    output_schema_raw: str = ""  # Raw JSON schema string
    
    # Source
    source_file: Optional[str] = None
    
    def get_audit_type(self) -> str:
        """Get the audit type identifier from the source filename."""
        if self.source_file:
            return Path(self.source_file).stem.upper()
        return self.name.upper().replace(" ", "_")


# ============================================================================
# SCHEMA PARSING
# ============================================================================

def _parse_field(field_data: dict) -> FieldDefinition:
    """Parse a single field definition from YAML."""
    name = field_data.get('name', '')
    field_type = field_data.get('type', 'string')
    
    # Handle range for integers
    range_val = None
    if 'range' in field_data and isinstance(field_data['range'], list):
        if len(field_data['range']) == 2:
            range_val = tuple(field_data['range'])
    
    # Handle enum values
    values = field_data.get('values')
    if values and not isinstance(values, list):
        values = [values]
    
    return FieldDefinition(
        name=name,
        type=field_type,
        description=field_data.get('description', ''),
        range=range_val,
        values=values,
        default=field_data.get('default'),
        required=field_data.get('required', True)
    )


def _parse_scoring(scoring_data: dict) -> ScoringConfig:
    """Parse scoring configuration from YAML."""
    buckets = []
    if 'buckets' in scoring_data:
        for bucket in scoring_data['buckets']:
            if 'range' in bucket and 'label' in bucket:
                range_val = bucket['range']
                if isinstance(range_val, list) and len(range_val) == 2:
                    buckets.append(ScoreBucket(
                        min_value=range_val[0],
                        max_value=range_val[1],
                        label=bucket['label']
                    ))
    
    return ScoringConfig(
        primary_metric=scoring_data.get('primary_metric', 'overall_score'),
        prefix_format=scoring_data.get('prefix_format', '{score:03d}'),
        save_condition=scoring_data.get('save_condition', 'always'),
        buckets=buckets,
        secondary_field=scoring_data.get('secondary_field')
    )


def _parse_output_schema(schema_data: dict) -> Tuple[str, List[FieldDefinition], Optional[str], List[FieldDefinition]]:
    """
    Parse output schema definition from YAML.
    
    Returns:
        Tuple of (root_key, fields, issues_key, issues_schema)
    """
    root_key = schema_data.get('root_key', 'audit')
    
    # Parse main fields
    fields = []
    if 'fields' in schema_data:
        for field_data in schema_data['fields']:
            fields.append(_parse_field(field_data))
    
    # Parse issues schema
    issues_key = schema_data.get('issues_key')
    issues_schema = []
    if 'issues_schema' in schema_data:
        for field_data in schema_data['issues_schema']:
            issues_schema.append(_parse_field(field_data))
    
    return root_key, fields, issues_key, issues_schema


# ============================================================================
# AUDIT DEFINITION LOADING
# ============================================================================

def load_custom_audit(yaml_path: str) -> AuditDefinition:
    """
    Load and parse a custom audit definition from a YAML file.
    
    Args:
        yaml_path: Path to the YAML audit definition file
        
    Returns:
        Parsed AuditDefinition object
        
    Raises:
        AuditNotFoundError: If file doesn't exist
        AuditSchemaError: If YAML is malformed or missing required fields
    """
    if not os.path.isfile(yaml_path):
        raise AuditNotFoundError(f"Audit definition file not found: {yaml_path}")
    
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise AuditSchemaError(f"Failed to parse YAML file {yaml_path}: {e}")
    
    if not data:
        raise AuditSchemaError(f"Empty YAML file: {yaml_path}")
    
    # Validate required fields
    required_fields = ['name', 'description', 'version']
    missing = [f for f in required_fields if f not in data or not data[f]]
    if missing:
        raise AuditSchemaError(
            f"Missing required fields in {yaml_path}: {', '.join(missing)}"
        )
    
    # Parse output schema
    root_key = "audit"
    fields = []
    issues_key = None
    issues_schema = []
    output_schema_raw = ""
    
    if 'output_schema' in data:
        if isinstance(data['output_schema'], dict):
            root_key, fields, issues_key, issues_schema = _parse_output_schema(data['output_schema'])
        elif isinstance(data['output_schema'], str):
            # Legacy format: raw JSON string
            output_schema_raw = data['output_schema']
            # Try to extract root_key from JSON string
            try:
                import json
                schema_dict = json.loads(output_schema_raw.replace('\n', ''))
                if schema_dict:
                    root_key = list(schema_dict.keys())[0]
            except (json.JSONDecodeError, IndexError):
                pass
    
    # Parse scoring configuration
    scoring = None
    if 'scoring' in data:
        scoring_data = data['scoring']
        if isinstance(scoring_data, dict):
            scoring = _parse_scoring(scoring_data)
        elif isinstance(scoring_data, list):
            # Legacy format: list of metric definitions
            # Convert to new format with defaults
            scoring = ScoringConfig(
                primary_metric='overall_score',
                prefix_format='{score:03d}',
                save_condition='always',
                buckets=[]
            )
    
    # Parse criteria
    criteria = data.get('criteria', [])
    if not isinstance(criteria, list):
        criteria = []
    
    return AuditDefinition(
        name=data['name'],
        description=data['description'],
        version=data['version'],
        author=data.get('author', 'Unknown'),
        category=data.get('category', 'generic'),
        role=data.get('role', ''),
        task=data.get('task', ''),
        criteria=criteria,
        root_key=root_key,
        fields=fields,
        issues_key=issues_key,
        issues_schema=issues_schema,
        scoring=scoring,
        language_hint=data.get('language_hint', 'auto'),
        output_schema_raw=output_schema_raw,
        source_file=yaml_path
    )


def is_custom_audit(yaml_path: str) -> bool:
    """
    Check if a YAML file is a custom audit with extended schema.
    
    Custom audits have structured output_schema (dict) and/or scoring config.
    """
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        if not data:
            return False
        
        # Check for new schema indicators
        has_structured_output = (
            isinstance(data.get('output_schema'), dict) and
            'root_key' in data.get('output_schema', {})
        )
        
        has_scoring_config = (
            isinstance(data.get('scoring'), dict) and
            'primary_metric' in data.get('scoring', {})
        )
        
        return has_structured_output or has_scoring_config
        
    except Exception:
        return False


# ============================================================================
# PROMPT BUILDING
# ============================================================================

def build_system_prompt(definition: AuditDefinition) -> str:
    """
    Build the system prompt from an audit definition.
    
    Args:
        definition: Parsed audit definition
        
    Returns:
        Assembled system message string
    """
    parts = []
    
    # Add role
    if definition.role:
        parts.append(definition.role.strip())
    
    # Add task
    if definition.task:
        parts.append(definition.task.strip())
    
    # Add criteria sections
    for criterion in definition.criteria:
        if isinstance(criterion, dict):
            section_name = criterion.get('section', '')
            items = criterion.get('items', [])
            
            if section_name:
                parts.append(f"\n{section_name}")
            
            if items:
                for item in items:
                    parts.append(item)
    
    # Add output format section
    output_format = build_json_schema(definition)
    if output_format:
        parts.append("\nOUTPUT FORMAT (Strict JSON):")
        parts.append("Return ONLY the following structure:")
        parts.append(output_format)
    
    return "\n".join(parts)


def build_json_schema(definition: AuditDefinition) -> str:
    """
    Generate the JSON schema string from the output_schema definition.
    
    Args:
        definition: Parsed audit definition
        
    Returns:
        JSON schema string for the LLM
    """
    # If we have raw schema (legacy format), use it
    if definition.output_schema_raw:
        return definition.output_schema_raw.strip()
    
    # Build from structured definition
    if not definition.fields and not definition.issues_key:
        return ""
    
    # Build the schema dynamically
    lines = ["{"]
    
    # Add root key
    root_key = definition.root_key
    lines.append(f'"{root_key}": {{')
    
    # Add fields
    field_lines = []
    for field_def in definition.fields:
        type_hint = _get_type_hint(field_def)
        field_lines.append(f'"{field_def.name}": {type_hint}')
    
    lines.append(",\n".join(field_lines))
    lines.append("},")
    
    # Add issues array if defined
    if definition.issues_key:
        lines.append(f'"{definition.issues_key}": [')
        lines.append("{")
        
        issue_lines = []
        for field_def in definition.issues_schema:
            type_hint = _get_type_hint(field_def)
            issue_lines.append(f'"{field_def.name}": {type_hint}')
        
        lines.append(",\n".join(issue_lines))
        lines.append("}")
        lines.append("]")
    
    lines.append("}")
    
    return "\n".join(lines)


def _get_type_hint(field_def: FieldDefinition) -> str:
    """Get JSON type hint string for a field definition."""
    if field_def.type == 'integer':
        if field_def.range:
            return f"integer ({field_def.range[0]}-{field_def.range[1]})"
        return "integer"
    elif field_def.type == 'enum':
        if field_def.values:
            return '|'.join(f'"{v}"' for v in field_def.values)
        return '"string"'
    elif field_def.type == 'boolean':
        return "boolean"
    elif field_def.type == 'array':
        return '["string"]'
    else:
        return '"string"'


# ============================================================================
# SCORE EXTRACTION
# ============================================================================

def get_score_prefix(definition: AuditDefinition, result: dict) -> str:
    """
    Extract the filename prefix from audit results based on scoring config.
    
    Args:
        definition: Audit definition with scoring config
        result: The audit result dictionary
        
    Returns:
        Formatted prefix string for filename
    """
    if not definition.scoring:
        return "000"
    
    scoring = definition.scoring
    root_key = definition.root_key
    
    # Get the audit data from result
    audit_data = result.get(root_key, result)
    
    # Extract primary metric value
    primary_value = _extract_metric_value(audit_data, scoring.primary_metric, result)
    
    # Handle issues-based scoring (count)
    if definition.issues_key and scoring.primary_metric == 'issues_count':
        issues = result.get(definition.issues_key, [])
        primary_value = len(issues) if isinstance(issues, list) else 0
    
    # Format the prefix
    try:
        # Check if there's a secondary field for the prefix
        if scoring.secondary_field:
            secondary_value = _extract_metric_value(audit_data, scoring.secondary_field, result)
            if secondary_value is None:
                secondary_value = 'unknown'
            
            # Format: {score}_{label}
            if '{' in scoring.prefix_format:
                prefix = scoring.prefix_format.format(
                    score=primary_value or 0,
                    **{scoring.primary_metric: primary_value or 0},
                    **{scoring.secondary_field: secondary_value}
                )
            else:
                prefix = f"{primary_value or 0:03d}_{secondary_value}"
        else:
            # Simple score prefix
            if '{' in scoring.prefix_format:
                prefix = scoring.prefix_format.format(
                    score=primary_value or 0,
                    **{scoring.primary_metric: primary_value or 0}
                )
            else:
                prefix = f"{primary_value or 0:03d}"
                
    except (ValueError, KeyError) as e:
        logger.warning(f"Error formatting prefix: {e}, using default")
        prefix = f"{primary_value or 0:03d}"
    
    return prefix


def _extract_metric_value(audit_data: dict, metric_path: str, full_result: dict) -> Any:
    """
    Extract a metric value from nested audit data.
    
    Supports dot notation for nested access: "summary.overall_score"
    """
    if not metric_path:
        return None
    
    # Try direct access first
    if metric_path in audit_data:
        return audit_data[metric_path]
    
    # Try dot notation
    if '.' in metric_path:
        parts = metric_path.split('.')
        current = audit_data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = None
                break
        if current is not None:
            return current
    
    # Try in full result
    if metric_path in full_result:
        return full_result[metric_path]
    
    return None


def get_save_condition(definition: AuditDefinition, result: dict) -> bool:
    """
    Determine if a result should be saved based on the save_condition config.
    
    Args:
        definition: Audit definition with scoring config
        result: The audit result dictionary
        
    Returns:
        True if result should be saved
    """
    if not definition.scoring:
        return True
    
    condition = definition.scoring.save_condition
    
    if condition == "always":
        return True
    
    elif condition == "has_issues":
        # Check if there are any issues
        if definition.issues_key:
            issues = result.get(definition.issues_key, [])
            return isinstance(issues, list) and len(issues) > 0
        return True
    
    elif condition.startswith("score_below:"):
        try:
            threshold = int(condition.split(":")[1])
            score = _get_score_value(definition, result)
            return score < threshold
        except (ValueError, IndexError):
            return True
    
    elif condition.startswith("score_above:"):
        try:
            threshold = int(condition.split(":")[1])
            score = _get_score_value(definition, result)
            return score > threshold
        except (ValueError, IndexError):
            return True
    
    return True


def _get_score_value(definition: AuditDefinition, result: dict) -> int:
    """Get the primary score value from result."""
    if not definition.scoring:
        return 0
    
    root_key = definition.root_key
    audit_data = result.get(root_key, result)
    
    value = _extract_metric_value(audit_data, definition.scoring.primary_metric, result)
    
    if isinstance(value, (int, float)):
        return int(value)
    
    return 0


def get_score_buckets(definition: AuditDefinition) -> List[Tuple[int, int, str]]:
    """
    Get bucket configuration for determine_score.py.
    
    Returns:
        List of (min, max, label) tuples
    """
    if not definition.scoring or not definition.scoring.buckets:
        return [(0, 100, 'default')]
    
    return [
        (bucket.min_value, bucket.max_value, bucket.label)
        for bucket in definition.scoring.buckets
    ]


# ============================================================================
# AUDIT REGISTRY
# ============================================================================

class AuditRegistry:
    """
    Registry of available audit types, both built-in and custom.
    
    Provides unified access to all audit definitions with caching.
    """
    
    def __init__(self, prompts_dir: str = None):
        """
        Initialize the registry.
        
        Args:
            prompts_dir: Path to prompts directory (default: ./prompts/)
        """
        if prompts_dir is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            prompts_dir = os.path.join(current_dir, "prompts")
        
        self.prompts_dir = prompts_dir
        self._cache: Dict[str, AuditDefinition] = {}
        self._custom_audits: Dict[str, str] = {}  # type -> yaml_path
        
        # Scan for custom audits
        self._scan_custom_audits()
    
    def _scan_custom_audits(self):
        """Scan prompts directory for custom audit definitions."""
        if not os.path.isdir(self.prompts_dir):
            return
        
        # Skip files that are documentation/templates, not actual audits
        skip_files = {'schema.yaml', 'README.md'}
        
        for filename in os.listdir(self.prompts_dir):
            if filename.endswith('.yaml') and not filename.startswith('.'):
                # Skip schema documentation file
                if filename in skip_files:
                    continue
                    
                yaml_path = os.path.join(self.prompts_dir, filename)
                audit_type = filename[:-5].upper()
                
                if is_custom_audit(yaml_path):
                    self._custom_audits[audit_type] = yaml_path
                    logger.debug(f"Found custom audit: {audit_type}")
    
    def is_custom_audit(self, audit_type: str) -> bool:
        """Check if an audit type is a custom audit."""
        return audit_type.upper() in self._custom_audits
    
    def get_definition(self, audit_type: str) -> Optional[AuditDefinition]:
        """
        Get the audit definition for a type.
        
        Returns None if not a custom audit (use prompt_loader instead).
        """
        audit_type = audit_type.upper()
        
        if audit_type in self._cache:
            return self._cache[audit_type]
        
        if audit_type not in self._custom_audits:
            return None
        
        yaml_path = self._custom_audits[audit_type]
        definition = load_custom_audit(yaml_path)
        self._cache[audit_type] = definition
        
        return definition
    
    def list_custom_audits(self) -> List[Dict[str, str]]:
        """List all available custom audits with metadata."""
        audits = []
        
        for audit_type, yaml_path in self._custom_audits.items():
            try:
                definition = self.get_definition(audit_type)
                if definition:
                    audits.append({
                        'type': audit_type,
                        'name': definition.name,
                        'description': definition.description,
                        'category': definition.category,
                        'is_custom': True
                    })
            except Exception as e:
                logger.warning(f"Failed to load audit {audit_type}: {e}")
        
        return audits
    
    def clear_cache(self):
        """Clear the definition cache."""
        self._cache.clear()
    
    def reload(self):
        """Reload the registry, rescanning for audits."""
        self._cache.clear()
        self._custom_audits.clear()
        self._scan_custom_audits()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

# Singleton registry instance
_registry_instance: Optional[AuditRegistry] = None


def get_registry() -> AuditRegistry:
    """Get the singleton AuditRegistry instance."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = AuditRegistry()
    return _registry_instance


def is_custom_audit_type(audit_type: str) -> bool:
    """Check if an audit type is a custom audit."""
    return get_registry().is_custom_audit(audit_type)


def get_custom_audit_definition(audit_type: str) -> Optional[AuditDefinition]:
    """Get the definition for a custom audit type."""
    return get_registry().get_definition(audit_type)


def get_custom_prefix(audit_type: str, result: dict) -> str:
    """Get the filename prefix for a custom audit result."""
    definition = get_custom_audit_definition(audit_type)
    if definition:
        return get_score_prefix(definition, result)
    return "000"


def should_save_custom_result(audit_type: str, result: dict) -> bool:
    """Check if a custom audit result should be saved."""
    definition = get_custom_audit_definition(audit_type)
    if definition:
        return get_save_condition(definition, result)
    return True


def get_custom_buckets(audit_type: str) -> List[Tuple[int, int, str]]:
    """Get score buckets for a custom audit type."""
    definition = get_custom_audit_definition(audit_type)
    if definition:
        return get_score_buckets(definition)
    return [(0, 100, 'default')]


# ============================================================================
# VALIDATION
# ============================================================================

def validate_audit_result(definition: AuditDefinition, result: dict) -> List[str]:
    """
    Validate an audit result against the definition schema.
    
    Args:
        definition: Audit definition with schema
        result: The audit result to validate
        
    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []
    
    # Check root key
    if definition.root_key and definition.root_key not in result:
        errors.append(f"Missing root key: {definition.root_key}")
        return errors
    
    audit_data = result.get(definition.root_key, result)
    
    # Validate fields
    for field_def in definition.fields:
        if field_def.required and field_def.name not in audit_data:
            errors.append(f"Missing required field: {field_def.name}")
            continue
        
        if field_def.name in audit_data:
            value = audit_data[field_def.name]
            field_errors = _validate_field_value(field_def, value)
            errors.extend(field_errors)
    
    # Validate issues array if defined
    if definition.issues_key:
        if definition.issues_key not in result:
            errors.append(f"Missing issues array: {definition.issues_key}")
        else:
            issues = result[definition.issues_key]
            if not isinstance(issues, list):
                errors.append(f"Issues must be an array: {definition.issues_key}")
            else:
                for i, issue in enumerate(issues):
                    for field_def in definition.issues_schema:
                        if field_def.required and field_def.name not in issue:
                            errors.append(f"Issue {i} missing field: {field_def.name}")
                        elif field_def.name in issue:
                            field_errors = _validate_field_value(
                                field_def, issue[field_def.name],
                                prefix=f"Issue {i}."
                            )
                            errors.extend(field_errors)
    
    return errors


def _validate_field_value(field_def: FieldDefinition, value: Any, prefix: str = "") -> List[str]:
    """Validate a single field value against its definition."""
    errors = []
    field_name = f"{prefix}{field_def.name}"
    
    if field_def.type == 'integer':
        if not isinstance(value, (int, float)):
            errors.append(f"{field_name} must be an integer, got {type(value).__name__}")
        elif field_def.range:
            if not (field_def.range[0] <= value <= field_def.range[1]):
                errors.append(
                    f"{field_name} must be in range {field_def.range}, got {value}"
                )
    
    elif field_def.type == 'enum':
        if field_def.values and value not in field_def.values:
            errors.append(
                f"{field_name} must be one of {field_def.values}, got '{value}'"
            )
    
    elif field_def.type == 'boolean':
        if not isinstance(value, bool):
            errors.append(f"{field_name} must be a boolean, got {type(value).__name__}")
    
    elif field_def.type == 'array':
        if not isinstance(value, list):
            errors.append(f"{field_name} must be an array, got {type(value).__name__}")
    
    elif field_def.type == 'string':
        if not isinstance(value, str):
            errors.append(f"{field_name} must be a string, got {type(value).__name__}")
    
    return errors
