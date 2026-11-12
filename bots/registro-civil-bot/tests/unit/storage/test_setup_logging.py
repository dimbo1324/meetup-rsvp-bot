"""setup_logging: ротация 5 × 2 МБ, консоль при наличии, фильтр секретов на всём (SPEC 2.7)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from rcbot.infrastructure.logging import setup as logging_setup
from rcbot.infrastructure.logging.redaction import REDACTOR, RedactionFilter
from rcbot.infrastructure.logging.setup import BACKUPS, MAX_BYTES, setup_logging

NOISY = ("aiohttp", "aiogram", "aiogram_dialog", "sqlalchemy", "alembic", "asyncio", "playwright")


@pytest.fixture(autouse=True)
def restore_named_levels() -> Iterator[None]:
    saved = {name: logging.getLogger(name).level for name in NOISY}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "rcbot.log"


def file_handlers() -> list[RotatingFileHandler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def console_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]


def test_параметры_ротации_пять_копий_по_два_мегабайта() -> None:
    assert (MAX_BYTES, BACKUPS) == (2 * 1024 * 1024, 5)


def test_создаёт_папку_и_файл_с_ротацией_по_спецификации(log_file: Path) -> None:
    setup_logging(log_file)
    (handler,) = file_handlers()
    assert (handler.maxBytes, handler.backupCount) == (2 * 1024 * 1024, 5)
    assert handler.encoding == "utf-8"
    logging.getLogger("rcbot.t").info("привет")
    handler.flush()
    assert "привет" in log_file.read_text(encoding="utf-8")


def test_ротация_хранит_ровно_пять_копий(log_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 300)
    setup_logging(log_file)
    for n in range(80):
        logging.getLogger("rcbot.t").info("строка %03d %s", n, "я" * 60)
    names = sorted(p.name for p in log_file.parent.iterdir())
    assert names == ["rcbot.log"] + [f"rcbot.log.{n}" for n in range(1, 6)]
    # Самая новая запись — в текущем файле, самая старая из ротации потеряна, а не «зависла»
    assert "строка 079" in log_file.read_text(encoding="utf-8")
    assert all(p.stat().st_size <= 600 for p in log_file.parent.iterdir())


def test_консоль_подключается_если_есть_stderr(log_file: Path) -> None:
    setup_logging(log_file, console=True)
    assert len(console_handlers()) == 1 and len(file_handlers()) == 1


def test_без_stderr_pythonw_консоли_нет_и_запись_не_падает(
    log_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stderr", None)
    setup_logging(log_file, console=True)
    assert console_handlers() == []
    logging.getLogger("rcbot.t").error("в файл")
    assert "в файл" in log_file.read_text(encoding="utf-8")


def test_console_false_отключает_консоль_даже_при_stderr(log_file: Path) -> None:
    setup_logging(log_file, console=False)
    assert console_handlers() == []
    assert len(file_handlers()) == 1


def test_повторный_вызов_не_дублирует_обработчики_и_строки(log_file: Path) -> None:
    for _ in range(3):
        setup_logging(log_file)
    assert len(file_handlers()) == 1 and len(console_handlers()) == 1
    logging.getLogger("rcbot.t").info("однажды")
    file_handlers()[0].flush()
    assert log_file.read_text(encoding="utf-8").count("однажды") == 1


def test_повторный_вызов_закрывает_прежний_файловый_обработчик(log_file: Path) -> None:
    """Прежний обработчик держал бы файл: в Windows это ломает ротацию (два дескриптора)."""
    setup_logging(log_file)
    (first,) = file_handlers()
    setup_logging(log_file)
    assert first.stream is None  # закрыт


def test_фильтр_секретов_висит_на_каждом_обработчике(log_file: Path) -> None:
    setup_logging(log_file)
    for handler in logging.getLogger().handlers:
        assert any(isinstance(f, RedactionFilter) for f in handler.filters), handler


def test_секрет_не_попадает_ни_в_файл_ни_в_консоль(
    log_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    REDACTOR.register("hunter2hunter2")
    setup_logging(log_file)
    try:
        raise ValueError("упало на hunter2hunter2")
    except ValueError:
        logging.getLogger("rcbot.t").error("пароль hunter2hunter2", exc_info=True)
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hunter2hunter2" not in log_file.read_text(encoding="utf-8")
    assert "hunter2hunter2" not in capsys.readouterr().err


def test_шумные_библиотеки_приглушены_до_warning(log_file: Path) -> None:
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.NOTSET)
    setup_logging(log_file, level="DEBUG")
    for name in NOISY:
        assert logging.getLogger(name).level == logging.WARNING, name
    logging.getLogger("aiohttp.client").info("заголовки Cookie: ...")
    logging.getLogger("rcbot.t").debug("своя подробность")
    file_handlers()[0].flush()
    text = log_file.read_text(encoding="utf-8")
    assert "заголовки" not in text and "своя подробность" in text


@pytest.mark.parametrize(("level", "expected"), [("DEBUG", 10), ("INFO", 20), ("WARNING", 30)])
def test_уровень_корневого_логгера(log_file: Path, level: str, expected: int) -> None:
    setup_logging(log_file, level=level)
    assert logging.getLogger().level == expected


def test_по_умолчанию_info_отсекает_debug(log_file: Path) -> None:
    setup_logging(log_file)
    logging.getLogger("rcbot.t").debug("тихо")
    logging.getLogger("rcbot.t").info("слышно")
    file_handlers()[0].flush()
    text = log_file.read_text(encoding="utf-8")
    assert "слышно" in text and "тихо" not in text


def test_формат_строки_время_уровень_имя_сообщение(log_file: Path) -> None:
    setup_logging(log_file)
    logging.getLogger("rcbot.fmt").warning("текст")
    file_handlers()[0].flush()
    (line,) = log_file.read_text(encoding="utf-8").splitlines()
    assert line.endswith("WARNING rcbot.fmt: текст")
