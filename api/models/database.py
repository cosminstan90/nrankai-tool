"""
Database models and SQLite configuration for Website LLM Analyzer API.

Uses SQLAlchemy with async SQLite for non-blocking database operations.
"""

import os
import json
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON, Boolean,
    create_engine, event, func
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.pool import StaticPool

# Database file location
DATABASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATABASE_DIR, exist_ok=True)
DATABASE_PATH = os.path.join(DATABASE_DIR, "analyzer.db")

# Async SQLite URL
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

# Create async engine
engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    echo=False
)

# Sync engine for migrations
sync_engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

# Session factory
AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# Base class for models
Base = declarative_base()


class Audit(Base):
    """
    Represents a website audit job.
    
    Status flow: pending → scraping → converting → analyzing → completed
                                                           ↘ failed
    """
    __tablename__ = "audits"
    
    id = Column(String(36), primary_key=True)
    website = Column(String(255), nullable=False, index=True)
    sitemap_url = Column(String(512), nullable=True)
    audit_type = Column(String(50), nullable=False)
    provider = Column(String(20), nullable=False)
    model = Column(String(100), nullable=False)
    status = Column(String(20), default="pending", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    total_pages = Column(Integer, default=0)
    pages_scraped = Column(Integer, default=0)
    pages_analyzed = Column(Integer, default=0)
    average_score = Column(Float, nullable=True)
    batch_job_id = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    current_step = Column(String(50), default="pending")
    progress_percent = Column(Integer, default=0)
    # v2.1 additions — safe to add to existing DBs (nullable with sensible defaults)
    language = Column(String(30), nullable=True)           # Output language, e.g. "English"
    webhook_url = Column(String(512), nullable=True)       # Optional completion webhook

    # Relationship to results
    results = relationship("AuditResult", back_populates="audit", cascade="all, delete-orphan")
    logs = relationship("AuditLog", back_populates="audit", cascade="all, delete-orphan")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "website": self.website,
            "sitemap_url": self.sitemap_url,
            "audit_type": self.audit_type,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "total_pages": self.total_pages,
            "pages_scraped": self.pages_scraped,
            "pages_analyzed": self.pages_analyzed,
            "average_score": self.average_score,
            "batch_job_id": self.batch_job_id,
            "error_message": self.error_message,
            "current_step": self.current_step,
            "progress_percent": self.progress_percent,
            "language": self.language or "English",
            "webhook_url": self.webhook_url,
        }


class AuditResult(Base):
    """
    Individual page result from an audit.
    """
    __tablename__ = "audit_results"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False, index=True)
    page_url = Column(String(512), nullable=False, index=True)       # indexed: used in WHERE/JOIN filters
    filename = Column(String(255), nullable=False)
    score = Column(Integer, nullable=True)
    classification = Column(String(50), nullable=True, index=True)   # indexed: used in GROUP BY / ORDER BY
    result_json = Column(Text, nullable=True)  # Full JSON result
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship back to audit
    audit = relationship("Audit", back_populates="results")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        import json
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "page_url": self.page_url,
            "filename": self.filename,
            "score": self.score,
            "classification": self.classification,
            "result_json": json.loads(self.result_json) if self.result_json else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class AuditLog(Base):
    """
    Log entries for an audit (for real-time status updates via SSE).
    """
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False, index=True)
    level = Column(String(10), default="INFO")  # INFO, WARNING, ERROR
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship back to audit
    audit = relationship("Audit", back_populates="logs")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "level": self.level,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class AuditSummary(Base):
    """
    AI-generated executive summary and action plan for a completed audit.
    
    Generated after audit completion using a second LLM call that analyzes
    all audit results to create a narrative summary and prioritized action plan.
    """
    __tablename__ = "audit_summaries"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    executive_summary = Column(Text)  # Narrative 3-4 paragraphs for C-level
    key_findings = Column(Text)  # JSON array of {finding, impact, category}
    action_plan = Column(Text)  # JSON array with weekly breakdown {week, action, pages_affected, expected_impact, priority}
    competitive_position = Column(Text)  # 1 paragraph evaluation
    language = Column(String(30), default="English")
    provider = Column(String(20))  # Provider used for summary generation
    model = Column(String(100))  # Model used for summary generation
    generated_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship back to audit
    audit = relationship("Audit", backref="summary")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        import json
        
        # Parse JSON fields
        key_findings_parsed = None
        action_plan_parsed = None
        
        try:
            if self.key_findings:
                key_findings_parsed = json.loads(self.key_findings)
        except (json.JSONDecodeError, TypeError):
            key_findings_parsed = []
        
        try:
            if self.action_plan:
                action_plan_parsed = json.loads(self.action_plan)
        except (json.JSONDecodeError, TypeError):
            action_plan_parsed = []
        
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "executive_summary": self.executive_summary,
            "key_findings": key_findings_parsed,
            "action_plan": action_plan_parsed,
            "competitive_position": self.competitive_position,
            "language": self.language,
            "provider": self.provider,
            "model": self.model,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None
        }


class AuditTemplate(Base):
    """Reusable audit configuration template."""
    __tablename__ = "audit_templates"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)               # "SEO Full Stack - E-commerce"
    description = Column(Text, nullable=True)                 # "Complete SEO audit optimized for online stores"
    icon = Column(String(10), nullable=True)                  # Emoji: "🛒", "🏦", "📝"
    
    # Audit config (all optional — null means "ask user")
    audit_type = Column(String(50), nullable=True)
    provider = Column(String(20), nullable=True)
    model = Column(String(100), nullable=True)
    language = Column(String(30), nullable=True)
    use_perplexity = Column(Integer, nullable=True)           # null = ask, 0 = no, 1 = yes
    concurrency = Column(Integer, nullable=True)
    max_chars = Column(Integer, nullable=True)
    
    # Auto-actions post-audit
    auto_summary = Column(Integer, default=0)                 # Auto-generate AI summary
    summary_provider = Column(String(20), nullable=True)
    summary_model = Column(String(100), nullable=True)
    auto_briefs = Column(Integer, default=0)                  # Auto-generate content briefs
    auto_schemas = Column(Integer, default=0)                 # Auto-generate schema markup
    
    # Metadata
    use_count = Column(Integer, default=0)                     # How many times used
    is_default = Column(Integer, default=0)                    # Show in quick-launch
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "audit_type": self.audit_type,
            "provider": self.provider,
            "model": self.model,
            "language": self.language,
            "use_perplexity": bool(self.use_perplexity) if self.use_perplexity is not None else None,
            "concurrency": self.concurrency,
            "max_chars": self.max_chars,
            "auto_summary": bool(self.auto_summary),
            "summary_provider": self.summary_provider,
            "summary_model": self.summary_model,
            "auto_briefs": bool(self.auto_briefs),
            "auto_schemas": bool(self.auto_schemas),
            "use_count": self.use_count,
            "is_default": bool(self.is_default),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class BenchmarkProject(Base):
    """Project for competitor benchmarking across multiple audits."""
    __tablename__ = "benchmark_projects"

    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    audit_type = Column(String(100), nullable=False)
    target_audit_id = Column(String(36), nullable=False)
    competitor_audit_ids = Column(Text, nullable=False)  # JSON array of audit IDs
    benchmark_summary = Column(Text, nullable=True)   # JSON: AI-generated competitive analysis
    # Analysis lifecycle: "pending" → "generating" → "completed" | "failed"
    analysis_status = Column(String(20), nullable=False, default="pending", server_default="pending")
    analysis_error  = Column(Text, nullable=True)     # Error message when status == "failed"
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        import json

        competitor_ids = []
        try:
            if self.competitor_audit_ids:
                competitor_ids = json.loads(self.competitor_audit_ids)
        except (json.JSONDecodeError, TypeError):
            competitor_ids = []

        summary = None
        try:
            if self.benchmark_summary:
                summary = json.loads(self.benchmark_summary)
        except (json.JSONDecodeError, TypeError):
            summary = None

        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "audit_type": self.audit_type,
            "target_audit_id": self.target_audit_id,
            "competitor_audit_ids": competitor_ids,
            "benchmark_summary": summary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class ScheduledAudit(Base):
    """Recurring audit configuration."""
    __tablename__ = "scheduled_audits"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    website = Column(String(255), nullable=False)
    sitemap_url = Column(String(512), nullable=True)
    audit_type = Column(String(50), nullable=False)
    provider = Column(String(20), nullable=False)
    model = Column(String(100), nullable=False)
    schedule_cron = Column(String(100), nullable=False)  # "0 9 * * 1" (Monday 9AM)
    is_active = Column(Integer, default=1)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "website": self.website,
            "sitemap_url": self.sitemap_url,
            "audit_type": self.audit_type,
            "provider": self.provider,
            "model": self.model,
            "schedule_cron": self.schedule_cron,
            "is_active": bool(self.is_active),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class GeoMonitorProject(Base):
    """Project for monitoring GEO performance over time."""
    __tablename__ = "geo_monitor_projects"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    website = Column(String(255), nullable=False)
    target_queries = Column(Text, nullable=False)  # JSON array: ["best online bank", "mortgage rates", ...]
    providers_config = Column(Text, nullable=False)  # JSON: {"chatgpt": true, "perplexity": true, ...}
    schedule_cron = Column(String(100), nullable=True)  # "0 10 * * 1" (weekly)
    is_active = Column(Integer, default=1)
    last_scan_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship to scans
    scans = relationship("GeoMonitorScan", back_populates="project", cascade="all, delete-orphan")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        import json
        
        target_queries_parsed = []
        providers_config_parsed = {}
        
        try:
            if self.target_queries:
                target_queries_parsed = json.loads(self.target_queries)
        except (json.JSONDecodeError, TypeError):
            target_queries_parsed = []
        
        try:
            if self.providers_config:
                providers_config_parsed = json.loads(self.providers_config)
        except (json.JSONDecodeError, TypeError):
            providers_config_parsed = {}
        
        return {
            "id": self.id,
            "name": self.name,
            "website": self.website,
            "target_queries": target_queries_parsed,
            "providers_config": providers_config_parsed,
            "schedule_cron": self.schedule_cron,
            "is_active": bool(self.is_active),
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class GeoMonitorScan(Base):
    """A single GEO scan run with aggregated metrics."""
    __tablename__ = "geo_monitor_scans"
    
    id = Column(String(36), primary_key=True)
    project_id = Column(String(36), ForeignKey("geo_monitor_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), default="pending")
    total_queries = Column(Integer, default=0)
    mentioned_count = Column(Integer, default=0)
    visibility_score = Column(Float, nullable=True)  # (mentioned / total) * 100
    results_json = Column(Text, nullable=True)  # Full results per query per provider
    provider_breakdown = Column(Text, nullable=True)  # JSON: {"chatgpt": {mentioned: 5, total: 10}, ...}
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship back to project
    project = relationship("GeoMonitorProject", back_populates="scans")
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        import json
        
        results_parsed = []
        provider_breakdown_parsed = {}
        
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
        
        return {
            "id": self.id,
            "project_id": self.project_id,
            "status": self.status,
            "total_queries": self.total_queries,
            "mentioned_count": self.mentioned_count,
            "visibility_score": self.visibility_score,
            "results_json": results_parsed,
            "provider_breakdown": provider_breakdown_parsed,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


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


class CostRecord(Base):
    """Individual cost record for an API call."""
    __tablename__ = "cost_records"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(String(36), nullable=True, index=True)  # Null for non-audit costs (GEO scan, citation scan)
    source = Column(String(50), nullable=False, index=True)  # "audit", "summary", "geo_scan", "citation_scan", "brief", "schema"
    source_id = Column(String(36), nullable=True)  # ID of the source (scan_id, brief_id, etc.)
    website = Column(String(255), nullable=True, index=True)
    provider = Column(String(20), nullable=False, index=True)
    model = Column(String(100), nullable=False)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, default=0.0)  # Calculated from token counts × price per million
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "audit_id": self.audit_id,
            "source": self.source,
            "source_id": self.source_id,
            "website": self.website,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class ClientBilling(Base):
    """Client billing configuration for margin calculation."""
    __tablename__ = "client_billing"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    website = Column(String(255), unique=True, nullable=False, index=True)
    client_name = Column(String(255), nullable=True)
    monthly_fee_eur = Column(Float, nullable=True)  # What you charge the client
    currency = Column(String(3), default="EUR")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "website": self.website,
            "client_name": self.client_name,
            "monthly_fee_eur": self.monthly_fee_eur,
            "currency": self.currency,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class BrandingConfig(Base):
    """White-label branding configuration for PDF reports."""
    __tablename__ = "branding_configs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, default="Default")
    agency_name = Column(String(255), nullable=True)
    tagline = Column(String(500), nullable=True)
    logo_path = Column(String(500), nullable=True)
    primary_color = Column(String(7), default="#1e40af")
    secondary_color = Column(String(7), default="#3b82f6")
    text_color = Column(String(7), default="#1e293b")
    footer_text = Column(Text, nullable=True)
    contact_email = Column(String(255), nullable=True)
    contact_website = Column(String(255), nullable=True)
    is_default = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "agency_name": self.agency_name,
            "tagline": self.tagline,
            "logo_path": self.logo_path,
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "text_color": self.text_color,
            "footer_text": self.footer_text,
            "contact_email": self.contact_email,
            "contact_website": self.contact_website,
            "is_default": bool(self.is_default),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
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


class TrackingProject(Base):
    """Before/After tracking project — monitors score changes over time."""
    __tablename__ = "tracking_projects"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    website = Column(String(500), nullable=False, index=True)
    audit_type = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    baseline_audit_id = Column(String(36), ForeignKey("audits.id", ondelete="SET NULL"), nullable=True)
    baseline_score = Column(Float, nullable=True)
    baseline_date = Column(DateTime, nullable=True)
    current_audit_id = Column(String(36), ForeignKey("audits.id", ondelete="SET NULL"), nullable=True)
    current_score = Column(Float, nullable=True)
    current_date = Column(DateTime, nullable=True)
    score_delta = Column(Float, nullable=True)  # current - baseline
    status = Column(String(20), default="active")  # active, archived
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    snapshots = relationship("TrackingSnapshot", back_populates="project", cascade="all, delete-orphan",
                            order_by="TrackingSnapshot.created_at")
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "website": self.website,
            "audit_type": self.audit_type,
            "description": self.description,
            "baseline_audit_id": self.baseline_audit_id,
            "baseline_score": self.baseline_score,
            "baseline_date": self.baseline_date.isoformat() if self.baseline_date else None,
            "current_audit_id": self.current_audit_id,
            "current_score": self.current_score,
            "current_date": self.current_date.isoformat() if self.current_date else None,
            "score_delta": self.score_delta,
            "status": self.status,
            "snapshot_count": len(self.__dict__["snapshots"]) if "snapshots" in self.__dict__ and getattr(self, "snapshots", None) else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class TrackingSnapshot(Base):
    """Individual snapshot/milestone in a tracking project."""
    __tablename__ = "tracking_snapshots"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(36), ForeignKey("tracking_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    audit_id = Column(String(36), ForeignKey("audits.id", ondelete="SET NULL"), nullable=True)
    label = Column(String(100), nullable=True)  # "Baseline", "After meta fix", "Week 4"
    score = Column(Float, nullable=True)
    pages_analyzed = Column(Integer, nullable=True)
    delta_from_previous = Column(Float, nullable=True)
    delta_from_baseline = Column(Float, nullable=True)
    page_scores_json = Column(Text, nullable=True)  # JSON: per-page scores for drill-down
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    project = relationship("TrackingProject", back_populates="snapshots")
    
    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "audit_id": self.audit_id,
            "label": self.label,
            "score": self.score,
            "pages_analyzed": self.pages_analyzed,
            "delta_from_previous": self.delta_from_previous,
            "delta_from_baseline": self.delta_from_baseline,
            "page_scores": json.loads(self.page_scores_json) if self.page_scores_json else [],
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class AuditWeightConfig(Base):
    """Per-audit-type weight for the composite score calculation.

    If no rows exist the application falls back to the hardcoded
    _COMPOSITE_WEIGHTS dict in main.py, so the table can safely be empty.
    """
    __tablename__ = "audit_weight_configs"

    audit_type = Column(String(50), primary_key=True)
    weight     = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ResultNote(Base):
    """Free-text analyst note attached to a single AuditResult row."""
    __tablename__ = "result_notes"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    result_id  = Column(Integer, ForeignKey("audit_results.id", ondelete="CASCADE"),
                        unique=True, index=True)   # one note per result (upsert)
    note       = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class KeywordSession(Base):
    """A keyword research session — seeds in, expanded keywords + questions out."""
    __tablename__ = "keyword_sessions"

    id               = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name             = Column(String(200), nullable=False)
    seed_keywords    = Column(JSON,        nullable=False)       # list[str]
    location_key     = Column(String(10),  nullable=False, default="RO")  # e.g. "RO", "US"
    location_code    = Column(Integer,     nullable=False, default=1037)
    language_code    = Column(String(10),  nullable=False, default="ro")
    language_name    = Column(String(60),  nullable=False, default="Romanian")
    pass2_limit      = Column(Integer,     nullable=False, default=50)
    llm_provider     = Column(String(50),  nullable=False, default="anthropic")
    status           = Column(String(20),  nullable=False, default="pending")  # pending/running/completed/failed
    progress         = Column(Integer,     nullable=False, default=0)          # 0–100
    progress_message = Column(String(500), nullable=True)
    total_keywords   = Column(Integer,     nullable=False, default=0)
    total_questions  = Column(Integer,     nullable=False, default=0)
    created_at       = Column(DateTime,    default=datetime.utcnow)
    completed_at     = Column(DateTime,    nullable=True)
    error            = Column(Text,        nullable=True)


class KeywordResult(Base):
    """A single keyword belonging to a KeywordSession."""
    __tablename__ = "keyword_results"

    id             = Column(Integer,    primary_key=True, autoincrement=True)
    session_id     = Column(String(36), ForeignKey("keyword_sessions.id", ondelete="CASCADE"), index=True)
    keyword        = Column(String(500), nullable=False)
    search_volume  = Column(Integer,    nullable=True)
    cpc            = Column(Float,      nullable=True)
    competition    = Column(Float,      nullable=True)   # 0.0–1.0
    is_question    = Column(Boolean,    nullable=False, default=False, index=True)
    pass_number    = Column(Integer,    nullable=False, default=1)  # 0=seed, 1=pass1, 2=pass2
    created_at     = Column(DateTime,   default=datetime.utcnow)


# ── Google Search Console models ─────────────────────────────────────────────

class GscProperty(Base):
    """A Google Search Console property (website)."""
    __tablename__ = "gsc_properties"

    id               = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name             = Column(String(255), nullable=False)
    site_url         = Column(String(500), nullable=False)
    date_range_start = Column(String(10),  nullable=True)   # YYYY-MM-DD (from CSV metadata)
    date_range_end   = Column(String(10),  nullable=True)
    total_queries    = Column(Integer,     nullable=False, default=0)
    total_pages      = Column(Integer,     nullable=False, default=0)
    last_synced_at   = Column(DateTime,    nullable=True)   # last OAuth API sync
    sync_type        = Column(String(10),  nullable=False, default="csv", server_default="csv")  # csv|api
    created_at       = Column(DateTime,    default=datetime.utcnow)
    updated_at       = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)


class GscQueryRow(Base):
    """A single keyword row from a GSC queries export."""
    __tablename__ = "gsc_query_rows"

    id          = Column(Integer,     primary_key=True, autoincrement=True)
    property_id = Column(String(36),  ForeignKey("gsc_properties.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    query       = Column(String(1000), nullable=False)
    clicks      = Column(Integer,     nullable=False, default=0)
    impressions = Column(Integer,     nullable=False, default=0)
    ctr         = Column(Float,       nullable=True)   # 0.0 – 1.0
    position    = Column(Float,       nullable=True)


class GscPageRow(Base):
    """A single page row from a GSC pages export."""
    __tablename__ = "gsc_page_rows"

    id          = Column(Integer,     primary_key=True, autoincrement=True)
    property_id = Column(String(36),  ForeignKey("gsc_properties.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    page        = Column(String(2000), nullable=False)
    clicks      = Column(Integer,     nullable=False, default=0)
    impressions = Column(Integer,     nullable=False, default=0)
    ctr         = Column(Float,       nullable=True)
    position    = Column(Float,       nullable=True)


# ---------------------------------------------------------------------------
# GA4 Analytics Models
# ---------------------------------------------------------------------------

class Ga4Property(Base):
    """A Google Analytics 4 property (website)."""
    __tablename__ = "ga4_properties"

    id               = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name             = Column(String(255), nullable=False)
    site_url         = Column(String(500), nullable=False)
    date_range_start = Column(String(10),  nullable=True)   # YYYY-MM-DD
    date_range_end   = Column(String(10),  nullable=True)
    total_pages      = Column(Integer,     nullable=False, default=0)
    total_channels   = Column(Integer,     nullable=False, default=0)
    created_at       = Column(DateTime,    default=datetime.utcnow)
    updated_at       = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    page_rows     = relationship("Ga4PageRow",    back_populates="property", cascade="all, delete-orphan")
    channel_rows  = relationship("Ga4ChannelRow", back_populates="property", cascade="all, delete-orphan")


class Ga4PageRow(Base):
    """A single page row from a GA4 pages / landing-page report."""
    __tablename__ = "ga4_page_rows"

    id                  = Column(Integer,      primary_key=True, autoincrement=True)
    property_id         = Column(String(36),   ForeignKey("ga4_properties.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    page                = Column(String(2000), nullable=False)
    views               = Column(Integer,      nullable=False, default=0)
    users               = Column(Integer,      nullable=False, default=0)
    sessions            = Column(Integer,      nullable=False, default=0)
    avg_engagement_time = Column(Float,        nullable=True)   # seconds
    bounce_rate         = Column(Float,        nullable=True)   # 0.0–1.0 (GA4 exports as decimal, NOT %)
    conversions         = Column(Float,        nullable=True)

    property = relationship("Ga4Property", back_populates="page_rows")


class Ga4ChannelRow(Base):
    """A single channel row from a GA4 channel-group report."""
    __tablename__ = "ga4_channel_rows"

    id                  = Column(Integer,     primary_key=True, autoincrement=True)
    property_id         = Column(String(36),  ForeignKey("ga4_properties.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    channel             = Column(String(255), nullable=False)
    sessions            = Column(Integer,     nullable=False, default=0)
    users               = Column(Integer,     nullable=False, default=0)
    avg_engagement_time = Column(Float,       nullable=True)   # seconds
    conversions         = Column(Float,       nullable=True)
    conversion_rate     = Column(Float,       nullable=True)   # 0.0–1.0

    property = relationship("Ga4Property", back_populates="channel_rows")


# ---------------------------------------------------------------------------
# Google Ads Models
# ---------------------------------------------------------------------------

class AdsAccount(Base):
    """A Google Ads account."""
    __tablename__ = "ads_accounts"

    id              = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name            = Column(String(255), nullable=False)
    account_id      = Column(String(100), nullable=True)   # customer ID (optional)
    currency        = Column(String(10),  nullable=True)
    total_terms     = Column(Integer,     nullable=False, default=0)
    total_campaigns = Column(Integer,     nullable=False, default=0)
    created_at      = Column(DateTime,    default=datetime.utcnow)
    updated_at      = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    search_term_rows = relationship("AdsSearchTermRow", back_populates="account", cascade="all, delete-orphan")
    campaign_rows    = relationship("AdsCampaignRow",   back_populates="account", cascade="all, delete-orphan")


class AdsSearchTermRow(Base):
    """A single search term row from a Google Ads search terms report."""
    __tablename__ = "ads_search_term_rows"

    id          = Column(Integer,      primary_key=True, autoincrement=True)
    account_id  = Column(String(36),   ForeignKey("ads_accounts.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    search_term = Column(String(1000), nullable=False)
    campaign    = Column(String(500),  nullable=True)
    ad_group    = Column(String(500),  nullable=True)
    match_type  = Column(String(50),   nullable=True)
    impressions = Column(Integer,      nullable=False, default=0)
    clicks      = Column(Integer,      nullable=False, default=0)
    ctr         = Column(Float,        nullable=True)   # 0.0–1.0
    cost        = Column(Float,        nullable=True)   # currency-stripped float
    conversions = Column(Float,        nullable=True)
    conv_rate   = Column(Float,        nullable=True)   # 0.0–1.0

    account = relationship("AdsAccount", back_populates="search_term_rows")


class AdsCampaignRow(Base):
    """A single campaign row from a Google Ads campaigns report."""
    __tablename__ = "ads_campaign_rows"

    id            = Column(Integer,     primary_key=True, autoincrement=True)
    account_id    = Column(String(36),  ForeignKey("ads_accounts.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    campaign      = Column(String(500), nullable=False)
    campaign_type = Column(String(100), nullable=True)
    impressions   = Column(Integer,     nullable=False, default=0)
    clicks        = Column(Integer,     nullable=False, default=0)
    ctr           = Column(Float,       nullable=True)   # 0.0–1.0
    cost          = Column(Float,       nullable=True)
    conversions   = Column(Float,       nullable=True)
    conv_rate     = Column(Float,       nullable=True)   # 0.0–1.0

    account = relationship("AdsAccount", back_populates="campaign_rows")


# ---------------------------------------------------------------------------
# Multi-Source Insights Models (Haiku-powered)
# ---------------------------------------------------------------------------

class InsightRun(Base):
    """A single Haiku insight analysis run joining multiple data sources."""
    __tablename__ = "insight_runs"

    id               = Column(String(36),  primary_key=True, default=lambda: str(uuid.uuid4()))
    name             = Column(String(255), nullable=False)
    gsc_property_id  = Column(String(36),  ForeignKey("gsc_properties.id"),  nullable=True)
    ga4_property_id  = Column(String(36),  ForeignKey("ga4_properties.id"),  nullable=True)
    ads_account_id   = Column(String(36),  ForeignKey("ads_accounts.id"),    nullable=True)
    audit_id         = Column(String(36),  ForeignKey("audits.id"),          nullable=True)
    status           = Column(String(20),  nullable=False, default="pending")  # pending|running|completed|failed
    progress         = Column(Integer,     nullable=False, default=0)
    progress_message = Column(Text,        nullable=True)
    total_cards      = Column(Integer,     nullable=False, default=0)
    created_at       = Column(DateTime,    default=datetime.utcnow)

    cards = relationship("InsightCard", back_populates="run", cascade="all, delete-orphan")


class InsightCard(Base):
    """A single insight card produced by Haiku for one page/query."""
    __tablename__ = "insight_cards"

    # Issue type enum values:
    # low_ctr | poor_engagement | ranks_but_bounces | paid_dependency |
    # organic_opportunity | no_audit | content_gap | near_miss

    id            = Column(Integer,      primary_key=True, autoincrement=True)
    run_id        = Column(String(36),   ForeignKey("insight_runs.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    page_or_query = Column(String(2000), nullable=False)
    issue_type    = Column(String(50),   nullable=False)
    priority      = Column(String(10),   nullable=False)   # high|medium|low
    reason        = Column(Text,         nullable=False)
    action        = Column(Text,         nullable=False)
    # Source metrics (nullable — populated only when that source was included)
    gsc_clicks      = Column(Integer, nullable=True)
    gsc_impressions = Column(Integer, nullable=True)
    gsc_ctr         = Column(Float,   nullable=True)
    gsc_position    = Column(Float,   nullable=True)
    ga4_sessions    = Column(Integer, nullable=True)
    ga4_bounce_rate = Column(Float,   nullable=True)
    ga4_engagement  = Column(Float,   nullable=True)
    ads_cost        = Column(Float,   nullable=True)
    ads_clicks      = Column(Integer, nullable=True)
    audit_score     = Column(Float,   nullable=True)

    run = relationship("InsightRun", back_populates="cards")


# ---------------------------------------------------------------------------
# Google OAuth Token (single-row — one connected Google account at a time)
# ---------------------------------------------------------------------------

class GoogleOAuthToken(Base):
    """Stores the OAuth 2.0 tokens for the connected Google account."""
    __tablename__ = "google_oauth_tokens"

    id            = Column(Integer,     primary_key=True, autoincrement=True)
    email         = Column(String(255), nullable=True)    # Google account email
    access_token  = Column(Text,        nullable=False)
    refresh_token = Column(Text,        nullable=False)
    token_expiry  = Column(DateTime,    nullable=True)    # UTC expiry of access_token
    created_at    = Column(DateTime,    default=datetime.utcnow)
    updated_at    = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# Per-URL GEO & SEO Guide
# ---------------------------------------------------------------------------

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


# Default templates to seed on first run
DEFAULT_TEMPLATES = [
    {
        "name": "SEO Full Stack",
        "description": "Complete SEO audit covering meta tags, structure, content, internal linking, schema markup",
        "icon": "🔍",
        "audit_type": "SEO_AUDIT",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "language": "English",
        "concurrency": 10,
        "auto_summary": 1,
        "summary_provider": "anthropic",
        "summary_model": "claude-haiku-4-5-20251001",
        "is_default": 1
    },
    {
        "name": "GEO Readiness Check",
        "description": "Generative Engine Optimization audit — how well does content perform for AI search",
        "icon": "🌐",
        "audit_type": "GEO_AUDIT",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "language": "English",
        "concurrency": 5,
        "use_perplexity": 1,
        "auto_summary": 1,
        "is_default": 1
    },
    {
        "name": "Quick Content Check",
        "description": "Fast content quality assessment — ideal for blog audits",
        "icon": "📝",
        "audit_type": "CONTENT_QUALITY",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "concurrency": 15,
        "is_default": 1
    },
    {
        "name": "E-commerce Product Pages",
        "description": "Optimized for product pages — checks descriptions, schema, pricing, reviews",
        "icon": "🛒",
        "audit_type": "SEO_AUDIT",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "auto_schemas": 1,
        "auto_briefs": 1,
        "is_default": 1
    },
    {
        "name": "Banking/Finance (Romanian)",
        "description": "SEO + GEO audit tailored for Romanian banking sector",
        "icon": "🏦",
        "audit_type": "SEO_AUDIT",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "language": "Romanian",
        "use_perplexity": 1,
        "concurrency": 5,
        "auto_summary": 1,
        "summary_model": "claude-haiku-4-5-20251001",
        "is_default": 1
    }
]


def init_db():
    """Initialize database tables (sync version for startup)."""
    # Migrate benchmark_projects if it has the old schema (websites/audit_config columns)
    from sqlalchemy import text as sa_text
    with sync_engine.connect() as conn:
        try:
            rows = conn.execute(sa_text("PRAGMA table_info(benchmark_projects)")).fetchall()
            col_names = [row[1] for row in rows]
            if col_names and "websites" in col_names:
                conn.execute(sa_text("DROP TABLE IF EXISTS benchmark_projects"))
                conn.commit()
                print("✓ Migrated benchmark_projects to new schema")
        except Exception:
            pass

    Base.metadata.create_all(bind=sync_engine)

    # Migrate gsc_properties: add last_synced_at + sync_type if missing
    with sync_engine.connect() as conn:
        try:
            rows = conn.execute(sa_text("PRAGMA table_info(gsc_properties)")).fetchall()
            col_names = [row[1] for row in rows]
            if col_names and "last_synced_at" not in col_names:
                conn.execute(sa_text("ALTER TABLE gsc_properties ADD COLUMN last_synced_at DATETIME"))
                conn.commit()
                print("✓ Migrated gsc_properties: added last_synced_at")
            if col_names and "sync_type" not in col_names:
                conn.execute(sa_text("ALTER TABLE gsc_properties ADD COLUMN sync_type VARCHAR(10) NOT NULL DEFAULT 'csv'"))
                conn.commit()
                print("✓ Migrated gsc_properties: added sync_type")
        except Exception as _e:
            print(f"[WARN] gsc_properties migration: {_e}")

    # Migrate url_guides: add reviewed column if missing
    with sync_engine.connect() as conn:
        try:
            rows = conn.execute(sa_text("PRAGMA table_info(url_guides)")).fetchall()
            col_names = [row[1] for row in rows]
            if col_names and "reviewed" not in col_names:
                conn.execute(sa_text("ALTER TABLE url_guides ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0"))
                conn.commit()
                print("✓ Migrated url_guides: added reviewed")
        except Exception as _e:
            print(f"[WARN] url_guides migration: {_e}")

    # Seed default templates if table is empty
    from sqlalchemy.orm import Session
    with Session(sync_engine) as session:
        existing_templates = session.query(AuditTemplate).count()
        if existing_templates == 0:
            print("🌱 Seeding default audit templates...")
            for template_data in DEFAULT_TEMPLATES:
                template = AuditTemplate(**template_data)
                session.add(template)
            session.commit()
            print(f"✓ Seeded {len(DEFAULT_TEMPLATES)} default templates")


async def init_db_async():
    """Initialize database tables (async version)."""
    # Migrate benchmark_projects if it has the old schema (websites/audit_config columns)
    from sqlalchemy import text as sa_text
    async with engine.begin() as conn:
        try:
            rows = await conn.execute(sa_text("PRAGMA table_info(benchmark_projects)"))
            col_names = [row[1] for row in rows.fetchall()]
            if col_names and "websites" in col_names:
                await conn.execute(sa_text("DROP TABLE IF EXISTS benchmark_projects"))
                print("✓ Migrated benchmark_projects to new schema")
        except Exception:
            pass

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Add new columns to gsc_properties if they don't exist yet (SQLite migration)
    async with engine.begin() as conn:
        try:
            rows = await conn.execute(sa_text("PRAGMA table_info(gsc_properties)"))
            col_names = [row[1] for row in rows.fetchall()]
            if col_names and "last_synced_at" not in col_names:
                await conn.execute(sa_text("ALTER TABLE gsc_properties ADD COLUMN last_synced_at DATETIME"))
                print("✓ Migrated gsc_properties: added last_synced_at")
            if col_names and "sync_type" not in col_names:
                await conn.execute(sa_text("ALTER TABLE gsc_properties ADD COLUMN sync_type VARCHAR(10) NOT NULL DEFAULT 'csv'"))
                print("✓ Migrated gsc_properties: added sync_type")
        except Exception:
            pass

    # Migrate url_guides: add reviewed column if missing
    async with engine.begin() as conn:
        try:
            rows = await conn.execute(sa_text("PRAGMA table_info(url_guides)"))
            col_names = [row[1] for row in rows.fetchall()]
            if col_names and "reviewed" not in col_names:
                await conn.execute(sa_text("ALTER TABLE url_guides ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0"))
                print("✓ Migrated url_guides: added reviewed")
        except Exception:
            pass

    # Seed default templates if table is empty
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(func.count(AuditTemplate.id)))
        existing_templates = result.scalar()
        
        if existing_templates == 0:
            print("🌱 Seeding default audit templates...")
            for template_data in DEFAULT_TEMPLATES:
                template = AuditTemplate(**template_data)
                session.add(template)
            await session.commit()
            print(f"✓ Seeded {len(DEFAULT_TEMPLATES)} default templates")


# Dependency for getting database session
async def get_db():
    """Dependency that provides a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# Enable foreign key support for SQLite
@event.listens_for(sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
