"""ContentIQ ORM models — content audit engine (KUCD verdicts)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey,
    Boolean, SmallInteger, JSON,
)
from sqlalchemy.orm import relationship

from api.models._base import Base


class CiqAudit(Base):
    """A ContentIQ audit run for a domain."""
    __tablename__ = "ciq_audits"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    label        = Column(String(255), nullable=False)
    domain       = Column(String(512), nullable=False)
    sitemap_url  = Column(String(512), nullable=True)
    status       = Column(String(20),  default="pending", index=True)   # pending|crawling|scoring|done|failed
    total_urls   = Column(Integer,     default=0)
    scored_urls  = Column(Integer,     default=0)
    triggered_by = Column(String(20),  default="manual")                # manual|scheduled|api
    notes        = Column(Text,        nullable=True)
    created_at   = Column(DateTime,    default=datetime.utcnow)
    finished_at  = Column(DateTime,    nullable=True)

    pages       = relationship("CiqPage",       back_populates="audit", cascade="all, delete-orphan")
    competitors = relationship("CiqCompetitor", back_populates="audit", cascade="all, delete-orphan")
    gsc_token   = relationship("CiqGscToken",   back_populates="audit", uselist=False, cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "label":       self.label,
            "domain":      self.domain,
            "sitemap_url": self.sitemap_url,
            "status":      self.status,
            "total_urls":  self.total_urls,
            "scored_urls": self.scored_urls,
            "triggered_by": self.triggered_by,
            "notes":       self.notes,
            "created_at":  self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class CiqPage(Base):
    """One URL within a ContentIQ audit."""
    __tablename__ = "ciq_pages"

    id               = Column(Integer,     primary_key=True, autoincrement=True)
    audit_id         = Column(Integer,     ForeignKey("ciq_audits.id", ondelete="CASCADE"), nullable=False, index=True)
    url              = Column(String(2048), nullable=False)
    title            = Column(String(512),  nullable=True)
    h1               = Column(String(512),  nullable=True)
    meta_description = Column(Text,         nullable=True)
    canonical        = Column(String(2048), nullable=True)
    word_count       = Column(Integer,      nullable=True)
    last_modified    = Column(String(20),   nullable=True)   # ISO date string
    status_code      = Column(Integer,      nullable=True)
    # Ahrefs
    ahrefs_traffic   = Column(Integer,      nullable=True)
    ahrefs_keywords  = Column(Integer,      nullable=True)
    ahrefs_backlinks = Column(Integer,      nullable=True)
    ahrefs_dr        = Column(SmallInteger, nullable=True)
    # GSC
    gsc_clicks       = Column(Integer,      nullable=True)
    gsc_impressions  = Column(Integer,      nullable=True)
    gsc_ctr          = Column(Float,        nullable=True)
    gsc_position     = Column(Float,        nullable=True)
    # Scores
    score_freshness  = Column(SmallInteger, nullable=True)
    score_geo        = Column(SmallInteger, nullable=True)
    score_eeat       = Column(SmallInteger, nullable=True)
    score_seo_health = Column(SmallInteger, nullable=True)
    score_total      = Column(SmallInteger, nullable=True)
    # Score reasons
    freshness_reason  = Column(Text, nullable=True)
    geo_reason        = Column(Text, nullable=True)
    eeat_reason       = Column(Text, nullable=True)
    seo_health_reason = Column(Text, nullable=True)
    # Verdict
    verdict        = Column(String(20),  nullable=True, index=True)   # KEEP|UPDATE|CONSOLIDATE|DELETE
    verdict_reason = Column(Text,        nullable=True)
    # Brief
    brief_generated = Column(Boolean,    default=False)
    brief_content   = Column(Text,       nullable=True)
    # Meta
    competitor_gap = Column(Boolean,     default=False)
    crawled_at     = Column(DateTime,    nullable=True)
    scored_at      = Column(DateTime,    nullable=True)

    audit = relationship("CiqAudit", back_populates="pages")

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "audit_id":         self.audit_id,
            "url":              self.url,
            "title":            self.title,
            "h1":               self.h1,
            "meta_description": self.meta_description,
            "canonical":        self.canonical,
            "word_count":       self.word_count,
            "last_modified":    self.last_modified,
            "status_code":      self.status_code,
            "ahrefs_traffic":   self.ahrefs_traffic,
            "ahrefs_keywords":  self.ahrefs_keywords,
            "ahrefs_backlinks": self.ahrefs_backlinks,
            "ahrefs_dr":        self.ahrefs_dr,
            "gsc_clicks":       self.gsc_clicks,
            "gsc_impressions":  self.gsc_impressions,
            "gsc_ctr":          self.gsc_ctr,
            "gsc_position":     self.gsc_position,
            "score_freshness":  self.score_freshness,
            "score_geo":        self.score_geo,
            "score_eeat":       self.score_eeat,
            "score_seo_health": self.score_seo_health,
            "score_total":      self.score_total,
            "freshness_reason":  self.freshness_reason,
            "geo_reason":        self.geo_reason,
            "eeat_reason":       self.eeat_reason,
            "seo_health_reason": self.seo_health_reason,
            "verdict":          self.verdict,
            "verdict_reason":   self.verdict_reason,
            "brief_generated":  self.brief_generated,
            "brief_content":    self.brief_content,
            "competitor_gap":   self.competitor_gap,
            "crawled_at":       self.crawled_at.isoformat() if self.crawled_at else None,
            "scored_at":        self.scored_at.isoformat() if self.scored_at else None,
        }


class CiqCompetitor(Base):
    """Competitor domain tracked within a ContentIQ audit."""
    __tablename__ = "ciq_competitors"

    id       = Column(Integer,     primary_key=True, autoincrement=True)
    audit_id = Column(Integer,     ForeignKey("ciq_audits.id", ondelete="CASCADE"), nullable=False, index=True)
    domain   = Column(String(512), nullable=False)
    label    = Column(String(255), nullable=True)
    added_at = Column(DateTime,    default=datetime.utcnow)

    audit = relationship("CiqAudit", back_populates="competitors")

    def to_dict(self) -> dict:
        return {
            "id":       self.id,
            "audit_id": self.audit_id,
            "domain":   self.domain,
            "label":    self.label,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }


class CiqGscToken(Base):
    """GSC OAuth tokens for a ContentIQ audit."""
    __tablename__ = "ciq_gsc_tokens"

    audit_id      = Column(Integer,     ForeignKey("ciq_audits.id", ondelete="CASCADE"), primary_key=True)
    access_token  = Column(Text,        nullable=True)
    refresh_token = Column(Text,        nullable=True)
    expires_at    = Column(DateTime,    nullable=True)
    property_url  = Column(String(512), nullable=True)
    updated_at    = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    audit = relationship("CiqAudit", back_populates="gsc_token")
