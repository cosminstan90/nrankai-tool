"""Analytics ORM models (Keywords, GSC, GA4, Ads, Insights)."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON, Boolean, func
)
from sqlalchemy.orm import relationship

from api.models._base import Base

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
    source           = Column(String(20),  nullable=False, default="dataforseo")  # dataforseo | import
    status           = Column(String(20),  nullable=False, default="pending")  # pending/running/completed/failed
    progress         = Column(Integer,     nullable=False, default=0)          # 0–100
    progress_message = Column(String(500), nullable=True)
    total_keywords   = Column(Integer,     nullable=False, default=0)
    total_questions  = Column(Integer,     nullable=False, default=0)
    created_at       = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
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
    intent         = Column(String(30), nullable=True)   # informational|commercial|transactional|navigational
    cluster        = Column(String(200),nullable=True)   # topic cluster label
    priority_score = Column(Float,      nullable=True)   # 1–10
    created_at     = Column(DateTime,   default=lambda: datetime.now(timezone.utc))


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
    created_at       = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
    updated_at       = Column(DateTime,    default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))



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
    created_at       = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
    updated_at       = Column(DateTime,    default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

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
    created_at      = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
    updated_at      = Column(DateTime,    default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

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
    created_at       = Column(DateTime,    default=lambda: datetime.now(timezone.utc))

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
    created_at    = Column(DateTime,    default=lambda: datetime.now(timezone.utc))
    updated_at    = Column(DateTime,    default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Per-URL GEO & SEO Guide
# ---------------------------------------------------------------------------


