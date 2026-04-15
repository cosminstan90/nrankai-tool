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
    Ga4Property, Ga4PageRow, Ga4ChannelRow, AdsAccount, AdsSearchTermRow,
    AdsCampaignRow, InsightRun, InsightCard, GoogleOAuthToken,
)
from api.models.content import (
    ContentBrief, SchemaMarkup, CitationTracker, CitationScan,
    CompetitorGapAnalysis, ContentGap, ActionCard, CrossReferenceJob,
    UrlGuide, LlmsTxtJob,
    FanoutSession, FanoutQuery, FanoutSource,
    FanoutTrackingConfig, FanoutTrackingRun, FanoutTrackingDetail,
)
from api.models.infra import (
    BenchmarkProject, ScheduledAudit, GeoMonitorProject, GeoMonitorScan,
    CostRecord, ClientBilling, BrandingConfig, TrackingProject, TrackingSnapshot,
)

DEFAULT_TEMPLATES = [
    {
        "name": "SEO Full Stack",
        "description": "Complete SEO audit covering meta tags, structure, content, internal linking, schema markup",
        "icon": "🔍",
        "audit_type": "SEO_AUDIT",
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
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
        "model": "claude-sonnet-4-20250514",
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
        "model": "claude-sonnet-4-20250514",
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
        "model": "claude-sonnet-4-20250514",
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
        except Exception:
            pass

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


async def init_db_async():
    """Initialize database tables (async version)."""
    # Migrate benchmark_projects if it has the old schema (websites/audit_config columns)
    from sqlalchemy import text as sa_text
    async with engine.begin() as conn:
        try:
            rows = await conn.execute(sa_text("PRAGMA table_info(benchmark_projects)"))
            col_names = [row[1] for row in rows.fetchall()]
            if col_names and "websites" in col_names:
                await conn.execute(sa_text("DROP TABLE IF EXISTS benchmark_projects"))
                print("✓ Migrated benchmark_projects to new schema")
        except Exception:
            pass

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Add new columns to gsc_properties if they don't exist yet (SQLite migration)
    async with engine.begin() as conn:
        try:
            rows = await conn.execute(sa_text("PRAGMA table_info(gsc_properties)"))
            col_names = [row[1] for row in rows.fetchall()]
            if col_names and "last_synced_at" not in col_names:
                await conn.execute(sa_text("ALTER TABLE gsc_properties ADD COLUMN last_synced_at DATETIME"))
                print("✓ Migrated gsc_properties: added last_synced_at")
            if col_names and "sync_type" not in col_names:
                await conn.execute(sa_text("ALTER TABLE gsc_properties ADD COLUMN sync_type VARCHAR(10) NOT NULL DEFAULT 'csv'"))
                print("✓ Migrated gsc_properties: added sync_type")
        except Exception:
            pass

    # Migrate url_guides: add reviewed column if missing
    async with engine.begin() as conn:
        try:
            rows = await conn.execute(sa_text("PRAGMA table_info(url_guides)"))
            col_names = [row[1] for row in rows.fetchall()]
            if col_names and "reviewed" not in col_names:
                await conn.execute(sa_text("ALTER TABLE url_guides ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0"))
                print("✓ Migrated url_guides: added reviewed")
        except Exception:
            pass

    # Seed default templates if table is empty
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(func.count(AuditTemplate.id)))
        existing_templates = result.scalar()
        
        if existing_templates == 0:
            print("🌱 Seeding default audit templates...")
            for template_data in DEFAULT_TEMPLATES:
                template = AuditTemplate(**template_data)
                session.add(template)
            await session.commit()
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
