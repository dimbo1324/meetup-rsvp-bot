# ruff: noqa: ASYNC240  (в тестах небольшие файловые операции допустимы)
"""Ежедневный бэкап SQLite (17.5): имя файла, ротация, права, целостность, задача планировщика."""

import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from meetup_bot.clock import FakeClock
from meetup_bot.scheduler import backup as backup_module
from meetup_bot.scheduler.backup import (
    BACKUP_GLOB,
    RETRY_AFTER_FAILURE,
    BackupTask,
    backup_database,
    backup_path_for,
)
from tests.helpers import App, make_app

TZ = "America/Montevideo"
# 09:00 по Монтевидео
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connection.executemany("INSERT INTO notes (body) VALUES (?)", [("один",), ("два",), ("три",)])
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    return tmp_path / "backups"


def read_notes(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute("SELECT body FROM notes ORDER BY id")]
    finally:
        connection.close()


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# ---- имя файла ------------------------------------------------------------------------


def test_имя_файла_по_локальной_дате(backups: Path) -> None:
    assert backup_path_for(backups, NOW, TZ) == backups / "bot-2026-10-23.db"


def test_имя_файла_у_полуночи_берёт_локальную_а_не_UTC_дату(backups: Path) -> None:
    # 01:30 UTC = 22:30 предыдущего дня по Монтевидео
    moment = datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    assert backup_path_for(backups, moment, TZ).name == "bot-2026-10-22.db"
    assert backup_path_for(backups, moment, "UTC").name == "bot-2026-10-23.db"


def test_имя_файла_по_умолчанию_в_часовом_поясе_по_умолчанию(backups: Path) -> None:
    moment = datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    assert backup_path_for(backups, moment).name == "bot-2026-10-22.db"


def test_шаблон_имён_совпадает_с_файлами_бэкапа() -> None:
    assert BACKUP_GLOB == "bot-????-??-??.db"


# ---- backup_database ------------------------------------------------------------------


async def test_бэкап_создаёт_файл_с_содержимым_базы(source: Path, backups: Path) -> None:
    path = await backup_database(source, backups, NOW, timezone=TZ)
    assert path == backups / "bot-2026-10-23.db"
    assert path is not None and path.exists()
    assert read_notes(path) == ["один", "два", "три"]


async def test_бэкап_создаёт_каталог_если_его_нет(source: Path, backups: Path) -> None:
    assert not backups.exists()
    await backup_database(source, backups, NOW, timezone=TZ)
    assert backups.is_dir()


async def test_бэкап_проходит_integrity_check(source: Path, backups: Path) -> None:
    path = await backup_database(source, backups, NOW, timezone=TZ)
    assert path is not None
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


async def test_после_бэкапа_не_остаётся_временных_файлов(source: Path, backups: Path) -> None:
    await backup_database(source, backups, NOW, timezone=TZ)
    assert names(backups) == ["bot-2026-10-23.db"]


async def test_повторный_бэкап_за_день_не_делается(source: Path, backups: Path) -> None:
    first = await backup_database(source, backups, NOW, timezone=TZ)
    assert first is not None
    before = first.stat().st_mtime_ns
    # База изменилась, но маркер «за сегодня уже сделан» — сам файл
    connection = sqlite3.connect(source)
    connection.execute("INSERT INTO notes (body) VALUES ('четыре')")
    connection.commit()
    connection.close()
    assert await backup_database(source, backups, NOW + timedelta(hours=5), timezone=TZ) is None
    assert first.stat().st_mtime_ns == before
    assert read_notes(first) == ["один", "два", "три"]


async def test_на_следующий_день_делается_новый_бэкап(source: Path, backups: Path) -> None:
    await backup_database(source, backups, NOW, timezone=TZ)
    second = await backup_database(source, backups, NOW + timedelta(days=1), timezone=TZ)
    assert second == backups / "bot-2026-10-24.db"
    assert names(backups) == ["bot-2026-10-23.db", "bot-2026-10-24.db"]


async def test_ротация_оставляет_14_последних(source: Path, backups: Path) -> None:
    backups.mkdir()
    for day in range(1, 21):  # 20 старых копий: 2026-09-01 … 2026-09-20
        (backups / f"bot-2026-09-{day:02d}.db").write_bytes(b"old")
    await backup_database(source, backups, NOW, timezone=TZ)
    kept = names(backups)
    assert len(kept) == 14
    assert kept[-1] == "bot-2026-10-23.db"
    assert kept[0] == "bot-2026-09-08.db"


async def test_ротация_по_умолчанию_14_а_keep_настраивается(source: Path, backups: Path) -> None:
    for day in range(5):
        await backup_database(source, backups, NOW + timedelta(days=day), keep=3, timezone=TZ)
    assert names(backups) == ["bot-2026-10-25.db", "bot-2026-10-26.db", "bot-2026-10-27.db"]


async def test_ротация_не_трогает_посторонние_файлы(source: Path, backups: Path) -> None:
    backups.mkdir()
    for name in ("notes.db", "bot-latest.db", "bot-2026-10-1.db", "README.txt"):
        (backups / name).write_bytes(b"keep")
    for day in range(3):
        await backup_database(source, backups, NOW + timedelta(days=day), keep=1, timezone=TZ)
    assert {"notes.db", "bot-latest.db", "bot-2026-10-1.db", "README.txt"} <= set(names(backups))
    assert "bot-2026-10-25.db" in names(backups)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-права на Windows не применяются")
async def test_права_на_файл_бэкапа_только_владельцу(source: Path, backups: Path) -> None:
    path = await backup_database(source, backups, NOW, timezone=TZ)
    assert path is not None
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-права на Windows не применяются")
async def test_права_на_каталог_бэкапов_только_владельцу(source: Path, backups: Path) -> None:
    await backup_database(source, backups, NOW, timezone=TZ)
    assert backups.stat().st_mode & 0o777 == 0o700


async def test_осиротевшие_tmp_чистятся(source: Path, backups: Path) -> None:
    backups.mkdir()
    (backups / "bot-2026-10-01.db.tmp").write_bytes(b"orphan")
    (backups / "bot-2026-10-23.db.tmp").write_bytes(b"orphan of today")
    await backup_database(source, backups, NOW, timezone=TZ)
    assert names(backups) == ["bot-2026-10-23.db"]
    assert read_notes(backups / "bot-2026-10-23.db") == ["один", "два", "три"]


async def test_провал_integrity_check_не_оставляет_ни_копии_ни_tmp(
    source: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_module, "_integrity_ok", lambda path: False)
    with pytest.raises(RuntimeError, match="целостности"):
        await backup_database(source, backups, NOW, timezone=TZ)
    assert names(backups) == []


async def test_после_провала_целостности_бэкап_можно_повторить(
    source: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(backup_module, "_integrity_ok", lambda path: False)
        with pytest.raises(RuntimeError):
            await backup_database(source, backups, NOW, timezone=TZ)
    assert await backup_database(source, backups, NOW, timezone=TZ) is not None


async def test_отсутствующая_база_это_FileNotFoundError(tmp_path: Path, backups: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await backup_database(tmp_path / "нет.db", backups, NOW, timezone=TZ)
    assert not (backups / "bot-2026-10-23.db").exists()


async def test_повреждённая_база_не_даёт_бэкапа(tmp_path: Path, backups: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        await backup_database(broken, backups, NOW, timezone=TZ)
    assert not (backups / "bot-2026-10-23.db").exists()


@pytest.mark.parametrize("keep", [0, -1])
async def test_keep_меньше_единицы_отклоняется(source: Path, backups: Path, keep: int) -> None:
    with pytest.raises(ValueError, match="keep"):
        await backup_database(source, backups, NOW, keep=keep, timezone=TZ)


def test_integrity_ok_на_исправной_базе(source: Path) -> None:
    assert backup_module._integrity_ok(source) is True


# ---- BackupTask -----------------------------------------------------------------------


def make_task(source: Path, backups: Path, **kwargs: object) -> BackupTask:
    return BackupTask(source, backups, timezone=TZ, **kwargs)  # type: ignore[arg-type]


async def test_до_четырёх_утра_бэкап_не_делается(source: Path, backups: Path) -> None:
    task = make_task(source, backups)
    assert await task.run(datetime(2026, 10, 23, 6, 59, tzinfo=UTC)) == 0  # 03:59 локально
    assert not backups.exists()


async def test_ровно_в_четыре_утра_бэкап_делается(source: Path, backups: Path) -> None:
    task = make_task(source, backups)
    assert await task.run(datetime(2026, 10, 23, 7, 0, tzinfo=UTC)) == 1  # 04:00 локально
    assert names(backups) == ["bot-2026-10-23.db"]


async def test_позже_четырёх_утра_бэкап_догоняется(source: Path, backups: Path) -> None:
    assert await make_task(source, backups).run(NOW) == 1


async def test_задача_не_делает_второй_бэкап_за_день(source: Path, backups: Path) -> None:
    task = make_task(source, backups)
    assert await task.run(NOW) == 1
    assert await task.run(NOW) == 0
    assert await task.run(NOW + timedelta(hours=8)) == 0
    assert len(names(backups)) == 1


async def test_задача_делает_бэкап_каждый_день(source: Path, backups: Path) -> None:
    task = make_task(source, backups)
    for day in range(3):
        assert await task.run(NOW + timedelta(days=day)) == 1
    assert len(names(backups)) == 3


async def test_задача_учитывает_keep(source: Path, backups: Path) -> None:
    task = make_task(source, backups, keep=2)
    for day in range(4):
        await task.run(NOW + timedelta(days=day))
    assert len(names(backups)) == 2


async def test_задача_вызывает_send_document_с_путём_копии(source: Path, backups: Path) -> None:
    sent: list[Path] = []

    async def send(path: Path) -> None:
        sent.append(path)

    task = make_task(source, backups, send_document=send)
    await task.run(NOW)
    await task.run(NOW)
    assert sent == [backups / "bot-2026-10-23.db"]


async def test_сбой_отправки_документа_не_ломает_задачу(
    source: Path, backups: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def send(path: Path) -> None:
        raise RuntimeError("чат недоступен")

    task = make_task(source, backups, send_document=send)
    with caplog.at_level(logging.ERROR):
        assert await task.run(NOW) == 1
    assert (backups / "bot-2026-10-23.db").exists()
    assert "не отправлена" in caplog.text
    # отправку не повторяют каждую минуту
    assert await task.run(NOW + timedelta(minutes=1)) == 0


async def test_без_send_document_задача_просто_делает_копию(source: Path, backups: Path) -> None:
    assert await make_task(source, backups, send_document=None).run(NOW) == 1


async def test_ошибка_бэкапа_пробрасывается_и_включает_паузу(tmp_path: Path, backups: Path) -> None:
    task = make_task(tmp_path / "нет.db", backups)
    with pytest.raises(FileNotFoundError):
        await task.run(NOW)
    # в течение паузы диск не долбят и ошибку не повторяют
    assert await task.run(NOW + timedelta(minutes=1)) == 0
    assert await task.run(NOW + RETRY_AFTER_FAILURE - timedelta(seconds=1)) == 0


async def test_после_паузы_бэкап_пробуется_снова(tmp_path: Path, backups: Path) -> None:
    task = make_task(tmp_path / "нет.db", backups)
    with pytest.raises(FileNotFoundError):
        await task.run(NOW)
    with pytest.raises(FileNotFoundError):
        await task.run(NOW + RETRY_AFTER_FAILURE)


async def test_после_починки_бэкап_успешен_и_пауза_сброшена(
    tmp_path: Path, source: Path, backups: Path
) -> None:
    missing = tmp_path / "later.db"
    task = make_task(missing, backups)
    with pytest.raises(FileNotFoundError):
        await task.run(NOW)
    source.rename(missing)
    later = NOW + RETRY_AFTER_FAILURE
    assert await task.run(later) == 1
    assert await task.run(later + timedelta(days=1)) == 1


async def test_часовой_пояс_читается_из_провайдера_при_каждом_запуске(
    source: Path, backups: Path
) -> None:
    zone = {"value": "UTC"}
    task = BackupTask(source, backups, timezone=lambda: zone["value"], at=time(4, 0))
    moment = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)  # 05:00 UTC, но 02:00 в Монтевидео
    assert await task.run(moment) == 1
    zone["value"] = TZ
    # В Монтевидео уже 02:00 следующих суток — до 04:00 копию не делаем
    assert await task.run(datetime(2026, 10, 24, 5, 0, tzinfo=UTC)) == 0


async def test_имя_задачи() -> None:
    assert BackupTask.name == "backup"


# ---- в составе контейнера -------------------------------------------------------------


async def test_планировщик_делает_бэкап_живой_базы(app: App) -> None:
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert (first["backup"], second["backup"]) == (1, 0)
    path = app.tmp_path / "backups" / "bot-2026-10-23.db"
    connection = sqlite3.connect(path)
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert {"events", "registrations", "settings", "audit_log"} <= tables
        assert connection.execute("SELECT count(*) FROM admins").fetchone() == (2,)
    finally:
        connection.close()


async def test_планировщик_отправляет_копию_в_служебный_чат(
    tmp_path: Path, clock: FakeClock
) -> None:
    sent: list[Path] = []

    async def send(path: Path) -> None:
        sent.append(path)

    application = await make_app(tmp_path, clock, send_backup=send)
    try:
        await application.services.scheduler.tick()
        await application.services.scheduler.tick()
    finally:
        await application.engine.dispose()
    assert sent == [tmp_path / "backups" / "bot-2026-10-23.db"]


async def test_бэкап_берёт_часовой_пояс_из_настроек(app: App) -> None:
    # 06:00 UTC: в Монтевидео (03:00) бэкап ещё рано, а после смены пояса на UTC уже пора
    app.clock.advance(timedelta(hours=-6))
    assert (await app.services.scheduler.tick())["backup"] == 0
    await app.services.settings.set("timezone", "UTC", 1)
    assert (await app.services.scheduler.tick())["backup"] == 1
    assert os.path.exists(app.tmp_path / "backups" / "bot-2026-10-23.db")
