"""Content-related ORM models (Briefs, Schema, Citations, Gaps, Actions)."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON, Boolean, func
)
from sqlalchemy.orm import relationship

from api.models._base import Base

class ContentBrief(Base):
    """AI-generated content brief for a specific page."""
    __tablename__ = "content_briefs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), nullable=True, index=True)
    result_id = Column(Integer, ForeignKey("audit_results.id", ondelete="CASCADE"), nullable=True, index=True)
    page_url = Column(String(512), nullable=True)
    brief_json = Column(Text, nullable=True)  # Full brief as JSON
    status = Column(String(20), default="generated")  # generated, approved, in_progress, completed, failed
    priority = Column(String(20), default="medium")  # critical, high, medium, low
    provider = Column(String(20), nullable=True)
    model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    audit = relationship("Audit", backref="briefs")

    def to_dict(self):
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "result_id": self.result_id,
            "page_url": self.page_url,
            "brief": json.loads(self.brief_json) if self.brief_json else None,
            "status": self.status,
            "priority": self.priority,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }



class SchemaMarkup(Base):
    """AI-generated schema.org markup for a page."""
    __tablename__ = "schema_markups"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), nullable=True, index=True)
    result_id = Column(Integer, ForeignKey("audit_results.id", ondelete="CASCADE"), nullable=True, index=True)
    page_url = Column(String(512), nullable=False)
    schema_type = Column(String(100))  # "Product", "Article", "Organization", etc.
    schema_json = Column(Text)  # JSON-LD markup
    validation_status = Column(String(20), nullable=True)  # valid, has_warnings, invalid, validated
    validation_notes = Column(Text, nullable=True)  # JSON array of validation notes
    provider = Column(String(20))
    model = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship
    audit = relationship("Audit", backref="schemas")

    def to_dict(self):
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "result_id": self.result_id,
            "page_url": self.page_url,
            "schema_type": self.schema_type,
            "schema_json": json.loads(self.schema_json) if self.schema_json else {},
            "validation_status": self.validation_status,
            "validation_notes": json.loads(self.validation_notes) if self.validation_notes else [],
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }



class CitationTracker(Base):
    """Project for tracking citations across AI platforms."""
    __tablename__ = "citation_trackers"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    website = Column(String(255), nullable=False)
    url_patterns = Column(Text, nullable=False)  # JSON array: ["ing.ro", "www.ing.ro"]
    tracking_queries = Column(Text, nullable=False)  # JSON array: 20-50 queries
    providers_config = Column(Text, nullable=False)  # JSON: {"chatgpt": true, "claude": true, "perplexity": true}
    schedule_cron = Column(String(100), nullable=True)  # "0 9 * * 1" (weekly)
    is_active = Column(Integer, default=1)
    last_scan_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship to scans
    scans = relationship("CitationScan", back_populates="tracker", cascade="all, delete-orphan")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        import json
        
        url_patterns_parsed = []
        tracking_queries_parsed = []
        providers_config_parsed = {}
        
        try:
            if self.url_patterns:
                url_patterns_parsed = json.loads(self.url_patterns)
        except (json.JSONDecodeError, TypeError):
            url_patterns_parsed = []
        
        try:
            if self.tracking_queries:
                tracking_queries_parsed = json.loads(self.tracking_queries)
        except (json.JSONDecodeError, TypeError):
            tracking_queries_parsed = []
        
        try:
            if self.providers_config:
                providers_config_parsed = json.loads(self.providers_config)
        except (json.JSONDecodeError, TypeError):
            providers_config_parsed = {}
        
        return {
            "id": self.id,
            "name": self.name,
            "website": self.website,
            "url_patterns": url_patterns_parsed,
            "tracking_queries": tracking_queries_parsed,
            "providers_config": providers_config_parsed,
            "schedule_cron": self.schedule_cron,
            "is_active": bool(self.is_active),
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }



class CitationScan(Base):
    """A single citation scan run with aggregated metrics."""
    __tablename__ = "citation_scans"
    
    id = Column(String(36), primary_key=True)
    tracker_id = Column(String(36), ForeignKey("citation_trackers.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), default="pending")
    total_queries = Column(Integer, default=0)
    total_citations = Column(Integer, default=0)  # URL appeared in response
    total_mentions = Column(Integer, default=0)  # Brand mentioned (broader)
    citation_rate = Column(Float, nullable=True)  # citations / (queries × providers) × 100
    results_json = Column(Text, nullable=True)  # Full results per query per provider
    provider_breakdown = Column(Text, nullable=True)  # JSON: {"chatgpt": {citations: 5, mentions: 8, queries: 20}, ...}
    top_cited_urls = Column(Text, nullable=True)  # JSON: [{"url": "/services", "count": 12}, ...]
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship back to tracker
    tracker = relationship("CitationTracker", back_populates="scans")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        import json
        
        results_parsed = []
        provider_breakdown_parsed = {}
        top_cited_urls_parsed = []
        
        try:
            if self.results_json:
                results_parsed = json.loads(self.results_json)
        except (json.JSONDecodeError, TypeError):
            results_parsed = []
        
        try:
            if self.provider_breakdown:
                provider_breakdown_parsed = json.loads(self.provider_breakdown)
        except (json.JSONDecodeError, TypeError):
            provider_breakdown_parsed = {}
        
        try:
            if self.top_cited_urls:
                top_cited_urls_parsed = json.loads(self.top_cited_urls)
        except (json.JSONDecodeError, TypeError):
            top_cited_urls_parsed = []
        
        return {
            "id": self.id,
            "tracker_id": self.tracker_id,
            "status": self.status,
            "total_queries": self.total_queries,
            "total_citations": self.total_citations,
            "total_mentions": self.total_mentions,
            "citation_rate": self.citation_rate,
            "results_json": results_parsed,
            "provider_breakdown": provider_breakdown_parsed,
            "top_cited_urls": top_cited_urls_parsed,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }



class CompetitorGapAnalysis(Base):
    """Per-criterion competitor gap analysis results."""
    __tablename__ = "competitor_gap_analyses"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    benchmark_id = Column(String(36), ForeignKey("benchmark_projects.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=True)
    target_website = Column(String(500), nullable=True)
    target_audit_id = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False, index=True)
    competitor_audit_ids = Column(Text, nullable=True)  # JSON array of audit IDs
    status = Column(String(20), default="pending")  # pending, running, completed, failed
    overall_gap_score = Column(Float, nullable=True)
    gaps_json = Column(Text, nullable=True)  # JSON: per-criterion gaps
    strengths_json = Column(Text, nullable=True)  # JSON: areas where target wins
    recommendations_json = Column(Text, nullable=True)  # JSON: fix actions
    error_message = Column(Text, nullable=True)
    provider = Column(String(20), nullable=True)
    model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    def to_dict(self):
        return {
            "id": self.id,
            "benchmark_id": self.benchmark_id,
            "name": self.name,
            "target_website": self.target_website,
            "target_audit_id": self.target_audit_id,
            "competitor_audit_ids": json.loads(self.competitor_audit_ids) if self.competitor_audit_ids else [],
            "status": self.status,
            "overall_gap_score": self.overall_gap_score,
            "gaps": json.loads(self.gaps_json) if self.gaps_json else [],
            "strengths": json.loads(self.strengths_json) if self.strengths_json else [],
            "recommendations": json.loads(self.recommendations_json) if self.recommendations_json else [],
            "error_message": self.error_message,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None
        }



class ContentGap(Base):
    """Content gap identified from multiple signal sources."""
    __tablename__ = "content_gaps"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    analysis_id = Column(String(36), nullable=False, index=True)  # Groups gaps from same analysis run
    website = Column(String(500), nullable=False, index=True)
    topic = Column(String(500), nullable=False)
    gap_source = Column(String(50), nullable=True)  # geo_monitor, citation_tracker, competitor
    source_detail = Column(Text, nullable=True)  # JSON: source-specific details
    priority = Column(String(20), default="medium")  # high, medium, low
    priority_score = Column(Float, default=50.0)
    content_type = Column(String(50), nullable=True)  # blog_post, landing_page, faq, etc.
    suggested_title = Column(String(500), nullable=True)
    suggested_url_slug = Column(String(255), nullable=True)
    target_keywords = Column(Text, nullable=True)  # JSON array
    estimated_word_count = Column(Integer, nullable=True)
    estimated_effort = Column(String(20), nullable=True)  # low, medium, high
    brief_json = Column(Text, nullable=True)  # JSON: full content brief
    status = Column(String(20), default="identified")  # identified, in_progress, published, dismissed
    provider = Column(String(20), nullable=True)
    model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            "id": self.id,
            "analysis_id": self.analysis_id,
            "website": self.website,
            "topic": self.topic,
            "gap_source": self.gap_source,
            "source_detail": json.loads(self.source_detail) if self.source_detail else None,
            "priority": self.priority,
            "priority_score": self.priority_score,
            "content_type": self.content_type,
            "suggested_title": self.suggested_title,
            "suggested_url_slug": self.suggested_url_slug,
            "target_keywords": json.loads(self.target_keywords) if self.target_keywords else [],
            "estimated_word_count": self.estimated_word_count,
            "estimated_effort": self.estimated_effort,
            "brief_json": json.loads(self.brief_json) if self.brief_json else None,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }



class ActionCard(Base):
    """Page-level action cards with concrete fix actions."""
    __tablename__ = "action_cards"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    audit_id = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False, index=True)
    result_id = Column(Integer, ForeignKey("audit_results.id", ondelete="CASCADE"), nullable=True, index=True)
    page_url = Column(String(500), nullable=True)
    page_title = Column(String(500), nullable=True)
    current_score = Column(Float, nullable=True)
    target_score = Column(Float, nullable=True)
    priority = Column(String(20), default="medium")  # critical, high, medium, low
    status = Column(String(20), default="pending")  # pending, in_progress, completed, dismissed
    actions_json = Column(Text, nullable=True)  # JSON: list of concrete actions
    total_actions = Column(Integer, default=0)
    completed_actions = Column(Integer, default=0)
    provider = Column(String(20), nullable=True)
    model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    audit = relationship("Audit", backref="action_cards")
    
    def to_dict(self):
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "result_id": self.result_id,
            "page_url": self.page_url,
            "page_title": self.page_title,
            "current_score": self.current_score,
            "target_score": self.target_score,
            "priority": self.priority,
            "status": self.status,
            "actions": json.loads(self.actions_json) if self.actions_json else [],
            "total_actions": self.total_actions,
            "completed_actions": self.completed_actions,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }



class CrossReferenceJob(Base):
    """
    Persisted cross-reference analysis job.

    Replaces the in-memory _jobs dict in cross_reference.py so that job
    status survives server restarts.
    """
    __tablename__ = "cross_reference_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    website = Column(String(255), nullable=False, index=True)
    audit_type = Column(String(50), nullable=False)
    no_llm = Column(Integer, default=0)           # stored as 0/1 (SQLite has no BOOLEAN)
    provider = Column(String(20), nullable=True)
    model = Column(String(100), nullable=True)
    status = Column(String(20), default="queued", index=True)  # queued|running|completed|failed
    output_path = Column(String(512), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "job_id": self.id,
            "website": self.website,
            "audit_type": self.audit_type,
            "no_llm": bool(self.no_llm),
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "output_path": self.output_path,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }



class UrlGuide(Base):
    """An LLM-generated GEO & SEO improvement guide for a specific page URL."""
    __tablename__ = "url_guides"

    id              = Column(Integer,      primary_key=True, autoincrement=True)
    url             = Column(String(2000), nullable=False, index=True)
    status          = Column(String(20),   nullable=False, default="pending")  # pending|running|completed|failed
    provider        = Column(String(50),   nullable=True)
    model           = Column(String(100),  nullable=True)
    gsc_property_id = Column(String(36),   nullable=True)   # GSC property used for per-page queries
    guide_json      = Column(Text,         nullable=True)   # structured JSON from LLM
    error_message   = Column(Text,         nullable=True)
    reviewed        = Column(Boolean,      nullable=False, default=False)
    created_at      = Column(DateTime,     default=datetime.utcnow)
    updated_at      = Column(DateTime,     default=datetime.utcnow, onupdate=datetime.utcnow)


# ------------------------------------------------------------------
# llms.txt Generator
# ------------------------------------------------------------------


class LlmsTxtJob(Base):
    """A job that generates a valid llms.txt file for a website."""
    __tablename__ = "llms_txt_jobs"

    id               = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name             = Column(String(255), nullable=False)
    site_url         = Column(String(500), nullable=False)
    site_name        = Column(String(255), nullable=True)   # H1 heading
    # Optional data sources
    audit_id         = Column(String(36),  ForeignKey("audits.id", ondelete="SET NULL"),  nullable=True)
    gsc_property_id  = Column(String(36),  ForeignKey("gsc_properties.id", ondelete="SET NULL"), nullable=True)
    # LLM settings
    llm_provider     = Column(String(50),  nullable=False, default="anthropic")
    llm_model        = Column(String(100), nullable=True)
    # Status tracking
    status           = Column(String(20),  nullable=False, default="pending")  # pending|running|completed|failed
    progress         = Column(Integer,     nullable=False, default=0)
    progress_message = Column(String(500), nullable=True)
    error            = Column(Text,        nullable=True)
    # Output
    generated_content = Column(Text,       nullable=True)   # full llms.txt markdown
    page_count        = Column(Integer,    nullable=False, default=0)
    # Timestamps
    created_at       = Column(DateTime,    default=datetime.utcnow)
    completed_at     = Column(DateTime,    nullable=True)


# ============================================================================
# Fan-Out Analyzer models
# ============================================================================

class FanoutSession(Base):
    """
    One fan-out analysis run for a single prompt.

    Records the prompt, provider/model used, aggregate stats, and an optional
    target URL so callers can track whether their site appears in AI sources.
    """
    __tablename__ = "fanout_sessions"

    id              = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    prompt          = Column(Text,        nullable=False)
    provider        = Column(String(20),  nullable=False)           # openai | anthropic | gemini | perplexity
    model           = Column(String(100), nullable=False)
    user_location   = Column(String(200), nullable=True)
    total_fanout_queries  = Column(Integer, default=0)
    total_sources         = Column(Integer, default=0)
    total_search_calls    = Column(Integer, default=0)
    target_url            = Column(String(500), nullable=True, index=True)
    target_found          = Column(Boolean, default=False)
    target_position       = Column(Integer, nullable=True)          # 1-based index in sources list
    # Optional linkage to other geo_tool entities
    audit_id        = Column(String(36),  ForeignKey("audits.id",  ondelete="SET NULL"), nullable=True, index=True)
    created_at      = Column(DateTime,    default=datetime.utcnow, index=True)
    # ── Prompt 15 enrichment columns ─────────────────────────────────────────
    query_origin      = Column(String(20),  default="actual")        # actual | inferred | generated
    source_origin     = Column(String(20),  default="citation")      # citation | grounding | extracted
    prompt_cluster    = Column(String(50),  nullable=True, index=True)
    run_cost_usd      = Column(Float,       default=0.0)
    locale            = Column(String(20),  default="en-US")
    language          = Column(String(10),  default="en")
    confidence_score  = Column(Float,       nullable=True)
    engine            = Column(String(50),  nullable=True, index=True)  # alias for provider (multi-engine sessions)
    model_version     = Column(String(50),  nullable=True)
    from_cache        = Column(Boolean,     default=False)

    # Relationships
    queries = relationship("FanoutQuery",  back_populates="session", cascade="all, delete-orphan")
    sources = relationship("FanoutSource", back_populates="session", cascade="all, delete-orphan")

    def to_dict(self, include_children: bool = False) -> dict:
        data = {
            "id":                   self.id,
            "prompt":               self.prompt,
            "provider":             self.provider,
            "model":                self.model,
            "user_location":        self.user_location,
            "total_fanout_queries": self.total_fanout_queries,
            "total_sources":        self.total_sources,
            "total_search_calls":   self.total_search_calls,
            "target_url":           self.target_url,
            "target_found":         self.target_found,
            "target_position":      self.target_position,
            "audit_id":             self.audit_id,
            "created_at":           self.created_at.isoformat() if self.created_at else None,
            # Enrichment
            "query_origin":         self.query_origin,
            "source_origin":        self.source_origin,
            "prompt_cluster":       self.prompt_cluster,
            "run_cost_usd":         self.run_cost_usd,
            "locale":               self.locale,
            "language":             self.language,
            "confidence_score":     self.confidence_score,
            "engine":               self.engine or self.provider,
            "model_version":        self.model_version or self.model,
            "from_cache":           self.from_cache,
        }
        if include_children:
            data["fanout_queries"] = [q.to_dict() for q in (self.queries or [])]
            data["sources"]        = [s.to_dict() for s in (self.sources or [])]
        return data


class FanoutQuery(Base):
    """A single predicted/actual search query extracted from a fan-out session."""
    __tablename__ = "fanout_queries"

    id            = Column(Integer,     primary_key=True, autoincrement=True)
    session_id    = Column(String(36),  ForeignKey("fanout_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    query_text    = Column(Text,        nullable=False)
    query_position = Column(Integer,   nullable=True)   # 1-based order of appearance

    session = relationship("FanoutSession", back_populates="queries")

    def to_dict(self) -> dict:
        return {
            "id":             self.id,
            "session_id":     self.session_id,
            "query_text":     self.query_text,
            "query_position": self.query_position,
        }


class FanoutSource(Base):
    """A cited source URL extracted from an AI response during fan-out analysis."""
    __tablename__ = "fanout_sources"

    id             = Column(Integer,     primary_key=True, autoincrement=True)
    session_id     = Column(String(36),  ForeignKey("fanout_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    url            = Column(String(2000), nullable=False)
    title          = Column(String(500),  nullable=True)
    domain         = Column(String(500),  nullable=True, index=True)
    is_target      = Column(Boolean,      default=False)   # True if domain matches target_url
    source_position = Column(Integer,    nullable=True)   # 1-based order in sources list

    session = relationship("FanoutSession", back_populates="sources")

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "session_id":      self.session_id,
            "url":             self.url,
            "title":           self.title,
            "domain":          self.domain,
            "is_target":       self.is_target,
            "source_position": self.source_position,
        }


# ============================================================================
# FAN-OUT HISTORICAL TRACKING
# ============================================================================

class FanoutTrackingConfig(Base):
    """
    A recurring tracking job: watch how a set of prompts mention a domain
    across one or more AI engines on a schedule (daily/weekly/monthly).
    """
    __tablename__ = "fanout_tracking_configs"

    id            = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name          = Column(String(200), nullable=False)
    target_domain = Column(String(500), nullable=True, index=True)
    target_brand  = Column(String(200), nullable=True)
    prompts       = Column(JSON,        nullable=False)          # List[str]
    engines       = Column(JSON,        default=lambda: ["openai"])
    schedule      = Column(String(20),  default="weekly")        # daily | weekly | monthly
    is_active     = Column(Boolean,     default=True,  index=True)
    last_run_at   = Column(DateTime,    nullable=True)
    next_run_at   = Column(DateTime,    nullable=True, index=True)
    project_id    = Column(String(36),  nullable=True)
    created_at    = Column(DateTime,    default=datetime.utcnow)

    runs = relationship("FanoutTrackingRun", back_populates="config", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "name":          self.name,
            "target_domain": self.target_domain,
            "target_brand":  self.target_brand,
            "prompts":       self.prompts or [],
            "engines":       self.engines or ["openai"],
            "schedule":      self.schedule,
            "is_active":     self.is_active,
            "last_run_at":   self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at":   self.next_run_at.isoformat() if self.next_run_at else None,
            "project_id":    self.project_id,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
        }


class FanoutTrackingRun(Base):
    """
    One execution of a FanoutTrackingConfig — aggregate stats for a single date.
    Includes retry/dead-letter fields (from Prompt 29).
    """
    __tablename__ = "fanout_tracking_runs"

    id                   = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    config_id            = Column(String(36),  ForeignKey("fanout_tracking_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    run_date             = Column(String(10),  nullable=False)         # ISO date "YYYY-MM-DD"
    total_prompts        = Column(Integer,     nullable=True)
    mention_rate         = Column(Float,       nullable=True)
    avg_source_position  = Column(Float,       nullable=True)
    total_unique_sources = Column(Integer,     nullable=True)
    composite_score      = Column(Float,       nullable=True)
    score_breakdown      = Column(JSON,        nullable=True)
    sentiment_breakdown  = Column(JSON,        nullable=True)
    top_competitors      = Column(JSON,        nullable=True)          # [{domain, appearances}]
    model_version        = Column(String(50),  nullable=True)
    baseline_mention_rate = Column(Float,      nullable=True)
    cost_usd             = Column(Float,       default=0.0)
    # Retry / dead-letter (Prompt 29)
    retry_count          = Column(Integer,     default=0)
    max_retries          = Column(Integer,     default=3)
    next_retry_at        = Column(DateTime,    nullable=True)
    failure_reason       = Column(String(500), nullable=True)
    is_dead_letter       = Column(Boolean,     default=False, index=True)
    status               = Column(String(20),  default="pending", index=True)  # pending|running|completed|failed
    error_message        = Column(Text,        nullable=True)
    created_at           = Column(DateTime,    default=datetime.utcnow)

    config  = relationship("FanoutTrackingConfig", back_populates="runs")
    details = relationship("FanoutTrackingDetail",  back_populates="run", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "config_id":            self.config_id,
            "run_date":             self.run_date,
            "total_prompts":        self.total_prompts,
            "mention_rate":         self.mention_rate,
            "avg_source_position":  self.avg_source_position,
            "total_unique_sources": self.total_unique_sources,
            "composite_score":      self.composite_score,
            "score_breakdown":      self.score_breakdown,
            "top_competitors":      self.top_competitors,
            "model_version":        self.model_version,
            "cost_usd":             self.cost_usd,
            "status":               self.status,
            "retry_count":          self.retry_count,
            "is_dead_letter":       self.is_dead_letter,
            "failure_reason":       self.failure_reason,
            "created_at":           self.created_at.isoformat() if self.created_at else None,
        }


class FanoutTrackingDetail(Base):
    """Per-prompt, per-engine detail row for a tracking run."""
    __tablename__ = "fanout_tracking_details"

    id                = Column(Integer,    primary_key=True, autoincrement=True)
    run_id            = Column(String(36), ForeignKey("fanout_tracking_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt            = Column(Text,       nullable=False)
    prompt_cluster    = Column(String(50), nullable=True)
    engine            = Column(String(50), nullable=True)
    query_origin      = Column(String(20), default="actual")
    target_found      = Column(Boolean,    default=False)
    source_position   = Column(Integer,    nullable=True)   # 1-based, None if not found
    fanout_query_count = Column(Integer,   nullable=True)
    source_count      = Column(Integer,    nullable=True)
    session_id        = Column(String(36), nullable=True)   # FK to fanout_sessions if saved

    run = relationship("FanoutTrackingRun", back_populates="details")

    def to_dict(self) -> dict:
        return {
            "id":                 self.id,
            "run_id":             self.run_id,
            "prompt":             self.prompt,
            "prompt_cluster":     self.prompt_cluster,
            "engine":             self.engine,
            "target_found":       self.target_found,
            "source_position":    self.source_position,
            "fanout_query_count": self.fanout_query_count,
            "source_count":       self.source_count,
        }


class FanoutCompetitiveReport(Base):
    """Stored result of a competitive fan-out comparison run."""
    __tablename__ = "fanout_competitive_reports"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_domain = Column(String(500), nullable=True, index=True)
    project_id    = Column(String(36),  nullable=True)
    competitors   = Column(JSON,        nullable=True)   # List[str]
    report        = Column(JSON,        nullable=True)   # full CompetitiveReport dict
    created_at    = Column(DateTime,    default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "target_domain": self.target_domain,
            "project_id":    self.project_id,
            "competitors":   self.competitors,
            "report":        self.report,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# FAN-OUT CACHE  (Prompt 16)
# ============================================================================

class FanoutCacheEntry(Base):
    """
    Stores serialised FanoutResult objects to avoid re-calling AI APIs.
    TTL buckets: adhoc=4h | daily=20h | weekly=160h | monthly=700h
    """
    __tablename__ = "fanout_cache"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    cache_key    = Column(String(64),  nullable=False, unique=True, index=True)
    prompt_hash  = Column(String(64),  nullable=False, index=True)
    engine       = Column(String(50),  nullable=False)
    model        = Column(String(100), nullable=False)
    locale       = Column(String(20),  default="en-US")
    result_json  = Column(Text,        nullable=False)   # JSON-serialised FanoutResult
    hit_count    = Column(Integer,     default=0)
    created_at   = Column(DateTime,    default=datetime.utcnow, index=True)
    expires_at   = Column(DateTime,    nullable=False,           index=True)

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "cache_key":   self.cache_key,
            "engine":      self.engine,
            "model":       self.model,
            "locale":      self.locale,
            "hit_count":   self.hit_count,
            "created_at":  self.created_at.isoformat() if self.created_at else None,
            "expires_at":  self.expires_at.isoformat() if self.expires_at else None,
        }


# ============================================================================
# SERP VALIDATION  (Prompt 19)
# ============================================================================

class FanoutSerpValidation(Base):
    """Per-query SERP validation result via Serper.dev."""
    __tablename__ = "fanout_serp_validation"

    id                     = Column(Integer,     primary_key=True, autoincrement=True)
    session_id             = Column(String(36),  ForeignKey("fanout_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    query_text             = Column(Text,        nullable=False)
    target_domain          = Column(String(500), nullable=True)
    target_found           = Column(Boolean,     default=False)
    target_position        = Column(Integer,     nullable=True)
    has_featured_snippet   = Column(Boolean,     default=False)
    featured_snippet_domain = Column(String(500), nullable=True)
    top_10_domains         = Column(JSON,        nullable=True)
    people_also_ask        = Column(JSON,        nullable=True)
    gl                     = Column(String(5),   default="us")
    hl                     = Column(String(5),   default="en")
    cost_usd               = Column(Float,       default=0.001)
    validated_at           = Column(DateTime,    default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id":                      self.id,
            "session_id":              self.session_id,
            "query_text":              self.query_text,
            "target_domain":           self.target_domain,
            "target_found":            self.target_found,
            "target_position":         self.target_position,
            "has_featured_snippet":    self.has_featured_snippet,
            "featured_snippet_domain": self.featured_snippet_domain,
            "top_10_domains":          self.top_10_domains,
            "people_also_ask":         self.people_also_ask,
            "gl":                      self.gl,
            "hl":                      self.hl,
            "cost_usd":                self.cost_usd,
            "validated_at":            self.validated_at.isoformat() if self.validated_at else None,
        }


# ============================================================================
# WEBHOOKS  (Prompt 20)
# ============================================================================

class FanoutWebhook(Base):
    """Registered webhook endpoint for fan-out events."""
    __tablename__ = "fanout_webhooks"

    id          = Column(Integer,     primary_key=True, autoincrement=True)
    name        = Column(String(200), nullable=False)
    webhook_url = Column(String(2000), nullable=False)
    events      = Column(JSON,        nullable=False)   # List[str] event names
    is_active   = Column(Boolean,     default=True, index=True)
    secret_key  = Column(String(200), nullable=True)
    created_at  = Column(DateTime,    default=datetime.utcnow)

    logs = relationship("FanoutWebhookLog", back_populates="webhook", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "name":        self.name,
            "webhook_url": self.webhook_url,
            "events":      self.events,
            "is_active":   self.is_active,
            "created_at":  self.created_at.isoformat() if self.created_at else None,
        }


class FanoutWebhookLog(Base):
    """Delivery log entry for a webhook call."""
    __tablename__ = "fanout_webhook_logs"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    webhook_id   = Column(Integer,     ForeignKey("fanout_webhooks.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type   = Column(String(100), nullable=False)
    status       = Column(String(20),  nullable=False)   # success | failure
    error        = Column(String(500), nullable=True)
    payload_size = Column(Integer,     nullable=True)
    response_code = Column(Integer,   nullable=True)
    sent_at      = Column(DateTime,    default=datetime.utcnow, index=True)

    webhook = relationship("FanoutWebhook", back_populates="logs")

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "webhook_id":   self.webhook_id,
            "event_type":   self.event_type,
            "status":       self.status,
            "error":        self.error,
            "payload_size": self.payload_size,
            "response_code": self.response_code,
            "sent_at":      self.sent_at.isoformat() if self.sent_at else None,
        }


# ============================================================================
# WLA CROSS-REFERENCE  (Prompt 18)
# ============================================================================

class FanoutCrossRefResult(Base):
    """Stored result of a fan-out × WLA audit cross-reference analysis."""
    __tablename__ = "fanout_crossref_results"

    id            = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id    = Column(String(36),  ForeignKey("fanout_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    audit_id      = Column(String(36),  nullable=True, index=True)
    project_id    = Column(String(36),  nullable=True)
    target_domain = Column(String(500), nullable=True)
    result_json   = Column(Text,        nullable=False)   # JSON-serialised CrossRefResult
    created_at    = Column(DateTime,    default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        import json as _json
        try:
            result = _json.loads(self.result_json)
        except Exception:
            result = {}
        return {
            "id":            self.id,
            "session_id":    self.session_id,
            "audit_id":      self.audit_id,
            "project_id":    self.project_id,
            "target_domain": self.target_domain,
            "result":        result,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# PROMPT LIBRARY  (Prompt 21)
# ============================================================================

class FanoutPromptLibrary(Base):
    """Reusable prompt templates with aggregated performance statistics."""
    __tablename__ = "fanout_prompt_library"

    id               = Column(Integer,     primary_key=True, autoincrement=True)
    prompt_text      = Column(Text,        nullable=False)
    prompt_hash      = Column(String(64),  nullable=False, unique=True, index=True)
    vertical         = Column(String(100), nullable=False, default="generic", index=True)
    cluster          = Column(String(50),  nullable=True, index=True)
    language         = Column(String(10),  default="en")
    locale           = Column(String(20),  default="en-US")
    tags             = Column(JSON,        nullable=True)
    is_template      = Column(Boolean,     default=False)
    template_vars    = Column(JSON,        nullable=True)     # e.g. ["brand", "city", "year"]
    times_used       = Column(Integer,     default=0)
    avg_fanout_queries = Column(Float,     nullable=True)
    avg_mention_rate = Column(Float,       nullable=True)
    avg_source_count = Column(Float,       nullable=True)
    performance_tier = Column(String(20),  default="untested", index=True)  # high|medium|low|untested
    created_at       = Column(DateTime,    default=datetime.utcnow)
    last_used_at     = Column(DateTime,    nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "prompt_text":       self.prompt_text,
            "prompt_hash":       self.prompt_hash,
            "vertical":          self.vertical,
            "cluster":           self.cluster,
            "language":          self.language,
            "locale":            self.locale,
            "tags":              self.tags,
            "is_template":       self.is_template,
            "template_vars":     self.template_vars,
            "times_used":        self.times_used,
            "avg_fanout_queries": self.avg_fanout_queries,
            "avg_mention_rate":  self.avg_mention_rate,
            "avg_source_count":  self.avg_source_count,
            "performance_tier":  self.performance_tier,
            "created_at":        self.created_at.isoformat() if self.created_at else None,
            "last_used_at":      self.last_used_at.isoformat() if self.last_used_at else None,
        }


# ============================================================================
# PROJECTS  (Prompt 25)
# ============================================================================

class FanoutProject(Base):
    """
    Client/project container — groups fan-out sessions, tracking configs,
    competitive reports and cross-references for one brand/domain.
    """
    __tablename__ = "fanout_projects"

    id            = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name          = Column(String(200), nullable=False)
    client_name   = Column(String(200), nullable=True)
    target_domain = Column(String(500), nullable=False)
    target_brand  = Column(String(200), nullable=False)
    vertical      = Column(String(100), default="generic", index=True)
    locale        = Column(String(20),  default="en-US")
    language      = Column(String(10),  default="en")
    gl            = Column(String(5),   default="us")
    color         = Column(String(7),   default="#6366f1")
    notes         = Column(Text,        nullable=True)
    is_active     = Column(Boolean,     default=True, index=True)
    created_at    = Column(DateTime,    default=datetime.utcnow)
    updated_at    = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "name":          self.name,
            "client_name":   self.client_name,
            "target_domain": self.target_domain,
            "target_brand":  self.target_brand,
            "vertical":      self.vertical,
            "locale":        self.locale,
            "language":      self.language,
            "gl":            self.gl,
            "color":         self.color,
            "notes":         self.notes,
            "is_active":     self.is_active,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
            "updated_at":    self.updated_at.isoformat() if self.updated_at else None,
        }


# ============================================================================
# SENTIMENT  (Prompt 23)
# ============================================================================

class FanoutSentiment(Base):
    """Sentiment analysis result for a fan-out session (Claude Haiku)."""
    __tablename__ = "fanout_sentiment"

    id                  = Column(Integer,     primary_key=True, autoincrement=True)
    session_id          = Column(String(36),  ForeignKey("fanout_sessions.id", ondelete="CASCADE"),
                                 nullable=False, unique=True, index=True)
    overall_sentiment   = Column(String(20),  nullable=False)   # positive|neutral|negative|mixed|not_mentioned
    confidence          = Column(Float,       nullable=True)
    brand_mention_count = Column(Integer,     default=0)
    mentions_json       = Column(JSON,        nullable=True)     # [{text, sentiment, context_type}]
    summary             = Column(Text,        nullable=True)
    analyzed_at         = Column(DateTime,    default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id":                  self.id,
            "session_id":          self.session_id,
            "overall_sentiment":   self.overall_sentiment,
            "confidence":          self.confidence,
            "brand_mention_count": self.brand_mention_count,
            "mentions":            self.mentions_json,
            "summary":             self.summary,
            "analyzed_at":         self.analyzed_at.isoformat() if self.analyzed_at else None,
        }


# ============================================================================
# GEO BENCHMARKS  (Prompt 24)
# ============================================================================

class GeoBenchmark(Base):
    """Aggregated GEO benchmarks per vertical + locale for a calendar month."""
    __tablename__ = "geo_benchmarks"

    id                   = Column(Integer,    primary_key=True, autoincrement=True)
    vertical             = Column(String(100), nullable=False, index=True)
    locale               = Column(String(20),  nullable=False, index=True)
    period_month         = Column(String(7),   nullable=False)   # "2026-04"
    sample_size          = Column(Integer,     default=0)
    avg_mention_rate     = Column(Float,       nullable=True)
    median_mention_rate  = Column(Float,       nullable=True)
    p25_mention_rate     = Column(Float,       nullable=True)
    p75_mention_rate     = Column(Float,       nullable=True)
    avg_composite_score  = Column(Float,       nullable=True)
    calculated_at        = Column(DateTime,    default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":                  self.id,
            "vertical":            self.vertical,
            "locale":              self.locale,
            "period_month":        self.period_month,
            "sample_size":         self.sample_size,
            "avg_mention_rate":    self.avg_mention_rate,
            "median_mention_rate": self.median_mention_rate,
            "p25_mention_rate":    self.p25_mention_rate,
            "p75_mention_rate":    self.p75_mention_rate,
            "avg_composite_score": self.avg_composite_score,
            "calculated_at":       self.calculated_at.isoformat() if self.calculated_at else None,
        }


# ============================================================================
# ENTITY CHECKS  (Prompt 31)
# ============================================================================

class EntityCheck(Base):
    """Entity authority audit result for a brand/domain."""
    __tablename__ = "entity_checks"

    id                    = Column(Integer,    primary_key=True, autoincrement=True)
    project_id            = Column(String(36), nullable=True, index=True)
    target_domain         = Column(String(500), nullable=False)
    target_brand          = Column(String(200), nullable=False)
    report_json           = Column(Text,        nullable=False)   # JSON EntityReport
    entity_authority_score = Column(Float,      nullable=True)
    analyzed_at           = Column(DateTime,    default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        import json as _json
        try:
            report = _json.loads(self.report_json)
        except Exception:
            report = {}
        return {
            "id":                     self.id,
            "project_id":             self.project_id,
            "target_domain":          self.target_domain,
            "target_brand":           self.target_brand,
            "report":                 report,
            "entity_authority_score": self.entity_authority_score,
            "analyzed_at":            self.analyzed_at.isoformat() if self.analyzed_at else None,
        }


# ============================================================================
# GSC FANOUT CONNECTION  (Prompt 27)
# ============================================================================

class GscFanoutConnection(Base):
    """Stores GSC OAuth tokens for fanout-specific GSC cross-reference."""
    __tablename__ = "gsc_fanout_connections"

    id            = Column(Integer,    primary_key=True, autoincrement=True)
    project_id    = Column(String(36), nullable=False, unique=True, index=True)
    gsc_property  = Column(String(500), nullable=False)
    access_token  = Column(Text,        nullable=True)
    refresh_token = Column(Text,        nullable=True)
    token_expiry  = Column(DateTime,    nullable=True)
    created_at    = Column(DateTime,    default=datetime.utcnow)
    updated_at    = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "project_id":   self.project_id,
            "gsc_property": self.gsc_property,
            "has_token":    bool(self.access_token),
            "token_expiry": self.token_expiry.isoformat() if self.token_expiry else None,
            "created_at":   self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================================
# MENTION SEEDING  (Prompt 32 — Phase 5 model added early)
# ============================================================================

class MentionSeedingConfig(Base):
    """Configuration for monitoring brand presence on authoritative platforms."""
    __tablename__ = "mention_seeding_configs"

    id                   = Column(Integer,    primary_key=True, autoincrement=True)
    project_id           = Column(String(36), nullable=True, index=True)
    target_brand         = Column(String(200), nullable=False)
    target_domain        = Column(String(500), nullable=False)
    vertical             = Column(String(100), default="generic")
    monitor_reddit       = Column(Boolean,     default=True)
    monitor_quora        = Column(Boolean,     default=True)
    monitor_review_sites = Column(Boolean,     default=True)
    monitor_press        = Column(Boolean,     default=True)
    keywords             = Column(JSON,        nullable=True)
    schedule             = Column(String(20),  default="weekly")
    is_active            = Column(Boolean,     default=True, index=True)
    last_run_at          = Column(DateTime,    nullable=True)
    created_at           = Column(DateTime,    default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "project_id":           self.project_id,
            "target_brand":         self.target_brand,
            "target_domain":        self.target_domain,
            "vertical":             self.vertical,
            "monitor_reddit":       self.monitor_reddit,
            "monitor_quora":        self.monitor_quora,
            "monitor_review_sites": self.monitor_review_sites,
            "monitor_press":        self.monitor_press,
            "keywords":             self.keywords,
            "schedule":             self.schedule,
            "is_active":            self.is_active,
            "last_run_at":          self.last_run_at.isoformat() if self.last_run_at else None,
        }


class MentionSeedingResult(Base):
    """Individual mention found during a seeding scan."""
    __tablename__ = "mention_seeding_results"

    id               = Column(Integer,    primary_key=True, autoincrement=True)
    config_id        = Column(Integer,    ForeignKey("mention_seeding_configs.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    run_date         = Column(String(10), nullable=False)   # ISO date
    platform         = Column(String(50), nullable=False)
    mention_url      = Column(String(2000), nullable=True)
    mention_title    = Column(Text,        nullable=True)
    mention_context  = Column(Text,        nullable=True)
    sentiment        = Column(String(20),  nullable=True)
    is_new           = Column(Boolean,     default=True)
    discovered_at    = Column(DateTime,    default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id":             self.id,
            "config_id":      self.config_id,
            "run_date":       self.run_date,
            "platform":       self.platform,
            "mention_url":    self.mention_url,
            "mention_title":  self.mention_title,
            "mention_context": self.mention_context,
            "sentiment":      self.sentiment,
            "is_new":         self.is_new,
            "discovered_at":  self.discovered_at.isoformat() if self.discovered_at else None,
        }


# Default templates to seed on first run

