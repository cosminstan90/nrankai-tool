#!/usr/bin/env python3
"""
Interactive Audit Builder CLI

Creates new audit YAML definitions through an interactive wizard.
Users can define custom audits without writing Python code.

Usage:
    python create_audit.py
    python create_audit.py --output my_audit.yaml
    python create_audit.py --template basic

Author: Website LLM Analyzer Team
Created: 2026-02-12
"""

import argparse
import os
import re
import sys
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logger import get_logger, setup_logging

# Initialize logger
logger = get_logger(__name__)


# ============================================================================
# TEMPLATES
# ============================================================================

TEMPLATES = {
    'basic': {
        'name': 'Basic Audit Template',
        'description': 'Simple audit with score and issues',
        'category': 'generic',
        'fields': [
            {'name': 'overall_score', 'type': 'integer', 'range': [0, 100]},
            {'name': 'summary', 'type': 'string'}
        ],
        'issues_schema': [
            {'name': 'fragment', 'type': 'string'},
            {'name': 'severity', 'type': 'enum', 'values': ['critical', 'major', 'minor']},
            {'name': 'description', 'type': 'string'},
            {'name': 'recommendation', 'type': 'string'}
        ],
        'buckets': [
            {'range': [0, 49], 'label': 'poor'},
            {'range': [50, 69], 'label': 'needs_work'},
            {'range': [70, 84], 'label': 'good'},
            {'range': [85, 100], 'label': 'excellent'}
        ]
    },
    'compliance': {
        'name': 'Compliance Audit Template',
        'description': 'Compliance check with violations tracking',
        'category': 'compliance',
        'fields': [
            {'name': 'compliance_score', 'type': 'integer', 'range': [0, 100]},
            {'name': 'risk_level', 'type': 'enum', 'values': ['low', 'medium', 'high', 'critical']},
            {'name': 'audit_summary', 'type': 'string'}
        ],
        'issues_schema': [
            {'name': 'fragment', 'type': 'string'},
            {'name': 'violation_type', 'type': 'string'},
            {'name': 'severity', 'type': 'enum', 'values': ['critical', 'major', 'minor']},
            {'name': 'regulation_ref', 'type': 'string'},
            {'name': 'remediation', 'type': 'string'}
        ],
        'buckets': [
            {'range': [0, 49], 'label': 'non_compliant'},
            {'range': [50, 69], 'label': 'partial'},
            {'range': [70, 84], 'label': 'mostly_compliant'},
            {'range': [85, 100], 'label': 'compliant'}
        ]
    },
    'technical': {
        'name': 'Technical Audit Template',
        'description': 'Technical analysis with detailed metrics',
        'category': 'technical',
        'fields': [
            {'name': 'technical_score', 'type': 'integer', 'range': [0, 100]},
            {'name': 'pass_rate', 'type': 'integer', 'range': [0, 100]},
            {'name': 'critical_count', 'type': 'integer'},
            {'name': 'recommendation_summary', 'type': 'string'}
        ],
        'issues_schema': [
            {'name': 'check_name', 'type': 'string'},
            {'name': 'status', 'type': 'enum', 'values': ['pass', 'fail', 'warning', 'info']},
            {'name': 'details', 'type': 'string'},
            {'name': 'fix', 'type': 'string'}
        ],
        'buckets': [
            {'range': [0, 49], 'label': 'failing'},
            {'range': [50, 69], 'label': 'needs_fixes'},
            {'range': [70, 84], 'label': 'mostly_passing'},
            {'range': [85, 100], 'label': 'passing'}
        ]
    }
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def print_header(text: str):
    """Print a formatted header."""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print('='*60)


def print_section(text: str):
    """Print a section header."""
    print(f"\n--- {text} ---")


def get_input(prompt: str, default: str = None, required: bool = True) -> str:
    """Get user input with optional default."""
    if default:
        display_prompt = f"{prompt} [{default}]: "
    else:
        display_prompt = f"{prompt}: "
    
    while True:
        value = input(display_prompt).strip()
        
        if not value:
            if default:
                return default
            elif required:
                print("  This field is required.")
                continue
            else:
                return ""
        
        return value


def get_yes_no(prompt: str, default: bool = True) -> bool:
    """Get yes/no input."""
    default_str = "Y/n" if default else "y/N"
    response = input(f"{prompt} [{default_str}]: ").strip().lower()
    
    if not response:
        return default
    
    return response in ('y', 'yes', '1', 'true')


def get_multiline_input(prompt: str) -> str:
    """Get multi-line input until empty line."""
    print(f"{prompt}")
    print("  (Enter text, empty line to finish)")
    
    lines = []
    while True:
        line = input("  > ")
        if not line:
            break
        lines.append(line)
    
    return "\n".join(lines)


def get_list_input(prompt: str, min_items: int = 1) -> List[str]:
    """Get a list of items from user."""
    print(f"{prompt}")
    print("  (Enter items one per line, empty line to finish)")
    
    items = []
    while True:
        item = input(f"  [{len(items)+1}] ").strip()
        if not item:
            if len(items) < min_items:
                print(f"  Please enter at least {min_items} item(s).")
                continue
            break
        items.append(item)
    
    return items


def get_choice(prompt: str, choices: List[str], default: str = None) -> str:
    """Get user choice from a list."""
    print(f"{prompt}")
    for i, choice in enumerate(choices, 1):
        marker = "*" if choice == default else " "
        print(f"  {marker}[{i}] {choice}")
    
    while True:
        if default:
            response = input(f"  Choice (1-{len(choices)}) [{default}]: ").strip()
        else:
            response = input(f"  Choice (1-{len(choices)}): ").strip()
        
        if not response and default:
            return default
        
        try:
            idx = int(response) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        except ValueError:
            if response in choices:
                return response
        
        print(f"  Please enter a number 1-{len(choices)}")


def sanitize_filename(name: str) -> str:
    """Convert name to valid filename."""
    # Remove special characters, replace spaces with underscores
    sanitized = re.sub(r'[^a-zA-Z0-9_\s-]', '', name)
    sanitized = re.sub(r'[\s-]+', '_', sanitized)
    return sanitized.lower()


# ============================================================================
# AUDIT CREATION WIZARD
# ============================================================================

class AuditWizard:
    """Interactive wizard for creating audit definitions."""
    
    def __init__(self, prompts_dir: str = None):
        """Initialize the wizard."""
        if prompts_dir is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            prompts_dir = os.path.join(current_dir, "prompts")
        
        self.prompts_dir = prompts_dir
        self.audit_data = {}
    
    def run(self, template: str = None, output_path: str = None) -> Optional[str]:
        """
        Run the interactive wizard.
        
        Returns:
            Path to created YAML file, or None if cancelled
        """
        print_header("Custom Audit Builder")
        print("Create a new audit definition for the Website LLM Analyzer")
        
        # Step 1: Basic metadata
        print_section("Step 1: Basic Information")
        
        audit_name = get_input("Audit name")
        description = get_input("Description")
        
        category = get_choice(
            "Category:",
            ['generic', 'compliance', 'brand', 'technical'],
            default='generic'
        )
        
        author = get_input("Author", default="Unknown", required=False)
        
        self.audit_data = {
            'name': audit_name,
            'description': description,
            'version': '1.0',
            'author': author,
            'category': category
        }
        
        # Step 2: Template selection or custom
        print_section("Step 2: Choose Starting Point")
        
        if template and template in TEMPLATES:
            use_template = True
            template_name = template
        else:
            use_template = get_yes_no("Use a template to start?", default=True)
            
            if use_template:
                template_name = get_choice(
                    "Select template:",
                    list(TEMPLATES.keys()),
                    default='basic'
                )
        
        if use_template:
            self._apply_template(template_name)
        else:
            self._collect_custom_schema()
        
        # Step 3: Role and Task
        print_section("Step 3: Define the LLM Role and Task")
        
        print("\nDescribe the role for the LLM (who is it acting as):")
        role = get_multiline_input("Role definition")
        self.audit_data['role'] = role
        
        print("\nDescribe the task (what should the LLM do):")
        task = get_multiline_input("Task description")
        self.audit_data['task'] = task
        
        # Step 4: Criteria
        print_section("Step 4: Define Evaluation Criteria")
        
        criteria = []
        while True:
            print(f"\nCriteria Section {len(criteria) + 1}:")
            section_name = get_input("Section name (e.g., 'Content Quality')")
            
            items = get_list_input(f"Items for '{section_name}'", min_items=1)
            
            criteria.append({
                'section': section_name,
                'items': items
            })
            
            if not get_yes_no("Add another criteria section?", default=False):
                break
        
        self.audit_data['criteria'] = criteria
        
        # Step 5: Customize scoring (if not from template)
        if not use_template or get_yes_no("\nCustomize scoring configuration?", default=False):
            self._collect_scoring_config()
        
        # Step 6: Save
        print_section("Step 5: Save Audit Definition")
        
        if output_path:
            yaml_path = output_path
        else:
            default_filename = sanitize_filename(audit_name) + ".yaml"
            filename = get_input(
                "Filename",
                default=default_filename
            )
            
            if not filename.endswith('.yaml'):
                filename += '.yaml'
            
            yaml_path = os.path.join(self.prompts_dir, filename)
        
        # Write YAML file
        self._write_yaml(yaml_path)
        
        print(f"\n✓ Audit definition saved to: {yaml_path}")
        
        # Offer to test
        if get_yes_no("\nValidate the audit definition?", default=True):
            self._validate_audit(yaml_path)
        
        # Show usage
        audit_type = Path(yaml_path).stem.upper()
        print(f"\n{'='*60}")
        print("  Ready to use!")
        print('='*60)
        print(f"\nRun your audit with:")
        print(f"  python website_llm_analyzer.py --audit {audit_type}")
        print(f"\nOr in direct mode:")
        print(f"  python website_llm_analyzer.py --audit {audit_type} --direct")
        print()
        
        return yaml_path
    
    def _apply_template(self, template_name: str):
        """Apply a template to the audit data."""
        template = TEMPLATES[template_name]
        
        # Create root key from audit name
        root_key = sanitize_filename(self.audit_data['name']) + "_audit"
        
        # Build output schema
        self.audit_data['output_schema'] = {
            'root_key': root_key,
            'fields': template['fields'],
            'issues_key': 'issues',
            'issues_schema': template['issues_schema']
        }
        
        # Build scoring config
        primary_metric = template['fields'][0]['name']  # Use first field as primary
        
        self.audit_data['scoring'] = {
            'primary_metric': primary_metric,
            'prefix_format': '{score:03d}',
            'save_condition': 'always',
            'buckets': template['buckets']
        }
        
        print(f"  ✓ Applied '{template_name}' template")
    
    def _collect_custom_schema(self):
        """Collect custom output schema from user."""
        print_section("Define Output Schema")
        
        # Root key
        default_root = sanitize_filename(self.audit_data['name']) + "_audit"
        root_key = get_input("Root key for JSON output", default=default_root)
        
        # Fields
        print("\nDefine output fields:")
        fields = []
        
        while True:
            field_name = get_input("Field name (empty to finish)", required=False)
            if not field_name:
                if not fields:
                    print("  At least one field is required.")
                    continue
                break
            
            field_type = get_choice(
                "  Field type:",
                ['integer', 'string', 'enum', 'boolean'],
                default='string'
            )
            
            field_def = {'name': field_name, 'type': field_type}
            
            if field_type == 'integer':
                if get_yes_no("  Add value range?", default=True):
                    min_val = int(get_input("    Min value", default="0"))
                    max_val = int(get_input("    Max value", default="100"))
                    field_def['range'] = [min_val, max_val]
            
            elif field_type == 'enum':
                values = get_input("  Allowed values (comma-separated)")
                field_def['values'] = [v.strip() for v in values.split(',')]
            
            fields.append(field_def)
        
        # Issues schema
        issues_key = None
        issues_schema = []
        
        if get_yes_no("\nTrack individual issues?", default=True):
            issues_key = get_input("Issues array key", default="issues")
            
            print("\nDefine issue fields:")
            
            # Add standard fields
            issues_schema = [
                {'name': 'fragment', 'type': 'string'},
                {'name': 'severity', 'type': 'enum', 'values': ['critical', 'major', 'minor']},
                {'name': 'description', 'type': 'string'},
                {'name': 'recommendation', 'type': 'string'}
            ]
            
            print("  Default fields added: fragment, severity, description, recommendation")
            
            if get_yes_no("  Add custom issue fields?", default=False):
                while True:
                    field_name = get_input("  Field name (empty to finish)", required=False)
                    if not field_name:
                        break
                    issues_schema.append({
                        'name': field_name,
                        'type': 'string'
                    })
        
        self.audit_data['output_schema'] = {
            'root_key': root_key,
            'fields': fields
        }
        
        if issues_key:
            self.audit_data['output_schema']['issues_key'] = issues_key
            self.audit_data['output_schema']['issues_schema'] = issues_schema
    
    def _collect_scoring_config(self):
        """Collect scoring configuration from user."""
        print_section("Scoring Configuration")
        
        # Get available fields
        fields = self.audit_data.get('output_schema', {}).get('fields', [])
        field_names = [f['name'] for f in fields if f.get('type') == 'integer']
        
        if not field_names:
            print("  No integer fields found. Using default scoring.")
            self.audit_data['scoring'] = {
                'primary_metric': 'overall_score',
                'prefix_format': '{score:03d}',
                'save_condition': 'always',
                'buckets': [
                    {'range': [0, 49], 'label': 'poor'},
                    {'range': [50, 69], 'label': 'needs_work'},
                    {'range': [70, 84], 'label': 'good'},
                    {'range': [85, 100], 'label': 'excellent'}
                ]
            }
            return
        
        # Primary metric
        if len(field_names) == 1:
            primary_metric = field_names[0]
            print(f"  Using '{primary_metric}' as primary metric")
        else:
            primary_metric = get_choice(
                "Primary metric for scoring:",
                field_names,
                default=field_names[0]
            )
        
        # Prefix format
        prefix_digits = get_choice(
            "Prefix digit count:",
            ['2 digits (00-99)', '3 digits (000-999)'],
            default='3 digits (000-999)'
        )
        prefix_format = "{score:02d}" if '2 digits' in prefix_digits else "{score:03d}"
        
        # Secondary field
        secondary_field = None
        other_fields = [f['name'] for f in fields if f['name'] != primary_metric]
        
        if other_fields and get_yes_no("Add secondary field to prefix?", default=False):
            secondary_field = get_choice(
                "Secondary field:",
                other_fields,
                default=other_fields[0]
            )
        
        # Save condition
        save_condition = get_choice(
            "When to save results:",
            ['always', 'has_issues', 'score_below:50', 'score_above:80'],
            default='always'
        )
        
        # Buckets
        buckets = []
        if get_yes_no("Define custom score buckets?", default=True):
            print("\nDefine score ranges (e.g., 0-49 = poor):")
            
            while True:
                range_str = get_input("  Range (e.g., '0-49') or empty to finish", required=False)
                if not range_str:
                    break
                
                try:
                    parts = range_str.split('-')
                    min_val = int(parts[0])
                    max_val = int(parts[1])
                except (ValueError, IndexError):
                    print("    Invalid range format. Use 'min-max' (e.g., '0-49')")
                    continue
                
                label = get_input(f"  Label for {min_val}-{max_val}")
                buckets.append({'range': [min_val, max_val], 'label': label})
        
        if not buckets:
            buckets = [
                {'range': [0, 49], 'label': 'poor'},
                {'range': [50, 69], 'label': 'needs_work'},
                {'range': [70, 84], 'label': 'good'},
                {'range': [85, 100], 'label': 'excellent'}
            ]
        
        self.audit_data['scoring'] = {
            'primary_metric': primary_metric,
            'prefix_format': prefix_format,
            'save_condition': save_condition,
            'buckets': buckets
        }
        
        if secondary_field:
            self.audit_data['scoring']['secondary_field'] = secondary_field
    
    def _write_yaml(self, path: str):
        """Write the audit definition to a YAML file."""
        # Ensure directory exists
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        # Build final structure
        yaml_data = {
            'name': self.audit_data['name'],
            'description': self.audit_data['description'],
            'version': self.audit_data['version'],
            'author': self.audit_data.get('author', 'Unknown'),
            'category': self.audit_data.get('category', 'generic'),
            'role': self.audit_data.get('role', ''),
            'task': self.audit_data.get('task', ''),
            'criteria': self.audit_data.get('criteria', []),
            'output_schema': self.audit_data.get('output_schema', {}),
            'scoring': self.audit_data.get('scoring', {}),
            'language_hint': 'auto'
        }
        
        # Write with nice formatting
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(
                yaml_data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=120
            )
    
    def _validate_audit(self, yaml_path: str):
        """Run validation on the created audit."""
        print("\nValidating audit definition...")
        
        try:
            from audit_builder import load_custom_audit, build_system_prompt
            
            definition = load_custom_audit(yaml_path)
            print("  ✓ YAML structure valid")
            
            prompt = build_system_prompt(definition)
            if prompt:
                print(f"  ✓ System prompt generated ({len(prompt)} chars)")
            
            print("  ✓ Audit definition is valid!")
            
        except Exception as e:
            print(f"  ✗ Validation error: {e}")


# ============================================================================
# CLI
# ============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Interactive wizard for creating custom audit definitions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Start interactive wizard
  python create_audit.py
  
  # Use a specific template
  python create_audit.py --template compliance
  
  # Specify output path
  python create_audit.py --output prompts/my_audit.yaml
  
  # List available templates
  python create_audit.py --list-templates

Templates:
  basic      - Simple audit with score and issues
  compliance - Compliance check with violations tracking
  technical  - Technical analysis with detailed metrics
        '''
    )
    
    parser.add_argument(
        '--template', '-t',
        type=str,
        choices=list(TEMPLATES.keys()),
        help='Start with a template'
    )
    
    parser.add_argument(
        '--output', '-o',
        type=str,
        help='Output file path'
    )
    
    parser.add_argument(
        '--prompts-dir',
        type=str,
        help='Directory for prompt files (default: ./prompts/)'
    )
    
    parser.add_argument(
        '--list-templates',
        action='store_true',
        help='List available templates and exit'
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    if args.list_templates:
        print("\nAvailable Templates:")
        print("-" * 40)
        for name, template in TEMPLATES.items():
            print(f"\n{name}:")
            print(f"  {template['description']}")
            print(f"  Category: {template['category']}")
            fields = [f['name'] for f in template['fields']]
            print(f"  Fields: {', '.join(fields)}")
        print()
        return
    
    try:
        wizard = AuditWizard(prompts_dir=args.prompts_dir)
        wizard.run(template=args.template, output_path=args.output)
    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
