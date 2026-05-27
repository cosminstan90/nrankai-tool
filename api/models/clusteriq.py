"""ClusterIQ ORM models — SERP Cluster Intelligence engine."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index,
    Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from api.models._base import Base


class CluProject(Base):
    """A ClusterIQ analysis run for a domain."""
    __tablename__ = "clusteriq_projects"

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    # Optional link to an external project / parent entity (no FK — table doesn't exist yet)
    project_id              = Column(Integer, nullable=True, index=True)
    domain                  = Column(String(512), nullable=False)
    status                  = Column(String(20),  default="pending", index=True)  # pending/crawling/clustering/done/error
    gsc_property            = Column(String(512), nullable=True)
    dataforseo_credits_used = Column(Integer,     default=0)
    created_at              = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
    updated_at              = Column(DateTime,    default=lambda: datetime.now(timezone.utc),
                                                  onupdate=lambda: datetime.now(timezone.utc))

    urls       = relationship("CluUrl",       back_populates="project", cascade="all, delete-orphan")
    serp_data  = relationship("CluSerpData",  back_populates="project", cascade="all, delete-orphan")
    clusters   = relationship("CluCluster",   back_populates="project", cascade="all, delete-orphan")
    duplicates = relationship("CluDuplicate", back_populates="project", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":                      self.id,
            "project_id":              self.project_id,
            "domain":                  self.domain,
            "status":                  self.status,
            "gsc_property":            self.gsc_property,
            "dataforseo_credits_used": self.dataforseo_credits_used,
            "created_at":              self.created_at.isoformat() if self.created_at else None,
            "updated_at":              self.updated_at.isoformat() if self.updated_at else None,
        }


class CluUrl(Base):
    """A crawled URL within a ClusterIQ project."""
    __tablename__ = "clusteriq_urls"
    __table_args__ = (
        Index("ix_clusteriq_urls_project_url", "project_id", "url"),
    )

    id            = Column(Integer,      primary_key=True, autoincrement=True)
    project_id    = Column(Integer,      ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    url           = Column(String(2048), nullable=False)
    status_code   = Column(Integer,      nullable=True)
    is_indexable  = Column(Boolean,      default=True)
    canonical_url = Column(String(2048), nullable=True)
    title         = Column(String(512),  nullable=True)
    word_count    = Column(Integer,      nullable=True)
    crawled_at    = Column(DateTime,     nullable=True)

    project         = relationship("CluProject",   back_populates="urls")
    url_clusters    = relationship("CluUrlCluster", back_populates="url", cascade="all, delete-orphan")
    decisions       = relationship("CluDecision",   back_populates="url", cascade="all, delete-orphan")
    won_duplicates  = relationship("CluDuplicate",  foreign_keys="CluDuplicate.winner_url_id",
                                   back_populates="winner_url")
    lost_duplicates = relationship("CluDuplicate",  foreign_keys="CluDuplicate.duplicate_url_id",
                                   back_populates="duplicate_url")

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "project_id":    self.project_id,
            "url":           self.url,
            "status_code":   self.status_code,
            "is_indexable":  self.is_indexable,
            "canonical_url": self.canonical_url,
            "title":         self.title,
            "word_count":    self.word_count,
            "crawled_at":    self.crawled_at.isoformat() if self.crawled_at else None,
        }


class CluSerpData(Base):
    """SERP data row — keyword/URL pair with GSC or DataForSEO metrics."""
    __tablename__ = "clusteriq_serp_data"
    __table_args__ = (
        UniqueConstraint("project_id", "keyword", "url", name="uq_clu_serp_project_keyword_url"),
    )

    id          = Column(Integer,      primary_key=True, autoincrement=True)
    project_id  = Column(Integer,      ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword     = Column(String(500),  nullable=False)
    url         = Column(String(2048), nullable=False)
    position    = Column(Float,        nullable=True)
    impressions = Column(Integer,      nullable=True)
    clicks      = Column(Integer,      nullable=True)
    serp_urls   = Column(JSON,         nullable=True)   # top-20 SERP URLs for this keyword
    data_source = Column(String(20),   nullable=False)  # gsc/dataforseo
    fetched_at  = Column(DateTime,     default=lambda: datetime.now(timezone.utc))

    project = relationship("CluProject", back_populates="serp_data")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "project_id":  self.project_id,
            "keyword":     self.keyword,
            "url":         self.url,
            "position":    self.position,
            "impressions": self.impressions,
            "clicks":      self.clicks,
            "serp_urls":   self.serp_urls,
            "data_source": self.data_source,
            "fetched_at":  self.fetched_at.isoformat() if self.fetched_at else None,
        }


class CluCluster(Base):
    """A keyword/URL cluster produced by Louvain community detection."""
    __tablename__ = "clusteriq_clusters"

    id                      = Column(Integer,      primary_key=True, autoincrement=True)
    project_id              = Column(Integer,      ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    cluster_label           = Column(String(512),  nullable=False)
    primary_url             = Column(String(2048), nullable=True)
    primary_keyword         = Column(String(500),  nullable=True)
    total_impressions       = Column(Integer,      default=0)
    total_clicks            = Column(Integer,      default=0)
    url_count               = Column(Integer,      default=0)
    search_demand_confirmed = Column(Boolean,      default=False)
    louvain_community_id    = Column(Integer,      nullable=True)
    created_at              = Column(DateTime,     default=lambda: datetime.now(timezone.utc))

    project      = relationship("CluProject",    back_populates="clusters")
    url_clusters = relationship("CluUrlCluster", back_populates="cluster", cascade="all, delete-orphan")
    decisions    = relationship("CluDecision",   back_populates="cluster", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id":                      self.id,
            "project_id":              self.project_id,
            "cluster_label":           self.cluster_label,
            "primary_url":             self.primary_url,
            "primary_keyword":         self.primary_keyword,
            "total_impressions":       self.total_impressions,
            "total_clicks":            self.total_clicks,
            "url_count":               self.url_count,
            "search_demand_confirmed": self.search_demand_confirmed,
            "louvain_community_id":    self.louvain_community_id,
            "created_at":              self.created_at.isoformat() if self.created_at else None,
        }


class CluUrlCluster(Base):
    """Many-to-many bridge — URL membership and role within a cluster."""
    __tablename__ = "clusteriq_url_clusters"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    url_id               = Column(Integer, ForeignKey("clusteriq_urls.id",     ondelete="CASCADE"), nullable=False, index=True)
    cluster_id           = Column(Integer, ForeignKey("clusteriq_clusters.id", ondelete="CASCADE"), nullable=False, index=True)
    role                 = Column(String(20), default="collateral")  # primary/collateral
    impression_share_pct = Column(Float,   nullable=True)
    rank_in_cluster      = Column(Integer, nullable=True)
    is_multi_topic       = Column(Boolean, default=False)

    url     = relationship("CluUrl",     back_populates="url_clusters")
    cluster = relationship("CluCluster", back_populates="url_clusters")

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "url_id":               self.url_id,
            "cluster_id":           self.cluster_id,
            "role":                 self.role,
            "impression_share_pct": self.impression_share_pct,
            "rank_in_cluster":      self.rank_in_cluster,
            "is_multi_topic":       self.is_multi_topic,
        }


class CluDecision(Base):
    """AI / KUCD-derived action decision for a URL within a cluster."""
    __tablename__ = "clusteriq_decisions"

    id           = Column(Integer,      primary_key=True, autoincrement=True)
    cluster_id   = Column(Integer,      ForeignKey("clusteriq_clusters.id", ondelete="CASCADE"),  nullable=False, index=True)
    url_id       = Column(Integer,      ForeignKey("clusteriq_urls.id",     ondelete="SET NULL"), nullable=True,  index=True)
    verdict      = Column(String(20),   nullable=False, index=True)  # Fix/Consolidate/Optimize/Create/Prune/Keep
    evidence     = Column(JSON,         nullable=True)   # array of reason strings
    target_url   = Column(String(2048), nullable=True)
    confidence   = Column(Float,        nullable=True)   # 0.0–1.0
    generated_by = Column(String(10),   default="kucd")  # kucd/llm
    notes        = Column(Text,         nullable=True)   # free-text notes / LLM brief
    brief_id     = Column(Integer,      nullable=True)   # FK to content_briefs.id (no constraint)
    created_at   = Column(DateTime,     default=lambda: datetime.now(timezone.utc))

    cluster = relationship("CluCluster", back_populates="decisions")
    url     = relationship("CluUrl",     back_populates="decisions")

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "cluster_id":   self.cluster_id,
            "url_id":       self.url_id,
            "verdict":      self.verdict,
            "evidence":     self.evidence,
            "target_url":   self.target_url,
            "confidence":   self.confidence,
            "generated_by": self.generated_by,
            "notes":        self.notes,
            "brief_id":     self.brief_id,
            "created_at":   self.created_at.isoformat() if self.created_at else None,
        }


class CluDuplicate(Base):
    """Duplicate URL pair identified within a ClusterIQ project."""
    __tablename__ = "clusteriq_duplicates"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    project_id       = Column(Integer, ForeignKey("clusteriq_projects.id", ondelete="CASCADE"),  nullable=False, index=True)
    winner_url_id    = Column(Integer, ForeignKey("clusteriq_urls.id",     ondelete="SET NULL"), nullable=True,  index=True)
    duplicate_url_id = Column(Integer, ForeignKey("clusteriq_urls.id",     ondelete="SET NULL"), nullable=True,  index=True)
    similarity_score = Column(Float,   nullable=True)
    overlap_keywords = Column(JSON,    nullable=True)  # array of shared keywords
    winner_reason    = Column(Text,    nullable=True)
    action           = Column(String(20), nullable=True)  # consolidate/redirect/keep
    dev_brief        = Column(Text,    nullable=True)

    project       = relationship("CluProject", back_populates="duplicates")
    winner_url    = relationship("CluUrl", foreign_keys=[winner_url_id],    back_populates="won_duplicates")
    duplicate_url = relationship("CluUrl", foreign_keys=[duplicate_url_id], back_populates="lost_duplicates")

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "project_id":       self.project_id,
            "winner_url_id":    self.winner_url_id,
            "duplicate_url_id": self.duplicate_url_id,
            "similarity_score": self.similarity_score,
            "overlap_keywords": self.overlap_keywords,
            "winner_reason":    self.winner_reason,
            "action":           self.action,
            "dev_brief":        self.dev_brief,
        }


class CluCompetitorCache(Base):
    """
    Cached Ahrefs organic-keyword rows for a competitor domain.
    TTL-checked by the service layer (24 h); rows are replaced on refresh.
    """
    __tablename__ = "clusteriq_competitor_cache"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "competitor_domain", "keyword",
            name="uq_clu_comp_cache_project_domain_keyword",
        ),
        Index("ix_clu_comp_cache_project_domain", "project_id", "competitor_domain"),
    )

    id                = Column(Integer,      primary_key=True, autoincrement=True)
    project_id        = Column(Integer,      ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False)
    competitor_domain = Column(String(512),  nullable=False)
    keyword           = Column(String(500),  nullable=False)
    competitor_url    = Column(String(2048), nullable=True)
    position          = Column(Float,        nullable=True)
    volume            = Column(Integer,      nullable=True)
    fetched_at        = Column(DateTime,     default=lambda: datetime.now(timezone.utc))

    project = relationship("CluProject")

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "project_id":        self.project_id,
            "competitor_domain": self.competitor_domain,
            "keyword":           self.keyword,
            "competitor_url":    self.competitor_url,
            "position":          self.position,
            "volume":            self.volume,
            "fetched_at":        self.fetched_at.isoformat() if self.fetched_at else None,
        }


class CluCompetitorJob(Base):
    """
    Tracks the async competitor-analysis job for a project × domain pair.
    One active job per (project_id, competitor_domain) at a time.
    """
    __tablename__ = "clusteriq_competitor_jobs"
    __table_args__ = (
        Index("ix_clu_comp_jobs_project_domain", "project_id", "competitor_domain"),
    )

    id                      = Column(Integer,     primary_key=True, autoincrement=True)
    project_id              = Column(Integer,     ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False)
    competitor_domain       = Column(String(512), nullable=False)
    status                  = Column(String(20),  default="pending", nullable=False)  # pending/running/done/error
    keywords_fetched        = Column(Integer,     default=0)
    gap_keywords_found      = Column(Integer,     default=0)
    create_decisions_added  = Column(Integer,     default=0)
    cost_estimate_credits   = Column(Float,       nullable=True)
    error_message           = Column(Text,        nullable=True)
    started_at              = Column(DateTime,    nullable=True)
    completed_at            = Column(DateTime,    nullable=True)
    created_at              = Column(DateTime,    default=lambda: datetime.now(timezone.utc))

    project = relationship("CluProject")

    def to_dict(self) -> dict:
        return {
            "id":                     self.id,
            "project_id":             self.project_id,
            "competitor_domain":      self.competitor_domain,
            "status":                 self.status,
            "keywords_fetched":       self.keywords_fetched,
            "gap_keywords_found":     self.gap_keywords_found,
            "create_decisions_added": self.create_decisions_added,
            "cost_estimate_credits":  self.cost_estimate_credits,
            "error_message":          self.error_message,
            "started_at":             self.started_at.isoformat()  if self.started_at  else None,
            "completed_at":           self.completed_at.isoformat() if self.completed_at else None,
            "created_at":             self.created_at.isoformat()   if self.created_at   else None,
        }
