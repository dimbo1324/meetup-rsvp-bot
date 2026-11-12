"""Фоновая запись журнала (решение R6, находка T9 BUG-T9 №6): диск не должен тормозить бота.

Подготовка строк идёт в вызывающем потоке, запись на диск — в отдельном потоке с очередью.
Тесты используют настоящие потоки и настоящие файлы; «зависший диск» — настоящая пауза в записи.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from rcbot.application.ports import CheckRecord, OpeningRecord
from rcbot.domain.models import SourceKind
from rcbot.infrastructure.journal.files import FileJournal
from tests.fakes import T0, FakeClock, slots

TZ = ZoneInfo("America/Montevideo")


def make(root: Path, **kwargs: object) -> FileJournal:
    return FileJournal(root, TZ, FakeClock(), 10**9, 5 * 10**8, background=True, **kwargs)  # type: ignore[arg-type]


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_запись_доходит_до_диска_после_flush(tmp_path: Path) -> None:
    journal = make(tmp_path / "j")
    try:
        for i in range(50):
            journal.event("x.y", f"событие {i}", n=i)
        journal.flush()
        events = lines(next((tmp_path / "j" / "events").glob("*.jsonl")))
        assert [e["data"]["n"] for e in events] == list(range(50))  # type: ignore[index]  # порядок сохранён
    finally:
        journal.close()


def test_все_виды_записей_идут_через_очередь(tmp_path: Path) -> None:
    journal = make(tmp_path / "j")
    try:
        journal.check(CheckRecord(T0, SourceKind.DOM, True, slots(23), 5.0, "running", 60.0))
        journal.opening(OpeningRecord(T0, "появились", slots(23), SourceKind.DOM))
        journal.status({"alive_at": "x", "state": "running"})
        journal.flush()
        root = tmp_path / "j"
        assert len(lines(next((root / "checks").glob("*.jsonl")))) == 1
        assert "появились" in (root / "openings.csv").read_text(encoding="utf-8-sig")
        assert json.loads((root / "status.json").read_text(encoding="utf-8"))["state"] == "running"
    finally:
        journal.close()


def test_зависший_диск_не_задерживает_вызывающего(tmp_path: Path, monkeypatch) -> None:
    journal = make(tmp_path / "j")
    release = threading.Event()
    original = FileJournal._write_line

    def slow_write(writer, text, at):
        release.wait(5)  # диск «завис»
        original(writer, text, at)

    monkeypatch.setattr(FileJournal, "_write_line", staticmethod(slow_write))
    try:
        started = time.perf_counter()
        for i in range(100):
            journal.event("x.y", f"событие {i}")
        spent = time.perf_counter() - started
        assert spent < 0.2, f"вызовы ждали диск: {spent:.3f} с"
        release.set()
        journal.flush()
        assert len(lines(next((tmp_path / "j" / "events").glob("*.jsonl")))) == 100
    finally:
        release.set()
        journal.close()


def test_переполнение_очереди_отбрасывает_и_считает_а_не_блокирует(
    tmp_path: Path, monkeypatch
) -> None:
    journal = make(tmp_path / "j", queue_limit=5)
    release = threading.Event()
    original = FileJournal._write_line

    def slow_write(writer, text, at):
        release.wait(5)
        original(writer, text, at)

    monkeypatch.setattr(FileJournal, "_write_line", staticmethod(slow_write))
    try:
        started = time.perf_counter()
        for i in range(50):
            journal.event("x.y", f"событие {i}")
        assert time.perf_counter() - started < 0.2
        assert journal.dropped >= 40  # очередь на 5 + одна запись в работе
    finally:
        release.set()
        journal.close()


def test_close_дописывает_очередь_и_останавливает_поток(tmp_path: Path) -> None:
    journal = make(tmp_path / "j")
    for i in range(30):
        journal.event("x.y", f"событие {i}")
    worker = journal._worker
    assert worker is not None and worker.is_alive()
    journal.close()  # без flush: close сам дожидается очереди
    assert len(lines(next((tmp_path / "j" / "events").glob("*.jsonl")))) == 30
    assert not worker.is_alive()
    journal.close()  # повторное закрытие безопасно


def test_ошибка_одной_записи_не_убивает_поток(tmp_path: Path, monkeypatch) -> None:
    journal = make(tmp_path / "j")
    calls: list[int] = []
    original = FileJournal._write_line

    def flaky(writer, text, at):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сбой записи")
        original(writer, text, at)

    monkeypatch.setattr(FileJournal, "_write_line", staticmethod(flaky))
    try:
        journal.event("a", "первая")
        journal.event("b", "вторая")
        journal.flush()
        recorded = lines(next((tmp_path / "j" / "events").glob("*.jsonl")))
        assert [e["code"] for e in recorded] == ["b"]
    finally:
        journal.close()


def test_время_события_берётся_в_момент_вызова_а_не_записи(tmp_path: Path) -> None:
    clock = FakeClock()
    journal = FileJournal(tmp_path / "j", TZ, clock, 10**9, 5 * 10**8, background=True)
    try:
        journal.event("x.y", "раньше")
        clock.advance(3600)
        journal.event("x.y", "позже")
        journal.flush()
        first, second = lines(next((tmp_path / "j" / "events").glob("*.jsonl")))
        assert str(first["at"]).startswith("2026-11-12T12:00:00")
        assert str(second["at"]).startswith("2026-11-12T13:00:00")
    finally:
        journal.close()


def test_подробный_лог_пишется_через_очередь_и_logging_не_ждёт_диск(tmp_path: Path) -> None:
    journal = make(tmp_path / "j")
    handler = journal.attach_log_handler(logging.INFO)
    release = threading.Event()
    original = handler.emit

    def slow_emit(record: logging.LogRecord) -> None:
        release.wait(5)
        original(record)

    handler.emit = slow_emit  # type: ignore[method-assign]
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.INFO)
    try:
        started = time.perf_counter()
        for i in range(100):
            logging.getLogger("rcbot.test").info("строка лога %d", i)
        assert time.perf_counter() - started < 0.2
        release.set()
    finally:
        journal.close()
        root.setLevel(previous)
    log = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "j" / "logs").glob("*.log"))
    assert "строка лога 0" in log and "строка лога 99" in log


def test_подробный_лог_в_фоновом_режиме_вырезает_секреты(tmp_path: Path) -> None:
    from rcbot.infrastructure.logging.redaction import REDACTOR

    REDACTOR.register("hunter2hunter2")
    journal = make(tmp_path / "j")
    journal.attach_log_handler(logging.INFO)
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.INFO)
    try:
        logging.getLogger("rcbot.test").info("пароль hunter2hunter2 в сообщении")
        try:
            raise ValueError("в исключении hunter2hunter2")
        except ValueError:
            logging.getLogger("rcbot.test").exception("сбой")
    finally:
        journal.close()
        root.setLevel(previous)
    log = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "j" / "logs").glob("*.log"))
    assert "hunter2hunter2" not in log and "в сообщении" in log


def test_квота_работает_и_в_фоновом_режиме(tmp_path: Path) -> None:
    clock = FakeClock()
    journal = FileJournal(tmp_path / "j", TZ, clock, 3000, 1500, background=True)
    try:
        (tmp_path / "j" / "checks").mkdir(parents=True)
        for day in range(1, 6):
            (tmp_path / "j" / "checks" / f"checks-2026-11-0{day}.jsonl").write_text("x" * 1000)
        journal.event("x.y", "сегодня")
        journal.flush()
        files, freed = journal.enforce_quota()
        assert files >= 3 and freed >= 3000
        assert (tmp_path / "j" / "events" / "events-2026-11-12.jsonl").exists()
    finally:
        journal.close()
