"""Квота журнала (R6): больше max — удаляются самые старые дни до trim; сегодняшнее не трогаем."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.infrastructure.journal.files import FileJournal
from tests.fakes import FakeClock, ManualClock

TZ = ZoneInfo("America/Montevideo")
# Малые пороги вместо гигабайт; README (~1 КБ) всегда лежит в папке, поэтому пороги выше него
MAX, TRIM = 5000, 3000
TODAY = "2026-11-12"  # FakeClock по умолчанию: 12:00 12 ноября в Монтевидео


def make(root: Path, clock: FakeClock | ManualClock | None = None) -> FileJournal:
    return FileJournal(root, TZ, clock or FakeClock(), max_bytes=MAX, trim_to_bytes=TRIM)


def put(root: Path, rel: str, size: int) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def dated(kind: str, day: str) -> str:
    folder, ext = {
        "checks": ("checks", "jsonl"),
        "events": ("events", "jsonl"),
        "logs": ("logs", "log"),
    }[kind]
    prefix = "rcbot" if kind == "logs" else kind
    return f"{folder}/{prefix}-{day}.{ext}"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "journal"


def readme_size(root: Path) -> int:
    return (root / "README.txt").stat().st_size


def fill_to(root: Path, journal: FileJournal, total: int, files: list[tuple[str, int]]) -> None:
    """Создаёт файлы, затем добавляет `openings.csv` так, чтобы размер папки стал ровно `total`."""
    for rel, size in files:
        put(root, rel, size)
    missing = total - journal.size()
    assert missing >= 0
    put(root, "openings.csv", missing)
    assert journal.size() == total


# --- граница max ---


def test_ровно_на_границе_max_ничего_не_удаляется(root: Path) -> None:
    journal = make(root)
    old = [(dated("checks", f"2026-11-{n:02d}"), 350) for n in range(1, 11)]
    fill_to(root, journal, MAX, old)
    assert journal.enforce_quota() == (0, 0)
    assert all((root / rel).exists() for rel, _ in old)


def test_на_один_байт_больше_max_удаляется_до_целевого_размера(root: Path) -> None:
    journal = make(root)
    old = [(dated("checks", f"2026-11-{n:02d}"), 350) for n in range(1, 11)]
    fill_to(root, journal, MAX + 1, old)
    # Нужно освободить 2001 байт: пять файлов (1750) мало, шесть (2100) достаточно
    assert journal.enforce_quota() == (6, 2100)
    assert journal.size() == MAX + 1 - 2100 <= TRIM


def test_меньше_max_ничего_не_удаляется_и_возвращает_нули(root: Path) -> None:
    journal = make(root)
    put(root, dated("checks", "2026-11-01"), 10)
    assert journal.enforce_quota() == (0, 0)


def test_пустая_папка_журнала(root: Path) -> None:
    journal = make(root)
    (root / "README.txt").unlink()
    assert journal.size() == 0
    assert journal.enforce_quota() == (0, 0)


# --- порядок и точность удаления ---


def test_удаляет_самые_старые_дни_первыми_и_останавливается_как_только_размер_не_больше_цели(
    root: Path,
) -> None:
    journal = make(root)
    days = [f"2026-11-{d:02d}" for d in range(1, 9)]  # 8 дней по 500 байт
    for day in days:
        put(root, dated("checks", day), 500)
    total = journal.size()
    assert total > MAX
    removed, freed = journal.enforce_quota()
    survivors = [day for day in days if (root / dated("checks", day)).exists()]
    # Удалены ровно самые старые, остались самые новые: ни одного лишнего
    assert survivors == days[removed:]
    assert journal.size() == total - freed <= TRIM
    assert journal.size() + 500 > TRIM  # ещё один файл удалять было не нужно


def test_порядок_по_дате_в_имени_а_не_по_времени_изменения_файла(root: Path) -> None:
    journal = make(root)
    names = {f"{n:02d}": put(root, dated("checks", f"2026-11-{n:02d}"), 1000) for n in range(1, 6)}
    # Порядок по mtime обратный порядку дат: самый старый по имени «тронут» последним
    now = time.time()
    for rank, day in enumerate(("05", "04", "03", "02", "01")):
        os.utime(names[day], (now - 1000 + rank * 100, now - 1000 + rank * 100))
    journal.enforce_quota()
    assert [d for d, p in names.items() if p.exists()] == ["05"]


def test_типы_файлов_checks_events_logs_удаляются_вперемешку_по_дате(root: Path) -> None:
    journal = make(root)
    plan = [
        ("logs", "2026-11-01"),
        ("events", "2026-11-02"),
        ("checks", "2026-11-03"),
        ("logs", "2026-11-04"),
        ("events", "2026-11-05"),
        ("checks", "2026-11-06"),
    ]
    for kind, day in plan:
        put(root, dated(kind, day), 1000)
    removed, _ = journal.enforce_quota()
    left = [(k, d) for k, d in plan if (root / dated(k, d)).exists()]
    assert removed == 5
    assert left == plan[removed:]  # снимается префикс по дате, тип не важен


def test_одна_дата_разные_типы_удаляются_оба_до_следующей_даты(root: Path) -> None:
    journal = make(root)
    for kind in ("checks", "events", "logs"):
        put(root, dated(kind, "2026-11-01"), 1500)
    next_day = put(root, dated("checks", "2026-11-02"), 1500)
    journal.enforce_quota()
    assert not any((root / dated(k, "2026-11-01")).exists() for k in ("checks", "events", "logs"))
    assert next_day.exists()  # цель достигнута раньше: следующая дата не тронута


# --- что не удаляется никогда ---


def test_сегодняшние_файлы_не_удаляются_даже_если_одни_они_превышают_квоту(
    root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    journal = make(root)
    todays = [put(root, dated(k, TODAY), 1600) for k in ("checks", "events", "logs")]
    old = put(root, dated("checks", "2026-11-01"), 100)
    with caplog.at_level("WARNING"):
        removed = journal.enforce_quota()
    assert removed == (1, 100) and not old.exists()
    assert all(p.exists() for p in todays)
    assert any("старых дней не осталось" in r.getMessage() for r in caplog.records)


def test_файлы_будущих_дат_тоже_не_удаляются(root: Path) -> None:
    journal = make(root)
    future = put(root, dated("checks", "2026-11-13"), 5000)  # часы ноутбука могли откатиться
    assert journal.enforce_quota() == (0, 0)
    assert future.exists()


def test_служебные_файлы_не_удаляются_как_бы_велики_ни_были(root: Path) -> None:
    journal = make(root)
    service = [
        put(root, "openings.csv", 3000),
        put(root, "status.json", 1000),
    ]
    readme = root / "README.txt"
    old = put(root, dated("checks", "2026-10-01"), 50)
    journal.enforce_quota()
    assert not old.exists()
    assert all(p.exists() for p in service) and readme.exists()


def test_файлы_с_нестандартными_именами_не_удаляются(root: Path) -> None:
    journal = make(root)
    odd = [
        put(root, "checks/checks-2026-1-1.jsonl", 1200),  # не ГГГГ-ММ-ДД
        put(root, "checks/checks-2026-11-01.jsonl.bak", 1200),
        put(root, "notes-2026-11-01.txt", 1200),  # другое расширение
        put(root, "checks/readme.log", 1200),
        put(root, "checks/checks-20261101.jsonl", 1200),
    ]
    assert journal.enforce_quota() == (0, 0)
    assert all(p.exists() for p in odd)


def test_вложенные_каталоги_не_обходятся_файлы_в_них_не_считаются_и_не_удаляются(
    root: Path,
) -> None:
    """SEC-2: квота смотрит только на сами подпапки журнала, без рекурсии."""
    journal = make(root)
    nested = put(root, "checks/archive/2026/checks-2026-10-01.jsonl", 5000)
    assert journal.size() == readme_size(root)
    assert journal.enforce_quota() == (0, 0)
    assert nested.exists()


def test_журнал_на_рабочем_столе_чужие_файлы_с_похожими_именами_не_трогаются(
    tmp_path: Path,
) -> None:
    """SEC-2: папку журнала указали как Рабочий стол — там лежат чужие файлы.

    Ни в корне, ни в чужих подпапках, ни с чужим префиксом в своих подпапках они не считаются
    в размер и не удаляются, даже когда собственные файлы журнала превышают квоту.
    """
    desktop = tmp_path / "Desktop"
    journal = make(desktop)
    foreign = [
        put(desktop, "checks-2026-11-01.jsonl", 4000),  # имя как у журнала, но вне подпапки
        put(desktop, "rcbot-2026-11-01.log", 4000),
        put(desktop, "photos/events-2026-11-01.jsonl", 4000),  # чужая подпапка
        put(desktop, "checks/events-2026-11-01.jsonl", 4000),  # чужой префикс в своей папке
        put(desktop, "logs/backup-2026-11-01.log", 4000),
        put(desktop, "Отчёт-2026-11-01.log", 4000),
    ]
    assert journal.size() == readme_size(desktop)
    own = put(desktop, dated("checks", "2026-11-02"), 6000)
    assert journal.size() == readme_size(desktop) + 6000
    assert journal.enforce_quota() == (1, 6000)
    assert not own.exists()
    assert all(p.exists() and p.stat().st_size == 4000 for p in foreign)


def test_файл_открытый_писателем_сегодня_защищён_даже_если_сутки_сменились_без_записи(
    root: Path,
) -> None:
    """После полуночи писатель ещё держит вчерашний файл (новой записи не было): удалять его
    нельзя — в Windows это и невозможно, а в других системах запись ушла бы в «призрак»."""
    clock = ManualClock(start=datetime(2026, 11, 12, 15, 0, tzinfo=UTC))
    journal = make(root, clock)
    journal.event("x", "y")
    held = root / dated("events", TODAY)
    held.write_bytes(b"x" * 6000)
    clock._now = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # прошли сутки без записи
    journal.enforce_quota()
    assert held.exists()


async def test_граница_суток_квоты_это_полночь_монтевидео_а_не_utc(root: Path) -> None:
    clock = ManualClock(start=datetime(2026, 11, 13, 2, 59, 59, tzinfo=UTC))  # 23:59:59 12-го
    journal = make(root, clock)
    day12 = put(root, dated("checks", "2026-11-12"), 6000)
    assert journal.enforce_quota() == (0, 0) and day12.exists()  # 12-е ещё «сегодня»
    await clock.advance(1)  # 03:00:00 UTC = 00:00:00 13-го в Монтевидео
    assert journal.enforce_quota() == (1, 6000)
    assert not day12.exists()


# --- сбои файловой системы ---


def test_файл_который_не_удалось_удалить_пропускается_и_не_засчитывается(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = make(root)
    stuck = put(root, dated("checks", "2026-11-01"), 1500)  # самый старый — удалить не дают
    for n in (2, 3, 4):
        put(root, dated("checks", f"2026-11-0{n}"), 1500)
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == stuck:
            raise PermissionError("файл открыт в Excel")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    removed, freed = journal.enforce_quota()
    assert stuck.exists()
    assert (removed, freed) == (3, 4500)  # удалось три остальных; застрявший не засчитан
    assert journal.size() <= TRIM


def test_размер_пропускает_файлы_пропавшие_во_время_подсчёта(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = make(root)
    gone = put(root, "checks/checks-2026-11-01.jsonl", 500)
    kept = put(root, "events/events-2026-11-01.jsonl", 70)
    real_stat = Path.stat

    def stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self == gone:
            raise FileNotFoundError(str(self))
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", stat)
    assert journal.size() == readme_size(root) + kept.stat().st_size


def test_размер_пяти_тысяч_файлов_считается_быстро(root: Path) -> None:
    make(root)
    folder = root / "checks"
    folder.mkdir(exist_ok=True)
    start = date(2000, 1, 1)
    for n in range(5000):
        (folder / f"checks-{start + timedelta(days=n)}.jsonl").write_bytes(b"x" * 10)
    journal = make(root)
    started = time.perf_counter()
    total = journal.size()
    elapsed = time.perf_counter() - started
    assert total >= 50_000
    assert elapsed < 2.0


# --- свойство на всё множество входов ---

day_numbers = st.integers(min_value=1, max_value=11)  # дни ноября до сегодняшнего 12-го
file_specs = st.lists(
    st.tuples(st.sampled_from(["checks", "events", "logs"]), day_numbers, st.integers(1, 1500)),
    min_size=0,
    max_size=25,
    unique_by=lambda spec: (spec[0], spec[1]),
)


@given(file_specs, st.integers(0, 3000), st.integers(0, 800))
def test_свойство_квоты(
    specs: list[tuple[str, int, int]], today_size: int, trim_extra: int
) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "journal"
        journal = make(root)
        for kind, day, size in specs:
            put(root, dated(kind, f"2026-11-{day:02d}"), size)
        today = put(root, dated("checks", TODAY), today_size)
        service = put(root, "status.json", trim_extra)
        before = journal.size()
        removed, freed = journal.enforce_quota()

        assert today.exists() and service.exists() and (root / "README.txt").exists()
        assert journal.size() == before - freed
        if before <= MAX:
            assert (removed, freed) == (0, 0)
            return
        survivors = sorted(
            (day, kind)
            for kind, day, _ in specs
            if (root / dated(kind, f"2026-11-{day:02d}")).exists()
        )
        gone = sorted(
            (day, kind)
            for kind, day, _ in specs
            if not (root / dated(kind, f"2026-11-{day:02d}")).exists()
        )
        assert len(gone) == removed
        # Удалённое всегда старше (по дате в имени) уцелевшего или той же даты
        if gone and survivors:
            assert max(day for day, _ in gone) <= min(day for day, _ in survivors)
        # Либо цель достигнута, либо удалены все старые файлы
        assert journal.size() <= TRIM or not survivors
        # Лишнего не удалено: без последнего удалённого файла размер был бы больше цели
        if gone:
            last_size = next(s for k, d, s in specs if (d, k) == gone[-1])
            assert journal.size() + last_size > TRIM


def test_файл_текстового_лога_открытый_обработчиком_защищён_после_смены_суток(root: Path) -> None:
    clock = ManualClock(start=datetime(2026, 11, 12, 15, 0, tzinfo=UTC))
    journal = make(root, clock)
    handler = journal.attach_log_handler()
    record = logging.LogRecord("rcbot.t", logging.INFO, __file__, 1, "строка", None, None)
    record.created = clock.now().timestamp()
    handler.handle(record)
    held = root / dated("logs", TODAY)
    held.write_bytes(b"x" * 6000)
    clock._now = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # сутки прошли, записей не было
    journal.enforce_quota()
    assert held.exists()
