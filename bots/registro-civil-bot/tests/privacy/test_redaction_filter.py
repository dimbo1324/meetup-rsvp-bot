"""`RedactionFilter` и настройка логов: секрета не видит ни один обработчик (SPEC 2.6, 2.7).

Проверяется то, что делает приложение: настоящий `setup_logging`, настоящий журнал на рабочем
столе, исключения с цепочкой причин. Читается содержимое файлов, а не вызовы функций.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rcbot.infrastructure.journal.files import FileJournal
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR, RedactionFilter, Redactor
from rcbot.infrastructure.logging.setup import setup_logging
from tests.fakes import FakeClock

SECRET = "Zx9-pAssw0rd-Canary"
log = logging.getLogger("rcbot.test.privacy")


def _record(msg: str, *args: object, exc_info: BaseException | None = None) -> logging.LogRecord:
    info = (type(exc_info), exc_info, exc_info.__traceback__) if exc_info else None
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args or None, info)


@pytest.fixture
def redactor() -> Redactor:
    instance = Redactor()
    instance.register(SECRET)
    return instance


def test_сообщение_и_аргументы_форматируются_до_вырезания(redactor: Redactor) -> None:
    """Секрет, собранный из шаблона и аргумента («pass» + «word…»), тоже вырезается."""
    redactor.register("passwordXYZ12")
    record = _record("значение: pass%s и %s", "wordXYZ12", SECRET)
    assert RedactionFilter(redactor).filter(record) is True
    assert record.getMessage() == f"значение: {MASK} и {MASK}"
    assert record.args is None
    assert isinstance(record.msg, str)


def test_повторный_вызов_фильтра_на_той_же_записи_безопасен(redactor: Redactor) -> None:
    """Запись одна на все обработчики: каждый запускает свой фильтр по уже готовой записи."""
    record = _record("a %s", SECRET)
    flt = RedactionFilter(redactor)
    flt.filter(record)
    first = record.getMessage()
    flt.filter(record)
    assert record.getMessage() == first == f"a {MASK}"


def test_неверные_аргументы_формата_не_роняют_вызывающий_код(redactor: Redactor) -> None:
    for record in (_record("%s %s", SECRET), _record("без аргументов", SECRET, 1)):
        assert RedactionFilter(redactor).filter(record) is True
        assert SECRET not in record.getMessage()


def test_нестроковое_сообщение_и_dict_аргументы(redactor: Redactor) -> None:
    record = logging.LogRecord("t", 20, __file__, 1, ValueError(SECRET), None, None)
    RedactionFilter(redactor).filter(record)
    assert record.getMessage() == f"{MASK}" or SECRET not in record.getMessage()
    record = logging.LogRecord("t", 20, __file__, 1, "%(k)s", ({"k": SECRET},), None)
    RedactionFilter(redactor).filter(record)
    assert SECRET not in record.getMessage()


def _chain() -> Exception:
    """OSError(секрет) → RuntimeError (причина) → ValueError (неявный контекст) с заметкой."""
    try:
        try:
            raise OSError(f"корневая причина: {SECRET}")
        except OSError as root:
            raise RuntimeError("обёртка без секрета") from root
    except RuntimeError:
        try:
            raise ValueError("внешняя ошибка") from None
        except ValueError as outer:
            outer.__suppress_context__ = False
            outer.add_note(f"заметка: {SECRET}")
            return outer


def test_трассировка_с_цепочкой_причин_контекстом_и_заметками_вырезается(
    redactor: Redactor,
) -> None:
    error = _chain()
    record = _record("сбой", exc_info=error)
    RedactionFilter(redactor).filter(record)
    assert record.exc_info is None
    assert record.exc_text is not None
    assert SECRET not in record.exc_text
    assert "корневая причина" in record.exc_text  # диагностика сохранена
    assert "RuntimeError" in record.exc_text


def test_группа_исключений_вырезается(redactor: Redactor) -> None:
    group = ExceptionGroup("группа", [OSError(SECRET), ValueError(f"второй {SECRET}")])
    record = _record("сбой", exc_info=group)
    RedactionFilter(redactor).filter(record)
    assert record.exc_text is not None
    assert SECRET not in record.exc_text


def test_stack_info_вырезается(redactor: Redactor) -> None:
    record = _record("стек")
    record.stack_info = f"Stack (most recent call last):\n  x = '{SECRET}'"
    RedactionFilter(redactor).filter(record)
    assert record.stack_info is not None
    assert SECRET not in record.stack_info


def test_закешированный_exc_text_без_exc_info_тоже_вырезается(redactor: Redactor) -> None:
    """Обработчик без фильтра мог отформатировать запись раньше и оставить сырой текст."""
    record = _record("сбой")
    record.exc_text = f"Traceback...\nValueError: {SECRET}"
    RedactionFilter(redactor).filter(record)
    assert record.exc_text is not None
    assert SECRET not in record.exc_text


def test_setup_logging_фильтр_стоит_на_каждом_обработчике_и_файл_чист(tmp_path: Path) -> None:
    REDACTOR.register(SECRET)
    log_file = tmp_path / "logs" / "rcbot.log"
    setup_logging(log_file, level="DEBUG", console=True)
    root = logging.getLogger()
    assert root.handlers
    for handler in root.handlers:
        assert any(isinstance(f, RedactionFilter) for f in handler.filters), handler

    log.info("пароль %s принят", SECRET)
    try:
        raise RuntimeError(f"падение: {SECRET}")
    except RuntimeError:
        log.exception("шаг входа")
    for handler in root.handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "шаг входа" in text
    assert "RuntimeError" in text


def test_шумные_библиотеки_не_имеют_своих_обработчиков_и_идут_через_корень(tmp_path: Path) -> None:
    """Обработчик на логгере библиотеки обошёл бы фильтр корневых обработчиков."""
    setup_logging(tmp_path / "x.log", level="DEBUG", console=False)
    for name in ("aiohttp", "aiogram", "aiogram_dialog", "sqlalchemy", "alembic", "playwright"):
        lib = logging.getLogger(name)
        assert lib.handlers == []
        assert lib.propagate is True


def test_журнал_на_рабочем_столе_пишет_лог_через_тот_же_фильтр(tmp_path: Path) -> None:
    REDACTOR.register(SECRET)
    journal = FileJournal(tmp_path / "journal", ZoneInfo("UTC"), FakeClock(), 10**9, 10**8)
    handler = journal.attach_log_handler()
    assert any(isinstance(f, RedactionFilter) for f in handler.filters)
    log.warning("ошибка для %s", SECRET)
    try:
        raise ValueError(SECRET)
    except ValueError:
        log.exception("сбой")
    journal.close()
    logs = list((tmp_path / "journal" / "logs").glob("*.log"))
    assert logs
    assert all(SECRET not in p.read_text(encoding="utf-8") for p in logs)


def test_обработчик_с_потоком_видит_только_вырезанное(redactor: Redactor) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter(redactor))
    logger = logging.getLogger("rcbot.test.stream")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("ключ=%s", SECRET)
    finally:
        logger.removeHandler(handler)
    assert SECRET not in stream.getvalue()
