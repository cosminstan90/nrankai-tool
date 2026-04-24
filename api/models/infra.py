"""Infrastructure ORM models (Benchmarks, Schedules, Monitoring, Costs)."""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON, Boolean, func
)
from sqlalchemy.orm import relationship

from api.models._base import Base

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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    alert_threshold = Column(Float, default=15.0, nullable=True)
    alert_webhook_url = Column(String(500), nullable=True)
    competitors = Column(JSON, nullable=True, default=list)
    # Format: [{"name": "Competitor A", "brand_keywords": ["brand-a"], "website": "brand-a.com"}]

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
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "competitors": self.competitors if self.competitors is not None else []
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    competitor_scores = Column(JSON, nullable=True, default=dict)
    # Format: {"brand-a.com": {"name": "Competitor A", "mention_rate": 72.0}}

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
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "competitor_scores": self.competitor_scores if self.competitor_scores is not None else {}
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
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



