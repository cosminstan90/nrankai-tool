"""
Centralized LLM Provider & Model Registry.

SINGLE SOURCE OF TRUTH for all model definitions, pricing, and provider config.
Import from this file everywhere — NEVER hardcode models/prices elsewhere.

Usage:
    from api.provider_registry import get_default_model, get_providers_for_ui, calculate_cost
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ModelInfo:
    """Single model definition with metadata and pricing."""
    id: str                          # Exact API model string: "gemini-2.5-flash"
    name: str                        # Human display: "Gemini 2.5 Flash"
    provider: str                    # "google", "anthropic", "openai", "mistral"
    tier: str                        # "cheap", "balanced", "premium"
    input_price: float               # $ per 1M input tokens
    output_price: float              # $ per 1M output tokens
    max_output_tokens: int = 8192
    supports_json_mode: bool = True  # Can return structured JSON
    notes: str = ""                  # "Best for testing", "Recommended for production"


@dataclass
class ProviderConfig:
    """Provider configuration."""
    id: str                          # "google", "anthropic", "openai", "mistral"
    name: str                        # "Google Gemini", "Anthropic Claude"
    env_key: str                     # "GEMINI_API_KEY", "ANTHROPIC_API_KEY"
    default_model: str               # Default model ID for this provider


@dataclass
class ProviderRateLimits:
    """Conservative rate limits per provider (requests per minute).

    These are intentionally set below the actual API ceilings so that
    parallel audit workers share headroom and avoid 429s.
    Configurable via env vars: ANTHROPIC_RPM, OPENAI_RPM, MISTRAL_RPM, GOOGLE_RPM
    """
    requests_per_minute: int   # Hard cap on outbound requests / min
    tokens_per_minute: int = 0 # 0 = uncapped


# ============================================================================
# MODEL REGISTRY — Edit HERE to add/update models and prices
# ============================================================================

ALL_MODELS: List[ModelInfo] = [
    # ---- Google Gemini ----
    ModelInfo("gemini-2.0-flash-lite",  "Gemini 2.0 Flash-Lite",  "google",    "cheap",     0.075,  0.30,  8192, True,  "Ultra-cheap. Fastest. Great for bulk testing."),
    ModelInfo("gemini-2.5-flash",       "Gemini 2.5 Flash",       "google",    "cheap",     0.15,   0.60,  8192, True,  "Best value. Hybrid reasoning."),
    ModelInfo("gemini-2.0-flash",       "Gemini 2.0 Flash",       "google",    "balanced",  0.10,   0.40,  8192, True,  "Fast and reliable."),
    ModelInfo("gemini-2.5-pro",         "Gemini 2.5 Pro",         "google",    "premium",   1.25,  10.00,  8192, True,  "Best Gemini. Strong reasoning."),

    # ---- Anthropic Claude ----
    ModelInfo("claude-haiku-4-5-20251001",  "Claude 4.5 Haiku",   "anthropic", "cheap",     1.00,   5.00,  8192, True,  "Fast + cheap Claude. Good for briefs/schemas."),
    ModelInfo("claude-sonnet-4-20250514",   "Claude Sonnet 4",    "anthropic", "balanced",  3.00,  15.00,  8192, True,  "Best balance quality/price. Default for audits."),
    ModelInfo("claude-opus-4-5-20251101",   "Claude Opus 4.5",    "anthropic", "premium",  15.00,  75.00,  8192, True,  "Most capable. Complex analysis only."),

    # ---- OpenAI ----
    ModelInfo("gpt-4o-mini",            "GPT-4o Mini",            "openai",    "cheap",     0.15,   0.60,  8192, True,  "Cheapest OpenAI. Great for testing."),
    ModelInfo("gpt-4o",                 "GPT-4o",                 "openai",    "balanced",  2.50,  10.00,  8192, True,  "Best OpenAI balance."),
    ModelInfo("gpt-4-turbo",            "GPT-4 Turbo",            "openai",    "premium",  10.00,  30.00,  8192, True,  "Legacy premium."),

    # ---- Mistral ----
    ModelInfo("mistral-small-latest",   "Mistral Small",          "mistral",   "cheap",     0.20,   0.60,  8192, True,  "Cheapest Mistral."),
    ModelInfo("mistral-medium-latest",  "Mistral Medium",         "mistral",   "balanced",  2.70,   8.10,  8192, True,  "Balanced Mistral."),
    ModelInfo("mistral-large-latest",   "Mistral Large",          "mistral",   "premium",   2.00,   6.00,  8192, True,  "Most capable Mistral."),
]


# ============================================================================
# PROVIDER CONFIG
# ============================================================================

PROVIDERS: List[ProviderConfig] = [
    ProviderConfig("google",    "Google Gemini",    "GEMINI_API_KEY",    "gemini-2.5-flash"),
    ProviderConfig("anthropic", "Anthropic Claude", "ANTHROPIC_API_KEY", "claude-sonnet-4-20250514"),
    ProviderConfig("openai",    "OpenAI",           "OPENAI_API_KEY",    "gpt-4o"),
    ProviderConfig("mistral",   "Mistral",          "MISTRAL_API_KEY",   "mistral-large-latest"),
]


# ============================================================================
# RATE LIMITS — Conservative defaults, override via environment variables
# ============================================================================

def get_provider_rate_limits(provider_id: str) -> ProviderRateLimits:
    """Return rate limits for a provider, respecting env var overrides.

    Env vars: ANTHROPIC_RPM, OPENAI_RPM, MISTRAL_RPM, GOOGLE_RPM
    Defaults are set conservatively at ~50% of official tier-1 ceilings.
    """
    defaults = {
        "anthropic": ProviderRateLimits(requests_per_minute=int(os.getenv("ANTHROPIC_RPM", "2000")),
                                        tokens_per_minute=int(os.getenv("ANTHROPIC_TPM", "100000"))),
        "openai":    ProviderRateLimits(requests_per_minute=int(os.getenv("OPENAI_RPM", "1500")),
                                        tokens_per_minute=int(os.getenv("OPENAI_TPM", "125000"))),
        "mistral":   ProviderRateLimits(requests_per_minute=int(os.getenv("MISTRAL_RPM", "300"))),
        "google":    ProviderRateLimits(requests_per_minute=int(os.getenv("GOOGLE_RPM", "500"))),
    }
    return defaults.get(provider_id.lower(), ProviderRateLimits(requests_per_minute=500))

# Tier display config
TIER_META = {
    "cheap":    {"emoji": "💰", "label": "Cheap",    "color": "green"},
    "balanced": {"emoji": "⚖️", "label": "Balanced", "color": "blue"},
    "premium":  {"emoji": "🚀", "label": "Premium",  "color": "purple"},
}


# ============================================================================
# HELPER FUNCTIONS — import and use these everywhere
# ============================================================================

def get_available_providers() -> Dict[str, bool]:
    """Check which providers have API keys configured.
    
    Returns:
        {"google": True, "anthropic": True, "openai": False, "mistral": False}
    """
    return {p.id: bool(os.getenv(p.env_key)) for p in PROVIDERS}


def get_provider_config(provider_id: str) -> Optional[ProviderConfig]:
    """Get provider config by ID."""
    provider_id = provider_id.lower()
    return next((p for p in PROVIDERS if p.id == provider_id), None)


def get_model(model_id: str) -> Optional[ModelInfo]:
    """Get model info by exact model ID."""
    return next((m for m in ALL_MODELS if m.id == model_id), None)


def get_models_for_provider(provider_id: str) -> List[ModelInfo]:
    """Get all models for a provider, sorted cheap → premium."""
    provider_id = provider_id.lower()
    tier_order = {"cheap": 0, "balanced": 1, "premium": 2}
    models = [m for m in ALL_MODELS if m.provider == provider_id]
    return sorted(models, key=lambda m: tier_order.get(m.tier, 1))


def get_default_model(provider_id: str) -> str:
    """Get default model ID for a provider."""
    provider = get_provider_config(provider_id)
    return provider.default_model if provider else "claude-sonnet-4-20250514"


def get_cheapest_model(provider_id: str = None) -> ModelInfo:
    """Get the cheapest model, optionally filtered by provider."""
    if provider_id:
        provider_id = provider_id.lower()
        candidates = [m for m in ALL_MODELS if m.provider == provider_id]
    else:
        candidates = ALL_MODELS
    return min(candidates, key=lambda m: m.input_price + m.output_price)


def get_cheapest_available_model() -> ModelInfo:
    """Get the cheapest model across all configured providers."""
    available = get_available_providers()
    candidates = [m for m in ALL_MODELS if available.get(m.provider, False)]
    if not candidates:
        candidates = ALL_MODELS  # Fallback
    return min(candidates, key=lambda m: m.input_price + m.output_price)


def get_models_by_tier(tier: str) -> List[ModelInfo]:
    """Get all models of a specific tier across all providers."""
    return [m for m in ALL_MODELS if m.tier == tier]


def calculate_cost(provider: str, model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for a specific API call.
    
    Args:
        provider: Provider ID (used as fallback if model not found)
        model_id: Exact model ID string
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
    
    Returns:
        Estimated cost in USD
    """
    model = get_model(model_id)
    if not model:
        # Fallback: try provider's default
        models = get_models_for_provider(provider)
        model = models[0] if models else ModelInfo("unknown", "Unknown", provider, "balanced", 3.0, 15.0)
    return (input_tokens * model.input_price + output_tokens * model.output_price) / 1_000_000


def estimate_audit_cost(provider: str, model_id: str, num_pages: int, avg_input_tokens: int = 3000) -> float:
    """Estimate total audit cost for a number of pages.
    
    Returns:
        Estimated cost in USD
    """
    model = get_model(model_id) or get_model(get_default_model(provider))
    if not model:
        return 0.0
    estimated_output = 2000  # Average output tokens per page audit
    per_page = (avg_input_tokens * model.input_price + estimated_output * model.output_price) / 1_000_000
    return round(per_page * num_pages, 4)


# ============================================================================
# LEGACY COMPATIBILITY — for direct_analyzer.py backward compat
# ============================================================================

def get_cost_per_million_tokens() -> Dict:
    """Generate COST_PER_MILLION_TOKENS dict in legacy format.
    
    Returns:
        {"ANTHROPIC": {"claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0}}, ...}
    """
    result = {}
    for model in ALL_MODELS:
        provider_key = model.provider.upper()
        if provider_key not in result:
            result[provider_key] = {}
        result[provider_key][model.id] = {
            "input": model.input_price,
            "output": model.output_price
        }
    return result


def get_provider_models_dict() -> Dict[str, str]:
    """Generate PROVIDER_MODELS dict in legacy format.
    
    Returns:
        {"ANTHROPIC": "claude-sonnet-4-20250514", "OPENAI": "gpt-4o", ...}
    """
    return {p.id.upper(): p.default_model for p in PROVIDERS}


# ============================================================================
# UI HELPERS — for template rendering
# ============================================================================

def get_providers_for_ui() -> Dict:
    """Generate full provider+models structure for Jinja2 templates.
    
    Returns:
        {
            "google": {
                "name": "Google Gemini",
                "available": True,
                "default_model": "gemini-2.5-flash",
                "models": [
                    {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash-Lite", 
                     "tier": "cheap", "tier_emoji": "💰", "tier_color": "green",
                     "price_label": "$0.08/$0.30 per 1M", "notes": "Ultra-cheap..."}
                ]
            },
            ...
        }
    """
    available = get_available_providers()
    result = {}
    for provider in PROVIDERS:
        models = get_models_for_provider(provider.id)
        result[provider.id] = {
            "name": provider.name,
            "available": available[provider.id],
            "default_model": provider.default_model,
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "tier": m.tier,
                    "tier_emoji": TIER_META.get(m.tier, {}).get("emoji", ""),
                    "tier_color": TIER_META.get(m.tier, {}).get("color", "gray"),
                    "price_label": f"${m.input_price:.2f}/${m.output_price:.2f} per 1M",
                    "notes": m.notes
                }
                for m in models
            ]
        }
    return result


def get_tier_presets() -> Dict[str, Dict[str, str]]:
    """Get cheap/balanced/premium model presets for each available provider.
    
    Used by UI quick-select buttons: "Use cheapest" → selects right model.
    
    Returns:
        {
            "cheap": {"google": "gemini-2.0-flash-lite", "openai": "gpt-4o-mini", ...},
            "balanced": {"google": "gemini-2.0-flash", "anthropic": "claude-sonnet-4-20250514", ...},
            "premium": {"google": "gemini-2.5-pro", ...}
        }
    """
    available = get_available_providers()
    presets = {"cheap": {}, "balanced": {}, "premium": {}}

    for provider in PROVIDERS:
        if not available.get(provider.id):
            continue
        models = get_models_for_provider(provider.id)
        for tier in presets:
            tier_model = next((m for m in models if m.tier == tier), None)
            if tier_model:
                presets[tier][provider.id] = tier_model.id

    return presets


def get_models_flat_for_schedules() -> List[Dict]:
    """Generate flat provider list with models for schedules template.
    
    Backward compatible with old schedules.html format:
    [{"name": "Anthropic", "models": ["claude-sonnet-4-20250514", ...]}]
    """
    available = get_available_providers()
    result = []
    for provider in PROVIDERS:
        if not available.get(provider.id):
            continue
        models = get_models_for_provider(provider.id)
        result.append({
            "name": provider.name,
            "models": [m.id for m in models]
        })
    return result
