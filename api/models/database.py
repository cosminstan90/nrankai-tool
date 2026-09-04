"""
Database configuration and backward-compatible model re-exports.

Domain models live in separate files:
  audit.py     — Audit, AuditResult, AuditLog, AuditSummary, AuditTemplate, AuditWeightConfig, ResultNote
  analytics.py — Keywords, GSC, GA4, Ads, Insights models
  content.py   — ContentBrief, Schema, Citations, Gaps, Actions models
  infra.py     — Benchmarks, Schedules, GeoMonitor, Costs, Branding models

All models remain importable from this module for backward compatibility.
"""

import json
from sqlalchemy import event, func
from sqlalchemy.orm import Session

# Re-export engine/session/Base from _base
from api.models._base import Base, engine, sync_engine, AsyncSessionLocal, DATABASE_PATH

# Re-export all domain models (backward-compatible)
from api.models.audit import (
    Audit, AuditResult, AuditLog, AuditSummary, AuditTemplate,
    AuditWeightConfig, ResultNote,
)
from api.models.analytics import (
    KeywordSession, KeywordResult, GscProperty, GscQueryRow, GscPageRow,
    GscQueryHistory, GscPageHistory,
    Ga4Property, Ga4PageRow, Ga4ChannelRow, AdsAccount, AdsSearchTermRow,
    AdsCampaignRow, InsightRun, InsightCard, GoogleOAuthToken,
)
from api.models.content import (
    ContentBrief, SchemaMarkup, CitationTracker, CitationScan,
    DraftOptimization,
    CompetitorGapAnalysis, ContentGap, ActionCard, CrossReferenceJob,
    UrlGuide, LlmsTxtJob,
    FanoutSession, FanoutQuery, FanoutSource,
    FanoutTrackingConfig, FanoutTrackingRun, FanoutTrackingDetail,
    FanoutCompetitiveReport,
    FanoutCacheEntry,
    FanoutSerpValidation,
    FanoutWebhook, FanoutWebhookLog,
    FanoutCrossRefResult,
    FanoutPromptLibrary,
    # Phase 4
    FanoutProject,
    FanoutSentiment,
    GeoBenchmark,
    EntityCheck,
    GscFanoutConnection,
    MentionSeedingConfig, MentionSeedingResult,
    # Phase 5
    BotAccessAudit,
    CocitationMap,
    AnswerCalibration,
    MultilingualGapReport,
)
from api.models.contentiq import CiqAudit, CiqPage, CiqCompetitor, CiqGscToken
from api.models.clusteriq import (
    CluProject, CluUrl, CluSerpData, CluCluster,
    CluUrlCluster, CluDecision, CluDuplicate,
    CluCompetitorCache, CluCompetitorJob,
)
from api.models.serpiq import SiqSnapshot, SiqSerpItem
from api.models.infra import (
    BenchmarkProject, ScheduledAudit, GeoMonitorProject, GeoMonitorScan,
    CostRecord, ClientBilling, BrandingConfig, TrackingProject, TrackingSnapshot,
    PerformanceSnapshot,
)

DEFAULT_TEMPLATES = [
    {
        "name": "SEO Full Stack",
        "description": "Complete SEO audit covering meta tags, structure, content, internal linking, schema markup",
        "icon": "🔍",
        "audit_type": "SEO_AUDIT",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
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
        "model": "claude-sonnet-4-6",
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
        "model": "claude-sonnet-4-6",
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
        "model": "claude-sonnet-4-6",
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
        except Exception as _e:
            import logging as _logging
            _logging.getLogger(__name__).debug(f"ALTER TABLE skipped (likely already migrated): {_e}")

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

    # Migrate content_briefs: add current_score + executive_summary if missing
    # (covered by Alembic migration 0009, but kept here too since not every
    # deployment necessarily runs `alembic upgrade` before startup)
    with sync_engine.connect() as conn:
        try:
            rows = conn.execute(sa_text("PRAGMA table_info(content_briefs)")).fetchall()
            col_names = [row[1] for row in rows]
            if col_names and "current_score" not in col_names:
                conn.execute(sa_text("ALTER TABLE content_briefs ADD COLUMN current_score REAL"))
                conn.commit()
                print("✓ Migrated content_briefs: added current_score")
            if col_names and "executive_summary" not in col_names:
                conn.execute(sa_text("ALTER TABLE content_briefs ADD COLUMN executive_summary TEXT"))
                conn.commit()
                print("✓ Migrated content_briefs: added executive_summary")
        except Exception as _e:
            print(f"[WARN] content_briefs migration: {_e}")

    # Migrate fanout_sessions: add Prompt 15 enrichment columns if missing
    # (covered by Alembic migration 0009 — kept here for the same reason as above)
    _fanout_session_migrations = [
        ("query_origin",     "ALTER TABLE fanout_sessions ADD COLUMN query_origin VARCHAR(20) DEFAULT 'actual'"),
        ("source_origin",    "ALTER TABLE fanout_sessions ADD COLUMN source_origin VARCHAR(20) DEFAULT 'citation'"),
        ("prompt_cluster",   "ALTER TABLE fanout_sessions ADD COLUMN prompt_cluster VARCHAR(50)"),
        ("run_cost_usd",     "ALTER TABLE fanout_sessions ADD COLUMN run_cost_usd REAL DEFAULT 0.0"),
        ("locale",           "ALTER TABLE fanout_sessions ADD COLUMN locale VARCHAR(20) DEFAULT 'en-US'"),
        ("language",         "ALTER TABLE fanout_sessions ADD COLUMN language VARCHAR(10) DEFAULT 'en'"),
        ("confidence_score", "ALTER TABLE fanout_sessions ADD COLUMN confidence_score REAL"),
        ("engine",           "ALTER TABLE fanout_sessions ADD COLUMN engine VARCHAR(50)"),
        ("model_version",    "ALTER TABLE fanout_sessions ADD COLUMN model_version VARCHAR(50)"),
        ("from_cache",       "ALTER TABLE fanout_sessions ADD COLUMN from_cache INTEGER DEFAULT 0"),
    ]
    with sync_engine.connect() as conn:
        try:
            rows = conn.execute(sa_text("PRAGMA table_info(fanout_sessions)")).fetchall()
            col_names = {row[1] for row in rows}
            for col, sql in _fanout_session_migrations:
                if col not in col_names:
                    conn.execute(sa_text(sql))
                    conn.commit()
                    print(f"✓ Migrated fanout_sessions: added {col}")
        except Exception as _e:
            print(f"[WARN] fanout_sessions migration: {_e}")

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


# The listener above only ever covered sync_engine, which is used for migrations.
# Every HTTP request goes through the async engine, and its connections never got
# the pragma -- so on that path SQLite silently ignored every ON DELETE CASCADE in
# the schema, and deleting a parent left its children behind as orphan rows.
@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma_async(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
