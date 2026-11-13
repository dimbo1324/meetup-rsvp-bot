"""Сводка состояния, порты-заглушки и системные часы."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest

from rcbot.application.ports import (
    CheckRecord,
    NoopCoordinator,
    NullJournal,
    OpeningRecord,
    SessionCookie,
)
from rcbot.application.status import PollStatus
from rcbot.domain.models import PollState, SourceKind
from rcbot.infrastructure.clock import SystemClock
from tests.fakes import T0, slots

COOKIE_VALUE = "TEST-COOKIE-VALUE-xyz123"


# --- PollStatus ---


def test_статус_по_умолчанию_работает_без_ошибок_и_проверок() -> None:
    status = PollStatus(started_at=T0)
    assert status.state is PollState.RUNNING
    assert status.source is SourceKind.DOM
    assert (status.checks_total, status.errors_total) == (0, 0)
    assert status.last_check_at is None and status.last_latency_ms is None
    assert status.last_error == "" and status.last_error_at is None
    assert status.recent_latencies == []


def test_списки_задержек_у_разных_статусов_не_общие() -> None:
    first, second = PollStatus(T0), PollStatus(T0)
    first.note_latency(5.0)
    assert second.recent_latencies == []


def test_note_latency_хранит_последние_200_значений_по_порядку() -> None:
    status = PollStatus(T0)
    for value in range(250):
        status.note_latency(float(value))
    assert len(status.recent_latencies) == 200
    assert status.recent_latencies[0] == 50.0
    assert status.recent_latencies[-1] == 249.0


def test_note_latency_на_границе_лимита_ничего_не_теряет() -> None:
    status = PollStatus(T0)
    for value in range(200):
        status.note_latency(float(value))
    assert status.recent_latencies == [float(v) for v in range(200)]
    status.note_latency(200.0)
    assert status.recent_latencies[0] == 1.0 and len(status.recent_latencies) == 200


def test_note_latency_с_малым_лимитом() -> None:
    status = PollStatus(T0)
    for value in (1.0, 2.0, 3.0, 4.0):
        status.note_latency(value, keep=2)
    assert status.recent_latencies == [3.0, 4.0]


# --- SessionCookie ---


def test_cookie_не_показывает_значение_ни_в_одном_представлении() -> None:
    cookie = SessionCookie("simple_bpm_session", COOKIE_VALUE, "bpmgob.mec.gub.uy")
    for text in (
        repr(cookie),
        str(cookie),
        f"{cookie}",
        f"{cookie!r}",
        repr([cookie]),
        repr({"c": cookie}),
        "%s" % cookie,  # noqa: UP031
    ):
        assert COOKIE_VALUE not in text
    assert "simple_bpm_session" in repr(cookie)
    assert "bpmgob.mec.gub.uy" in repr(cookie)


def test_cookie_значение_не_попадает_в_запись_лога(caplog: pytest.LogCaptureFixture) -> None:
    cookie = SessionCookie("sid", COOKIE_VALUE, "x.example")
    with caplog.at_level(logging.INFO):
        logging.getLogger("rcbot.test").info("вход выполнен: %s", cookie)
    assert COOKIE_VALUE not in caplog.text


def test_cookie_по_умолчанию_путь_корень_и_secure() -> None:
    cookie = SessionCookie("a", "b", "c")
    assert (cookie.path, cookie.secure) == ("/", True)
    assert cookie.value == "b"  # значение доступно коду, который передаёт его в aiohttp


def test_cookie_неизменяема_и_сравнивается_по_значению() -> None:
    cookie = SessionCookie("a", "b", "c")
    assert cookie == SessionCookie("a", "b", "c")
    assert cookie != SessionCookie("a", "другое", "c")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cookie.value = "x"  # type: ignore[misc]


# --- NullJournal, NoopCoordinator ---


def test_null_journal_принимает_всё_и_ничего_не_пишет() -> None:
    journal = NullJournal()
    check = CheckRecord(T0, SourceKind.API, True, slots(23), 5.0, "running", 20.0)
    assert journal.check(check) is None
    assert journal.event("code", "сообщение", "WARNING", a=1, b="x") is None
    assert journal.event("code", "сообщение") is None
    opening = OpeningRecord(T0, "appeared", slots(23), SourceKind.API, 12.0)
    assert journal.opening(opening) is None
    assert journal.status({"state": "running"}) is None
    assert journal.enforce_quota() == (0, 0)
    assert journal.size() == 0


async def test_noop_coordinator_всегда_разрешает_тревогу() -> None:
    coordinator = NoopCoordinator()
    assert await coordinator.should_alert("abc") is True
    assert await coordinator.should_alert("") is True


def test_запись_проверки_по_умолчанию_без_ошибки() -> None:
    record = CheckRecord(T0, SourceKind.DOM, True, frozenset(), 3.0, "running", 20.0)
    assert (record.error_kind, record.error) == ("", "")
    assert OpeningRecord(T0, "gone", frozenset(), SourceKind.DOM).incident_open_sec is None


# --- SystemClock ---


def test_now_это_aware_utc_и_близко_к_системному() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=2)


async def test_monotonic_это_loop_time_и_не_идёт_назад() -> None:
    clock = SystemClock()
    first = clock.monotonic()
    assert abs(first - asyncio.get_running_loop().time()) < 1.0
    await asyncio.sleep(0.01)
    assert clock.monotonic() >= first


def test_monotonic_вне_цикла_событий_это_ошибка() -> None:
    with pytest.raises(RuntimeError):
        SystemClock().monotonic()


async def test_sleep_ждёт_заданное_время() -> None:
    started = time.monotonic()
    await SystemClock().sleep(0.03)
    assert time.monotonic() - started >= 0.025


@pytest.mark.parametrize("seconds", [0, -1, -1e9, -0.0])
async def test_sleep_с_нулевым_и_отрицательным_значением_возвращается_сразу(
    seconds: float,
) -> None:
    started = time.monotonic()
    await SystemClock().sleep(seconds)
    assert time.monotonic() - started < 0.5
