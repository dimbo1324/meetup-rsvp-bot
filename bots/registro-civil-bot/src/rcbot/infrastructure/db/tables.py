"""ORM-модели таблиц SPEC 3.2. Секретов и личных данных в БД нет; время — UTC в ISO-строке."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Длина столбца deliveries.error; репозиторий обрезает текст ошибки по ней же
DELIVERY_ERROR_MAX = 300


class UtcIso(TypeDecorator[datetime]):
    """Aware-datetime ⇄ ISO-строка в UTC: строки сравниваются так же, как моменты."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("в БД пишется только aware-datetime")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            # astimezone() принял бы naive-строку за местное время ноутбука; в БД же всё в UTC
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class WatchTargetRow(Base):
    __tablename__ = "watch_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    agenda_url: Mapped[str] = mapped_column(String(300))
    location_text: Mapped[str] = mapped_column(String(200))
    months_ahead: Mapped[int] = mapped_column(Integer)


class IncidentRow(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_target_status", "target_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("watch_targets.id"))
    opened_at: Mapped[datetime] = mapped_column(UtcIso())
    closed_at: Mapped[datetime | None] = mapped_column(UtcIso(), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(UtcIso(), nullable=True)
    status: Mapped[str] = mapped_column(String(16))


class IncidentSlotRow(Base):
    __tablename__ = "incident_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    day: Mapped[str] = mapped_column(String(10))
    at: Mapped[str | None] = mapped_column(String(5), nullable=True)


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    __table_args__ = (Index("ix_deliveries_incident", "incident_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True
    )
    at: Mapped[datetime] = mapped_column(UtcIso())
    channel: Mapped[str] = mapped_column(String(16))
    ok: Mapped[bool] = mapped_column()
    latency_ms: Mapped[float] = mapped_column()
    attempt: Mapped[int] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(String(DELIVERY_ERROR_MAX), default="")


class PollStatsHourRow(Base):
    __tablename__ = "poll_stats_hourly"

    hour: Mapped[datetime] = mapped_column(UtcIso(), primary_key=True)
    checks: Mapped[int] = mapped_column(Integer)
    errors: Mapped[int] = mapped_column(Integer)
    relogins: Mapped[int] = mapped_column(Integer)
    p50_ms: Mapped[float] = mapped_column()
    p95_ms: Mapped[float] = mapped_column()


class AppEventRow(Base):
    __tablename__ = "app_events"
    __table_args__ = (Index("ix_app_events_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UtcIso())
    level: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)


class KvSettingRow(Base):
    __tablename__ = "kv_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
