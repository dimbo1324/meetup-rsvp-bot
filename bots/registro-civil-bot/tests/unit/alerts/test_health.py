"""Здоровье бота (SPEC 2.4, 6.1): время ежедневного отчёта, текст и состав отчёта, «сторож»."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.fakes import T0, ManualClock, MemoryIncidentRepository, MemoryStatsRepository
from tests.unit.alerts.helpers import MONTEVIDEO

from rcbot.application.health import (
    WATCHDOG_EVERY_SEC,
    HealthMonitor,
    next_report_at,
    report_text,
)
from rcbot.domain.models import PollStatsHour

MB = 2**20


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- next_report_at ---
# Montevideo = UTC−3 круглый год (летнего времени нет), 09:00 по Монтевидео = 12:00 UTC


def test_до_часа_отчёта_ближайший_отчёт_сегодня() -> None:
    due = next_report_at(utc(2026, 11, 12, 11, 59, 59), MONTEVIDEO, 9)  # 08:59:59 локально
    assert due.astimezone(UTC) == utc(2026, 11, 12, 12, 0, 0)


def test_ровно_в_час_отчёта_следующий_отчёт_завтра() -> None:
    # target == local: сегодняшний отчёт уже «сейчас», ждать надо не ноль секунд, а сутки
    due = next_report_at(utc(2026, 11, 12, 12, 0, 0), MONTEVIDEO, 9)
    assert due.astimezone(UTC) == utc(2026, 11, 13, 12, 0, 0)


def test_через_секунду_после_часа_отчёта_отчёт_завтра() -> None:
    due = next_report_at(utc(2026, 11, 12, 12, 0, 1), MONTEVIDEO, 9)
    assert due.astimezone(UTC) == utc(2026, 11, 13, 12, 0, 0)


def test_переход_через_полночь_по_местному_времени() -> None:
    # 23:30 локально 12 ноября = 02:30 UTC 13 ноября; отчёт в 00:00 — через 30 минут
    now = utc(2026, 11, 13, 2, 30)
    due = next_report_at(now, MONTEVIDEO, 0)
    assert due - now == timedelta(minutes=30)
    assert due.astimezone(MONTEVIDEO).date().isoformat() == "2026-11-13"


def test_час_отчёта_до_которого_ещё_далеко_после_полуночи_по_местному() -> None:
    # 00:30 локально → отчёт в 09:00 того же местного дня
    now = utc(2026, 11, 12, 3, 30)
    assert next_report_at(now, MONTEVIDEO, 9) - now == timedelta(hours=8, minutes=30)


def test_местный_день_отличается_от_дня_по_utc() -> None:
    # 22:00 UTC 12 ноября — это 19:00 локально; отчёт в 09:00 — завтра в 12:00 UTC
    due = next_report_at(utc(2026, 11, 12, 22, 0), MONTEVIDEO, 9)
    assert due.astimezone(UTC) == utc(2026, 11, 13, 12, 0)


def test_часовой_пояс_берётся_из_настроек_а_не_utc() -> None:
    tokyo = ZoneInfo("Asia/Tokyo")  # UTC+9
    due = next_report_at(utc(2026, 11, 12, 0, 0), tokyo, 9)  # 09:00 в Токио — это 00:00 UTC
    assert due.astimezone(UTC) == utc(2026, 11, 13, 0, 0)


@given(
    now=st.datetimes(
        min_value=datetime(2024, 1, 1), max_value=datetime(2030, 1, 1), timezones=st.just(UTC)
    ),
    hour=st.integers(0, 23),
    zone=st.sampled_from(["America/Montevideo", "Europe/Madrid", "UTC", "Asia/Tokyo"]),
)
def test_свойство_отчёт_строго_в_будущем_не_дальше_суток_и_ровно_в_час_отчёта(
    now: datetime, hour: int, zone: str
) -> None:
    due = next_report_at(now, ZoneInfo(zone), hour)
    wait = due - now
    assert timedelta(0) < wait <= timedelta(hours=25)  # 25 ч — запас на переход на летнее время
    local = due.astimezone(ZoneInfo(zone))
    assert (local.hour, local.minute, local.second) == (hour, 0, 0)


# --- report_text ---


def _hour(checks: int, latencies: list[float], errors: int = 0, relogins: int = 0) -> PollStatsHour:
    return PollStatsHour(T0, checks, errors, relogins, list(latencies))


def test_отчёт_суммирует_проверки_ошибки_и_входы() -> None:
    text = report_text(
        [_hour(3, [100, 200, 300], errors=1, relogins=1), _hour(2, [400, 500], errors=4)], 2, None
    )
    assert "Проверок: 5, ошибок: 5, входов на сайт: 1" in text
    assert "Инцидентов: 2" in text
    assert "Размер папки" not in text
    assert "ни одной успешной" not in text


def test_отчёт_медиана_и_95_перцентиль_одного_часа_точны() -> None:
    latencies = [float(ms) for ms in range(100, 2100, 100)]  # 100…2000, 20 значений
    hour = _hour(20, latencies)
    text = report_text([hour], 0, None)
    assert f"медиана {hour.percentile(0.5):.0f} мс" in text
    assert f"95-й перцентиль {hour.percentile(0.95):.0f} мс" in text
    assert "медиана 1100 мс" in text and "95-й перцентиль 1900 мс" in text


def test_отчёт_по_нескольким_часам_медиана_медиан_и_наибольший_95_й() -> None:
    text = report_text([_hour(3, [100, 200, 300]), _hour(2, [400, 500])], 0, None)
    assert "медиана 300 мс" in text  # медиана из часовых медиан 200 и 400
    assert "95-й перцентиль 500 мс" in text  # хуже из часовых 95-х: 300 и 500


def test_часы_без_успешных_проверок_не_портят_задержки() -> None:
    text = report_text([_hour(0, [], errors=9), _hour(1, [250])], 0, None)
    assert "Проверок: 1, ошибок: 9" in text
    assert "медиана 250 мс" in text and "95-й перцентиль 250 мс" in text


def test_за_сутки_без_успешных_проверок_отчёт_предупреждает() -> None:
    for hours in ([], [_hour(0, [], errors=30)]):
        text = report_text(hours, 0, None)
        assert "⚠️ За сутки не было ни одной успешной проверки." in text
        assert "Проверок: 0" in text
        assert "медиана 0 мс" in text  # без данных не падает и не выдумывает


def test_размер_папки_показывается_в_мегабайтах() -> None:
    assert "Размер папки: 12 МБ" in report_text([_hour(1, [5])], 0, 12 * MB)
    assert "Размер папки: 0 МБ" in report_text([_hour(1, [5])], 0, 0)  # 0 — не «нет данных»


def test_заголовок_отчёта_и_порядок_строк() -> None:
    lines = report_text([_hour(1, [5])], 3, MB).split("\n")
    assert lines[0].startswith("📋 Отчёт rcbot")
    assert [line.split(":")[0] for line in lines[1:]] == [
        "Проверок",
        "Время ответа",
        "Инцидентов",
        "Размер папки",
    ]


# --- build_report ---


class Rig:
    def __init__(self, *, start: datetime = T0, watchdog: object = None) -> None:
        self.clock = ManualClock(start)
        self.stats = MemoryStatsRepository()
        self.incidents = MemoryIncidentRepository()
        self.sent: list[str] = []
        self.current: PollStatsHour | None = None
        self.size = 5 * MB
        self.send_error: Exception | None = None
        self.monitor = HealthMonitor(
            self.stats,
            self.incidents,
            self.clock,
            MONTEVIDEO,
            9,
            self._send,
            lambda: self.current,
            lambda: self.size,
            watchdog,  # type: ignore[arg-type]
        )

    async def _send(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)

    async def open_incident(self, at: datetime) -> None:
        await self.incidents.open(1, at, frozenset())


async def test_отчёт_берёт_последние_24_часа_текущий_час_и_инциденты_за_сутки() -> None:
    rig = Rig()
    await rig.stats.save_hour(PollStatsHour(T0 - timedelta(hours=26), 100, 0, 0, [1.0]))  # старый
    await rig.stats.save_hour(PollStatsHour(T0 - timedelta(hours=5), 10, 2, 1, [100.0] * 10))
    rig.current = PollStatsHour(T0, 4, 1, 0, [200.0] * 4)
    await rig.open_incident(T0 - timedelta(days=3))  # давно
    await rig.open_incident(T0 - timedelta(hours=3))
    await rig.open_incident(T0 - timedelta(minutes=1))
    text = await rig.monitor.build_report()
    assert "Проверок: 14, ошибок: 3, входов на сайт: 1" in text
    assert "Инцидентов: 2" in text
    assert "Размер папки: 5 МБ" in text


async def test_отчёт_без_текущего_часа_и_без_данных_предупреждает() -> None:
    rig = Rig()
    text = await rig.monitor.build_report()
    assert "Проверок: 0" in text and "Инцидентов: 0" in text
    assert "не было ни одной успешной проверки" in text


async def test_инцидент_ровно_суточной_давности_входит_в_отчёт_а_на_секунду_старше_нет() -> None:
    rig = Rig()
    await rig.open_incident(T0 - timedelta(days=1, seconds=1))
    await rig.open_incident(T0 - timedelta(days=1))
    assert "Инцидентов: 1" in await rig.monitor.build_report()


async def test_много_инцидентов_за_сутки_считаются_все_а_не_только_последние_50() -> None:
    rig = Rig()
    for i in range(120):
        await rig.open_incident(T0 - timedelta(minutes=i))
    await rig.open_incident(T0 - timedelta(days=2))
    assert "Инцидентов: 120" in await rig.monitor.build_report()


async def test_отчёт_включает_час_в_который_попадает_начало_суток() -> None:
    """Отчёт уходит чуть позже 09:00:00 (сон не бывает точнее), а часовая строка статистики
    помечена началом часа: сутки не должны терять первый час."""
    rig = Rig(start=T0 + timedelta(seconds=1))
    await rig.stats.save_hour(PollStatsHour(T0 - timedelta(hours=24), 7, 0, 0, [10.0] * 7))
    await rig.stats.save_hour(PollStatsHour(T0 - timedelta(hours=25), 99, 0, 0, [10.0] * 99))
    assert "Проверок: 7," in await rig.monitor.build_report()


# --- daily_reports ---


async def test_отчёт_уходит_в_ближайший_час_отчёта_и_затем_раз_в_сутки() -> None:
    rig = Rig(start=utc(2026, 11, 12, 11, 0))  # 08:00 локально
    task = asyncio.create_task(rig.monitor.daily_reports())
    await rig.clock.advance(3599)
    assert rig.sent == []
    await rig.clock.advance(1)
    assert len(rig.sent) == 1 and rig.sent[0].startswith("📋 Отчёт rcbot")
    await rig.clock.advance(86_399)
    assert len(rig.sent) == 1
    await rig.clock.advance(1)
    assert len(rig.sent) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_если_запуск_ровно_в_час_отчёта_первый_отчёт_через_сутки() -> None:
    rig = Rig(start=utc(2026, 11, 12, 12, 0))  # 09:00:00 по Монтевидео
    task = asyncio.create_task(rig.monitor.daily_reports())
    await rig.clock.advance(86_399)
    assert rig.sent == []
    await rig.clock.advance(1)
    assert len(rig.sent) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_сбой_отправки_отчёта_не_останавливает_отчёты_следующих_суток() -> None:
    rig = Rig(start=utc(2026, 11, 12, 11, 0))
    rig.send_error = OSError("Telegram недоступен")
    task = asyncio.create_task(rig.monitor.daily_reports())
    await rig.clock.advance(3600)
    rig.send_error = None
    await rig.clock.advance(86_400)
    assert len(rig.sent) == 1
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- сторож ---


class FakeWatchdog:
    def __init__(self, results: list[bool | Exception]) -> None:
        self.results = results
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        result = self.results[min(self.pings, len(self.results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


async def test_без_сторожа_задача_завершается_сразу() -> None:
    rig = Rig()
    await asyncio.wait_for(rig.monitor.watchdog(), 1)
    assert rig.clock.sleeping == 0


async def test_сторож_пингуется_сразу_и_раз_в_минуту() -> None:
    dog = FakeWatchdog([True])
    rig = Rig(watchdog=dog)
    task = asyncio.create_task(rig.monitor.watchdog())
    await rig.clock.advance(0)
    assert dog.pings == 1
    await rig.clock.advance(WATCHDOG_EVERY_SEC - 1)
    assert dog.pings == 1
    await rig.clock.advance(1)
    assert dog.pings == 2
    await rig.clock.advance(WATCHDOG_EVERY_SEC * 3)
    assert dog.pings == 5
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_отказ_сторожа_не_роняет_цикл_и_пишется_в_лог(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    dog = FakeWatchdog([False, False, True])
    rig = Rig(watchdog=dog)
    task = asyncio.create_task(rig.monitor.watchdog())
    await rig.clock.advance(WATCHDOG_EVERY_SEC * 2)
    assert dog.pings == 3 and not task.done()
    assert caplog.text.count("«сторож» не ответил") == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_исключение_пинга_не_останавливает_сторожа() -> None:
    dog = FakeWatchdog([OSError("сеть"), True])
    rig = Rig(watchdog=dog)
    task = asyncio.create_task(rig.monitor.watchdog())
    await rig.clock.advance(WATCHDOG_EVERY_SEC)
    assert dog.pings == 2 and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
