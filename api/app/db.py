"""Database wiring for the ShopForge API.

Owns the SQLAlchemy 2.0 engine, the session factory, the declarative ``Base``
(bound to the ``shop`` schema) and the schema lifecycle helpers used by
``make reset`` / ``make seed``.

Contract:
    DSN  postgresql+psycopg2://bugforge:bugforge@db:5432/bugforge
    Schemas: "shop" (application data) and "telemetry" (events, owned by the
    collector service — this module only guarantees the schema exists).
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

DEFAULT_DATABASE_URL = "postgresql+psycopg2://bugforge:bugforge@db:5432/bugforge"

DATABASE_URL: str = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

SHOP_SCHEMA: str = os.getenv("SHOP_SCHEMA", "shop")
TELEMETRY_SCHEMA: str = os.getenv("TELEMETRY_SCHEMA", "telemetry")

#: Both schemas this project owns. ``reset_database`` drops and recreates both.
ALL_SCHEMAS: tuple[str, ...] = (SHOP_SCHEMA, TELEMETRY_SCHEMA)


# --------------------------------------------------------------------------- #
#  Engine + session factory
# --------------------------------------------------------------------------- #

engine: Engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
)

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


# --------------------------------------------------------------------------- #
#  Declarative base — everything here lives in schema "shop"
# --------------------------------------------------------------------------- #

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema=SHOP_SCHEMA, naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base for every table in the ``shop`` schema."""

    metadata = metadata


# --------------------------------------------------------------------------- #
#  FastAPI dependency
# --------------------------------------------------------------------------- #


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session.

    Usage::

        @router.get("/api/products")
        def list_products(db: Session = Depends(get_db)): ...
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def session_scope() -> Session:
    """Return a bare session for scripts (seed, ghosts). Caller closes it."""
    return SessionLocal()


# --------------------------------------------------------------------------- #
#  Schema lifecycle
# --------------------------------------------------------------------------- #


def ensure_schemas(*schemas: str) -> None:
    """``CREATE SCHEMA IF NOT EXISTS`` for the shop and telemetry schemas."""
    targets = schemas or ALL_SCHEMAS
    with engine.begin() as conn:
        for schema in targets:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def drop_schemas(*schemas: str) -> None:
    """``DROP SCHEMA ... CASCADE`` — destroys every table inside."""
    targets = schemas or ALL_SCHEMAS
    with engine.begin() as conn:
        for schema in targets:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def create_all() -> None:
    """Create both schemas, then every ``shop`` table declared on ``Base``."""
    from app import models  # noqa: F401  (registers the mappers)

    ensure_schemas()
    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    """Drop every ``shop`` table (schemas are left in place)."""
    from app import models  # noqa: F401

    Base.metadata.drop_all(bind=engine)


def reset_database() -> None:
    """Hard reset: drop both schemas, recreate them, recreate ``shop`` tables.

    The ``telemetry`` schema is recreated empty here; the collector owns the
    ``telemetry.events`` table and creates it on start (or via
    ``collector.app.models.create_all``).
    """
    drop_schemas()
    ensure_schemas()
    create_all()


def ping() -> bool:
    """Cheap connectivity probe used by health checks and scripts."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def raw(sql: str, **params: Any) -> list[Any]:
    """Escape hatch for scripts: run raw SQL and return the rows."""
    with engine.begin() as conn:
        return list(conn.execute(text(sql), params))


__all__ = [
    "ALL_SCHEMAS",
    "Base",
    "DATABASE_URL",
    "SHOP_SCHEMA",
    "TELEMETRY_SCHEMA",
    "SessionLocal",
    "create_all",
    "drop_all",
    "drop_schemas",
    "engine",
    "ensure_schemas",
    "get_db",
    "metadata",
    "ping",
    "raw",
    "reset_database",
    "session_scope",
]
