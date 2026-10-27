"""Shared SQLite/Postgres schema and connection configuration."""

import os
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String,
    UniqueConstraint, Uuid, create_engine, false, func, inspect, text,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent


class Base(DeclarativeBase):
    pass


class Interest(Base):
    __tablename__ = "interests"
    # Keep the original fields so earlier submissions remain readable.
    __table_args__ = (
        CheckConstraint(
            "choir IN ('children', 'youth', 'adult', 'multiple', 'helper')",
            name="valid_choir",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), index=True)
    choir: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(String(2000))
    notify: Mapped[bool] = mapped_column(Boolean)
    volunteer: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    children: Mapped[list["InterestChild"]] = relationship(
        cascade="all, delete-orphan", order_by="InterestChild.position", lazy="selectin"
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InterestChild(Base):
    __tablename__ = "interest_children"
    __table_args__ = (UniqueConstraint("interest_id", "position"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    interest_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("interests.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer)


def initialize_database(engine: Engine) -> None:
    """Create tables and apply the additive family-form upgrade without losing entries."""
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("interests")}
        if "volunteer" not in columns:
            connection.execute(text("ALTER TABLE interests ADD COLUMN volunteer BOOLEAN NOT NULL DEFAULT FALSE"))
            connection.execute(text("UPDATE interests SET volunteer = TRUE WHERE choir = 'helper'"))


def build_engine(database_url: str | None = None) -> Engine:
    if not os.getenv("VERCEL"):
        load_dotenv(ROOT / ".env")

    configured = database_url or os.getenv("DATABASE_URL")
    if not configured:
        if os.getenv("VERCEL"):
            raise RuntimeError("Set DATABASE_URL to your Neon Postgres connection string.")
        (ROOT / "data").mkdir(exist_ok=True)
        configured = f"sqlite:///{(ROOT / 'data' / 'choir-festival.db').as_posix()}"

    # Neon supplies a standard postgres:// or postgresql:// URL. Select psycopg 3
    # explicitly while preserving its password, SSL, and channel-binding options.
    if configured.startswith("postgres://"):
        configured = "postgresql://" + configured[len("postgres://"):]
    url = make_url(configured)
    if url.get_backend_name() == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
        return create_engine(
            url,
            poolclass=NullPool,  # Neon handles pooling; no idle serverless connections.
            connect_args={"connect_timeout": 10, "prepare_threshold": None},
            hide_parameters=True,
        )
    if url.get_backend_name() == "sqlite" and not os.getenv("VERCEL"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 10},
            hide_parameters=True,
        )
    raise RuntimeError("Use SQLite locally or a Postgres DATABASE_URL on Vercel.")
