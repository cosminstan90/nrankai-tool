"""
Database engine, session, and Base for ORM models.

Imported by domain model files to avoid circular imports.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import StaticPool

# Database file location
DATABASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATABASE_DIR, exist_ok=True)
# GEO_TOOL_DB_PATH lets the test suite point at a throwaway database.
# Read at import time, so anything overriding it must do so before this
# module is first imported (see tests/conftest.py).
DATABASE_PATH = os.environ.get("GEO_TOOL_DB_PATH") or os.path.join(DATABASE_DIR, "analyzer.db")

# Async SQLite URL
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

# Create async engine
engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    echo=False
)

# Sync engine for migrations
sync_engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

# Session factory
AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# Base class for all ORM models
Base = declarative_base()
