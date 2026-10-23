"""Модели таблиц (раздел 10 спецификации). Всё время хранится в UTC."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from meetup_bot.db.base import Base
from meetup_bot.db.types import UtcDateTime


class EventStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class RegistrationStatus(enum.StrEnum):
    ACTIVE = "active"
    WAITLIST = "waitlist"
    CANCELLED = "cancelled"
    REMOVED = "removed"


class Attendance(enum.StrEnum):
    UNKNOWN = "unknown"
    ATTENDED = "attended"
    NO_SHOW = "no_show"


class RegistrationSource(enum.StrEnum):
    BUTTON = "button"
    ADMIN = "admin"


class BanReason(enum.StrEnum):
    NO_SHOW = "no_show"
    MANUAL = "manual"


class AdminRole(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"


def _in(column: str, enum_cls: type[enum.StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_name: Mapped[str] = mapped_column(Text, default="")
    last_name: Mapped[str | None] = mapped_column(Text, default=None)
    username: Mapped[str | None] = mapped_column(Text, default=None)
    dm_available: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Admin(Base):
    __tablename__ = "admins"
    __table_args__ = (CheckConstraint(_in("role", AdminRole), name="role"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(10))
    added_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    added_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(_in("status", EventStatus), name="status"),
        CheckConstraint("capacity >= 1", name="capacity_positive"),
        Index("ix_events_status_starts_at", "status", "starts_at"),
        Index("ix_events_status_registration_closes_at", "status", "registration_closes_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(10), default=EventStatus.OPEN.value)
    starts_at: Mapped[datetime] = mapped_column(UtcDateTime)
    capacity: Mapped[int] = mapped_column(Integer)
    description_html: Mapped[str] = mapped_column(Text, default="")
    photo_file_id: Mapped[str | None] = mapped_column(Text, default=None)
    place: Mapped[str] = mapped_column(Text, default="")
    place_url: Mapped[str] = mapped_column(Text, default="")
    cancel_deadline_at: Mapped[datetime] = mapped_column(UtcDateTime)
    registration_closes_at: Mapped[datetime] = mapped_column(UtcDateTime)
    reminder_at: Mapped[datetime] = mapped_column(UtcDateTime)
    attendance_prompt_at: Mapped[datetime] = mapped_column(UtcDateTime)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    attendance_prompt_sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    attendance_reminder_sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    attendance_marked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    attendance_marked_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    cancel_reason: Mapped[str | None] = mapped_column(Text, default=None)
    recurrence_key: Mapped[str | None] = mapped_column(Text, unique=True, default=None)
    created_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (
        CheckConstraint(_in("status", RegistrationStatus), name="status"),
        CheckConstraint(_in("attendance", Attendance), name="attendance"),
        CheckConstraint(_in("source", RegistrationSource), name="source"),
        CheckConstraint("guests IN (0, 1)", name="guests"),
        CheckConstraint("user_id IS NOT NULL OR manual_name IS NOT NULL", name="who"),
        Index(
            "uq_registrations_event_user_live",
            "event_id",
            "user_id",
            unique=True,
            sqlite_where=text("status IN ('active', 'waitlist') AND user_id IS NOT NULL"),
        ),
        Index("ix_registrations_event_status_created_at", "event_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    manual_name: Mapped[str | None] = mapped_column(Text, default=None)
    guests: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(10), default=RegistrationSource.BUTTON.value)
    attendance: Mapped[str] = mapped_column(String(10), default=Attendance.UNKNOWN.value)
    late_promoted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    promoted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    removed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    removed_by: Mapped[int | None] = mapped_column(BigInteger, default=None)


class EventPost(Base):
    __tablename__ = "event_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    inline_message_id: Mapped[str | None] = mapped_column(Text, unique=True, default=None)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    message_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    posted_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_render_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    last_edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    is_dead: Mapped[bool] = mapped_column(Boolean, default=False)


class Ban(Base):
    __tablename__ = "bans"
    __table_args__ = (
        CheckConstraint(_in("reason", BanReason), name="reason"),
        Index(
            "uq_bans_user_active",
            "user_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(String(10))
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), default=None)
    banned_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    banned_at: Mapped[datetime] = mapped_column(UtcDateTime)
    unbanned_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    unbanned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    action: Mapped[str] = mapped_column(Text)
    event_id: Mapped[int | None] = mapped_column(Integer, default=None)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    payload: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
