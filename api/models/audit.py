"""Audit-related ORM models."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON, Boolean, func
)
from sqlalchemy.orm import relationship

from api.models._base import Base

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



