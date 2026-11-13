"""Репозитории SQLAlchemy — реализации портов хранения из `application/ports.py`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import ClassVar
from urllib.parse import urlsplit

from sqlalchemy import delete, desc, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from rcbot.domain.models import (
    AppEvent,
    Channel,
    DeliveryResult,
    Incident,
    IncidentStatus,
    PollStatsHour,
    Slot,
    WatchTarget,
)
from rcbot.infrastructure.db.tables import (
    DELIVERY_ERROR_MAX,
    AppEventRow,
    Base,
    DeliveryRow,
    IncidentRow,
    IncidentSlotRow,
    KvSettingRow,
    PollStatsHourRow,
    WatchTargetRow,
)
from rcbot.infrastructure.logging.redaction import REDACTOR


def _slot_rows(incident_id: int, slots: frozenset[Slot]) -> list[IncidentSlotRow]:
    return [
        IncidentSlotRow(
            incident_id=incident_id,
            day=slot.day.isoformat(),
            at=None if slot.at is None else slot.at.strftime("%H:%M"),
        )
        for slot in sorted(slots)
    ]


def _slot(row: IncidentSlotRow) -> Slot:
    return Slot(date.fromisoformat(row.day), None if row.at is None else time.fromisoformat(row.at))


def _without_path(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}/…" if parsed.netloc else "…"


class SqlTargetRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def ensure(
        self, name: str, agenda_url: str, location_text: str, months_ahead: int
    ) -> WatchTarget:
        async with self._sessions.begin() as session:
            row = await session.scalar(select(WatchTargetRow).where(WatchTargetRow.name == name))
            if row is None:
                row = WatchTargetRow(name=name)
                session.add(row)
            # В БД — только хост: путь адреса содержит номер этапа заявки (инвариант 6, решение R1).
            # Полный адрес живёт в настройках; из БД он не читается
            row.agenda_url = _without_path(agenda_url)
            row.location_text = location_text
            row.months_ahead = months_ahead
            await session.flush()
            return WatchTarget(row.id, row.name, agenda_url, row.location_text, row.months_ahead)


class SqlIncidentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _load(self, session: AsyncSession, row: IncidentRow) -> Incident:
        slots = await session.scalars(
            select(IncidentSlotRow).where(IncidentSlotRow.incident_id == row.id)
        )
        return Incident(
            id=row.id,
            target_id=row.target_id,
            opened_at=row.opened_at,
            slots=frozenset(_slot(s) for s in slots),
            status=IncidentStatus(row.status),
            closed_at=row.closed_at,
            acked_at=row.acked_at,
        )

    async def open(self, target_id: int, opened_at: datetime, slots: frozenset[Slot]) -> Incident:
        async with self._sessions.begin() as session:
            row = IncidentRow(target_id=target_id, opened_at=opened_at, status=IncidentStatus.OPEN)
            session.add(row)
            await session.flush()
            session.add_all(_slot_rows(row.id, slots))
            return Incident(row.id, target_id, opened_at, slots)

    async def update_slots(self, incident_id: int, slots: frozenset[Slot]) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(IncidentSlotRow).where(IncidentSlotRow.incident_id == incident_id)
            )
            session.add_all(_slot_rows(incident_id, slots))

    async def close(self, incident_id: int, closed_at: datetime) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(IncidentRow)
                .where(IncidentRow.id == incident_id)
                .values(status=IncidentStatus.CLOSED, closed_at=closed_at)
            )

    async def ack(self, incident_id: int, acked_at: datetime) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(IncidentRow)
                .where(IncidentRow.id == incident_id, IncidentRow.acked_at.is_(None))
                .values(acked_at=acked_at)
            )

    async def current(self, target_id: int) -> Incident | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IncidentRow)
                .where(
                    IncidentRow.target_id == target_id, IncidentRow.status == IncidentStatus.OPEN
                )
                .order_by(desc(IncidentRow.id))
                .limit(1)
            )
            return None if row is None else await self._load(session, row)

    async def last(self, limit: int) -> list[Incident]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(IncidentRow).order_by(desc(IncidentRow.id)).limit(limit)
            )
            return [await self._load(session, row) for row in rows]

    async def record_delivery(self, result: DeliveryResult, at: datetime) -> None:
        async with self._sessions.begin() as session:
            session.add(
                DeliveryRow(
                    incident_id=result.incident_id,
                    at=at,
                    channel=result.channel,
                    ok=result.ok,
                    latency_ms=result.latency_ms,
                    attempt=result.attempt,
                    # Текст ошибки мог собрать чужой ответ или исключение: в БД секретов нет
                    error=REDACTOR.redact(result.error)[:DELIVERY_ERROR_MAX],
                )
            )

    async def deliveries_for(self, incident_id: int) -> list[DeliveryResult]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(DeliveryRow)
                .where(DeliveryRow.incident_id == incident_id)
                .order_by(DeliveryRow.id)
            )
            return [
                DeliveryResult(
                    Channel(r.channel), r.ok, r.latency_ms, r.attempt, r.error, r.incident_id
                )
                for r in rows
            ]


@dataclass(frozen=True)
class _HourBase:
    """Строка часа, которую процесс застал в БД до своего первого сохранения этого часа."""

    checks: int = 0
    errors: int = 0
    relogins: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0


class SqlStatsRepository:
    """Статистика по часам.

    Опрос копит счётчики часа в памяти с нуля и сохраняет их целиком (накопленным итогом).
    После перезапуска в тот же час в БД уже лежит строка прошлого процесса: при первом
    сохранении часа она запоминается как база, и в БД пишется база + счётчики процесса.
    Повторные сохранения того же часа складываются с той же базой, а не с уже записанной
    суммой, — задвоения нет.
    """

    # Сохраняются только текущий и только что закончившийся час: старые базы не нужны
    _KEEP_BASES = timedelta(hours=2)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._bases: dict[datetime, _HourBase] = {}

    async def save_hour(self, stats: PollStatsHour) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(PollStatsHourRow, stats.hour)
            base = self._bases.get(stats.hour)
            if base is None:
                base = (
                    _HourBase()
                    if row is None
                    else _HourBase(row.checks, row.errors, row.relogins, row.p50_ms, row.p95_ms)
                )
                self._remember(stats.hour, base)
            if row is None:
                row = PollStatsHourRow(hour=stats.hour)
                session.add(row)
            row.checks = base.checks + stats.checks
            row.errors = base.errors + stats.errors
            row.relogins = base.relogins + stats.relogins
            row.p50_ms, row.p95_ms = _merged_percentiles(base, stats)

    def _remember(self, hour: datetime, base: _HourBase) -> None:
        self._bases = {h: b for h, b in self._bases.items() if h > hour - self._KEEP_BASES}
        self._bases[hour] = base

    async def since(self, since: datetime) -> list[PollStatsHour]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(PollStatsHourRow)
                .where(PollStatsHourRow.hour >= since)
                .order_by(PollStatsHourRow.hour)
            )
            result = []
            for row in rows:
                hour = PollStatsHour(row.hour, row.checks, row.errors, row.relogins)
                # Перцентили хранятся готовыми: восстанавливаем их как две точки распределения
                hour.latencies_ms = [row.p50_ms, row.p95_ms]
                result.append(hour)
            return result


def _merged_percentiles(base: _HourBase, stats: PollStatsHour) -> tuple[float, float]:
    """Перцентили базы и замеров процесса.

    Замеры прошлого процесса не хранятся — только его p50 и p95; как и в `since`, они
    восстанавливаются двумя точками. Это приближение, а не точный перцентиль объединения.
    """
    if base.checks == 0:
        return stats.percentile(0.50), stats.percentile(0.95)
    if not stats.latencies_ms:
        return base.p50_ms, base.p95_ms
    merged = PollStatsHour(stats.hour, latencies_ms=[*stats.latencies_ms, base.p50_ms, base.p95_ms])
    return merged.percentile(0.50), merged.percentile(0.95)


class SqlEventLog:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def write(self, event: AppEvent) -> None:
        async with self._sessions.begin() as session:
            session.add(
                AppEventRow(
                    ts=event.ts,
                    level=event.level,
                    code=event.code,
                    # Текст причины мог собрать чужой ответ или исключение: в БД секретов нет
                    message=REDACTOR.redact(event.message),
                )
            )

    async def last(self, limit: int, min_level: str = "WARNING") -> list[AppEvent]:
        names = logging.getLevelNamesMapping()
        threshold = names.get(min_level, logging.WARNING)
        levels = [name for name, value in names.items() if value >= threshold]
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AppEventRow)
                .where(AppEventRow.level.in_(levels))
                .order_by(desc(AppEventRow.id))
                .limit(limit)
            )
            return [AppEvent(r.ts, r.level, r.code, r.message) for r in rows]


class SqlKeyValueStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, key: str) -> str | None:
        async with self._sessions() as session:
            row = await session.get(KvSettingRow, key)
            return None if row is None else row.value

    async def set(self, key: str, value: str) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(KvSettingRow, key)
            if row is None:
                session.add(KvSettingRow(key=key, value=value))
            else:
                row.value = value

    async def all(self) -> dict[str, str]:
        async with self._sessions() as session:
            return {row.key: row.value for row in await session.scalars(select(KvSettingRow))}


class SqlMaintenance:
    """Удаление по срокам хранения и уплотнение файла БД (SPEC 2.7)."""

    _TABLES: ClassVar[dict[str, tuple[type[Base], InstrumentedAttribute[datetime]]]] = {
        "incidents": (IncidentRow, IncidentRow.opened_at),
        "poll_stats_hourly": (PollStatsHourRow, PollStatsHourRow.hour),
        "app_events": (AppEventRow, AppEventRow.ts),
    }

    def __init__(self, engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._engine = engine
        self._sessions = sessions

    async def delete_older_than(self, cutoffs: dict[str, datetime]) -> dict[str, int]:
        removed: dict[str, int] = {}
        async with self._sessions.begin() as session:
            for name, cutoff in cutoffs.items():
                model, column = self._TABLES[name]
                stmt = delete(model).where(column < cutoff)
                if model is IncidentRow:
                    # Открытый инцидент не удаляется, сколько бы он ни длился
                    stmt = stmt.where(IncidentRow.status == IncidentStatus.CLOSED)
                result = await session.execute(stmt)
                removed[name] = int(getattr(result, "rowcount", 0) or 0)
                if model is IncidentRow:
                    # Служебные доставки без инцидента (сбой, зависание) живут тот же срок:
                    # каскад по инциденту их не достаёт
                    await session.execute(
                        delete(DeliveryRow).where(
                            DeliveryRow.incident_id.is_(None), DeliveryRow.at < cutoff
                        )
                    )
        return removed

    async def compact(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            await conn.commit()
        # VACUUM нельзя выполнять внутри транзакции
        async with self._engine.connect() as conn:
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection
            if driver is not None:
                await driver.execute("VACUUM")
                # В WAL результат VACUUM лежит в журнале: без второго checkpoint файл на диске
                # остаётся прежнего размера, и уплотнение ничего не освобождает
                await driver.execute("PRAGMA wal_checkpoint(TRUNCATE)")
