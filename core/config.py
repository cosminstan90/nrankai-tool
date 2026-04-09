"""
Centralized configuration module for Website LLM Analyzer.

Handles environment variable loading, LLM provider detection, client initialization,
and path construction. Now supports CLI argument overrides.

Author: Cosmin
Created: 2026-02-10
Updated: 2026-02-10 - Added CLI override support
"""

import os
from dotenv import load_dotenv, find_dotenv
from typing import TypedDict, Optional

# Import LLM clients
from anthropic import Anthropic
from openai import OpenAI
from mistralai import Mistral


# ============================================================================
# PROVIDER-TO-MODEL MAPPING (from centralized registry)
# ============================================================================
try:
    from api.provider_registry import get_provider_models_dict
    PROVIDER_MODELS: dict[str, str] = get_provider_models_dict()
except ImportError:
    # Fallback if running standalone
    PROVIDER_MODELS: dict[str, str] = {
        "GOOGLE": "gemini-2.5-flash",
        "ANTHROPIC": "claude-sonnet-4-20250514",
        "OPENAI": "gpt-4o",
        "MISTRAL": "mistral-large-latest",
    }


# ============================================================================
# ENVIRONMENT LOADING
# ============================================================================
env_file = find_dotenv()
load_status = load_dotenv(env_file)

if not load_status:
    raise RuntimeError(
        "CRITICAL ERROR: .env file not found! "
        "Please create a .env file based on .env.example"
    )

# Note: env_file path is available via config.env_file if needed for debugging


# ============================================================================
# CONFIGURATION STATE (Module-level variables)
# ============================================================================
_config_state = {
    'website': None,
    'question_type': None,
    'sitemap': None,
    'proxy_host': None,
    'proxy_port': None,
    'provider': None,
    'model_name': None,
    'client': None,
    'max_chars': 30000,
}

_configured = False


def configure(
    website: Optional[str] = None,
    question_type: Optional[str] = None,
    sitemap: Optional[str] = None,
    proxy_host: Optional[str] = None,
    proxy_port: Optional[str] = None,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    max_chars: Optional[int] = None,
    no_proxy: bool = False,
) -> None:
    """
    Configure the module with CLI overrides or initialize from environment.
    
    Args:
        website: Target website domain (overrides WEBSITE env var)
        question_type: Audit type (overrides QUESTION env var)
        sitemap: Sitemap URL (overrides SITEMAP env var)
        proxy_host: Proxy hostname (overrides PROXY_HOST env var)
        proxy_port: Proxy port (overrides PROXY_PORT env var)
        provider: LLM provider ('anthropic', 'openai', or 'mistral')
        model_name: Specific model name to use
        max_chars: Maximum characters to send to LLM (default: 30000)
        no_proxy: Disable proxy even if configured in .env
    """
    global _configured
    
    # Website configuration
    _config_state['website'] = website or os.getenv("WEBSITE")
    if not _config_state['website']:
        raise ValueError(
            "Missing required configuration: WEBSITE\n"
            "Set via --website argument or WEBSITE in .env"
        )
    
    # Question type configuration
    _config_state['question_type'] = (question_type or os.getenv("QUESTION", "")).upper()
    if not _config_state['question_type']:
        raise ValueError(
            "Missing required configuration: QUESTION\n"
            "Set via --audit/--question argument or QUESTION in .env"
        )
    
    # Sitemap configuration (only required for web scraper)
    _config_state['sitemap'] = sitemap or os.getenv("SITEMAP", "")
    
    # Proxy configuration
    if no_proxy:
        _config_state['proxy_host'] = None
        _config_state['proxy_port'] = None
    else:
        _config_state['proxy_host'] = proxy_host or os.getenv("PROXY_HOST")
        _config_state['proxy_port'] = proxy_port or os.getenv("PROXY_PORT")
    
    # Max chars configuration
    if max_chars is not None:
        _config_state['max_chars'] = max_chars
    
    # Provider and model configuration
    _configure_provider(provider, model_name)
    
    _configured = True


def _configure_provider(provider_override: Optional[str], model_override: Optional[str]) -> None:
    """Configure LLM provider and initialize client."""
    # Get API keys
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    mistral_key = os.getenv("MISTRAL_API_KEY")
    google_key = os.getenv("GEMINI_API_KEY")
    
    # Determine which provider to use
    if provider_override:
        provider_name = provider_override.upper()
        
        if provider_name == "ANTHROPIC" and anthropic_key:
            _config_state['client'] = Anthropic(api_key=anthropic_key)
            _config_state['provider'] = "ANTHROPIC"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("ANTHROPIC", "claude-sonnet-4-20250514")
            print(f"✓ Running in ANTHROPIC (Claude) mode - Recommended for compliance audits.")
        elif provider_name == "OPENAI" and openai_key:
            _config_state['client'] = OpenAI(api_key=openai_key)
            _config_state['provider'] = "OPENAI"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("OPENAI", "gpt-4o")
            print(f"✓ Running in OPENAI mode.")
        elif provider_name == "MISTRAL" and mistral_key:
            _config_state['client'] = Mistral(api_key=mistral_key)
            _config_state['provider'] = "MISTRAL"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("MISTRAL", "mistral-large-latest")
            print(f"✓ Running in MISTRAL mode.")
        elif provider_name == "GOOGLE" and google_key:
            from google import genai
            _config_state['client'] = genai.Client(api_key=google_key)
            _config_state['provider'] = "GOOGLE"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("GOOGLE", "gemini-2.5-flash")
            print(f"✓ Running in GOOGLE (Gemini) mode.")
        else:
            raise ValueError(
                f"Provider {provider_name} specified but no API key found.\n"
                f"Please set {provider_name}_API_KEY (or GEMINI_API_KEY for Google) in your .env file."
            )
    else:
        # Auto-detect provider based on available API keys
        # Priority order: Anthropic > OpenAI > Mistral > Google
        if anthropic_key:
            _config_state['client'] = Anthropic(api_key=anthropic_key)
            _config_state['provider'] = "ANTHROPIC"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("ANTHROPIC", "claude-sonnet-4-20250514")
            print("✓ Running in ANTHROPIC (Claude) mode - Recommended for compliance audits.")
        elif openai_key:
            _config_state['client'] = OpenAI(api_key=openai_key)
            _config_state['provider'] = "OPENAI"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("OPENAI", "gpt-4o")
            print("✓ Running in OPENAI mode.")
        elif mistral_key:
            _config_state['client'] = Mistral(api_key=mistral_key)
            _config_state['provider'] = "MISTRAL"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("MISTRAL", "mistral-large-latest")
            print("✓ Running in MISTRAL mode.")
        elif google_key:
            from google import genai
            _config_state['client'] = genai.Client(api_key=google_key)
            _config_state['provider'] = "GOOGLE"
            _config_state['model_name'] = model_override or PROVIDER_MODELS.get("GOOGLE", "gemini-2.5-flash")
            print("✓ Running in GOOGLE (Gemini) mode.")
        else:
            raise ValueError(
                "No API key found. Please set one of the following in your .env file:\n"
                "  - ANTHROPIC_API_KEY (recommended)\n"
                "  - OPENAI_API_KEY\n"
                "  - MISTRAL_API_KEY\n"
                "  - GEMINI_API_KEY"
            )


# ============================================================================
# CONFIGURATION ACCESSORS
# ============================================================================

def get_client():
    """Get the configured LLM client."""
    if not _configured:
        configure()
    return _config_state['client']


def get_provider() -> str:
    """Get the current provider name."""
    if not _configured:
        configure()
    return _config_state['provider']


def get_model_name() -> str:
    """Get the current model name."""
    if not _configured:
        configure()
    return _config_state['model_name']


def get_website() -> str:
    """Get the configured website."""
    if not _configured:
        configure()
    return _config_state['website']


def get_question_type() -> str:
    """Get the configured question/audit type."""
    if not _configured:
        configure()
    return _config_state['question_type']


def get_sitemap() -> str:
    """Get the configured sitemap URL."""
    if not _configured:
        configure()
    return _config_state['sitemap']


def get_proxy_host() -> Optional[str]:
    """Get the configured proxy host."""
    if not _configured:
        configure()
    return _config_state['proxy_host']


def get_proxy_port() -> Optional[str]:
    """Get the configured proxy port."""
    if not _configured:
        configure()
    return _config_state['proxy_port']


def get_max_chars() -> int:
    """Get the configured max characters limit."""
    if not _configured:
        configure()
    return _config_state['max_chars']


# Backwards compatibility - lazy module-level attribute access
# property() only works as a class descriptor, NOT at module level.
# __getattr__ is the correct mechanism for lazy module-level attributes (PEP 562).
def __getattr__(name):
    """Lazy module-level attribute access for backwards compatibility."""
    _mapping = {
        'client': get_client,
        'PROVIDER': get_provider,
        'MODEL_NAME': get_model_name,
        'WEBSITE': get_website,
        'QUESTION_TYPE': get_question_type,
        'SITEMAP': get_sitemap,
        'PROXY_HOST': get_proxy_host,
        'PROXY_PORT': get_proxy_port,
        'MAX_CHARS': get_max_chars,
    }
    if name in _mapping:
        return _mapping[name]()
    raise AttributeError(f"module 'config' has no attribute {name}")


# ============================================================================
# PATH CONSTRUCTION
# ============================================================================

class PathsDict(TypedDict):
    """Type definition for paths dictionary returned by get_paths()."""
    input_html_dir: str
    input_llm_dir: str
    output_dir: str
    batch_file_path: str


def get_paths(
    website_override: Optional[str] = None,
    question_type_override: Optional[str] = None,
    provider_override: Optional[str] = None
) -> PathsDict:
    """
    Construct all required file paths based on WEBSITE and QUESTION_TYPE.
    
    Args:
        website_override: Override website value (for CLI usage)
        question_type_override: Override question type (for CLI usage)
        provider_override: Override provider name (for CLI usage)
    
    Returns:
        Dictionary containing all path configurations
    """
    # Ensure configuration is initialized
    if not _configured:
        configure()
    
    # Use overrides if provided, otherwise use configured values
    website = website_override or _config_state['website']
    question_type = (question_type_override or _config_state['question_type']).upper()
    provider = (provider_override or _config_state['provider']).lower()
    
    input_html_dir = os.path.join(website, "input_html")
    input_llm_dir = os.path.join(website, "input_llm")
    
    # Output directory includes the question type (lowercased)
    output_folder_name = f"output_{question_type.lower()}"
    output_dir = os.path.join(website, output_folder_name)
    
    # Batch file name includes provider
    batch_filename = f"{website}_{provider}.jsonl"
    batch_file_path = os.path.join(website, batch_filename)
    
    return {
        "input_html_dir": input_html_dir,
        "input_llm_dir": input_llm_dir,
        "output_dir": output_dir,
        "batch_file_path": batch_file_path,
    }


def setup_output_directory() -> str:
    """Create the output directory for storing analysis results."""
    paths = get_paths()
    output_dir = paths["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


# ============================================================================
# NRANKAI CLOUD INTEGRATION
# ============================================================================

NRANKAI_CLOUD_URL = os.getenv("NRANKAI_CLOUD_URL", "")
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "")
PROSPECT_ID = os.getenv("PROSPECT_ID", "")
CAMPAIGN_ID = os.getenv("CAMPAIGN_ID", "")

# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Configuration function
    "configure",
    
    # Accessor functions
    "get_client",
    "get_provider",
    "get_model_name",
    "get_website",
    "get_question_type",
    "get_sitemap",
    "get_proxy_host",
    "get_proxy_port",
    "get_max_chars",
    
    # Constants
    "PROVIDER_MODELS",
    
    # Path Functions
    "get_paths",
    "setup_output_directory",
]
