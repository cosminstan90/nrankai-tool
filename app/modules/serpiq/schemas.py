"""SerpIQ Pydantic schemas — request/response models."""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class SerpiqAnalyzeRequest(BaseModel):
    """POST /api/serpiq/analyze — kick off a new SERP snapshot."""

    input_value:   str = Field(
        ...,
        min_length=2,
        max_length=2048,
        description="URL or keyword to analyse.",
    )
    location_code: int   = Field(2040, ge=1000, le=9999,  description="DataForSEO location code (default: 2040 = Romania).")
    language_code: str   = Field("ro",  min_length=2, max_length=10, description="Language code (default: 'ro').")
    generate_brief: bool = Field(False, description="If True, also call Claude to generate a 150-word content brief.")

    @field_validator("input_value", mode="before")
    @classmethod
    def strip_input(cls, v: str) -> str:
        return v.strip()


# ---------------------------------------------------------------------------
# SERP item (used inside response and as a standalone schema)
# ---------------------------------------------------------------------------

class SerpItemSchema(BaseModel):
    id:                   Optional[int]       = None
    snapshot_id:          Optional[int]       = None
    position:             int
    url:                  Optional[str]       = None
    domain:               Optional[str]       = None
    title:                Optional[str]       = None
    description:          Optional[str]       = None
    word_count_estimated: Optional[int]       = None
    has_schema:           bool                = False
    schema_types:         Optional[List[str]] = None
    is_featured_snippet:  bool                = False
    is_local_pack:        bool                = False
    is_video:             bool                = False

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# URL analysis payload (embedded in snapshot)
# ---------------------------------------------------------------------------

class UrlAnalysisSchema(BaseModel):
    """On-page metrics extracted for the input URL (only when input_type='url')."""

    title:            Optional[str]  = None
    meta_description: Optional[str]  = None
    h1:               Optional[str]  = None
    word_count:       Optional[int]  = None
    status_code:      Optional[int]  = None
    is_indexable:     Optional[bool] = None
    canonical_url:    Optional[str]  = None
    has_schema:       bool           = False
    schema_types:     Optional[List[str]] = None
    # Ahrefs / authority (populated if API key is available)
    domain_rating:    Optional[int]  = None
    backlinks:        Optional[int]  = None
    referring_domains: Optional[int] = None


# ---------------------------------------------------------------------------
# Snapshot response
# ---------------------------------------------------------------------------

class SnapshotResponse(BaseModel):
    """Full snapshot returned after analysis completes."""

    id:                 int
    input_type:         str                          # 'url' | 'keyword'
    input_value:        str
    keyword:            str
    location_code:      int
    language_code:      str
    verdict:            Optional[str]                = None  # optimize/create/compete/dominate/gap
    verdict_evidence:   Optional[List[str]]          = None
    llm_brief:          Optional[str]                = None
    position_found:     Optional[int]                = None
    url_analysis:       Optional[UrlAnalysisSchema]  = None
    serp_items:         List[SerpItemSchema]         = Field(default_factory=list)
    processing_time_ms: Optional[int]                = None
    created_at:         Optional[str]                = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# History list item (lighter — no serp_items)
# ---------------------------------------------------------------------------

class SnapshotListItem(BaseModel):
    """Lightweight row for history listing."""

    id:               int
    input_type:       str
    input_value:      str
    keyword:          str
    verdict:          Optional[str] = None
    position_found:   Optional[int] = None
    location_code:    int
    language_code:    str
    created_at:       Optional[str] = None

    model_config = {"from_attributes": True}


class SnapshotListResponse(BaseModel):
    items:    List[SnapshotListItem]
    total:    int
    page:     int
    per_page: int


# ---------------------------------------------------------------------------
# Verdict-only response (subset used by the verdict engine)
# ---------------------------------------------------------------------------

class VerdictResult(BaseModel):
    """Returned by the verdict engine — does not include full SERP data."""

    verdict:          str
    evidence:         List[str]
    position_found:   Optional[int] = None
    top_competitor:   Optional[str] = None   # URL of the #1 organic result
    avg_top3_words:   Optional[int] = None   # average word count of positions 1-3
