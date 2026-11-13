"""Корень сборки: каталоги, окружение процесса, логи, фильтр секретов, журнал, база."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path

import pytest
from tests.unit.cli.conftest import journal_events, write_env

from rcbot import __version__, bootstrap
from rcbot.application.ports import NullJournal
from rcbot.bootstrap import (
    build_core,
    ensure_target,
    open_storage,
    transient_target,
    upgrade_database,
    upgrade_database_async,
)
from rcbot.config.paths import Paths
from rcbot.infrastructure.gubuy.api_source import ApiSource
from rcbot.infrastructure.gubuy.dom_source import DomSource
from rcbot.infrastructure.journal.files import FileJournal
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR

# Условные номера из одной повторяющейся цифры: так их не принять за настоящие (решение R1)
STAGE_ID = "9" * 7
TRAMITE_ID = "TRAM-" + "8" * 6
STAGE_URL = f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{STAGE_ID}/1"


def tables(paths: Paths) -> set[str]:
    with sqlite3.connect(paths.database) as db:
        return {r[0] for r in db.execute("select name from sqlite_master where type='table'")}


def test_build_core_создаёт_каталоги_и_задаёт_окружение_процесса(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    core = build_core()

    for directory in (paths.data, paths.discovery, paths.quarantine, paths.shots, paths.tmp):
        assert directory.is_dir()
    assert paths.logs.is_dir()
    assert core.paths == paths
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(paths.browsers)
    assert os.environ["PYTHONPYCACHEPREFIX"] == str(paths.cache / "pycache")


def test_build_core_не_перезаписывает_заданный_путь_браузера(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "общий-браузер"))
    build_core()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "общий-браузер")


def test_логи_идут_в_файл_и_проходят_через_фильтр_секретов(paths: Paths) -> None:
    write_env(paths, agenda_url=STAGE_URL, tramite_id=TRAMITE_ID)
    core = build_core(console=False)

    logging.getLogger("rcbot.test").warning(
        f"заявка {TRAMITE_ID}, этап {STAGE_ID}, цель %s", STAGE_URL
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = paths.log_file.read_text(encoding="utf-8")
    assert TRAMITE_ID not in text
    assert STAGE_ID not in text
    assert f"заявка {MASK}, этап {MASK}" in text
    assert core.settings.tramite_id == TRAMITE_ID


def test_фильтр_получает_номер_заявки_и_номер_этапа_из_настроек(paths: Paths) -> None:
    write_env(paths, agenda_url=STAGE_URL, tramite_id=TRAMITE_ID)
    build_core(console=False)
    assert REDACTOR.redact(TRAMITE_ID) == MASK
    assert REDACTOR.redact(f"этап {STAGE_ID}") == f"этап {MASK}"


def test_адрес_с_меткой_вместо_этапа_ничего_лишнего_в_фильтр_не_кладёт(paths: Paths) -> None:
    """Метка <НОМЕР_ЭТАПА> не число; пустой номер заявки не должен превращаться в «вырезать всё»."""
    build_core(console=False)
    assert REDACTOR.redact("обычный текст про 1 и /etapas/ejecutar/") == (
        "обычный текст про 1 и /etapas/ejecutar/"
    )


@pytest.mark.parametrize(
    ("url", "stage"),
    [
        (STAGE_URL, STAGE_ID),
        ("https://bpmgob.mec.gub.uy/etapas/ejecutar/<НОМЕР_ЭТАПА>/1", "1"),
        ("https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ABC/", ""),
        ("", ""),
    ],
)
def test_etapa_берёт_первое_число_из_адреса(url: str, stage: str) -> None:
    assert bootstrap._etapa(url) == stage


def test_sentry_получает_настройки_и_версию(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bool, str, str]] = []
    monkeypatch.setattr(bootstrap, "init_sentry", lambda *args: calls.append(args))
    write_env(paths, sentry_enabled="true", sentry_dsn="https://key@sentry.invalid/1")
    build_core(console=False)
    assert calls == [(True, "https://key@sentry.invalid/1", f"rcbot@{__version__}")]


def test_журнал_создаётся_пишет_событие_команды_и_логи(tmp_path: Path) -> None:
    core = build_core(console=False, command="check")
    assert isinstance(core.journal, FileJournal)
    assert core.journal.root == tmp_path / "journal"

    logging.getLogger("rcbot.test").info("строка для журнала")
    core.close()

    [event] = [e for e in journal_events(tmp_path / "journal") if e["code"] == "cli.check"]
    assert event["message"] == "запущена команда rcbot check"
    log_text = "".join(p.read_text("utf-8") for p in (tmp_path / "journal" / "logs").glob("*.log"))
    assert "строка для журнала" in log_text


def test_без_имени_команды_событие_не_пишется(tmp_path: Path) -> None:
    build_core(console=False)
    assert journal_events(tmp_path / "journal") == []


def test_журнал_выключен_настройкой_это_нулевой_журнал(paths: Paths, tmp_path: Path) -> None:
    write_env(paths, journal_enabled="false")
    core = build_core(console=False, command="check")
    assert isinstance(core.journal, NullJournal)
    assert not (tmp_path / "journal").exists()
    core.close()  # у нулевого журнала закрывать нечего


def test_журнал_недоступен_бот_живёт_без_него_и_предупреждает_без_путей(
    monkeypatch: pytest.MonkeyPatch, paths: Paths, tmp_path: Path
) -> None:
    blocker = tmp_path / "файл-вместо-папки"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("RCBOT_JOURNAL_DIR", str(blocker / "journal"))

    # setup_logging заменяет обработчики корневого логгера, поэтому смотрим в лог-файл
    core = build_core(console=False, command="run")

    assert isinstance(core.journal, NullJournal)
    core.journal.event("bot.start", "бот запущен")  # пишется в никуда, не падая
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = paths.log_file.read_text(encoding="utf-8")
    assert re.search(r"журнал наблюдения недоступен: \w+Error", text)
    assert "файл-вместо-папки" not in text


def test_close_закрывает_журнал_и_снимает_обработчик_логов(tmp_path: Path) -> None:
    core = build_core(console=False)
    assert isinstance(core.journal, FileJournal)
    handler = core.journal.log_handler
    assert handler in logging.getLogger().handlers

    core.close()

    assert handler not in logging.getLogger().handlers
    assert core.journal.log_handler is None
    core.close()  # повторный вызов безопасен


# --- база данных ---


def test_миграции_создают_схему_и_повторный_запуск_безвреден(paths: Paths) -> None:
    paths.ensure()
    upgrade_database(paths)
    first = tables(paths)
    assert {"watch_targets", "incidents", "alembic_version"} <= first

    with sqlite3.connect(paths.database) as db:
        db.execute(
            "insert into watch_targets(name, agenda_url, location_text, months_ahead) "
            "values ('keep', 'https://x', 'y', 3)"
        )
    upgrade_database(paths)

    assert tables(paths) == first
    with sqlite3.connect(paths.database) as db:
        assert db.execute("select name from watch_targets").fetchall() == [("keep",)]


async def test_миграции_из_работающего_цикла_идут_в_потоке(paths: Paths) -> None:
    paths.ensure()
    loop_thread = asyncio.get_running_loop()
    await upgrade_database_async(paths)
    await upgrade_database_async(paths)
    assert loop_thread is asyncio.get_running_loop()
    assert "incidents" in tables(paths)


async def test_open_storage_применяет_миграции_и_закрывается(paths: Paths) -> None:
    core = build_core(console=False)
    storage = await open_storage(core)
    try:
        target = await ensure_target(core, storage)
        assert target.id > 0
        assert await storage.kv.get("нет-такого") is None
    finally:
        await storage.close()
    assert "poll_stats_hourly" in tables(paths)


async def test_ensure_target_идемпотентен_и_обновляет_настройки(paths: Paths) -> None:
    write_env(paths, agenda_url=STAGE_URL, location_text="Офис А", months_ahead="4")
    core = build_core(console=False)
    storage = await open_storage(core)
    try:
        first = await ensure_target(core, storage)
        second = await ensure_target(core, storage)
        assert first == second
        assert (first.agenda_url, first.location_text, first.months_ahead) == (
            STAGE_URL,
            "Офис А",
            4,
        )
        wider = await ensure_target(core, storage, months_ahead=9)
        assert wider.id == first.id
        assert wider.months_ahead == 9
    finally:
        await storage.close()
    with sqlite3.connect(paths.database) as db:
        assert db.execute("select count(*) from watch_targets").fetchone() == (1,)


def test_разовая_цель_для_check_не_имеет_записи_в_базе(paths: Paths) -> None:
    write_env(paths, agenda_url=STAGE_URL, months_ahead="5")
    core = build_core(console=False)
    target = transient_target(core)
    assert target.id == 0
    assert target.months_ahead == 5
    assert target.agenda_url == STAGE_URL
    assert not paths.database.exists()


# --- сборка источников ---


def test_api_источник_настроен_только_при_заданном_адресе(paths: Paths) -> None:
    core = build_core(console=False)
    assert isinstance(bootstrap.build_api_source(core), ApiSource)
    assert bootstrap.build_api_source(core).configured is False
    write_env(paths, api_url="https://bpmgob.mec.gub.uy/api/x")
    assert bootstrap.build_api_source(build_core(console=False)).configured is True


def test_dom_источник_и_параметры_браузера(paths: Paths) -> None:
    write_env(paths, headless="false")
    core = build_core(console=False)
    assert isinstance(bootstrap.build_dom_source(core), DomSource)
    assert bootstrap.browser_options(core).headless is False
    assert bootstrap.browser_options(core, headless=True).headless is True
    assert bootstrap.browser_options(core, channel="msedge").channel == "msedge"


def test_сессия_собирается_с_лимитом_входов_из_настроек(paths: Paths) -> None:
    write_env(paths, relogin_max_per_hour="2")
    session = bootstrap.build_session(build_core(console=False))
    assert session._max_per_hour == 2


# --- ключ сторожа, выбор источников, поля хранилища ---

PING_KEY = "ping" + "-key-" + "7" * 8


def test_ключ_пинга_сторожа_маскируется_в_логах(paths: Paths) -> None:
    """Путь адреса сторожа — секрет пинга: в логи он попасть не должен (SEC-7)."""
    write_env(paths, watchdog_url=f"https://hc.example/{PING_KEY}/")
    build_core(console=False)
    assert PING_KEY not in REDACTOR.redact(f"GET https://hc.example/{PING_KEY} timeout")


@pytest.mark.parametrize(
    ("url", "key"), [("", ""), ("https://hc.example", ""), ("https://hc.example/a/b/", "a/b")]
)
def test_ключ_пинга_это_путь_адреса(url: str, key: str) -> None:
    assert bootstrap._ping_key(url) == key


async def test_build_sources_без_api_только_dom_и_api_закрыт(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    class Api:
        configured = False

        async def close(self) -> None:
            closed.append("api")

    dom = object()
    monkeypatch.setattr(bootstrap, "build_api_source", lambda core: Api())
    monkeypatch.setattr(bootstrap, "build_dom_source", lambda core: dom)
    primary, fallback = await bootstrap.build_sources(build_core(console=False))
    assert primary is dom and fallback is None
    assert closed == ["api"]


async def test_build_sources_с_api_он_основной_dom_запасной(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Api:
        configured = True

    api, dom = Api(), object()
    monkeypatch.setattr(bootstrap, "build_api_source", lambda core: api)
    monkeypatch.setattr(bootstrap, "build_dom_source", lambda core: dom)
    primary, fallback = await bootstrap.build_sources(build_core(console=False))
    assert primary is api and fallback is dom


def test_api_источник_ходит_только_на_bpmgob(paths: Paths) -> None:
    api = bootstrap.build_api_source(build_core(console=False))
    assert api._http._allowed == frozenset({"bpmgob.mec.gub.uy"})  # type: ignore[attr-defined]


def test_core_как_менеджер_контекста_закрывает_журнал(paths: Paths) -> None:
    closed: list[bool] = []
    with build_core(console=False) as core:
        core.close = lambda: closed.append(True)  # type: ignore[method-assign]
    assert closed == [True]
