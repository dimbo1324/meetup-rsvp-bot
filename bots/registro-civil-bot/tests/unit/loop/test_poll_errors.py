"""Таблица ошибок цикла опроса (SPEC 2.3 «Состояния опроса», D4, 0.3 правило 4, 6.3).

Каждая ошибка источника превращается в конкретное состояние, паузу и событие. Моменты
проверок — по `ManualClock`, считаются от первой проверки (0 с).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import pytest
from tests.fakes import slots
from tests.unit.loop.harness import Rig, make_rig, spans

from rcbot.application import poll_loop
from rcbot.domain.errors import (
    HostNotAllowed,
    RateLimited,
    RcbotError,
    SiteUnavailable,
    UnexpectedResponse,
)
from rcbot.domain.events import PollerStateChanged, SlotsAppeared
from rcbot.domain.models import PollState, SourceKind

EMPTY: frozenset = frozenset()  # type: ignore[type-arg]
R, B, P = PollState.RUNNING, PollState.BACKOFF, PollState.PAUSED


def transitions(rig: Rig) -> list[tuple[PollState, PollState]]:
    return [(e.old, e.new) for e in rig.of_type(PollerStateChanged)]


# --- RateLimited (D4) ---


async def test_429_шаг_назад_по_лестнице_сохранение_BACKOFF_пауза_15_минут() -> None:
    rig = make_rig([RateLimited("API ответил 429"), EMPTY])  # лестница на базовой ступени
    before = rig.runtime.ladder_started_at
    await rig.start()
    assert rig.loop.status.state is B
    assert "429" in rig.loop.status.state_reason
    # шаг назад: с базовой ступени (≥ 18 ч) на ступень «30 с»
    assert rig.runtime.ladder_started_at > before
    assert rig.loop.current_interval(rig.clock.now()) == 30.0
    assert rig.kv.data["ladder_started_at"] == rig.runtime.ladder_started_at.isoformat()
    assert rig.loop.status.errors_total == 1
    await rig.advance(899)
    assert len(rig.source.starts) == 1
    await rig.advance(1)
    assert rig.rel(rig.source.starts) == [0, 900]
    assert rig.loop.status.state is R
    await rig.kill()


async def test_повторные_429_приводят_лестницу_на_самую_медленную_ступень_и_не_ниже() -> None:
    rig = make_rig([RateLimited("429")] * 3 + [EMPTY])
    await rig.start()
    await rig.advance(900)  # второй 429
    assert rig.loop.current_interval(rig.clock.now()) == 60.0
    await rig.advance(900)  # третий 429: ниже первой ступени отступать некуда
    assert rig.loop.current_interval(rig.clock.now()) == 60.0
    assert rig.runtime.ladder_started_at <= rig.clock.now()
    await rig.kill()


async def test_сбой_записи_лестницы_не_отменяет_паузу_после_429() -> None:
    rig = make_rig([RateLimited("429"), EMPTY])

    async def broken(key: str, value: str) -> None:
        raise OSError("диск")

    rig.kv.set = broken  # type: ignore[method-assign]
    await rig.start()
    assert rig.task is not None and not rig.task.done()
    assert rig.loop.status.state is B
    await rig.advance(899)
    assert len(rig.source.starts) == 1  # без паузы бот стучался бы повторно через 5 с
    await rig.kill()


# --- UnexpectedResponse ---


async def test_неожиданные_ответы_только_замедляют() -> None:
    rig = make_rig([UnexpectedResponse("формат")])
    await rig.start()
    await rig.advance(30 + 60 + 120)
    assert rig.rel(rig.source.starts) == [0, 30, 90, 210]
    assert rig.loop.status.state is B
    await rig.kill()


# --- SiteUnavailable и прочие ошибки ---


async def test_недоступный_сайт_пауза_30_60_120_до_900_и_предел() -> None:
    rig = make_rig([SiteUnavailable("503")])
    await rig.start()
    await rig.advance(3000)
    assert rig.rel(rig.source.starts) == [0, 30, 90, 210, 450, 930, 1830, 2730]
    await rig.kill()


async def test_BACKOFF_только_со_второго_сбоя_подряд() -> None:
    rig = make_rig([SiteUnavailable("503")])
    await rig.start()
    assert rig.loop.status.state is R  # одиночный сбой не тревожит владельца
    assert transitions(rig) == []
    await rig.advance(30)
    assert rig.loop.status.state is B
    assert transitions(rig) == [(R, B)]
    await rig.advance(60)
    await rig.advance(120)
    assert transitions(rig) == [(R, B)]  # повторный BACKOFF не дублируется
    await rig.kill()


async def test_после_успеха_пауза_снова_начинается_с_30_с() -> None:
    rig = make_rig([SiteUnavailable("a")] * 3 + [EMPTY, SiteUnavailable("b")])
    await rig.start()
    await rig.advance(1000)
    assert rig.rel(rig.source.starts)[:7] == [0, 30, 90, 210, 230, 260, 320]
    assert transitions(rig)[:2] == [(R, B), (B, R)]
    await rig.kill()


async def test_ошибка_не_заставляет_опрашивать_чаще_интервала() -> None:
    rig = make_rig([SiteUnavailable("503"), EMPTY], base_interval=10.0)
    await rig.start()
    await rig.advance(500)
    gaps = spans(rig.source.starts)
    assert min(gaps) >= 10.0
    await rig.kill()


@pytest.mark.parametrize(
    "error",
    [HostNotAllowed("evil.example"), RcbotError("прочее")],
)
async def test_прочие_ошибки_бота_замедляют_и_сохраняются_в_статусе(error: RcbotError) -> None:
    rig = make_rig([error, EMPTY])
    await rig.start()
    assert str(error) in rig.loop.status.last_error
    assert rig.loop.status.errors_total == 1
    assert rig.loop.status.last_error_at == rig.clock.now()
    await rig.advance(30)
    assert rig.rel(rig.source.starts) == [0, 30]
    await rig.kill()


async def test_ошибка_записывается_в_журнал_проверок() -> None:
    rig = make_rig([SiteUnavailable("API ответил 503"), EMPTY])
    await rig.start()
    record = rig.journal.checks[0]
    assert not record.ok
    assert record.error_kind == "SiteUnavailable"
    assert record.error == "API ответил 503"
    assert record.slots == frozenset()
    assert record.source is SourceKind.SAE
    await rig.kill()


# --- таймаут проверки ---


async def test_зависшая_проверка_прерывается_источник_пересоздаётся(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poll_loop, "FETCH_TIMEOUT_SEC", 0.05)

    async def hang() -> frozenset:  # type: ignore[type-arg]
        await asyncio.Event().wait()
        return EMPTY

    rig = make_rig([hang, hang, EMPTY])
    await rig.start()
    await asyncio.sleep(0.12)  # настоящее ожидание таймаута: единственное в зоне
    assert "check.timeout" in rig.journal.codes()
    assert rig.source.closed == 1
    assert rig.journal.checks[0].error_kind == "TimeoutError"
    assert rig.loop.status.errors_total == 1
    assert rig.loop.tick_started_at is None
    assert rig.loop.status.state is R  # первый сбой — без тревоги
    await rig.advance(30)
    await asyncio.sleep(0.12)
    assert rig.loop.status.state is B
    assert rig.source.closed == 2
    await rig.advance(60)
    assert rig.rel(rig.source.starts) == [0, 30, 90]
    assert rig.loop.status.state is R
    await rig.kill()


async def test_таймаут_изнутри_источника_это_сбой_источника_а_не_зависание() -> None:
    rig = make_rig([TimeoutError("сам источник"), EMPTY])
    await rig.start()
    assert "check.timeout" not in rig.journal.codes()
    assert "check.crash" in rig.journal.codes()
    await rig.kill()


# --- чужие исключения: цикл не падает ---


@pytest.mark.parametrize(
    "error",
    [
        ValueError("https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1"),
        aiohttp.ClientError("TEST-ETAPA"),
        KeyError("TEST-ETAPA"),
        OSError("TEST-ETAPA"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_чужое_исключение_источника_не_роняет_цикл_и_не_утекает_в_журнал(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = make_rig([error, error, EMPTY])
    await rig.start()
    assert rig.task is not None and not rig.task.done(), "цикл упал: бот перестал бы наблюдать"
    status = rig.loop.status
    assert status.errors_total == 1
    assert status.last_error == f"неожиданный сбой источника: {type(error).__name__}"
    assert rig.source.closed == 1  # источник пересоздаётся: соединение могло сломаться
    assert "check.crash" in rig.journal.codes()
    record = rig.journal.checks[0]
    assert (record.ok, record.error_kind, record.error) == (
        False,
        type(error).__name__,
        type(error).__name__,
    )
    # расписание — как при недоступности сайта (30, 60), а не повтор «в лоб»
    await rig.advance(30)
    assert rig.loop.status.state is B
    await rig.advance(60)
    assert rig.rel(rig.source.starts) == [0, 30, 90]
    assert rig.loop.status.state is R
    # ни текст исключения, ни адрес заявки не попали ни в журнал, ни в лог
    assert all("TEST-ETAPA" not in m for _, m, _, _ in rig.journal.events)
    assert all("TEST-ETAPA" not in c.error for c in rig.journal.checks)
    assert "TEST-ETAPA" not in caplog.text
    await rig.kill()


async def test_чужое_исключение_не_сбрасывает_накопленное_состояние() -> None:
    rig = make_rig([slots(23), ValueError("x"), slots(23)])
    await rig.start()
    await rig.advance(20 + 30)
    assert len(rig.of_type(SlotsAppeared)) == 1  # инцидент не перезапускается сбоем
    assert rig.loop.status.checks_total == 2
    await rig.kill()


async def test_сбой_внутри_цикла_а_не_источника_тоже_не_роняет_опрос(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig([slots(23), EMPTY])

    def broken(event: object) -> None:
        raise RuntimeError("баг в шине")

    monkeypatch.setattr(rig.bus, "publish", broken)
    await rig.start()
    assert rig.task is not None and not rig.task.done()
    assert "poll.crash" in rig.journal.codes()
    assert rig.loop.status.last_error == "сбой цикла опроса: RuntimeError"
    await rig.advance(30)
    assert rig.rel(rig.source.starts) == [0, 30]
    await rig.kill()


async def test_несколько_разных_сбоев_подряд_наращивают_одну_паузу() -> None:
    rig = make_rig([ValueError("a"), SiteUnavailable("b"), RateLimited("c")])
    await rig.start()
    await rig.advance(30)
    assert rig.loop.status.state is B
    await rig.advance(60)
    assert rig.rel(rig.source.starts) == [0, 30, 90]
    await rig.kill()


# --- учёт времени проверки с ошибкой (COL-7) ---


async def test_у_проверки_с_ошибкой_в_журнале_фактическая_задержка_до_следующей() -> None:
    rig = make_rig([SiteUnavailable("503"), EMPTY])
    await rig.start()
    assert rig.journal.checks[0].interval_sec == 30.0  # раньше — 0.0
    await rig.kill()
