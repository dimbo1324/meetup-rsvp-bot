"""Проверка самих тестовых фейков и изоляции: на них держатся тесты всех зон."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

import keyring
import pytest

from rcbot.application.ports import SessionCookie
from rcbot.bootstrap import build_core, upgrade_database_async
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretKey, SecretStore
from rcbot.domain.errors import HostNotAllowed
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.journal.files import FileJournal
from rcbot.infrastructure.logging.redaction import REDACTOR
from tests.fakes import (
    T0,
    TARGET,
    FakeAuthenticator,
    FakeClock,
    FakeNotifier,
    FakeSource,
    LocalTlsServer,
    ManualClock,
    MemoryIncidentRepository,
    RecordingJournal,
    slots,
    slots_at,
)

# --- изоляция окружения ---


def test_keyring_в_памяти_а_не_credential_manager() -> None:
    assert type(keyring.get_keyring()).__name__ == "MemoryKeyring"
    store = SecretStore()
    store.set(SecretKey.PASSWORD, "пароль-для-теста")
    assert store.get(SecretKey.PASSWORD) is not None
    assert store.delete(SecretKey.PASSWORD) is True
    assert store.delete(SecretKey.PASSWORD) is False


def test_окружение_изолировано(bot_home: Path, tmp_path: Path) -> None:
    assert Paths.default().home == bot_home.resolve()
    assert not [
        name
        for name in os.environ
        if name.startswith("RCBOT_")
        and name
        not in (
            "RCBOT_HOME",
            "RCBOT_JOURNAL_DIR",
        )
    ]
    assert os.environ["RCBOT_JOURNAL_DIR"].startswith(str(tmp_path))


def test_фильтр_секретов_чистый_в_начале_теста() -> None:
    assert REDACTOR.redact("пароль-для-теста") == "пароль-для-теста"
    REDACTOR.register("пароль-для-теста")
    assert REDACTOR.redact("пароль-для-теста") == "***"


async def test_миграции_применяются_к_временной_базе(paths: Paths) -> None:
    paths.ensure()
    await upgrade_database_async(paths)
    assert paths.database.exists()


def test_журнал_наблюдения_пишется_во_временную_папку_а_не_на_рабочий_стол(tmp_path: Path) -> None:
    core = build_core(console=False, command="doctor")
    try:
        assert isinstance(core.journal, FileJournal)
        assert core.journal.root == tmp_path / "journal"
        assert (tmp_path / "journal" / "events").exists()
        assert "Desktop" not in str(core.journal.root)
    finally:
        core.close()


# --- часы ---


async def test_fake_clock_сон_сдвигает_время() -> None:
    clock = FakeClock()
    await clock.sleep(90)
    assert clock.now() == T0 + timedelta(seconds=90)
    assert clock.monotonic() == 1090
    assert clock.sleeps == [90]


async def test_manual_clock_будит_по_порядку_сроков() -> None:
    clock = ManualClock()
    woke: list[str] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(f"{name}@{clock.monotonic() - 1000:.0f}")

    tasks = [
        asyncio.create_task(sleeper("c", 30)),
        asyncio.create_task(sleeper("a", 10)),
        asyncio.create_task(sleeper("b", 20)),
    ]
    await clock.advance(5)
    assert woke == []
    await clock.advance(10)
    assert woke == ["a@10"]
    await clock.advance(100)
    assert woke == ["a@10", "b@20", "c@30"]
    assert clock.now() == T0 + timedelta(seconds=115)
    assert all(t.done() for t in tasks)


async def test_manual_clock_спящий_цикл_видит_моменты_пробуждения() -> None:
    clock = ManualClock()
    ticks: list[float] = []

    async def loop() -> None:
        while True:
            await clock.sleep(60)
            ticks.append(clock.monotonic() - 1000)

    task = asyncio.create_task(loop())
    await clock.advance(185)
    assert ticks == [60, 120, 180]
    task.cancel()


# --- адаптеры ---


async def test_fake_source_сценарий_и_повтор_последнего() -> None:
    source = FakeSource([slots(), slots(23), RuntimeError("упал")])
    assert (await source.fetch(TARGET)).slots == frozenset()
    assert (await source.fetch(TARGET)).slots == slots(23)
    with pytest.raises(RuntimeError):
        await source.fetch(TARGET)
    with pytest.raises(RuntimeError):  # после конца сценария повторяется последний элемент
        await source.fetch(TARGET)
    assert len(source.calls) == 4


async def test_fake_source_без_повтора_падает_на_исчерпанном_сценарии() -> None:
    source = FakeSource([slots()], repeat_last=False)
    await source.fetch(TARGET)
    with pytest.raises(AssertionError):
        await source.fetch(TARGET)


async def test_fake_authenticator_отказы_затем_успех() -> None:
    auth = FakeAuthenticator(failures=[RuntimeError("раз")])
    with pytest.raises(RuntimeError):
        await auth.login()
    cookies = await auth.login()
    assert auth.logins == 2
    assert isinstance(cookies[0], SessionCookie)
    assert "TEST" not in repr(cookies[0])  # значение cookie в repr не попадает


async def test_fake_notifier_запоминает_и_отказывает() -> None:
    from rcbot.domain.models import Alert, AlertKind, Channel

    alert = Alert(AlertKind.TEST, "текст")
    good, bad = FakeNotifier(Channel.NTFY), FakeNotifier(Channel.CALL, ok=False, error="нет связи")
    assert (await good.send(alert, 1)).ok
    result = await bad.send(alert, 2)
    assert not result.ok and result.error == "нет связи" and result.attempt == 2
    assert good.attempts == [1] and bad.attempts == [2]


async def test_memory_incident_repository_жизненный_цикл() -> None:
    repo = MemoryIncidentRepository()
    incident = await repo.open(1, T0, slots(23))
    assert (await repo.current(1)) is incident
    await repo.ack(incident.id, T0)
    await repo.close(incident.id, T0 + timedelta(minutes=3))
    assert await repo.current(1) is None
    assert (await repo.last(5))[0].acked


def test_recording_journal_запоминает() -> None:
    journal = RecordingJournal()
    journal.event("x.y", "сообщение", "WARNING", a=1)
    assert journal.codes() == ["x.y"] and journal.events[0][3] == {"a": 1}


def test_слоты_со_временем() -> None:
    assert [s.label() for s in sorted(slots_at((23, 10, 30), (23, 10, 45)))] == [
        "2027-02-23 10:30",
        "2027-02-23 10:45",
    ]


# --- локальный TLS-сервер и SafeHttp ---


async def test_tls_сервер_и_safehttp_по_настоящему_сокету(tls_server: LocalTlsServer) -> None:
    tls_server.respond("GET", "/api", text='{"ok": true}', content_type="application/json")
    http = SafeHttp({"127.0.0.1"}, 5, ssl_context=tls_server.client_ssl)
    try:
        async with http.request("GET", tls_server.url("/api"), headers={"X-Test": "1"}) as response:
            assert response.status == 200
            assert await response.json() == {"ok": True}
    finally:
        await http.close()
    assert tls_server.requests[0].headers["X-Test"] == "1"


async def test_safehttp_не_доверяет_чужому_сертификату(tls_server: LocalTlsServer) -> None:
    http = SafeHttp({"127.0.0.1"}, 5)  # без доверия к trustme: проверка TLS работает
    try:
        with pytest.raises(Exception, match=r"(?i)certificate|ssl"):
            async with http.request("GET", tls_server.url("/")):
                pass
    finally:
        await http.close()


async def test_safehttp_отказывает_хосту_вне_списка(tls_server: LocalTlsServer) -> None:
    http = SafeHttp({"api.telegram.org"}, 5, ssl_context=tls_server.client_ssl)
    with pytest.raises(HostNotAllowed):
        http.request("GET", tls_server.url("/"))
    assert tls_server.requests == []
