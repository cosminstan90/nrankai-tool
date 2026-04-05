"""
Prompt Loader Module

This module provides functionality to load, validate, and cache audit prompts from YAML files.
Each audit type has its own YAML file in the prompts/ directory with a standardized schema.

Supports both legacy prompts and the new custom audit builder system.
Custom audits (with extended schema) are automatically detected and processed via audit_builder.

Author: Refactored by Claude
Created: 2026-02-10
Updated: 2026-02-12 - Added integration with audit_builder for custom audits
"""

import os
import yaml
from typing import Dict, List, Optional, Any


class PromptLoaderError(Exception):
    """Base exception for prompt loader errors."""
    pass


class PromptNotFoundError(PromptLoaderError):
    """Raised when a prompt file is not found."""
    pass


class PromptValidationError(PromptLoaderError):
    """Raised when a prompt file has invalid structure."""
    pass


# Try to import audit_builder for custom audit support
try:
    from core.audit_builder import (
        is_custom_audit as check_custom_audit,
        load_custom_audit,
        build_system_prompt as build_custom_prompt,
        AuditDefinition
    )
    AUDIT_BUILDER_AVAILABLE = True
except ImportError:
    AUDIT_BUILDER_AVAILABLE = False
    AuditDefinition = None


class PromptLoader:
    """
    Loads and manages audit prompts from YAML files.
    
    Features:
    - Loads prompts from YAML files in the prompts/ directory
    - Validates prompt structure
    - Caches loaded prompts in memory for performance
    - Provides list of available audit types
    - Automatically detects and handles custom audits via audit_builder
    """
    
    def __init__(self, prompts_dir: str = None):
        """
        Initialize the prompt loader.
        
        Args:
            prompts_dir: Path to the prompts directory. If None, uses ./prompts/
        """
        if prompts_dir is None:
            # Default to prompts/ directory relative to this file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            prompts_dir = os.path.join(current_dir, "prompts")
        
        self.prompts_dir = prompts_dir
        self._cache: Dict[str, str] = {}
        self._custom_audit_cache: Dict[str, Any] = {}  # Cache for AuditDefinition objects
        
        # Ensure prompts directory exists
        if not os.path.isdir(self.prompts_dir):
            raise PromptLoaderError(
                f"Prompts directory not found: {self.prompts_dir}"
            )
    
    def _get_prompt_file_path(self, audit_type: str) -> str:
        """
        Get the file path for a given audit type.
        
        Args:
            audit_type: The audit type (e.g., 'SEO_AUDIT', 'GEO_AUDIT')
        
        Returns:
            Full path to the YAML file
        """
        # Convert audit type to lowercase filename
        filename = audit_type.lower() + ".yaml"
        return os.path.join(self.prompts_dir, filename)
    
    def _is_custom_audit(self, yaml_path: str) -> bool:
        """Check if a YAML file is a custom audit with extended schema."""
        if not AUDIT_BUILDER_AVAILABLE:
            return False
        return check_custom_audit(yaml_path)
    
    def _load_yaml(self, file_path: str) -> dict:
        """
        Load and parse a YAML file.
        
        Args:
            file_path: Path to the YAML file
        
        Returns:
            Parsed YAML content as dictionary
        
        Raises:
            PromptNotFoundError: If file doesn't exist
            PromptValidationError: If YAML is malformed
        """
        if not os.path.isfile(file_path):
            raise PromptNotFoundError(
                f"Prompt file not found: {file_path}"
            )
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise PromptValidationError(
                f"Failed to parse YAML file {file_path}: {e}"
            )
        except Exception as e:
            raise PromptLoaderError(
                f"Error reading file {file_path}: {e}"
            )
    
    def _validate_prompt_structure(self, prompt_data: dict, audit_type: str) -> None:
        """
        Validate that a prompt has the required structure.
        
        Args:
            prompt_data: Parsed YAML data
            audit_type: The audit type being validated
        
        Raises:
            PromptValidationError: If structure is invalid
        """
        required_fields = ['name', 'description', 'version', 'role', 'task', 'output_schema']
        
        for field in required_fields:
            if field not in prompt_data:
                raise PromptValidationError(
                    f"Prompt {audit_type} missing required field: {field}"
                )
            
            if not prompt_data[field]:
                raise PromptValidationError(
                    f"Prompt {audit_type} has empty field: {field}"
                )
        
        # Validate criteria structure if present
        if 'criteria' in prompt_data:
            if not isinstance(prompt_data['criteria'], list):
                raise PromptValidationError(
                    f"Prompt {audit_type} criteria must be a list"
                )
            
            for criterion in prompt_data['criteria']:
                if not isinstance(criterion, dict):
                    raise PromptValidationError(
                        f"Prompt {audit_type} criterion must be a dictionary"
                    )
                
                if 'section' not in criterion or 'items' not in criterion:
                    raise PromptValidationError(
                        f"Prompt {audit_type} criterion must have 'section' and 'items'"
                    )
    
    def _assemble_prompt(self, prompt_data: dict) -> str:
        """
        Assemble the full system message from prompt data.
        
        Args:
            prompt_data: Parsed and validated YAML data
        
        Returns:
            Assembled system message string
        """
        # Start with role and task
        message_parts = []
        
        # Add role
        if prompt_data.get('role'):
            message_parts.append(prompt_data['role'].strip())
        
        # Add task
        if prompt_data.get('task'):
            message_parts.append(prompt_data['task'].strip())
        
        # Add criteria sections
        if 'criteria' in prompt_data:
            for criterion in prompt_data['criteria']:
                section_name = criterion['section']
                message_parts.append(f"\n{section_name}")
                
                items = criterion['items']
                if items:
                    for item in items:
                        message_parts.append(item)
        
        # Add scoring if present
        if 'scoring' in prompt_data:
            message_parts.append("\nSCORING:")
            for score_item in prompt_data['scoring']:
                metric = score_item.get('metric', '')
                if 'range' in score_item:
                    message_parts.append(f"- {metric}: {score_item['range']}")
                elif 'values' in score_item:
                    message_parts.append(f"- {metric}: {score_item['values']}")
        
        # Add output schema
        if prompt_data.get('output_schema'):
            message_parts.append(f"\nOUTPUT FORMAT (Strict JSON):")
            message_parts.append("Return ONLY the following structure:")
            message_parts.append(prompt_data['output_schema'].strip())
        
        # Join all parts with proper spacing
        return "\n".join(message_parts)
    
    def load_prompt(self, audit_type: str) -> str:
        """
        Load a prompt for the given audit type.
        
        Args:
            audit_type: The audit type (e.g., 'SEO_AUDIT', 'GEO_AUDIT')
        
        Returns:
            The assembled system message string
        
        Raises:
            PromptNotFoundError: If prompt file doesn't exist
            PromptValidationError: If prompt structure is invalid
        """
        # Check cache first
        if audit_type in self._cache:
            return self._cache[audit_type]
        
        # Load from file
        file_path = self._get_prompt_file_path(audit_type)
        
        # Check if this is a custom audit
        if AUDIT_BUILDER_AVAILABLE and self._is_custom_audit(file_path):
            try:
                definition = load_custom_audit(file_path)
                assembled_prompt = build_custom_prompt(definition)
                
                # Cache both the prompt and the definition
                self._cache[audit_type] = assembled_prompt
                self._custom_audit_cache[audit_type] = definition
                
                return assembled_prompt
            except Exception as e:
                # Fall back to legacy loading on error
                pass
        
        # Legacy loading
        prompt_data = self._load_yaml(file_path)
        
        # Validate structure
        self._validate_prompt_structure(prompt_data, audit_type)
        
        # Assemble the prompt
        assembled_prompt = self._assemble_prompt(prompt_data)
        
        # Cache it
        self._cache[audit_type] = assembled_prompt
        
        return assembled_prompt
    
    def get_audit_definition(self, audit_type: str) -> Optional[Any]:
        """
        Get the AuditDefinition for a custom audit type.
        
        Args:
            audit_type: The audit type
            
        Returns:
            AuditDefinition object if custom audit, None otherwise
        """
        if not AUDIT_BUILDER_AVAILABLE:
            return None
        
        # Load prompt first to ensure definition is cached
        if audit_type not in self._custom_audit_cache:
            file_path = self._get_prompt_file_path(audit_type)
            if self._is_custom_audit(file_path):
                self.load_prompt(audit_type)
        
        return self._custom_audit_cache.get(audit_type)
    
    def is_custom_audit(self, audit_type: str) -> bool:
        """
        Check if an audit type is a custom audit with extended schema.
        
        Args:
            audit_type: The audit type to check
            
        Returns:
            True if custom audit, False otherwise
        """
        if not AUDIT_BUILDER_AVAILABLE:
            return False
        
        file_path = self._get_prompt_file_path(audit_type)
        return self._is_custom_audit(file_path)
    
    def validate_prompt(self, audit_type: str) -> bool:
        """
        Validate a prompt without loading it into cache.
        
        Args:
            audit_type: The audit type to validate
        
        Returns:
            True if prompt is valid
        
        Raises:
            PromptNotFoundError: If prompt file doesn't exist
            PromptValidationError: If prompt structure is invalid
        """
        file_path = self._get_prompt_file_path(audit_type)
        prompt_data = self._load_yaml(file_path)
        self._validate_prompt_structure(prompt_data, audit_type)
        return True
    
    def list_available_audits(self) -> List[Dict[str, str]]:
        """
        List all available audit types with their metadata.
        
        Returns:
            List of dictionaries containing 'type', 'name', 'description', 
            and 'is_custom' for each audit
        """
        audits = []
        
        # Scan prompts directory for YAML files
        if not os.path.isdir(self.prompts_dir):
            return audits
        
        # Skip files that are documentation/templates or internal generator prompts
        skip_files = {'schema.yaml', 'content_brief.yaml'}
        
        for filename in sorted(os.listdir(self.prompts_dir)):
            if filename.endswith('.yaml') and not filename.startswith('.'):
                if filename in skip_files:
                    continue
                
                # Extract audit type from filename
                audit_type = filename[:-5].upper()  # Remove .yaml and uppercase
                
                try:
                    # Load the YAML to get name and description
                    file_path = os.path.join(self.prompts_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = yaml.safe_load(f)
                    
                    # Check if custom audit
                    is_custom = self._is_custom_audit(file_path)
                    
                    audit_info = {
                        'type': audit_type,
                        'name': data.get('name', audit_type),
                        'description': data.get('description', ''),
                        'is_custom': is_custom
                    }
                    
                    # Add category for custom audits
                    if is_custom and 'category' in data:
                        audit_info['category'] = data['category']
                    
                    audits.append(audit_info)
                except Exception:
                    # Skip files that can't be loaded
                    continue
        
        return audits
    
    def clear_cache(self) -> None:
        """Clear the prompt and definition caches."""
        self._cache.clear()
        self._custom_audit_cache.clear()


# Singleton instance for convenience
_loader_instance: Optional[PromptLoader] = None


def get_loader() -> PromptLoader:
    """Get the singleton PromptLoader instance."""
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = PromptLoader()
    return _loader_instance


# Convenience functions that use the singleton instance
def load_prompt(audit_type: str) -> str:
    """
    Load a prompt for the given audit type.
    
    Args:
        audit_type: The audit type (e.g., 'SEO_AUDIT', 'GEO_AUDIT')
    
    Returns:
        The assembled system message string
    """
    return get_loader().load_prompt(audit_type)


def validate_prompt(audit_type: str) -> bool:
    """
    Validate a prompt structure.
    
    Args:
        audit_type: The audit type to validate
    
    Returns:
        True if prompt is valid
    """
    return get_loader().validate_prompt(audit_type)


def list_available_audits() -> List[Dict[str, str]]:
    """
    List all available audit types.
    
    Returns:
        List of dictionaries containing audit metadata
    """
    return get_loader().list_available_audits()


def get_audit_definition(audit_type: str) -> Optional[Any]:
    """
    Get the AuditDefinition for a custom audit type.
    
    Args:
        audit_type: The audit type
        
    Returns:
        AuditDefinition object if custom audit, None otherwise
    """
    return get_loader().get_audit_definition(audit_type)


def is_custom_audit(audit_type: str) -> bool:
    """
    Check if an audit type is a custom audit.
    
    Args:
        audit_type: The audit type to check
        
    Returns:
        True if custom audit, False otherwise
    """
    return get_loader().is_custom_audit(audit_type)
