"""Telemetry storage — schema ``telemetry``.

One wide events table, written only by ``POST /ingest`` and read by the
collector query API. This module carries its **own** declarative base, engine
and session factory: the collector must keep serving telemetry even when the
API service is broken or down, so it shares nothing with ``api.app`` beyond the
Postgres instance itself.

Deliberately **no CHECK constraints and almost no NOT NULLs**: ingest must be
tolerant of malformed events and must never 500. Validation belongs in the
ingest layer, which coerces and defaults; the table stores whatever survives.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

DEFAULT_DATABASE_URL = "postgresql+psycopg2://bugforge:bugforge@db:5432/bugforge"

DATABASE_URL: str = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
TELEMETRY_SCHEMA: str = os.getenv("TELEMETRY_SCHEMA", "telemetry")

#: Accepted vocabularies. Not enforced by the database on purpose — ingest
#: normalises what it can and stores the rest verbatim.
EVENT_SOURCES: tuple[str, ...] = ("web", "api")
EVENT_KINDS: tuple[str, ...] = (
    "click",
    "nav",
    "fetch",
    "console",
    "error",
    "request",
    "sql",
    "business",
    "vitals",
)
EVENT_LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")


# --------------------------------------------------------------------------- #
#  Engine + session factory (independent of the api service)
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

metadata = MetaData(schema=TELEMETRY_SCHEMA)


class Base(DeclarativeBase):
    """Declarative base for every table in the ``telemetry`` schema."""

    metadata = metadata


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
#  telemetry.events
# --------------------------------------------------------------------------- #


class Event(Base):
    """A single telemetry event from ``web`` or ``api``.

    ``trace_id`` is per **user interaction**, not per request: one click on
    Place Order that fires three API calls produces one trace_id spanning all
    of them. That is what makes the merged timeline readable.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: When the event happened, as reported by the emitter (UTC, ms precision).
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    #: When the collector persisted it — lets us spot clock skew / late batches.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False, default="web")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="business")
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")

    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    attrs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    __table_args__ = (
        Index("ix_events_trace_id", "trace_id"),
        Index("ix_events_session_id", "session_id"),
        Index("ix_events_user_id", "user_id"),
        Index("ix_events_ts", "ts"),
        # Timeline reads: everything for one trace, in order.
        Index("ix_events_trace_id_ts", "trace_id", "ts"),
        # Session summaries: every trace in a session, in order.
        Index("ix_events_session_id_ts", "session_id", "ts"),
        # /telemetry/search filters.
        Index("ix_events_kind_ts", "kind", "ts"),
        Index("ix_events_level_ts", "level", "ts"),
        Index("ix_events_name", "name"),
        Index("ix_events_source_ts", "source", "ts"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<Event id={self.id} {self.source}/{self.kind} {self.name!r} "
            f"trace={self.trace_id} ts={self.ts}>"
        )

    def as_dict(self) -> dict:
        """Wire shape used by every collector query endpoint."""
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "source": self.source,
            "kind": self.kind,
            "name": self.name,
            "level": self.level,
            "duration_ms": self.duration_ms,
            "attrs": self.attrs or {},
        }


# --------------------------------------------------------------------------- #
#  Lifecycle helpers
# --------------------------------------------------------------------------- #


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_schema() -> None:
    """``CREATE SCHEMA IF NOT EXISTS telemetry``."""
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{TELEMETRY_SCHEMA}"'))


def create_all() -> None:
    """Create the telemetry schema and the events table if missing."""
    ensure_schema()
    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    """Drop the events table (the schema itself is left in place)."""
    Base.metadata.drop_all(bind=engine)


def reset_database() -> None:
    """Hard reset of the telemetry schema — used by ``make reset``."""
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TELEMETRY_SCHEMA}" CASCADE'))
    create_all()


def ping() -> bool:
    """Cheap connectivity probe."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


__all__ = [
    "DATABASE_URL",
    "EVENT_KINDS",
    "EVENT_LEVELS",
    "EVENT_SOURCES",
    "TELEMETRY_SCHEMA",
    "Base",
    "Event",
    "SessionLocal",
    "create_all",
    "drop_all",
    "engine",
    "ensure_schema",
    "get_db",
    "metadata",
    "ping",
    "reset_database",
]
