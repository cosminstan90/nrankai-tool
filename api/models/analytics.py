"""Analytics ORM models (Keywords, GSC, GA4, Ads, Insights)."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON, Boolean, func,
    Index, UniqueConstraint
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


class GscQueryHistory(Base):
    """
    Dated snapshots of query performance, additive alongside GscQueryRow.

    GscQueryRow is a single current snapshot: every sync/upload deletes and
    replaces it, and 8 call sites across the app (ads.py, meta_generator.py,
    guide.py, etc.) filter it by property_id alone, assuming exactly one row
    per query. Turning that table itself into a time series would silently
    break every one of those aggregates and order_by(clicks.desc()) queries.

    This table accumulates instead of replacing, so it can carry real history
    without touching any existing behavior. GSC's Search Analytics API only
    serves the trailing 16 months -- data not captured here going forward is
    gone for good once that window rolls past it.
    """
    __tablename__ = "gsc_query_history"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    property_id  = Column(String(36),  ForeignKey("gsc_properties.id", ondelete="CASCADE"),
                          nullable=False)
    query        = Column(String(1000), nullable=False)
    clicks       = Column(Integer,     nullable=False, default=0)
    impressions  = Column(Integer,     nullable=False, default=0)
    ctr          = Column(Float,       nullable=True)
    position     = Column(Float,       nullable=True)
    period_start = Column(String(10), nullable=False)   # YYYY-MM-DD, inclusive
    period_end   = Column(String(10), nullable=False)   # YYYY-MM-DD, inclusive
                                                          # (== period_start for one API day)
    source       = Column(String(10), nullable=False)   # "csv" | "api"
    created_at   = Column(DateTime,    default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # Re-syncing the same day/period is an update, not a duplicate; kept
        # separate per source so a CSV upload and an API sync covering the
        # same period never silently clobber each other's numbers.
        UniqueConstraint("property_id", "query", "period_start", "period_end", "source",
                          name="uq_gsc_query_history_period"),
        Index("ix_gsc_query_history_prop_period", "property_id", "period_start"),
        Index("ix_gsc_query_history_prop_query_period", "property_id", "query", "period_start"),
    )


class GscPageHistory(Base):
    """Dated snapshots of page performance. See GscQueryHistory for why this
    is a separate additive table rather than a column added to GscPageRow."""
    __tablename__ = "gsc_page_history"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    property_id  = Column(String(36),  ForeignKey("gsc_properties.id", ondelete="CASCADE"),
                          nullable=False)
    page         = Column(String(2000), nullable=False)
    clicks       = Column(Integer,     nullable=False, default=0)
    impressions  = Column(Integer,     nullable=False, default=0)
    ctr          = Column(Float,       nullable=True)
    position     = Column(Float,       nullable=True)
    period_start = Column(String(10), nullable=False)
    period_end   = Column(String(10), nullable=False)
    source       = Column(String(10), nullable=False)
    created_at   = Column(DateTime,    default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("property_id", "page", "period_start", "period_end", "source",
                          name="uq_gsc_page_history_period"),
        Index("ix_gsc_page_history_prop_period", "property_id", "period_start"),
        Index("ix_gsc_page_history_prop_page_period", "property_id", "page", "period_start"),
    )


class UrlInspection(Base):
    """
    Dated snapshot of one URL's indexation state, from GSC's URL Inspection
    API (Etapa 3 of docs/IMPROVEMENTS_PLAN.md). searchanalytics (what
    GscPageRow/GscPageHistory come from) tells you how a URL performs in
    search; this tells you whether Google can find, crawl, and index it at
    all -- coverage state, which canonical Google actually chose vs what the
    page declares, mobile usability, rich results eligibility.

    Dated like gsc_page_history for the same reason: indexation state
    changes (a page can slip out of the index), and a single "latest state"
    row would erase the fact that it used to be indexed. Unlike GSC
    performance data there is no CSV alternative source -- URL Inspection is
    API-only -- so there's no `source` column here.

    Upserted on (property_id, page_url, checked_date): re-inspecting the
    same URL on the same day updates that day's row rather than duplicating
    it, but each such re-inspection still consumes one unit of Google's
    daily quota -- see UrlInspectionQuotaLog, a separate append-only table,
    for why quota usage can't be derived from counting rows here.
    """
    __tablename__ = "url_inspections"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    property_id  = Column(String(36),  ForeignKey("gsc_properties.id", ondelete="CASCADE"),
                          nullable=False)
    page_url     = Column(String(2000), nullable=False)
    checked_date = Column(String(10),  nullable=False)   # YYYY-MM-DD

    verdict             = Column(String(20),  nullable=True)   # PASS | NEUTRAL | FAIL | VERDICT_UNSPECIFIED
    coverage_state       = Column(String(200), nullable=True)   # Google's human-readable status, e.g. "Submitted and indexed"
    robots_txt_state      = Column(String(30),  nullable=True)   # ALLOWED | DISALLOWED
    indexing_state        = Column(String(40),  nullable=True)   # INDEXING_ALLOWED | BLOCKED_BY_META_TAG | ...
    page_fetch_state      = Column(String(30),  nullable=True)   # SUCCESSFUL | NOT_FOUND | ...
    google_canonical      = Column(String(2000), nullable=True)  # what Google actually picked
    user_canonical        = Column(String(2000), nullable=True)  # what the page declares
    sitemaps_json          = Column(Text, nullable=True)          # JSON list of sitemap URLs this page was found in
    last_crawl_time        = Column(DateTime, nullable=True)

    mobile_usability_verdict = Column(String(20), nullable=True)
    rich_results_verdict     = Column(String(20), nullable=True)

    raw_json     = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("property_id", "page_url", "checked_date",
                          name="uq_url_inspection_period"),
        Index("ix_url_inspection_prop_page", "property_id", "page_url"),
    )

    def to_dict(self):
        import json as _json
        return {
            "id": self.id,
            "page_url": self.page_url,
            "checked_date": self.checked_date,
            "verdict": self.verdict,
            "coverage_state": self.coverage_state,
            "robots_txt_state": self.robots_txt_state,
            "indexing_state": self.indexing_state,
            "page_fetch_state": self.page_fetch_state,
            "google_canonical": self.google_canonical,
            "user_canonical": self.user_canonical,
            "sitemaps": _json.loads(self.sitemaps_json) if self.sitemaps_json else [],
            "last_crawl_time": self.last_crawl_time.isoformat() if self.last_crawl_time else None,
            "mobile_usability_verdict": self.mobile_usability_verdict,
            "rich_results_verdict": self.rich_results_verdict,
        }


class UrlInspectionQuotaLog(Base):
    """
    One row per real call made to GSC's URL Inspection API -- append-only,
    never upserted, so COUNT(*) for (property_id, checked_date) is always
    the true number of quota units spent that day, regardless of how many
    of those calls landed on the same URL (a forced re-check of an already-
    cached page still costs a quota unit, and must still count as one).
    Google enforces roughly 2000 inspections/day/property; api/routes/gsc/
    url_inspection.py refuses to call out once this count would exceed that,
    rather than finding out from a 429 mid-request.
    """
    __tablename__ = "url_inspection_quota_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    property_id  = Column(String(36), ForeignKey("gsc_properties.id", ondelete="CASCADE"), nullable=False)
    checked_date = Column(String(10), nullable=False)
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_url_inspection_quota_prop_date", "property_id", "checked_date"),
    )


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


