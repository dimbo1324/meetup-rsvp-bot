"""Пути бота (решение R2): всё от каталога бота; вне него ничего не создаётся."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy.engine import make_url

from rcbot.config.paths import Paths
from tests.conftest import BOT_DIR


def test_производные_пути_лежат_строго_внутри_каталога_бота(tmp_path: Path) -> None:
    home = tmp_path / "bot"
    p = Paths(home)
    assert p.env_file == home / ".env"
    assert p.alembic_ini == home / "alembic.ini"
    assert p.data == home / "data"
    assert p.database == home / "data" / "rcbot.sqlite3"
    assert p.lock_file == home / "data" / "rcbot.lock"
    assert p.discovery == home / "data" / "discovery"
    assert p.quarantine == home / "data" / "quarantine"
    assert p.shots == home / "data" / "shots"
    assert p.tmp == home / "data" / "tmp"
    assert p.logs == home / "logs"
    assert p.log_file == home / "logs" / "rcbot.log"
    assert p.browsers == home / ".browsers"
    assert p.cache == home / ".cache"
    for derived in (p.env_file, p.database, p.lock_file, p.log_file, p.browsers, p.cache, p.tmp):
        assert derived.is_relative_to(home)


def test_database_url_с_прямыми_слешами_и_разбирается_sqlalchemy(tmp_path: Path) -> None:
    p = Paths(tmp_path / "каталог с пробелом")
    url = p.database_url()
    assert url.startswith("sqlite+aiosqlite:///")
    assert "\\" not in url
    assert url.endswith("/data/rcbot.sqlite3")
    parsed = make_url(url)
    assert parsed.drivername == "sqlite+aiosqlite"
    assert Path(str(parsed.database)) == p.database


def test_ensure_создаёт_все_каталоги_данных_но_не_лишнее(tmp_path: Path) -> None:
    home = tmp_path / "ещё" / "не" / "создан"
    p = Paths(home)
    p.ensure()
    for directory in (p.data, p.discovery, p.quarantine, p.shots, p.tmp, p.logs):
        assert directory.is_dir()
    assert not p.browsers.exists()
    assert not p.cache.exists()
    assert not p.database.exists()


def test_ensure_идемпотентен_и_не_трогает_существующие_файлы(tmp_path: Path) -> None:
    p = Paths(tmp_path)
    p.ensure()
    keep = p.quarantine / "dump.json"
    keep.write_text("{}", encoding="utf-8")
    p.ensure()
    p.ensure()
    assert keep.read_text(encoding="utf-8") == "{}"


def test_default_берёт_каталог_из_rcbot_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCBOT_HOME", str(tmp_path))
    assert Paths.default().home == tmp_path.resolve()


def test_default_превращает_относительный_rcbot_home_в_абсолютный(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RCBOT_HOME", "относительный")
    home = Paths.default().home
    assert home.is_absolute()
    assert home == (tmp_path / "относительный").resolve()


@pytest.mark.parametrize("empty", [None, ""])
def test_default_без_rcbot_home_это_каталог_бота(
    monkeypatch: pytest.MonkeyPatch, empty: str | None
) -> None:
    if empty is None:
        monkeypatch.delenv("RCBOT_HOME")
    else:
        monkeypatch.setenv("RCBOT_HOME", empty)
    home = Paths.default().home
    assert home == BOT_DIR
    assert (home / "pyproject.toml").is_file()


def test_apply_process_env_задаёт_каталоги_браузера_и_кеша(tmp_path: Path) -> None:
    p = Paths(tmp_path)
    with mock.patch.dict(os.environ):
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        os.environ.pop("PYTHONPYCACHEPREFIX", None)
        p.apply_process_env()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(p.browsers)
        assert os.environ["PYTHONPYCACHEPREFIX"] == str(p.cache / "pycache")


def test_apply_process_env_не_перекрывает_заданное_заранее(tmp_path: Path) -> None:
    p = Paths(tmp_path)
    with mock.patch.dict(
        os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "общий-браузер", "PYTHONPYCACHEPREFIX": "мой-кеш"}
    ):
        p.apply_process_env()
        p.apply_process_env()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "общий-браузер"
        assert os.environ["PYTHONPYCACHEPREFIX"] == "мой-кеш"


def test_paths_неизменяем_и_равенство_по_каталогу(tmp_path: Path) -> None:
    import dataclasses

    p = Paths(tmp_path)
    assert p == Paths(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.home = tmp_path / "x"  # type: ignore[misc]
