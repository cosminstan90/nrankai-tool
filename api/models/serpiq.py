"""SerpIQ ORM models — SERP Snapshot & Instant Analysis engine.

Two tables:
  serpiq_snapshots   — one analysis per input (URL or keyword)
  serpiq_serp_items  — top-20 SERP positions for each snapshot
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index,
    Integer, JSON, String, Text,
)
from sqlalchemy.orm import relationship

from api.models._base import Base


class SiqSnapshot(Base):
    """
    A single SerpIQ analysis — keyed by a URL or keyword input.

    Verdicts
    --------
    optimize   — URL already ranks but has room to improve (pos 4-20, weak CTR)
    create     — no content targeting this keyword exists on the site
    compete    — strong competitor page in top 3, direct head-to-head needed
    dominate   — site already ranks #1-3 with high CTR / authority signals
    gap        — keyword has demand but zero indexable content on site
    """
    __tablename__ = "serpiq_snapshots"

    id                  = Column(Integer,      primary_key=True, autoincrement=True)
    user_id             = Column(Integer,      nullable=True, index=True)   # nullable — anonymous allowed
    input_type          = Column(String(10),   nullable=False)              # 'url' | 'keyword'
    input_value         = Column(String(2048), nullable=False)              # raw user input
    keyword             = Column(String(500),  nullable=False, index=True)  # analysed keyword (extracted if URL)
    location_code       = Column(Integer,      default=2040)                # 2040 = Romania
    language_code       = Column(String(10),   default="ro")
    serp_results        = Column(JSON,         nullable=True)               # list of top-20 SERP items (raw)
    url_analysis        = Column(JSON,         nullable=True)               # on-page metrics if input_type='url'
    verdict             = Column(String(20),   nullable=True, index=True)   # optimize/create/compete/dominate/gap
    verdict_evidence    = Column(JSON,         nullable=True)               # list[str] explanatory bullets
    llm_brief           = Column(Text,         nullable=True)               # Claude-generated brief (optional)
    position_found      = Column(Integer,      nullable=True)               # rank of the input URL in SERP (1-based)
    processing_time_ms  = Column(Integer,      nullable=True)
    created_at          = Column(DateTime,     default=lambda: datetime.now(timezone.utc), index=True)

    serp_items = relationship(
        "SiqSerpItem", back_populates="snapshot", cascade="all, delete-orphan",
        order_by="SiqSerpItem.position",
    )

    def to_dict(self, include_items: bool = False) -> dict:
        data = {
            "id":                 self.id,
            "user_id":            self.user_id,
            "input_type":         self.input_type,
            "input_value":        self.input_value,
            "keyword":            self.keyword,
            "location_code":      self.location_code,
            "language_code":      self.language_code,
            "verdict":            self.verdict,
            "verdict_evidence":   self.verdict_evidence,
            "llm_brief":          self.llm_brief,
            "position_found":     self.position_found,
            "processing_time_ms": self.processing_time_ms,
            "url_analysis":       self.url_analysis,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
        }
        if include_items:
            data["serp_items"] = [item.to_dict() for item in (self.serp_items or [])]
        return data


class SiqSerpItem(Base):
    """One ranked result within a SerpIQ snapshot (positions 1-20)."""
    __tablename__ = "serpiq_serp_items"
    __table_args__ = (
        Index("ix_serpiq_serp_items_snapshot_position", "snapshot_id", "position"),
    )

    id                    = Column(Integer,      primary_key=True, autoincrement=True)
    snapshot_id           = Column(Integer,      ForeignKey("serpiq_snapshots.id", ondelete="CASCADE"),
                                   nullable=False, index=True)
    position              = Column(Integer,      nullable=False)            # 1-based rank
    url                   = Column(String(2048), nullable=True)
    domain                = Column(String(512),  nullable=True, index=True)
    title                 = Column(String(512),  nullable=True)
    description           = Column(Text,         nullable=True)
    word_count_estimated  = Column(Integer,      nullable=True)
    has_schema            = Column(Boolean,      default=False)
    schema_types          = Column(JSON,         nullable=True)             # e.g. ["Article","FAQPage"]
    is_featured_snippet   = Column(Boolean,      default=False)
    is_local_pack         = Column(Boolean,      default=False)
    is_video              = Column(Boolean,      default=False)

    snapshot = relationship("SiqSnapshot", back_populates="serp_items")

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "snapshot_id":          self.snapshot_id,
            "position":             self.position,
            "url":                  self.url,
            "domain":               self.domain,
            "title":                self.title,
            "description":          self.description,
            "word_count_estimated": self.word_count_estimated,
            "has_schema":           self.has_schema,
            "schema_types":         self.schema_types,
            "is_featured_snippet":  self.is_featured_snippet,
            "is_local_pack":        self.is_local_pack,
            "is_video":             self.is_video,
        }
