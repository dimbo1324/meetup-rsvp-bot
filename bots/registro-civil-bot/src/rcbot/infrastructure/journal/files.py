"""Журнал наблюдения в папке на рабочем столе (решение R6).

Раскладка:
    status.json                  текущее состояние, обновляется во время работы
    checks/checks-ГГГГ-ММ-ДД.jsonl   каждая проверка календаря
    events/events-ГГГГ-ММ-ДД.jsonl   каждое действие и событие бота
    logs/rcbot-ГГГГ-ММ-ДД.log        подробный текстовый лог
    openings.csv                 появления, изменения и пропажи мест (статистика окон)

Файлы по дням не переименовываются: их можно открывать, пока бот работает. Всё, что пишется,
проходит через тот же редактор, что и логи: секретов, cookies и номеров заявки здесь нет.
Квота: больше `max_bytes` → удаляются самые старые дневные файлы, пока размер не станет не
больше `trim_to_bytes`; сегодняшние файлы, `status.json` и `openings.csv` не удаляются.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import stat
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo

from rcbot.application.ports import CheckRecord, Clock, OpeningRecord
from rcbot.domain.models import Slot
from rcbot.infrastructure.logging.redaction import REDACTOR, RedactionFilter

logger = logging.getLogger(__name__)

DATED = re.compile(r"-(\d{4}-\d{2}-\d{2})\.(jsonl|log)$")
OPENINGS_HEADER = ("местное_время", "utc", "событие", "дат", "даты", "источник", "окно_открыто_мин")
README = """Журнал наблюдения rcbot (registro-civil-bot).

status.json     — текущее состояние бота; поле alive_at обновляется каждые 30 секунд.
                  Если alive_at старше нескольких минут — бот не работает или ноутбук спал.
checks/         — каждая проверка календаря, по файлу в день (JSON Lines).
events/         — каждое действие и событие бота, по файлу в день (JSON Lines).
logs/           — подробный текстовый лог, по файлу в день.
openings.csv    — когда появлялись, менялись и пропадали места (открывается в Excel).

Папка больше {max_gb:g} ГБ → бот сам удаляет самые старые дни, пока не останется {trim_gb:g} ГБ.
Секретов, cookies, номера документа и номера заявки здесь нет.
"""


class _DailyWriter:
    """Дописывает строки в файл текущего дня; при смене дня открывает новый файл."""

    def __init__(self, directory: Path, prefix: str, suffix: str, tz: ZoneInfo) -> None:
        self._directory = directory
        self._prefix = prefix
        self._suffix = suffix
        self._tz = tz
        self._day: date | None = None
        self._file: IO[str] | None = None
        self._lock = threading.Lock()

    def path_for(self, day: date) -> Path:
        return self._directory / f"{self._prefix}-{day.isoformat()}.{self._suffix}"

    def write(self, line: str, at: datetime) -> None:
        day = at.astimezone(self._tz).date()
        with self._lock:
            if day != self._day or self._file is None:
                self._close()
                self._directory.mkdir(parents=True, exist_ok=True)
                # errors="replace": одиночный суррогат в тексте не должен ронять запись
                self._file = self.path_for(day).open(
                    "a", encoding="utf-8", errors="replace", newline="\n"
                )
                self._day = day
            self._file.write(line if line.endswith("\n") else line + "\n")
            self._file.flush()

    def today(self) -> Path | None:
        return None if self._day is None else self.path_for(self._day)

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def close(self) -> None:
        with self._lock:
            self._close()


class DailyLogHandler(logging.Handler):
    """Подробный текстовый лог по дням в папке журнала (все действия бота)."""

    def __init__(self, directory: Path, tz: ZoneInfo) -> None:
        super().__init__(level=logging.DEBUG)
        self._tz = tz
        self._writer = _DailyWriter(directory, "rcbot", "log", tz)
        self.addFilter(RedactionFilter(REDACTOR))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            at = datetime.fromtimestamp(record.created, tz=self._tz)
            self._writer.write(self.format(record), at)
        except OSError:
            self.handleError(record)

    def today(self) -> Path | None:
        return self._writer.today()

    def close(self) -> None:
        self._writer.close()
        super().close()


def _scrub(value: object) -> object:
    """Вырезает секреты в каждом значении по отдельности, а не в готовом JSON-тексте.

    Редактор вырезает «до конца строки» (`Cookie: …`, `password=…`): по готовому JSON он съедал
    закрывающие кавычки и скобки, и запись ломалась. Числа проверяются как текст: длинный номер
    (chat_id) заменяется на `***`.
    """
    if isinstance(value, str):
        return REDACTOR.redact(value)
    if isinstance(value, dict):
        return {REDACTOR.redact(str(key)): _scrub(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(item) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        text = str(value)
        redacted = REDACTOR.redact(text)
        return value if redacted == text else redacted
    return REDACTOR.redact(str(value))


def _slots(slots: frozenset[Slot]) -> list[str]:
    return sorted(slot.label() for slot in slots)


class FileJournal:
    def __init__(
        self, root: Path, tz: ZoneInfo, clock: Clock, max_bytes: int, trim_to_bytes: int
    ) -> None:
        self.root = root
        self._tz = tz
        self._clock = clock
        self._max = max_bytes
        self._target = trim_to_bytes
        self._checks = _DailyWriter(root / "checks", "checks", "jsonl", tz)
        self._events = _DailyWriter(root / "events", "events", "jsonl", tz)
        self._openings_lock = threading.Lock()
        self.log_handler: DailyLogHandler | None = None
        root.mkdir(parents=True, exist_ok=True)
        readme = root / "README.txt"
        if not readme.exists():
            text = README.format(max_gb=max_bytes / 2**30, trim_gb=trim_to_bytes / 2**30)
            readme.write_text(text, encoding="utf-8-sig")

    def attach_log_handler(self, level: int = logging.DEBUG) -> DailyLogHandler:
        handler = DailyLogHandler(self.root / "logs", self._tz)
        handler.setLevel(level)
        formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        # Файл дня выбирается по поясу журнала: время в строках обязано быть в том же поясе,
        # а не в поясе машины (иначе строка 23:50 могла бы лежать в файле следующего дня)
        formatter.converter = lambda seconds: datetime.fromtimestamp(
            seconds or 0.0, self._tz
        ).timetuple()
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)
        self.log_handler = handler
        return handler

    def _local(self, at: datetime) -> str:
        return at.astimezone(self._tz).isoformat(timespec="seconds")

    def check(self, record: CheckRecord) -> None:
        line = {
            "at": self._local(record.at),
            "ok": record.ok,
            "source": record.source.value,
            "state": record.state,
            "slots": _slots(record.slots),
            "slots_count": len(record.slots),
            "latency_ms": round(record.latency_ms, 1),
            "interval_sec": round(record.interval_sec, 1),
        }
        if record.error_kind:
            line["error_kind"] = record.error_kind
            line["error"] = REDACTOR.redact(record.error)
        self._safe(self._checks, line, record.at)

    def event(self, code: str, message: str, level: str = "INFO", **data: object) -> None:
        at = self._clock.now()
        line: dict[str, object] = {
            "at": self._local(at),
            "level": level,
            "code": code,
            "message": REDACTOR.redact(message),
        }
        if data:
            line["data"] = _scrub(data)
        self._safe(self._events, line, at)

    def opening(self, record: OpeningRecord) -> None:
        days = sorted({s.day.isoformat() for s in record.slots})
        row = (
            self._local(record.at),
            record.at.astimezone(UTC).isoformat(timespec="seconds"),
            record.kind,
            len(days),
            " ".join(days),
            record.source.value,
            "" if record.incident_open_sec is None else f"{record.incident_open_sec / 60:.1f}",
        )
        path = self.root / "openings.csv"
        with self._openings_lock:
            try:
                new = not path.exists() or path.stat().st_size == 0
                buffer = io.StringIO()
                writer = csv.writer(buffer, delimiter=";")
                if new:
                    writer.writerow(OPENINGS_HEADER)
                writer.writerow(row)
                # utf-8-sig: Excel открывает русские заголовки без ручного выбора кодировки
                encoding = "utf-8-sig" if new else "utf-8"
                with path.open("a", encoding=encoding, errors="replace", newline="") as f:
                    f.write(buffer.getvalue())
            except OSError as error:
                logger.warning("журнал: openings.csv не записан: %s", type(error).__name__)

    def status(self, snapshot: dict[str, object]) -> None:
        path = self.root / "status.json"
        tmp = self.root / "status.json.tmp"
        try:
            text = json.dumps(_scrub(snapshot), ensure_ascii=False, indent=2)
            tmp.write_text(text, encoding="utf-8", errors="replace")
            os.replace(tmp, path)
        except OSError:
            # Файл открыт в редакторе — не страшно, запишем при следующем обновлении
            tmp.unlink(missing_ok=True)

    def _safe(self, writer: _DailyWriter, line: dict[str, object], at: datetime) -> None:
        try:
            writer.write(json.dumps(line, ensure_ascii=False), at)
        except OSError as error:
            logger.warning("журнал: запись не удалась: %s", type(error).__name__)

    # --- размер и квота ---

    def _files(self) -> list[tuple[Path, int]]:
        """Все файлы папки с размерами; один `stat` на файл (в Windows это самая дорогая часть)."""
        found = []
        for path in self.root.rglob("*"):
            try:
                info = path.stat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append((path, info.st_size))
        return found

    def size(self) -> int:
        return sum(size for _, size in self._files())

    def enforce_quota(self) -> tuple[int, int]:
        """Больше квоты → удаляются самые старые дни до целевого размера. (файлов, байт)."""
        files = self._files()
        total = sum(size for _, size in files)
        if total <= self._max:
            return 0, 0
        protected = {p for p in (self._checks.today(), self._events.today()) if p is not None}
        if self.log_handler is not None and (today := self.log_handler.today()) is not None:
            protected.add(today)
        today_iso = self._clock.now().astimezone(self._tz).date().isoformat()
        dated = []
        for path, size in files:
            match = DATED.search(path.name)
            if match and path not in protected and match.group(1) < today_iso:
                dated.append((match.group(1), path, size))
        removed_files = removed_bytes = 0
        for _, path, size in sorted(dated):
            if total <= self._target:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed_files += 1
            removed_bytes += size
        if total > self._target:
            logger.warning(
                "журнал: после очистки %.1f ГБ — больше цели: старых дней не осталось",
                total / 2**30,
            )
        return removed_files, removed_bytes

    def close(self) -> None:
        self._checks.close()
        self._events.close()
        if self.log_handler is not None:
            logging.getLogger().removeHandler(self.log_handler)
            self.log_handler.close()
            self.log_handler = None
