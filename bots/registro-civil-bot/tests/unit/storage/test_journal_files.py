"""FileJournal: формат и содержимое файлов журнала наблюдения (решение R6)."""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rcbot.application.ports import CheckRecord, OpeningRecord
from rcbot.domain.models import SourceKind
from rcbot.infrastructure.journal import files as journal_files
from rcbot.infrastructure.journal.files import FileJournal
from rcbot.infrastructure.logging.redaction import REDACTOR
from tests.fakes import T0, FakeClock, ManualClock, slots, slots_at

TZ = ZoneInfo("America/Montevideo")
GB = 2**30
# 02:59:59 UTC = 23:59:59 12 ноября в Монтевидео (UTC−3, перевода часов нет)
BEFORE_MIDNIGHT = datetime(2026, 11, 13, 2, 59, 59, tzinfo=UTC)
MIDNIGHT = datetime(2026, 11, 13, 3, 0, 0, tzinfo=UTC)


def make(root: Path, clock: FakeClock | ManualClock | None = None, **kwargs: int) -> FileJournal:
    return FileJournal(
        root,
        TZ,
        clock or FakeClock(),
        max_bytes=kwargs.get("max_bytes", 10 * GB),
        trim_to_bytes=kwargs.get("trim_to_bytes", 5 * GB),
    )


def check_record(at: datetime = T0, **overrides: object) -> CheckRecord:
    fields: dict[str, object] = {
        "at": at,
        "source": SourceKind.API,
        "ok": True,
        "slots": slots(24, 23),
        "latency_ms": 41.26,
        "state": "running",
        "interval_sec": 20.04,
    }
    fields.update(overrides)
    return CheckRecord(**fields)  # type: ignore[arg-type]


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "rcbot-journal"


# --- README ---


def test_readme_создаётся_при_создании_журнала_с_квотой_из_настроек(root: Path) -> None:
    make(root)
    text = (root / "README.txt").read_text(encoding="utf-8-sig")
    assert "больше 10 ГБ" in text and "останется 5 ГБ" in text
    assert "status.json" in text and "openings.csv" in text


def test_readme_пишется_с_bom_чтобы_блокнот_показал_кириллицу(root: Path) -> None:
    make(root)
    assert (root / "README.txt").read_bytes().startswith(b"\xef\xbb\xbf")


def test_readme_не_перезаписывается_при_повторном_создании(root: Path) -> None:
    make(root)
    (root / "README.txt").write_text("правка владельца", encoding="utf-8")
    make(root)
    assert (root / "README.txt").read_text(encoding="utf-8") == "правка владельца"


def test_квота_в_readme_берётся_из_параметров_а_не_зашита(root: Path) -> None:
    make(root, max_bytes=3 * GB // 2, trim_to_bytes=GB // 2)
    text = (root / "README.txt").read_text(encoding="utf-8-sig")
    assert "больше 1.5 ГБ" in text and "останется 0.5 ГБ" in text


def test_папка_создаётся_вместе_с_родителями(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "journal"
    make(deep)
    assert (deep / "README.txt").exists()


# --- проверки ---


def test_строка_проверки_содержит_все_поля_и_время_в_монтевидео(root: Path) -> None:
    make(root).check(check_record())
    (line,) = lines(root / "checks" / "checks-2026-11-12.jsonl")
    assert line == {
        "at": "2026-11-12T12:00:00-03:00",
        "ok": True,
        "source": "api",
        "state": "running",
        "slots": ["2027-02-23", "2027-02-24"],
        "slots_count": 2,
        "latency_ms": 41.3,
        "interval_sec": 20.0,
    }


def test_слоты_со_временем_пишутся_отсортированными_метками(root: Path) -> None:
    record = check_record(slots=slots_at((24, 9, 0), (23, 10, 45), (23, 10, 30)))
    make(root).check(record)
    (line,) = lines(root / "checks" / "checks-2026-11-12.jsonl")
    assert line["slots"] == ["2027-02-23 10:30", "2027-02-23 10:45", "2027-02-24 09:00"]
    assert line["slots_count"] == 3


def test_проверка_без_мест_пишет_пустой_список(root: Path) -> None:
    make(root).check(check_record(slots=frozenset()))
    (line,) = lines(root / "checks" / "checks-2026-11-12.jsonl")
    assert line["slots"] == [] and line["slots_count"] == 0


def test_поля_ошибки_есть_только_у_неудачной_проверки(root: Path) -> None:
    journal = make(root)
    journal.check(check_record())
    journal.check(check_record(ok=False, error_kind="RateLimited", error="HTTP 429"))
    ok_line, bad_line = lines(root / "checks" / "checks-2026-11-12.jsonl")
    assert "error_kind" not in ok_line and "error" not in ok_line
    assert (bad_line["ok"], bad_line["error_kind"], bad_line["error"]) == (
        False,
        "RateLimited",
        "HTTP 429",
    )


def test_ошибка_проверки_проходит_через_редактор_секретов(root: Path) -> None:
    REDACTOR.register("superSecretValue")
    record = check_record(
        ok=False,
        error_kind="Unexpected",
        error="Cookie: simple_bpm_session=abc; пароль superSecretValue, user@example.test",
    )
    make(root).check(record)
    raw = (root / "checks" / "checks-2026-11-12.jsonl").read_text(encoding="utf-8")
    assert "superSecretValue" not in raw and "abc" not in raw and "user@example.test" not in raw
    assert "***" in raw
    json.loads(raw)  # и строка осталась корректным JSON


def test_кириллица_пишется_как_есть_а_не_экранами(root: Path) -> None:
    make(root).check(check_record(state="ожидание"))
    raw = (root / "checks" / "checks-2026-11-12.jsonl").read_text(encoding="utf-8")
    assert "ожидание" in raw and "\\u" not in raw


def test_каждая_проверка_отдельная_строка_с_переводом_строки_unix(root: Path) -> None:
    journal = make(root)
    for n in range(3):
        journal.check(check_record(at=T0 + timedelta(seconds=20 * n)))
    raw = (root / "checks" / "checks-2026-11-12.jsonl").read_bytes()
    assert raw.count(b"\n") == 3 and b"\r" not in raw


def test_время_проверки_в_любом_поясе_приводится_к_монтевидео(root: Path) -> None:
    plus_nine = datetime(2026, 11, 13, 0, 0, 0, tzinfo=timezone(timedelta(hours=9)))  # 15:00 UTC
    make(root).check(check_record(at=plus_nine))
    (line,) = lines(root / "checks" / "checks-2026-11-12.jsonl")
    assert line["at"] == "2026-11-12T12:00:00-03:00"


# --- файлы по дням: границей служат сутки Монтевидео, а не UTC ---


@pytest.mark.parametrize(
    ("moment", "day"),
    [
        (datetime(2026, 11, 12, 3, 0, 0, tzinfo=UTC), "2026-11-12"),  # 00:00:00 местных — уже 12-е
        (datetime(2026, 11, 12, 2, 59, 59, tzinfo=UTC), "2026-11-11"),  # 23:59:59 11-го
        (BEFORE_MIDNIGHT, "2026-11-12"),
        (MIDNIGHT, "2026-11-13"),
        (datetime(2026, 11, 12, 0, 0, 0, tzinfo=UTC), "2026-11-11"),  # полночь UTC — вчера
        (datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC), "2026-11-12"),  # поздний вечер UTC
    ],
)
def test_день_файла_определяется_по_местному_времени(
    root: Path, moment: datetime, day: str
) -> None:
    make(root).check(check_record(at=moment))
    assert [p.name for p in (root / "checks").iterdir()] == [f"checks-{day}.jsonl"]


async def test_события_переходят_в_новый_файл_ровно_в_полночь_монтевидео(root: Path) -> None:
    clock = ManualClock(start=BEFORE_MIDNIGHT - timedelta(seconds=1))
    journal = make(root, clock)
    journal.event("a", "до полуночи")
    await clock.advance(1)  # 02:59:59 UTC
    journal.event("b", "за секунду до")
    await clock.advance(1)  # 03:00:00 UTC
    journal.event("c", "после полуночи")
    old = lines(root / "events" / "events-2026-11-12.jsonl")
    new = lines(root / "events" / "events-2026-11-13.jsonl")
    assert [line["code"] for line in old] == ["a", "b"]
    assert [line["code"] for line in new] == ["c"]
    assert new[0]["at"] == "2026-11-13T00:00:00-03:00"


def test_при_смене_дня_старый_файл_закрывается_и_его_можно_удалить(root: Path) -> None:
    journal = make(root)
    journal.check(check_record(at=BEFORE_MIDNIGHT))
    journal.check(check_record(at=MIDNIGHT))
    # В Windows открытый файл удалить нельзя: удаление удаётся, только если дескриптор закрыт
    (root / "checks" / "checks-2026-11-12.jsonl").unlink()
    assert (root / "checks" / "checks-2026-11-13.jsonl").exists()


def test_запись_с_более_ранним_временем_уходит_в_файл_своего_дня(root: Path) -> None:
    journal = make(root)
    journal.check(check_record(at=MIDNIGHT))
    journal.check(check_record(at=BEFORE_MIDNIGHT))  # запоздавшая запись вчерашнего дня
    journal.check(check_record(at=MIDNIGHT + timedelta(seconds=20)))
    assert len(lines(root / "checks" / "checks-2026-11-12.jsonl")) == 1
    assert len(lines(root / "checks" / "checks-2026-11-13.jsonl")) == 2


def test_новый_экземпляр_дописывает_в_файл_того_же_дня_а_не_затирает(root: Path) -> None:
    make(root).check(check_record())
    make(root).check(check_record(at=T0 + timedelta(seconds=20)))
    assert len(lines(root / "checks" / "checks-2026-11-12.jsonl")) == 2


def test_файлы_проверок_событий_и_лога_лежат_в_своих_папках(root: Path) -> None:
    journal = make(root)
    journal.check(check_record())
    journal.event("x", "y")
    assert (root / "checks" / "checks-2026-11-12.jsonl").exists()
    assert (root / "events" / "events-2026-11-12.jsonl").exists()


# --- события ---


def test_событие_содержит_время_уровень_код_и_сообщение(root: Path) -> None:
    make(root).event("poll.started", "цикл запущен", "WARNING")
    (line,) = lines(root / "events" / "events-2026-11-12.jsonl")
    assert line == {
        "at": "2026-11-12T12:00:00-03:00",
        "level": "WARNING",
        "code": "poll.started",
        "message": "цикл запущен",
    }


def test_уровень_события_по_умолчанию_info_и_данных_нет_без_аргументов(root: Path) -> None:
    make(root).event("a", "b")
    (line,) = lines(root / "events" / "events-2026-11-12.jsonl")
    assert line["level"] == "INFO" and "data" not in line


def test_данные_события_пишутся_вложенным_объектом(root: Path) -> None:
    make(root).event("alert.sent", "отправлено", channel="telegram", attempt=2, ok=True)
    (line,) = lines(root / "events" / "events-2026-11-12.jsonl")
    assert line["data"] == {"channel": "telegram", "attempt": 2, "ok": True}


def test_данные_события_нестандартных_типов_приводятся_к_строке(root: Path) -> None:
    make(root).event("x", "y", when=T0, path=Path("a/b"))
    (line,) = lines(root / "events" / "events-2026-11-12.jsonl")
    assert isinstance(line["data"], dict)
    assert line["data"]["when"] == str(T0)


def test_сообщение_события_проходит_редактор_секретов(root: Path) -> None:
    REDACTOR.register("hunter2hunter2")
    make(root).event("auth", "пароль hunter2hunter2, почта me@example.test")
    raw = (root / "events" / "events-2026-11-12.jsonl").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in raw and "me@example.test" not in raw


def test_данные_события_проходят_редактор_секретов(root: Path) -> None:
    REDACTOR.register("hunter2hunter2")
    make(root).event("auth", "вход", detail="пароль hunter2hunter2")
    raw = (root / "events" / "events-2026-11-12.jsonl").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in raw


@pytest.mark.parametrize(
    "secret_text",
    [
        "Cookie: simple_bpm_session=abc123",
        "Authorization: Bearer abcdef",
        "password=hunter2",
        "token: 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
    ],
)
def test_секрет_в_данных_события_не_ломает_json_и_не_пишется(root: Path, secret_text: str) -> None:
    """Редактор вырезает всё до конца строки: если применять его к уже готовому JSON, он
    съедает закрывающие кавычки и скобки — событие теряется, а в вызывающий код летит ошибка."""
    make(root).event("net.error", "сбой запроса", detail=secret_text, after="остаток")
    (line,) = lines(root / "events" / "events-2026-11-12.jsonl")
    data = line["data"]
    assert isinstance(data, dict)
    assert data["after"] == "остаток"
    assert "abc123" not in str(data) and "hunter2" not in str(data) and "abcdef" not in str(data)
    assert "***" in str(data["detail"])


def test_длинное_число_в_данных_события_не_пишется_открытым_как_chat_id(root: Path) -> None:
    make(root).event("tg.sent", "отправлено", chat_id=123456789012, attempt=3)
    (line,) = lines(root / "events" / "events-2026-11-12.jsonl")
    data = line["data"]
    assert isinstance(data, dict)
    assert "123456789012" not in str(data)
    assert data["attempt"] == 3


def test_вложенные_данные_события_редактируются_на_любой_глубине(root: Path) -> None:
    REDACTOR.register("hunter2hunter2")
    make(root).event("x", "y", nested={"list": ["ok", "hunter2hunter2"], "n": 1})
    raw = (root / "events" / "events-2026-11-12.jsonl").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in raw


def test_лишний_символ_суррогат_в_сообщении_не_роняет_журнал(root: Path) -> None:
    # Такие строки приходят из недекодируемых заголовков и имён; запись не должна бросать
    journal = make(root)
    journal.event("x", "плохой байт \udcff в тексте")
    journal.check(check_record(ok=False, error_kind="E", error="\udcff"))
    assert len(lines(root / "events" / "events-2026-11-12.jsonl")) == 1
    assert len(lines(root / "checks" / "checks-2026-11-12.jsonl")) == 1


# --- openings.csv ---


def opening(at: datetime = T0, kind: str = "appeared", **overrides: object) -> OpeningRecord:
    fields: dict[str, object] = {
        "at": at,
        "kind": kind,
        "slots": slots(24, 23, 23),
        "source": SourceKind.DOM,
        "incident_open_sec": None,
    }
    fields.update(overrides)
    return OpeningRecord(**fields)  # type: ignore[arg-type]


def read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


def test_openings_bom_и_заголовок_пишутся_один_раз(root: Path) -> None:
    journal = make(root)
    journal.opening(opening())
    journal.opening(opening(at=T0 + timedelta(minutes=1), kind="changed"))
    journal.opening(opening(at=T0 + timedelta(minutes=2), kind="gone"))
    raw = (root / "openings.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\xef\xbb\xbf") == 1
    rows = read_csv(root / "openings.csv")
    assert rows[0] == list(journal_files.OPENINGS_HEADER)
    assert [row[2] for row in rows[1:]] == ["appeared", "changed", "gone"]
    assert sum(1 for row in rows if row[0] == "местное_время") == 1


def test_openings_строка_содержит_время_даты_источник_и_длительность(root: Path) -> None:
    make(root).opening(opening(kind="gone", incident_open_sec=754.0))
    (_, row) = read_csv(root / "openings.csv")
    assert row == [
        "2026-11-12T12:00:00-03:00",
        "2026-11-12T15:00:00+00:00",
        "gone",
        "2",  # число различных дней: 23 и 24 февраля
        "2027-02-23 2027-02-24",
        "dom",
        "12.6",  # 754 с = 12,57 мин
    ]


def test_openings_окно_пусто_если_длительность_неизвестна_и_ноль_если_ноль(root: Path) -> None:
    journal = make(root)
    journal.opening(opening(incident_open_sec=None))
    journal.opening(opening(incident_open_sec=0.0))
    rows = read_csv(root / "openings.csv")
    assert rows[1][6] == "" and rows[2][6] == "0.0"


def test_openings_пропажа_без_слотов_пишет_ноль_дат_и_пустое_поле(root: Path) -> None:
    make(root).opening(opening(kind="gone", slots=frozenset()))
    (_, row) = read_csv(root / "openings.csv")
    assert row[3] == "0" and row[4] == ""


def test_openings_открывается_csv_читателем_с_разделителем_точка_с_запятой(root: Path) -> None:
    make(root).opening(opening())
    text = (root / "openings.csv").read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].count(";") == len(journal_files.OPENINGS_HEADER) - 1
    assert len(text.splitlines()[1].split(";")) == len(journal_files.OPENINGS_HEADER)


def test_openings_время_utc_не_зависит_от_пояса_переданного_момента(root: Path) -> None:
    local = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone(timedelta(hours=-3)))
    make(root).opening(opening(at=local))
    (_, row) = read_csv(root / "openings.csv")
    assert row[1] == "2026-11-12T15:00:00+00:00"  # колонка называется «utc»


def test_openings_пустой_файл_после_сбоя_получает_заголовок(root: Path) -> None:
    journal = make(root)
    (root / "openings.csv").write_bytes(b"")
    journal.opening(opening())
    rows = read_csv(root / "openings.csv")
    assert rows[0] == list(journal_files.OPENINGS_HEADER)


def test_openings_существующий_файл_дописывается_без_второго_bom(root: Path) -> None:
    make(root).opening(opening())
    make(root).opening(opening(at=T0 + timedelta(hours=1)))  # новый запуск бота
    raw = (root / "openings.csv").read_bytes()
    assert raw.count(b"\xef\xbb\xbf") == 1
    assert len(read_csv(root / "openings.csv")) == 3


def test_openings_ошибка_диска_не_бросает_наружу(root: Path) -> None:
    journal = make(root)
    (root / "openings.csv").mkdir()  # файл «занят»: на его месте каталог
    journal.opening(opening())


def test_openings_параллельные_записи_не_смешивают_строки(root: Path) -> None:
    journal = make(root)

    def work(n: int) -> None:
        for k in range(20):
            journal.opening(opening(at=T0 + timedelta(seconds=n * 100 + k)))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = read_csv(root / "openings.csv")
    assert len(rows) == 101
    assert all(len(row) == len(journal_files.OPENINGS_HEADER) for row in rows)


# --- status.json ---


def test_status_пишет_корректный_json_и_не_оставляет_временного_файла(root: Path) -> None:
    make(root).status({"alive_at": "2026-11-12T15:00:00+00:00", "state": "running", "n": 3})
    assert json.loads((root / "status.json").read_text(encoding="utf-8")) == {
        "alive_at": "2026-11-12T15:00:00+00:00",
        "state": "running",
        "n": 3,
    }
    assert not (root / "status.json.tmp").exists()


def test_status_кириллица_читаема_и_json_с_отступами(root: Path) -> None:
    make(root).status({"state_reason": "пауза владельца"})
    raw = (root / "status.json").read_text(encoding="utf-8")
    assert "пауза владельца" in raw and "\n  " in raw


def test_status_каждая_запись_заменяет_файл_целиком(root: Path) -> None:
    journal = make(root)
    journal.status({"a": "x" * 5000})
    journal.status({"b": 1})
    assert json.loads((root / "status.json").read_text(encoding="utf-8")) == {"b": 1}


def test_status_использует_атомарную_замену(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        # К моменту замены новое содержимое уже полностью записано во временный файл
        json.loads(Path(src).read_text(encoding="utf-8"))
        calls.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(journal_files.os, "replace", spy)
    make(root).status({"k": "v"})
    assert calls == [("status.json.tmp", "status.json")]


def test_status_секреты_вырезаются_а_json_остаётся_корректным(root: Path) -> None:
    REDACTOR.register("hunter2hunter2")
    make(root).status(
        {
            "last_error": "Cookie: simple_bpm_session=abc123",
            "other": "пароль hunter2hunter2",
            "after": "хвост",
        }
    )
    raw = (root / "status.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "abc123" not in raw and "hunter2hunter2" not in raw
    assert data["after"] == "хвост"


def test_status_ошибка_замены_не_роняет_бота_и_старый_файл_цел(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = make(root)
    journal.status({"version": 1})

    def busy(*_: object) -> None:
        raise PermissionError("файл открыт в редакторе")

    monkeypatch.setattr(journal_files.os, "replace", busy)
    journal.status({"version": 2})  # не должно бросать
    assert json.loads((root / "status.json").read_text(encoding="utf-8")) == {"version": 1}
    assert not (root / "status.json.tmp").exists()  # временный файл убран


@pytest.mark.skipif(
    sys.platform != "win32", reason="блокировка открытого файла — особенность Windows"
)
def test_status_настоящий_занятый_файл_в_windows_не_роняет_бота(root: Path) -> None:
    journal = make(root)
    journal.status({"version": 1})
    with (root / "status.json").open("rb"):  # как редактор: открыт без права удаления
        journal.status({"version": 2})
    journal.status({"version": 3})  # файл освободили — следующая запись проходит
    assert json.loads((root / "status.json").read_text(encoding="utf-8")) == {"version": 3}


def test_status_при_отсутствии_папки_не_бросает(root: Path) -> None:
    journal = make(root)
    for child in root.iterdir():
        child.unlink()
    root.rmdir()
    journal.status({"a": 1})


def test_status_читатель_не_видит_недописанный_json(root: Path) -> None:
    journal = make(root)
    journal.status({"n": 0, "pad": "слово " * 3000})
    stop = threading.Event()
    bad: list[str] = []

    def reader() -> None:
        path = root / "status.json"
        while not stop.is_set():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:  # в Windows замена на миг блокирует чтение — это допустимо
                stop.wait(0.001)  # не крутимся вхолостую: иначе писатель не получает процессор
                continue
            try:
                json.loads(text)
            except ValueError:
                bad.append(text[:60])

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for n in range(300):
            journal.status({"n": n, "pad": "слово " * 3000})
    finally:
        stop.set()
        thread.join()
    assert bad == []


def test_status_суррогат_в_тексте_не_роняет_запись(root: Path) -> None:
    make(root).status({"last_error": "битый \udcff байт"})
    json.loads((root / "status.json").read_text(encoding="utf-8"))


# --- ошибки диска ---


def test_ошибка_диска_при_записи_проверки_не_бросает_и_запись_возобновляется(root: Path) -> None:
    journal = make(root)
    blocker = root / "checks"
    blocker.write_text("не папка", encoding="utf-8")  # на месте папки лежит файл
    journal.check(check_record())  # mkdir/open упадут с OSError
    journal.event("x", "это событие пишется в другую папку")
    blocker.unlink()
    journal.check(check_record(at=T0 + timedelta(seconds=20)))  # диск «починили»
    assert len(lines(root / "checks" / "checks-2026-11-12.jsonl")) == 1
    assert len(lines(root / "events" / "events-2026-11-12.jsonl")) == 1


def test_ошибка_диска_при_записи_события_пишет_предупреждение_без_данных_события(
    root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    journal = make(root)
    (root / "events").write_text("не папка", encoding="utf-8")
    with caplog.at_level("WARNING"):
        journal.event("x", "секретное сообщение")
    assert any("запись не удалась" in r.getMessage() for r in caplog.records)
    assert "секретное сообщение" not in caplog.text


def test_запись_в_закрытый_журнал_снова_открывает_файл(root: Path) -> None:
    journal = make(root)
    journal.check(check_record())
    journal.close()
    journal.check(check_record(at=T0 + timedelta(seconds=20)))
    assert len(lines(root / "checks" / "checks-2026-11-12.jsonl")) == 2
