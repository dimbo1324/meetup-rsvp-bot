"""SessionManager (SPEC 2.3, D2, D5): cookies в памяти, вход под блокировкой, лимит 6 в час,
тихое окно после тревоги."""

from __future__ import annotations

import asyncio

import pytest
from tests.fakes import FakeAuthenticator, FakeClock, ManualClock, settle

from rcbot.application.ports import SessionCookie
from rcbot.application.session_manager import SessionManager
from rcbot.domain.errors import LoginFailed, ReloginBudgetExceeded, ReloginDeferred


def make(
    max_per_hour: int = 6,
    *,
    failures: list[BaseException] | None = None,
    quiet: float = 600.0,
) -> tuple[SessionManager, FakeAuthenticator, FakeClock]:
    clock = FakeClock()
    auth = FakeAuthenticator(failures=failures or [])
    return SessionManager(auth, clock, max_per_hour, quiet), auth, clock


# --- вход, cookies, подписчики ---


async def test_вход_ставит_cookies_считает_вход_и_раздаёт_подписчикам() -> None:
    manager, auth, _ = make()
    received: list[list[SessionCookie]] = []
    manager.subscribe(lambda cookies: received.append(list(cookies)))
    assert not manager.has_session and received == []  # без cookies подписчик не вызывается
    await manager.relogin("первый вход")
    assert manager.has_session and manager.logins == 1 and auth.logins == 1
    assert [c.name for c in received[0]] == ["simple_bpm_session"]


async def test_подписчик_после_входа_сразу_получает_существующие_cookies() -> None:
    manager, _, _ = make()
    await manager.relogin("x")
    received: list[int] = []
    manager.subscribe(lambda cookies: received.append(len(cookies)))
    assert received == [1]


async def test_cookies_менеджера_не_меняются_снаружи() -> None:
    manager, auth, _ = make()
    await manager.relogin("x")
    auth.cookies.clear()  # источник cookies поменялся — менеджер хранит свою копию
    seen: list[int] = []
    manager.subscribe(lambda cookies: seen.append(len(cookies)))
    assert seen == [1]


async def test_повторный_вход_при_живой_сессии_без_force_ничего_не_делает() -> None:
    manager, auth, _ = make()
    await manager.relogin("x")
    await manager.relogin("ещё раз")
    assert auth.logins == 1 and manager.logins_last_hour() == 1


async def test_force_входит_заново_и_раздаёт_новые_cookies() -> None:
    manager, auth, _ = make()
    seen: list[int] = []
    manager.subscribe(lambda cookies: seen.append(len(cookies)))
    await manager.relogin("x")
    await manager.relogin("по команде", force=True)
    assert auth.logins == 2 and manager.logins == 2 and seen == [1, 1]
    assert manager.session_lifetimes == []  # замена по команде — не «истечение сессии»


async def test_ensure_входит_только_если_нет_сессии() -> None:
    manager, auth, _ = make()
    await manager.ensure()
    await manager.ensure()
    assert auth.logins == 1


# --- время жизни сессии (D2) ---


async def test_время_жизни_сессии_пишется_при_истечении() -> None:
    manager, _, clock = make()
    await manager.relogin("x")
    clock.advance(5400)
    manager.mark_expired()
    assert manager.session_lifetimes == [5400.0]
    assert not manager.has_session
    manager.mark_expired()  # второй раз — сессии уже нет
    assert manager.session_lifetimes == [5400.0]


async def test_mark_expired_без_сессии_ничего_не_пишет() -> None:
    manager, _, _ = make()
    manager.mark_expired()
    assert manager.session_lifetimes == []


# --- неудачный вход ---


async def test_неудачный_вход_не_ставит_cookies_но_расходует_попытку() -> None:
    manager, auth, _ = make(failures=[LoginFailed("нет")])
    sink: list[object] = []
    manager.subscribe(sink.append)
    with pytest.raises(LoginFailed):
        await manager.relogin("x")
    assert not manager.has_session
    assert manager.logins == 0 and auth.logins == 1
    assert manager.logins_last_hour() == 1
    assert sink == []


async def test_неудачные_входы_тоже_упираются_в_лимит() -> None:
    manager, auth, _ = make(2, failures=[LoginFailed("a"), LoginFailed("b")])
    for _ in range(2):
        with pytest.raises(LoginFailed):
            await manager.relogin("x")
    with pytest.raises(ReloginBudgetExceeded):
        await manager.relogin("x")
    assert auth.logins == 2  # к сайту третий раз не ходили


# --- лимит в час ---


async def test_лимит_входов_скользящее_окно_ровно_на_границе_3600_с() -> None:
    manager, auth, clock = make(3)
    for moment in (0, 100, 100):  # попытки в 0, 100, 200 с
        clock.advance(moment)
        await manager.relogin("x", force=True)
    assert manager.logins_last_hour() == 3
    clock.advance(3599.9 - 200)
    with pytest.raises(ReloginBudgetExceeded) as raised:
        await manager.relogin("x", force=True)
    assert "3 входов за час (лимит 3)" in str(raised.value)
    assert auth.logins == 3
    clock.advance(0.1)  # ровно 3600 с с первой попытки: она выпала из окна
    await manager.relogin("x", force=True)
    assert auth.logins == 4
    with pytest.raises(ReloginBudgetExceeded):  # попытки 100, 200, 3600 — снова три
        await manager.relogin("x", force=True)


async def test_лимит_шесть_в_час_по_умолчанию_из_настроек_бота() -> None:
    manager, auth, clock = make(6)
    for _ in range(6):
        await manager.relogin("x", force=True)
        clock.advance(60)
    with pytest.raises(ReloginBudgetExceeded):
        await manager.relogin("x", force=True)
    assert auth.logins == 6


async def test_logins_last_hour_очищает_старые_попытки() -> None:
    manager, _, clock = make()
    await manager.relogin("x", force=True)
    clock.advance(3599)
    assert manager.logins_last_hour() == 1
    clock.advance(1)
    assert manager.logins_last_hour() == 0


async def test_отказ_по_лимиту_сам_попытку_не_расходует() -> None:
    manager, _, clock = make(1)
    await manager.relogin("x", force=True)
    for _ in range(3):
        with pytest.raises(ReloginBudgetExceeded):
            await manager.relogin("x", force=True)
    clock.advance(3600)
    await manager.relogin(
        "x", force=True
    )  # окно свободно ровно через час после единственной попытки


# --- тихое окно после тревоги (D5) ---


async def test_после_тревоги_вход_откладывается_на_600_с() -> None:
    manager, auth, clock = make()
    manager.quiet_after_alert()
    assert manager.quiet_remaining() == 600.0
    with pytest.raises(ReloginDeferred) as raised:
        await manager.relogin("сессия истекла")
    assert "600 с" in str(raised.value)
    clock.advance(599.9)
    with pytest.raises(ReloginDeferred):
        await manager.relogin("сессия истекла")
    assert auth.logins == 0
    assert manager.logins_last_hour() == 0  # отсрочка попытку не расходует
    clock.advance(0.1)
    assert manager.quiet_remaining() == 0.0
    await manager.relogin("сессия истекла")
    assert auth.logins == 1


async def test_новая_тревога_продлевает_тихое_окно_заново() -> None:
    manager, _, clock = make()
    manager.quiet_after_alert()
    clock.advance(500)
    manager.quiet_after_alert()
    assert manager.quiet_remaining() == 600.0


async def test_вход_по_команде_владельца_в_тихом_окне_откладывается_D5_и_сессию_не_портит() -> None:
    # D5: «после уведомления о слотах бот 10 минут не входит заново» — без исключений для
    # команды: владелец сам записывается, наш вход может выбить его сессию. Владелец получает
    # причину и остаток; рабочая сессия бота остаётся нетронутой.
    manager, auth, _ = make()
    await manager.relogin("x")
    manager.quiet_after_alert()
    with pytest.raises(ReloginDeferred):
        await manager.relogin("по команде", force=True)
    assert manager.has_session and auth.logins == 1 and manager.session_lifetimes == []


async def test_отсрочка_проверяется_раньше_лимита() -> None:
    manager, _, _ = make(1)
    await manager.relogin("x", force=True)
    manager.quiet_after_alert()
    with pytest.raises(ReloginDeferred):
        await manager.relogin("x", force=True)


# --- конкурентность ---


async def test_пять_параллельных_входов_это_один_вход() -> None:
    clock = ManualClock()
    auth = FakeAuthenticator(delay=lambda: clock.sleep(5))
    manager = SessionManager(auth, clock, 6)
    tasks = [asyncio.create_task(manager.relogin("гонка")) for _ in range(5)]
    await settle()
    assert auth.concurrent == 1
    await clock.advance(5)
    await asyncio.gather(*tasks)
    assert auth.logins == 1 and manager.logins == 1 and auth.max_concurrent == 1


async def test_параллельные_принудительные_входы_идут_строго_по_очереди() -> None:
    clock = ManualClock()
    auth = FakeAuthenticator(delay=lambda: clock.sleep(5))
    manager = SessionManager(auth, clock, 6)
    tasks = [asyncio.create_task(manager.relogin("x", force=True)) for _ in range(3)]
    await clock.advance(15)
    await asyncio.gather(*tasks)
    assert auth.logins == 3 and auth.max_concurrent == 1


async def test_отменённый_вход_освобождает_блокировку() -> None:
    clock = ManualClock()
    auth = FakeAuthenticator(delay=lambda: clock.sleep(1000))
    manager = SessionManager(auth, clock, 6)
    task = asyncio.create_task(manager.relogin("x"))
    await settle()
    assert auth.concurrent == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert auth.concurrent == 0 and not manager.has_session
    auth._delay = None  # type: ignore[assignment]
    await asyncio.wait_for(manager.relogin("ещё раз"), 1)
    assert manager.has_session
