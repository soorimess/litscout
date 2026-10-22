"""Web database tables. Paper data stays in each run's own store; jobs only point at their run."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    jobs: Mapped[list["Job"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AuthSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie value
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user: Mapped[User] = relationship()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(8))  # topic | pdf
    topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    n_seeds: Mapped[int] = mapped_column(Integer, default=5)
    seed_files: Mapped[list] = mapped_column(JSON, default=list)  # names only; the files are deleted after import
    review_criteria: Mapped[bool] = mapped_column(Boolean, default=False)
    max_cost_usd: Mapped[float] = mapped_column(Float)
    run_name: Mapped[str] = mapped_column(String(80), unique=True)
    # queued | running | awaiting_criteria | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    log_offset: Mapped[int] = mapped_column(Integer, default=0)  # where this attempt's output starts in job.log
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploads_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship(back_populates="jobs")
