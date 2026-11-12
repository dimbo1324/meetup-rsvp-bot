"""DailyLogHandler и подключение текстового лога к журналу наблюдения."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rcbot.infrastructure.journal.files import DailyLogHandler, FileJournal
from rcbot.infrastructure.logging.redaction import REDACTOR
from tests.fakes import FakeClock

TZ = ZoneInfo("America/Montevideo")
MIDNIGHT = datetime(2026, 11, 13, 3, 0, 0, tzinfo=UTC)  # 00:00 13 ноября в Монтевидео


@pytest.fixture
def journal(tmp_path: Path) -> FileJournal:
    return FileJournal(tmp_path / "journal", TZ, FakeClock(), max_bytes=10**6, trim_to_bytes=10**5)


def record_at(
    moment: datetime, message: str = "сообщение", level: int = logging.INFO
) -> logging.LogRecord:
    record = logging.LogRecord("rcbot.test", level, __file__, 1, message, None, None)
    record.created = moment.timestamp()
    return record


def log_text(journal: FileJournal, day: str) -> str:
    return (journal.root / "logs" / f"rcbot-{day}.log").read_text(encoding="utf-8")


def test_запись_лога_идёт_в_файл_дня_по_местному_времени(journal: FileJournal) -> None:
    handler = journal.attach_log_handler()
    handler.handle(record_at(MIDNIGHT - timedelta(seconds=1), "до полуночи"))
    handler.handle(record_at(MIDNIGHT, "после полуночи"))
    assert "до полуночи" in log_text(journal, "2026-11-12")
    assert "после полуночи" not in log_text(journal, "2026-11-12")
    assert "после полуночи" in log_text(journal, "2026-11-13")


def test_формат_строки_лога_время_уровень_имя_сообщение(journal: FileJournal) -> None:
    handler = journal.attach_log_handler()
    handler.handle(
        record_at(datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC), "привет", logging.WARNING)
    )
    (line,) = log_text(journal, "2026-11-12").splitlines()
    assert line.endswith("WARNING rcbot.test: привет")
    assert line.startswith("2026-11-12 12:00:00,")


def test_время_в_строке_лога_местное_даже_если_пояс_машины_другой(
    journal: FileJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Машина «в UTC»: без явного пояса в строке было бы 15:00, а файл выбран по Монтевидео
    monkeypatch.setattr(logging.Formatter, "converter", time.gmtime)
    handler = journal.attach_log_handler()
    handler.handle(record_at(datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC)))
    assert log_text(journal, "2026-11-12").startswith("2026-11-12 12:00:00,")


def test_секреты_и_трассировки_в_логе_журнала_вырезаются(journal: FileJournal) -> None:
    REDACTOR.register("hunter2hunter2")
    handler = journal.attach_log_handler()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    try:
        raise RuntimeError("пароль hunter2hunter2 и почта me@example.test")
    except RuntimeError:
        logging.getLogger("rcbot.auth").error("вход не удался hunter2hunter2", exc_info=True)
    handler.flush()
    text = (next((journal.root / "logs").iterdir())).read_text(encoding="utf-8")
    assert "hunter2hunter2" not in text and "me@example.test" not in text
    assert "RuntimeError" in text and "***" in text


def test_уровень_обработчика_отсекает_подробности(journal: FileJournal) -> None:
    journal.attach_log_handler(level=logging.WARNING)
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("rcbot.t").info("подробность")
    logging.getLogger("rcbot.t").error("важное")
    text = next((journal.root / "logs").iterdir()).read_text(encoding="utf-8")
    assert "важное" in text and "подробность" not in text


def test_по_умолчанию_пишется_всё_включая_debug(journal: FileJournal) -> None:
    journal.attach_log_handler()
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("rcbot.t").debug("мелочь")
    assert "мелочь" in next((journal.root / "logs").iterdir()).read_text(encoding="utf-8")


def test_до_первой_записи_файла_дня_нет(journal: FileJournal) -> None:
    handler = journal.attach_log_handler()
    assert handler.today() is None
    handler.handle(record_at(datetime(2026, 11, 12, 15, 0, tzinfo=UTC)))
    assert handler.today() == journal.root / "logs" / "rcbot-2026-11-12.log"


def test_подключение_кладёт_обработчик_в_корневой_логгер_и_в_журнал(journal: FileJournal) -> None:
    handler = journal.attach_log_handler()
    assert handler in logging.getLogger().handlers
    assert journal.log_handler is handler


def test_close_журнала_снимает_обработчик_и_освобождает_файл(journal: FileJournal) -> None:
    handler = journal.attach_log_handler()
    handler.handle(record_at(datetime(2026, 11, 12, 15, 0, tzinfo=UTC)))
    path = journal.root / "logs" / "rcbot-2026-11-12.log"
    journal.close()
    assert handler not in logging.getLogger().handlers
    assert journal.log_handler is None
    path.unlink()  # в Windows открытый файл удалить нельзя
    journal.close()  # повторное закрытие безопасно


def test_после_close_обработчик_не_получает_новых_записей(journal: FileJournal) -> None:
    journal.attach_log_handler()
    journal.close()
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("rcbot.t").error("после закрытия")
    assert not list((journal.root / "logs").glob("*.log"))


def test_ошибка_диска_в_обработчике_не_бросает_в_вызывающий_код(
    journal: FileJournal, capsys: pytest.CaptureFixture[str]
) -> None:
    (journal.root / "logs").write_text("не папка", encoding="utf-8")
    handler = journal.attach_log_handler()
    handler.handle(record_at(datetime(2026, 11, 12, 15, 0, tzinfo=UTC), "не запишется"))
    assert "не запишется" not in capsys.readouterr().out


def test_суррогат_в_сообщении_лога_не_бросает(journal: FileJournal) -> None:
    handler = journal.attach_log_handler()
    handler.handle(record_at(datetime(2026, 11, 12, 15, 0, tzinfo=UTC), "байт \udcff"))
    assert "байт" in log_text(journal, "2026-11-12")


def test_обработчик_сам_по_себе_несёт_фильтр_секретов(tmp_path: Path) -> None:
    REDACTOR.register("hunter2hunter2")
    handler = DailyLogHandler(tmp_path / "logs", TZ)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.handle(record_at(datetime(2026, 11, 12, 15, 0, tzinfo=UTC), "пароль hunter2hunter2"))
    handler.close()
    text = (tmp_path / "logs" / "rcbot-2026-11-12.log").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in text and "***" in text
