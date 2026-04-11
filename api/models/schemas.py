"""
Pydantic schemas for API validation and serialization.
"""

from typing import Optional, List, Any, Dict, Union
from pydantic import BaseModel, Field, field_validator
from datetime import datetime


# Helper: accept both str and datetime for date fields
DateTimeField = Optional[Union[str, datetime]]


# ============================================================================
# Audit schemas
# ============================================================================

class AuditCreate(BaseModel):
    """Schema for creating a new audit."""
    website: str = Field(..., min_length=1)
    sitemap_url: Optional[str] = None
    audit_type: str = Field(..., min_length=1)
    provider: Optional[str] = None
    model: Optional[str] = None
    language: Optional[str] = "English"
    webhook_url: Optional[str] = None      # POST this URL on completion/failure
    max_chars: Optional[int] = Field(None, ge=1000)
    use_direct_mode: Optional[bool] = True
    concurrency: Optional[int] = Field(10, ge=1, le=50)
    use_perplexity: Optional[bool] = False
    prompt_version: Optional[str] = "v3"  # "v3" = prompts/, "v2" = prompts_backup/

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("webhook_url must use http:// or https:// scheme")
        if len(v) > 2048:
            raise ValueError("webhook_url too long (max 2048 chars)")
        return v


class SingleAuditRequest(BaseModel):
    """Schema for single page audit request."""
    url: str = Field(..., min_length=1, description="The specific URL to audit")
    audit_type: str = Field(..., min_length=1, description="Specific audit type or 'god_mode'")
    provider: Optional[str] = None
    model: Optional[str] = None
    language: Optional[str] = "English"



class AuditResponse(BaseModel):
    """Schema for audit responses."""
    id: str
    website: str
    sitemap_url: Optional[str] = None
    audit_type: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    created_at: DateTimeField = None
    started_at: DateTimeField = None
    completed_at: DateTimeField = None
    total_pages: Optional[int] = 0
    pages_scraped: Optional[int] = 0
    pages_analyzed: Optional[int] = 0
    average_score: Optional[float] = None
    batch_job_id: Optional[str] = None
    error_message: Optional[str] = None
    current_step: Optional[str] = None
    progress_percent: Optional[float] = None
    language: Optional[str] = "English"
    webhook_url: Optional[str] = None
    prompt_version: Optional[str] = "v3"

    model_config = {"from_attributes": True}


class AuditListResponse(BaseModel):
    """Schema for paginated audit list."""
    audits: List[AuditResponse]
    total: int


class AuditTypeInfo(BaseModel):
    """Schema for audit type metadata."""
    type: str
    name: str
    description: Optional[str] = None
    is_custom: Optional[bool] = False


class StatsResponse(BaseModel):
    """Schema for dashboard statistics."""
    total_audits: int = 0
    pending_audits: int = 0
    running_audits: int = 0
    completed_audits: int = 0
    failed_audits: int = 0
    total_pages_analyzed: int = 0
    average_score: Optional[float] = None


# ============================================================================
# Audit Result schemas
# ============================================================================

class AuditResultResponse(BaseModel):
    """Schema for individual audit result."""
    id: int
    audit_id: Optional[str] = None
    page_url: Optional[str] = None
    filename: Optional[str] = None
    score: Optional[float] = None
    classification: Optional[str] = None
    result_json: Optional[Any] = None
    created_at: DateTimeField = None

    model_config = {"from_attributes": True}


class AuditResultsResponse(BaseModel):
    """Schema for audit results list."""
    results: List[AuditResultResponse]
    total: int


class AuditLogResponse(BaseModel):
    """Schema for audit log entry."""
    id: int
    audit_id: Optional[str] = None
    level: Optional[str] = None
    message: Optional[str] = None
    created_at: DateTimeField = None

    model_config = {"from_attributes": True}


# ============================================================================
# Health schemas
# ============================================================================

class HealthResponse(BaseModel):
    """Schema for health check response."""
    status: str                                # "healthy" | "degraded" | "unhealthy"
    version: str
    database: str                              # "connected" | "error: <msg>"
    db_response_ms: Optional[float] = None    # Round-trip time for the DB ping in ms
    providers: Optional[Dict[str, Any]] = None
    active_audits: Optional[int] = 0
    # Populated only when ?deep=true is passed
    provider_checks: Optional[Dict[str, Any]] = None


# ============================================================================
# Audit Template schemas
# ============================================================================


class AuditTemplateBase(BaseModel):
    """Base schema for audit template."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=10)
    
    # Audit config
    audit_type: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    language: Optional[str] = None
    use_perplexity: Optional[bool] = None
    concurrency: Optional[int] = Field(None, ge=1, le=50)
    max_chars: Optional[int] = Field(None, ge=1000)
    
    # Auto-actions
    auto_summary: bool = False
    summary_provider: Optional[str] = None
    summary_model: Optional[str] = None
    auto_briefs: bool = False
    auto_schemas: bool = False
    
    # Metadata
    is_default: bool = False


class AuditTemplateCreate(AuditTemplateBase):
    """Schema for creating a new audit template."""
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError('Name cannot be empty')
        return v.strip()
    
    @field_validator('audit_type', 'provider')
    @classmethod
    def validate_required_fields(cls, v, info):
        # At least one of audit_type or provider must be set
        return v


class AuditTemplateUpdate(BaseModel):
    """Schema for updating an audit template."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=10)
    
    # Audit config
    audit_type: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    language: Optional[str] = None
    use_perplexity: Optional[bool] = None
    concurrency: Optional[int] = Field(None, ge=1, le=50)
    max_chars: Optional[int] = Field(None, ge=1000)
    
    # Auto-actions
    auto_summary: Optional[bool] = None
    summary_provider: Optional[str] = None
    summary_model: Optional[str] = None
    auto_briefs: Optional[bool] = None
    auto_schemas: Optional[bool] = None
    
    # Metadata
    is_default: Optional[bool] = None


class AuditTemplateResponse(AuditTemplateBase):
    """Schema for template responses."""
    id: int
    use_count: int
    created_at: datetime
    updated_at: datetime
    
    model_config = {"from_attributes": True}


class TemplateLaunchRequest(BaseModel):
    """Schema for launching an audit from a template."""
    website: str = Field(..., min_length=1)
    sitemap_url: Optional[str] = None
    
    @field_validator('website')
    @classmethod
    def validate_website(cls, v):
        if not v or not v.strip():
            raise ValueError('Website URL is required')
        # Basic URL validation
        v = v.strip()
        if not v.startswith(('http://', 'https://')):
            v = 'https://' + v
        return v


class SaveFromAuditRequest(BaseModel):
    """Schema for creating a template from an existing audit."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=10)
    is_default: bool = False
    
    # Optional overrides
    auto_summary: Optional[bool] = None
    summary_provider: Optional[str] = None
    summary_model: Optional[str] = None
    auto_briefs: Optional[bool] = None
    auto_schemas: Optional[bool] = None
