"""Таблица ошибок цикла опроса (SPEC 2.3 «Состояния опроса», D3, D4, 0.3 правило 4, 6.3).

Каждая ошибка источника или входа превращается в конкретное состояние, паузу и событие. Моменты
проверок — по `ManualClock`, считаются от первой проверки (0 с).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import pytest
from playwright.async_api import Error as PlaywrightError
from tests.fakes import slots
from tests.unit.loop.harness import Rig, make_rig, spans

from rcbot.application import poll_loop
from rcbot.domain.errors import (
    HostNotAllowed,
    LoginFailed,
    ManualStepRequired,
    RateLimited,
    RcbotError,
    SessionExpired,
    SiteUnavailable,
    SourceNotConfigured,
    UnexpectedResponse,
)
from rcbot.domain.events import (
    ManualActionRequired,
    PollerStateChanged,
    SlotsAppeared,
    SourceSwitched,
)
from rcbot.domain.limits import MIN_DOM_POLL_INTERVAL_SEC
from rcbot.domain.models import PollState, SourceKind

EMPTY: frozenset = frozenset()  # type: ignore[type-arg]
R, A, B, N, P = (
    PollState.RUNNING,
    PollState.REAUTH,
    PollState.BACKOFF,
    PollState.NEEDS_MANUAL,
    PollState.PAUSED,
)


def transitions(rig: Rig) -> list[tuple[PollState, PollState]]:
    return [(e.old, e.new) for e in rig.of_type(PollerStateChanged)]


# --- SessionExpired → REAUTH → вход → продолжение ---


async def test_истёкшая_сессия_вход_и_продолжение_опроса() -> None:
    rig = make_rig([SessionExpired("страница входа"), EMPTY])
    await rig.start()
    await rig.advance(0)
    assert rig.loop.status.state is R  # вход прошёл сразу
    await rig.advance(10)
    # повторная проверка после входа — не раньше 10 с от прошлой (минимум между запросами)
    assert rig.rel(rig.source.starts) == [0, 10]
    assert transitions(rig) == [(R, A), (A, R)]
    assert rig.auth.logins == 1
    assert rig.session.has_session
    assert rig.source.cookie_history, "cookies должны дойти до источника"
    assert rig.journal.codes()[:2] == ["login.start", "login.ok"]
    assert [c.ok for c in rig.journal.checks] == [False, True]
    assert rig.journal.checks[0].error_kind == "SessionExpired"
    await rig.kill()


async def test_вход_засчитывается_в_статистику_часа() -> None:
    rig = make_rig([SessionExpired("x"), EMPTY])
    await rig.start()
    await rig.advance(10)
    hour = rig.loop.current_hour()
    assert hour is not None
    assert hour.relogins == 1
    assert hour.checks == 1  # неудачная проверка — не «проверка»
    await rig.kill()


async def test_после_тревоги_вход_откладывается_на_тихое_окно() -> None:
    rig = make_rig([SessionExpired("x"), SessionExpired("x"), EMPTY])
    rig.session.quiet_after_alert()  # D5: 10 минут не входим, владелец записывается сам
    await rig.start()
    assert rig.auth.logins == 0
    assert rig.loop.status.state is A
    await rig.advance(599)
    assert rig.rel(rig.source.starts) == [0]
    await rig.advance(1)
    # В момент 600 с окно закрылось: проверка, затем вход (ещё одна SessionExpired в сценарии)
    assert rig.rel(rig.source.starts) == [0, 600]
    assert rig.auth.logins == 1
    await rig.kill()


async def test_отсрочка_входа_не_короче_30_с() -> None:
    rig = make_rig([SessionExpired("x"), EMPTY], quiet_after_alert_sec=5.0)
    rig.session.quiet_after_alert()
    await rig.start()
    await rig.advance(29.9)
    assert len(rig.source.starts) == 1
    await rig.advance(0.1)
    assert rig.rel(rig.source.starts) == [0, 30]
    await rig.kill()


async def test_лимит_входов_в_час_переводит_в_BACKOFF_на_15_минут() -> None:
    rig = make_rig([SessionExpired("x")], max_logins_per_hour=1)
    await rig.start()
    await rig.advance(10)
    # первый вход прошёл (лимит 1), на втором — отказ по лимиту
    assert rig.auth.logins == 1
    assert rig.loop.status.state is B
    assert "лимит 1" in rig.loop.status.state_reason
    await rig.advance(899)
    assert rig.rel(rig.source.starts) == [0, 10]
    await rig.advance(1)
    assert rig.rel(rig.source.starts) == [0, 10, 910]
    await rig.kill()


async def test_три_неудачных_входа_подряд_BACKOFF_и_растущая_пауза() -> None:
    fails = [LoginFailed("нет элемента")] * 4
    rig = make_rig([SessionExpired("x")] * 5 + [EMPTY], failures=fails)
    await rig.start()
    assert rig.loop.status.state is A
    await rig.advance(30)  # 2-я неудача
    assert rig.loop.status.state is A
    await rig.advance(30)  # 3-я неудача → BACKOFF
    assert rig.loop.status.state is B
    assert "3 неудачных входов подряд" in rig.loop.status.state_reason
    await rig.advance(30)  # 4-я неудача: пауза растёт 30 → 60
    await rig.advance(60)  # пятый вход успешен
    await rig.advance(10)
    assert rig.rel(rig.source.starts)[:6] == [0, 30, 60, 90, 150, 160]
    assert rig.auth.logins == 5
    assert rig.loop.status.state is R
    assert transitions(rig) == [(R, A), (A, B), (B, R)]
    assert rig.journal.codes().count("login.failed") == 4
    await rig.kill()


async def test_вечно_неработающий_вход_не_превышает_6_попыток_в_час() -> None:
    rig = make_rig([SessionExpired("x")], failures=[LoginFailed("нет")] * 200)
    await rig.start()
    for _ in range(36):
        await rig.advance(100)
    assert rig.auth.logins <= 6
    assert rig.session.logins_last_hour() <= 6
    await rig.kill()


async def test_вход_заблокирован_шлюзом_это_шаг_назад_и_15_минут_а_не_повтор_через_30_с() -> None:
    rig = make_rig([SessionExpired("x"), EMPTY], failures=[RateLimited("шлюз")])
    await rig.start()
    assert rig.loop.status.state is B
    await rig.advance(899)
    assert len(rig.source.starts) == 1
    await rig.advance(1)
    assert rig.rel(rig.source.starts) == [0, 900]
    assert rig.loop.current_interval(rig.clock.now()) == 30.0  # шаг назад выполнен
    await rig.kill()


async def test_вход_упал_чужим_исключением_это_неудачный_вход_а_не_падение_цикла() -> None:
    rig = make_rig([SessionExpired("x"), EMPTY], failures=[ValueError("секрет-значение")])
    await rig.start()
    assert rig.task is not None and not rig.task.done()
    assert rig.loop.status.state is A
    assert rig.loop.status.errors_total == 1
    assert "ValueError" in rig.loop.status.last_error
    assert "секрет-значение" not in rig.loop.status.last_error
    assert all("секрет-значение" not in m for _, m, _, _ in rig.journal.events)
    await rig.advance(30)
    assert rig.loop.status.state is R
    await rig.kill()


async def test_вход_требует_шаг_1_это_NEEDS_MANUAL() -> None:
    rig = make_rig([SessionExpired("x")], failures=[ManualStepRequired("шаг 1")])
    await rig.start()
    assert rig.loop.status.state is N
    assert len(rig.of_type(ManualActionRequired)) == 1
    await rig.kill()


# --- ManualStepRequired (D3) ---


async def test_шаг_1_ровно_одно_событие_и_перепроверка_через_15_минут() -> None:
    rig = make_rig([ManualStepRequired("шаг 1")] * 3 + [EMPTY])
    await rig.start()
    assert rig.loop.status.state is N
    assert len(rig.of_type(ManualActionRequired)) == 1
    await rig.advance(899)
    assert len(rig.source.starts) == 1
    await rig.advance(1)
    await rig.advance(900)
    assert rig.rel(rig.source.starts) == [0, 900, 1800]
    # три проверки подряд требовали шаг 1 — событие всё равно одно
    assert len(rig.of_type(ManualActionRequired)) == 1
    await rig.advance(900)
    assert rig.loop.status.state is R
    assert transitions(rig) == [(R, N), (N, R)]
    await rig.kill()


async def test_шаг_1_после_выздоровления_снова_зовёт_владельца() -> None:
    rig = make_rig([ManualStepRequired("a"), EMPTY, ManualStepRequired("b")])
    await rig.start()
    await rig.advance(900)  # проверка прошла → RUNNING; следующая через 20 с — снова шаг 1
    await rig.advance(20)
    assert len(rig.of_type(ManualActionRequired)) == 2
    assert [e.reason for e in rig.of_type(ManualActionRequired)] == ["a", "b"]
    await rig.kill()


async def test_готово_а_сайт_всё_ещё_просит_шаг_1_владельца_предупреждают_снова() -> None:
    rig = make_rig([ManualStepRequired("шаг 1")])
    await rig.start()
    await rig.advance(30)
    rig.loop.manual_done()  # владелец нажал «Готово», но шаг 1 не пройден
    await rig.advance(0)
    assert len(rig.source.starts) == 2
    assert len(rig.of_type(ManualActionRequired)) == 2
    assert rig.loop.status.state is N
    # без нового «Готово» повторных сообщений нет
    await rig.advance(900)
    assert len(rig.of_type(ManualActionRequired)) == 2
    await rig.kill()


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


# --- UnexpectedResponse → запасной источник ---


async def test_три_неожиданных_ответа_api_включают_запасной_источник_один_раз() -> None:
    rig = make_rig([UnexpectedResponse("формат")], fallback_script=[EMPTY])
    await rig.start()
    await rig.advance(30)  # 2-й
    assert not rig.of_type(SourceSwitched)
    await rig.advance(60)  # 3-й (в 90 с) → переключение
    assert rig.rel(rig.source.starts) == [0, 30, 90]
    switched = rig.of_type(SourceSwitched)
    assert len(switched) == 1
    assert (switched[0].old, switched[0].new) == (SourceKind.API, SourceKind.DOM)
    assert "3 неожиданных" in switched[0].reason
    assert rig.source.closed == 1  # старый источник закрыт
    assert rig.loop.source_kind is SourceKind.DOM
    assert rig.loop.status.source is SourceKind.DOM
    assert rig.loop.status.errors_total == 3
    await rig.advance(10)
    assert rig.fallback.starts == []  # type: ignore[union-attr]  # DomSource: не чаще раза в 60 с
    await rig.advance(MIN_DOM_POLL_INTERVAL_SEC - 10)
    assert rig.rel(rig.fallback.starts) == [90 + MIN_DOM_POLL_INTERVAL_SEC]  # type: ignore[union-attr]
    await rig.kill()


async def test_запасной_источник_получает_cookies_и_остаётся_единственным() -> None:
    rig = make_rig(
        [UnexpectedResponse("формат")],
        fallback_script=[UnexpectedResponse("и тут"), EMPTY],
        base_interval=20.0,
    )
    await rig.start()
    await rig.advance(2000)
    assert len(rig.of_type(SourceSwitched)) == 1  # обратно и повторно не переключаемся
    assert rig.loop.source_kind is SourceKind.DOM
    assert rig.source.closed == 1
    await rig.kill()


async def test_успешная_проверка_обнуляет_счётчик_неожиданных_ответов() -> None:
    rig = make_rig(
        [
            UnexpectedResponse("1"),
            UnexpectedResponse("2"),
            EMPTY,
            UnexpectedResponse("3"),
            UnexpectedResponse("4"),
            EMPTY,
        ],
        fallback_script=[EMPTY],
    )
    await rig.start()
    await rig.advance(1000)
    assert not rig.of_type(SourceSwitched)
    await rig.kill()


async def test_без_запасного_источника_неожиданные_ответы_только_замедляют() -> None:
    rig = make_rig([UnexpectedResponse("формат")])
    await rig.start()
    await rig.advance(30 + 60 + 120)
    assert rig.rel(rig.source.starts) == [0, 30, 90, 210]
    assert not rig.of_type(SourceSwitched)
    assert rig.loop.status.state is B
    await rig.kill()


async def test_DomSource_на_неожиданном_ответе_не_переключается() -> None:
    rig = make_rig([UnexpectedResponse("x")], kind=SourceKind.DOM, fallback_script=[EMPTY])
    await rig.start()
    await rig.advance(1500)
    assert not rig.of_type(SourceSwitched)
    assert rig.loop.source_kind is SourceKind.DOM
    await rig.kill()


async def test_источник_не_настроен_переключение_на_запасной_через_60_с() -> None:
    # Запасной источник — DomSource: минимум между его запросами 60 с, а не 10 с API (COL-11)
    rig = make_rig([SourceNotConfigured("адрес API не задан")], fallback_script=[EMPTY])
    await rig.start()
    assert len(rig.of_type(SourceSwitched)) == 1
    assert rig.loop.next_due - rig.clock.monotonic() == MIN_DOM_POLL_INTERVAL_SEC
    await rig.advance(MIN_DOM_POLL_INTERVAL_SEC - 0.1)
    assert rig.fallback.starts == []  # type: ignore[union-attr]
    await rig.advance(0.1)
    assert rig.rel(rig.fallback.starts) == [MIN_DOM_POLL_INTERVAL_SEC]  # type: ignore[union-attr]
    assert rig.source.closed == 1
    await rig.kill()


async def test_источник_не_настроен_и_запасного_нет_это_BACKOFF_без_переключения() -> None:
    rig = make_rig([SourceNotConfigured("нет адреса")])
    await rig.start()
    assert not rig.of_type(SourceSwitched)
    await rig.advance(30)
    assert rig.rel(rig.source.starts) == [0, 30]
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
    [HostNotAllowed("evil.example"), LoginFailed("x"), RcbotError("прочее")],
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
    assert record.source is SourceKind.API
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
        PlaywrightError("TEST-ETAPA"),
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
    assert rig.source.closed == 1  # источник пересоздаётся: браузер мог умереть
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


# --- вход внутри проверки: отступление, таймаут, учёт времени (COL-4, COL-5, COL-7) ---


async def test_сайт_входа_недоступен_это_экспоненциальное_отступление_а_не_30_с() -> None:
    rig = make_rig(
        [SessionExpired("x"), SessionExpired("x"), SessionExpired("x"), EMPTY],
        failures=[SiteUnavailable("503"), SiteUnavailable("503")],
    )
    await rig.start()
    await rig.advance(30)  # вторая неудача входа: пауза растёт 30 → 60
    assert rig.loop.status.state is B
    await rig.advance(60)
    assert rig.rel(rig.source.starts)[:3] == [0, 30, 90]
    assert rig.journal.codes().count("login.failed") == 0  # это отступление, а не «вход не удался»
    await rig.kill()


async def test_вход_зависший_дольше_лимита_прерывается_как_неудачный_вход(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poll_loop, "LOGIN_TIMEOUT_SEC", 0.05)

    async def stuck_login() -> None:
        await asyncio.Event().wait()

    rig = make_rig([SessionExpired("x"), EMPTY], auth_delay=stuck_login)
    await rig.start()
    await asyncio.sleep(0.12)  # настоящее ожидание таймаута
    assert rig.task is not None and not rig.task.done()
    assert rig.loop.status.errors_total == 1
    assert "не уложился" in rig.loop.status.last_error
    assert "login.failed" in rig.journal.codes()
    assert rig.loop.tick_started_at is None
    await rig.kill()


async def test_длительность_проверки_с_ошибкой_не_включает_повторный_вход() -> None:
    holder: dict[str, Rig] = {}

    async def slow_login() -> None:
        await holder["rig"].clock.sleep(5)

    rig = make_rig([SessionExpired("x"), EMPTY], auth_delay=slow_login)
    holder["rig"] = rig
    await rig.start()
    await rig.advance(5)
    assert rig.journal.checks[0].latency_ms == 0.0  # раньше сюда попадали секунды входа


async def test_у_проверки_с_ошибкой_в_журнале_фактическая_задержка_до_следующей() -> None:
    rig = make_rig([SiteUnavailable("503"), EMPTY])
    await rig.start()
    assert rig.journal.checks[0].interval_sec == 30.0  # раньше — 0.0
    await rig.kill()
