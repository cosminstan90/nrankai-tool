"""Internal link-graph ORM models (site crawls, pages, filtered edges).

Etapa 5 of docs/IMPROVEMENTS_PLAN.md. Its own domain file rather than a
section of audit.py, which that plan explicitly sanctions for this case
("modelele merg in api/models/audit.py sau un fisier de domeniu nou daca
devine mare").

crawl_links deliberately does NOT hold the full graph. A verified crawl
produced 174,244 hyperlink edges of which 152,277 (87%) were navigation and
only 157 (0.09%) were body-content links -- storing them all would mean one
copy of the site's menu per page. Only content, broken and redirecting edges
get rows; see core/sf_parser._edge_reason. Navigation volume survives as
CrawlPage.nav_inlinks.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import relationship

from api.models._base import Base


class SiteCrawl(Base):
    """One Screaming Frog run against one website."""
    __tablename__ = "site_crawls"

    id = Column(String(36), primary_key=True)
    website = Column(String(255), nullable=False, index=True)
    status = Column(String(20), default="pending")  # pending|running|completed|failed
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    pages_crawled = Column(Integer, default=0)
    content_edges = Column(Integer, default=0)
    # Kept as a number rather than as rows -- see the module docstring.
    nav_edges_discarded = Column(Integer, default=0)
    sf_version = Column(String(20), nullable=True)
    config_name = Column(String(255), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    pages = relationship("CrawlPage", back_populates="crawl", cascade="all, delete-orphan")
    links = relationship("CrawlLink", back_populates="crawl", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "website": self.website,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "pages_crawled": self.pages_crawled,
            "content_edges": self.content_edges,
            "nav_edges_discarded": self.nav_edges_discarded,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CrawlPage(Base):
    """One crawled URL.

    content_inlinks is the figure that matters: prompts/internal_linking.yaml
    scores on body-content links and caps a page at 20/100 when it has none,
    counting navigation separately.
    """
    __tablename__ = "crawl_pages"

    id = Column(String(36), primary_key=True)
    crawl_id = Column(String(36), ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(String(2048), nullable=False, index=True)
    status_code = Column(Integer, nullable=True)
    indexability = Column(String(50), nullable=True)
    crawl_depth = Column(Integer, nullable=True)
    inlinks_total = Column(Integer, default=0)
    unique_inlinks = Column(Integer, default=0)
    outlinks_total = Column(Integer, default=0)
    unique_outlinks = Column(Integer, default=0)
    content_inlinks = Column(Integer, default=0)
    nav_inlinks = Column(Integer, default=0)
    is_orphan = Column(Boolean, default=False)

    crawl = relationship("SiteCrawl", back_populates="pages")

    def to_dict(self):
        return {
            "url": self.url,
            "status_code": self.status_code,
            "indexability": self.indexability,
            "crawl_depth": self.crawl_depth,
            "inlinks_total": self.inlinks_total,
            "content_inlinks": self.content_inlinks,
            "nav_inlinks": self.nav_inlinks,
            "outlinks_total": self.outlinks_total,
            "is_orphan": self.is_orphan,
        }


class CrawlLink(Base):
    """A stored edge. `reason` records why it earned a row: content|error|redirect."""
    __tablename__ = "crawl_links"

    id = Column(String(36), primary_key=True)
    crawl_id = Column(String(36), ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False, index=True)
    source_url = Column(String(2048), nullable=False)
    dest_url = Column(String(2048), nullable=False, index=True)
    anchor = Column(Text, nullable=True)
    link_position = Column(String(50), nullable=True)
    follow = Column(Boolean, default=True)
    dest_status_code = Column(Integer, nullable=True)
    reason = Column(String(20), nullable=False)

    crawl = relationship("SiteCrawl", back_populates="links")

    def to_dict(self):
        return {
            "source_url": self.source_url,
            "dest_url": self.dest_url,
            "anchor": self.anchor,
            "link_position": self.link_position,
            "follow": self.follow,
            "dest_status_code": self.dest_status_code,
            "reason": self.reason,
        }
