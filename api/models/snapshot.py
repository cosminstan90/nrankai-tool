"""Page content snapshots (Etapa 6 of docs/IMPROVEMENTS_PLAN.md).

Dated and additive, for the same reason as gsc_page_history and
performance_snapshots: the question is "what changed since last time", which a
"latest state only" row cannot answer. Each snapshot is one page as it looked
on one date.

Stores extracted fields rather than raw HTML -- measured at 4.3 KB against
67.7 KB for the same real page, and re-extracting the same fields from archived
markup later would gain nothing.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
)
from sqlalchemy.orm import relationship

from api.models._base import Base, UTCDateTime


class SnapshotRun(Base):
    """One pass over a site's stored pages, on one date."""
    __tablename__ = "snapshot_runs"

    id = Column(String(36), primary_key=True)
    website = Column(String(255), nullable=False, index=True)
    status = Column(String(20), default="pending")   # pending|running|completed|failed
    pages_captured = Column(Integer, default=0)
    source_dir = Column(String(500), nullable=True)  # where the HTML was read from
    error = Column(Text, nullable=True)
    started_at = Column(UTCDateTime, nullable=True)
    completed_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    pages = relationship("PageSnapshot", back_populates="run", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "website": self.website,
            "status": self.status,
            "pages_captured": self.pages_captured,
            "source_dir": self.source_dir,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PageSnapshot(Base):
    """
    One page's SEO-relevant shape at one point in time.

    NULL in title/meta_description/canonical/meta_robots means "not captured",
    never "absent from the page". The scraper stores document.body, so these
    arrive from the head sidecar it writes alongside; pages scraped before that
    existed have no sidecar and keep NULL. core/page_diff.py skips any
    comparison where either side is NULL for exactly that reason.
    """
    __tablename__ = "page_snapshots"

    id = Column(String(36), primary_key=True)
    run_id = Column(String(36), ForeignKey("snapshot_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    website = Column(String(255), nullable=False, index=True)
    url = Column(String(2048), nullable=False, index=True)

    title = Column(Text, nullable=True)
    meta_description = Column(Text, nullable=True)
    canonical = Column(Text, nullable=True)
    meta_robots = Column(Text, nullable=True)
    h1 = Column(JSON, nullable=True, default=list)
    h2 = Column(JSON, nullable=True, default=list)
    h3 = Column(JSON, nullable=True, default=list)
    word_count = Column(Integer, nullable=True)
    internal_links = Column(JSON, nullable=True, default=list)
    external_link_count = Column(Integer, nullable=True)
    images_total = Column(Integer, nullable=True)
    images_without_alt = Column(Integer, nullable=True)
    schema_types = Column(JSON, nullable=True, default=list)
    content_hash = Column(String(80), nullable=True, index=True)

    captured_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    run = relationship("SnapshotRun", back_populates="pages")

    # One snapshot per page per run: re-running a capture updates rather than
    # silently doubling a page's history.
    __table_args__ = (UniqueConstraint("run_id", "url", name="uq_page_snapshots_run_url"),)

    def to_fields(self) -> dict:
        """The shape core.page_diff.diff_snapshots expects."""
        return {
            "url": self.url,
            "title": self.title,
            "meta_description": self.meta_description,
            "canonical": self.canonical,
            "meta_robots": self.meta_robots,
            "h1": self.h1 or [],
            "h2": self.h2 or [],
            "h3": self.h3 or [],
            "word_count": self.word_count,
            "internal_links": self.internal_links or [],
            "external_link_count": self.external_link_count,
            "images_total": self.images_total,
            "images_without_alt": self.images_without_alt,
            "schema_types": self.schema_types or [],
            "content_hash": self.content_hash,
        }

    def to_dict(self):
        return {
            **self.to_fields(),
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
        }
