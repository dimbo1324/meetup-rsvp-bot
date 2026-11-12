"""Карантин неожиданных ответов (SPEC 2.3, 2.7): очистка, усечение, не больше 20 файлов."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.fakes import T0

from rcbot.infrastructure.gubuy.quarantine import MAX_BODY_CHARS, MAX_FILES, quarantine
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR


def _files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_снимок_содержит_только_статус_тип_и_тело(tmp_path: Path) -> None:
    path = quarantine(tmp_path / "q", T0, "api", 200, "text/html", "<html>ответ</html>")
    record = _load(path)
    assert record == {
        "at": T0.isoformat(),
        "source": "api",
        "status": 200,
        "content_type": "text/html",
        "body": "<html>ответ</html>",
    }


def test_заголовки_не_пишутся_вовсе(tmp_path: Path) -> None:
    # Cookie и Set-Cookie могли бы попасть в карантин только через заголовки ответа
    path = quarantine(tmp_path, T0, "api", 200, "text/html", "тело")
    text = path.read_text(encoding="utf-8").lower()
    assert set(_load(path)) == {"at", "source", "status", "content_type", "body"}
    assert "cookie" not in text
    assert "authorization" not in text


def test_каталог_создаётся_при_первой_записи(tmp_path: Path) -> None:
    target = tmp_path / "data" / "quarantine"
    assert not target.exists()
    quarantine(target, T0, "dom", 200, "", "x")
    assert target.is_dir()


def test_тело_проходит_через_редактор_секретов(tmp_path: Path) -> None:
    REDACTOR.register("УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА")
    body = "привет УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА, почта test.person@example.org, simple_bpm_session=abc123"
    record = _load(quarantine(tmp_path, T0, "api", 200, "text/html", body))
    text = str(record["body"])
    assert "УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА" not in text
    assert "test.person@example.org" not in text
    assert "abc123" not in text
    assert MASK in text


def test_тело_усекается_до_8000_символов(tmp_path: Path) -> None:
    assert MAX_BODY_CHARS == 8000
    record = _load(quarantine(tmp_path, T0, "api", 200, "text/html", "я" * 20_000))
    assert record["body"] == "я" * 8000


def test_тело_ровно_в_границу_не_теряет_символов(tmp_path: Path) -> None:
    record = _load(quarantine(tmp_path, T0, "api", 200, "text/html", "x" * 8000))
    assert len(str(record["body"])) == 8000


def test_секрет_на_границе_усечения_не_протекает_частью(tmp_path: Path) -> None:
    # Усечь тело ДО вырезания секретов нельзя: от значения, разрезанного границей, остался бы
    # префикс, который редактор уже не найдёт
    secret = "ЗНАЧЕНИЕ-СЕССИИ-ДЛЯ-ТЕСТА"
    REDACTOR.register(secret)
    body = "а" * (MAX_BODY_CHARS - 10) + secret
    text = str(_load(quarantine(tmp_path, T0, "api", 200, "text/html", body))["body"])
    assert "ЗНАЧЕН" not in text
    assert len(text) <= MAX_BODY_CHARS


def test_хранится_не_больше_двадцати_файлов_старейший_удаляется(tmp_path: Path) -> None:
    for i in range(MAX_FILES + 5):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api", 200, "", f"тело-{i}")
    files = _files(tmp_path)
    assert len(files) == MAX_FILES == 20
    bodies = [_load(f)["body"] for f in files]
    assert bodies == [f"тело-{i}" for i in range(5, 25)]


def test_ровно_двадцать_файлов_ничего_не_удаляет(tmp_path: Path) -> None:
    for i in range(MAX_FILES):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api", 200, "", f"тело-{i}")
    assert len(_files(tmp_path)) == 20
    quarantine(tmp_path, T0 + timedelta(seconds=99), "api", 200, "", "двадцать первый")
    files = _files(tmp_path)
    assert len(files) == 20
    assert _load(files[0])["body"] == "тело-1"
    assert _load(files[-1])["body"] == "двадцать первый"


def test_имена_разных_долей_одной_секунды_уникальны(tmp_path: Path) -> None:
    at = datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC)
    paths = {
        quarantine(tmp_path, at + timedelta(microseconds=i), "api", 200, "", f"t{i}")
        for i in range(5)
    }
    assert len(paths) == 5
    assert len(_files(tmp_path)) == 5


def test_два_ответа_в_один_и_тот_же_момент_не_затирают_друг_друга(tmp_path: Path) -> None:
    # Часы тестов и очень быстрые повторы дают одинаковую метку: перезапись теряла бы улику
    first = quarantine(tmp_path, T0, "api", 200, "", "первый")
    second = quarantine(tmp_path, T0, "api", 200, "", "второй")
    assert first != second
    assert sorted(_load(f)["body"] for f in _files(tmp_path)) == ["второй", "первый"]


def test_источники_api_и_dom_в_одной_папке_считаются_вместе(tmp_path: Path) -> None:
    for i in range(MAX_FILES + 2):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api" if i % 2 else "dom", 200, "", "x")
    assert len(_files(tmp_path)) == MAX_FILES


def test_чужие_не_json_файлы_лимит_не_трогает(tmp_path: Path) -> None:
    keep = tmp_path / "заметка.txt"
    keep.write_text("не карантин", encoding="utf-8")
    for i in range(MAX_FILES + 3):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api", 200, "", "x")
    assert keep.exists()
