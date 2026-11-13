"""Карантин неожиданных ответов (SPEC 2.3, 2.7): очистка, усечение, не больше 20 файлов."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.fakes import T0

from rcbot.infrastructure.gubuy.quarantine import MAX_BODY_CHARS, MAX_FILES, quarantine
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR

JSON = "application/json"
AGENDA = "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1"
LOGIN = "https://mi.iduruguay.gub.uy/login"


def _files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_снимок_json_содержит_только_статус_тип_и_тело(tmp_path: Path) -> None:
    path = quarantine(tmp_path / "q", T0, "api", 200, JSON, '{"dias": "ответ"}')
    record = _load(path)
    assert record == {
        "at": T0.isoformat(),
        "source": "api",
        "status": 200,
        "content_type": JSON,
        "body": '{"dias": "ответ"}',
    }


def test_снимок_страницы_вместо_тела_хранит_длину_хеш_и_вид(tmp_path: Path) -> None:
    html = "<html>ответ</html>"
    record = _load(quarantine(tmp_path, T0, "api", 200, "text/html", html))
    assert record == {
        "at": T0.isoformat(),
        "source": "api",
        "status": 200,
        "content_type": "text/html",
        "body": {
            "omitted": True,
            "chars": len(html),
            "sha256": hashlib.sha256(html.encode()).hexdigest(),
            "page": "unknown",
        },
    }


# Имя и документ владельца редактор не знает: защищает только то, что страница не пишется
OWNER_PAGE = (
    "<html><h2>Paso 2 de 3</h2><div>Titular: Ana Prueba Testova, pasaporte TEST000000</div>"
    "<p>Seleccionar día</p></html>"
)


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "text/html; charset=utf-8",
        "TEXT/PLAIN",
        "",
        "application/octet-stream",
        "application/xhtml+xml",
        JSON,  # разметка под типом JSON — тоже страница
    ],
)
def test_страница_с_именем_владельца_не_попадает_на_диск(tmp_path: Path, content_type: str) -> None:
    path = quarantine(tmp_path, T0, "api", 200, content_type, OWNER_PAGE, url=AGENDA)
    raw = path.read_text(encoding="utf-8")
    for leaked in ("Ana", "Prueba", "Testova", "TEST000000", "Titular", "<html"):
        assert leaked not in raw
    body = _load(path)["body"]
    assert isinstance(body, dict)
    assert body["omitted"] is True
    assert body["chars"] == len(OWNER_PAGE)
    assert body["page"] == "agenda"


def test_вид_страницы_в_карантине_по_адресу_и_статусу(tmp_path: Path) -> None:
    login = _load(quarantine(tmp_path, T0, "api", 200, "text/html", "<p>x</p>", url=LOGIN))
    blocked = _load(quarantine(tmp_path, T0, "api", 429, "text/html", "<p>x</p>"))
    assert login["body"]["page"] == "login"  # type: ignore[index]
    assert blocked["body"]["page"] == "blocked"  # type: ignore[index]


@pytest.mark.parametrize(
    "content_type", ["application/javascript", "text/javascript; charset=utf-8", JSON]
)
def test_json_и_скрипт_сохраняются_через_редактор(tmp_path: Path, content_type: str) -> None:
    body = "var cupos = [1, 2]; // test.person@example.org"
    record = _load(quarantine(tmp_path, T0, "api", 200, content_type, body))
    assert record["body"] == f"var cupos = [1, 2]; // {MASK}"


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
    quarantine(target, T0, "dom", 200, JSON, "x")
    assert target.is_dir()


def test_тело_проходит_через_редактор_секретов(tmp_path: Path) -> None:
    REDACTOR.register("УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА")
    body = "привет УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА, почта test.person@example.org, simple_bpm_session=abc123"
    record = _load(quarantine(tmp_path, T0, "api", 200, JSON, body))
    text = str(record["body"])
    assert "УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА" not in text
    assert "test.person@example.org" not in text
    assert "abc123" not in text
    assert MASK in text


def test_тело_усекается_до_8000_символов(tmp_path: Path) -> None:
    assert MAX_BODY_CHARS == 8000
    record = _load(quarantine(tmp_path, T0, "api", 200, JSON, "я" * 20_000))
    assert record["body"] == "я" * 8000


def test_тело_ровно_в_границу_не_теряет_символов(tmp_path: Path) -> None:
    record = _load(quarantine(tmp_path, T0, "api", 200, JSON, "x" * 8000))
    assert len(str(record["body"])) == 8000


def test_секрет_на_границе_усечения_не_протекает_частью(tmp_path: Path) -> None:
    # Усечь тело ДО вырезания секретов нельзя: от значения, разрезанного границей, остался бы
    # префикс, который редактор уже не найдёт
    secret = "ЗНАЧЕНИЕ-СЕССИИ-ДЛЯ-ТЕСТА"
    REDACTOR.register(secret)
    body = "а" * (MAX_BODY_CHARS - 10) + secret
    text = str(_load(quarantine(tmp_path, T0, "api", 200, JSON, body))["body"])
    assert "ЗНАЧЕН" not in text
    assert len(text) <= MAX_BODY_CHARS


@pytest.mark.parametrize("cut", [1, 5, 13, 25])
def test_redact_truncated_секрет_разрезанный_границей_не_протекает(cut: int) -> None:
    secret = "ЗНАЧЕНИЕ-СЕССИИ-ДЛЯ-ТЕСТА"
    REDACTOR.register(secret)
    limit = 100
    text = "а" * (limit - cut) + secret + " хвост" * 10
    result = REDACTOR.redact_truncated(text, limit)
    assert len(result) <= limit
    assert secret[:cut] not in result  # префикс секрета на границе вырезан, а не оставлен
    assert result.startswith("а" * (limit - cut))


def test_redact_truncated_читает_не_больше_предела_с_запасом() -> None:
    from rcbot.infrastructure.logging.redaction import TRUNCATE_MARGIN

    secret = "ДАЛЬНИЙ-СЕКРЕТ-ЗА-ЗАПАСОМ"
    REDACTOR.register(secret)
    text = "б" * 10 + " " * (TRUNCATE_MARGIN + 10) + secret
    assert REDACTOR.redact_truncated(text, 10) == "б" * 10
    assert REDACTOR.redact_truncated("короткий", 100) == "короткий"


def test_хранится_не_больше_двадцати_файлов_старейший_удаляется(tmp_path: Path) -> None:
    for i in range(MAX_FILES + 5):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api", 200, JSON, f"тело-{i}")
    files = _files(tmp_path)
    assert len(files) == MAX_FILES == 20
    bodies = [_load(f)["body"] for f in files]
    assert bodies == [f"тело-{i}" for i in range(5, 25)]


def test_ровно_двадцать_файлов_ничего_не_удаляет(tmp_path: Path) -> None:
    for i in range(MAX_FILES):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api", 200, JSON, f"тело-{i}")
    assert len(_files(tmp_path)) == 20
    quarantine(tmp_path, T0 + timedelta(seconds=99), "api", 200, JSON, "двадцать первый")
    files = _files(tmp_path)
    assert len(files) == 20
    assert _load(files[0])["body"] == "тело-1"
    assert _load(files[-1])["body"] == "двадцать первый"


def test_имена_разных_долей_одной_секунды_уникальны(tmp_path: Path) -> None:
    at = datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC)
    paths = {
        quarantine(tmp_path, at + timedelta(microseconds=i), "api", 200, JSON, f"t{i}")
        for i in range(5)
    }
    assert len(paths) == 5
    assert len(_files(tmp_path)) == 5


def test_два_ответа_в_один_и_тот_же_момент_не_затирают_друг_друга(tmp_path: Path) -> None:
    # Часы тестов и очень быстрые повторы дают одинаковую метку: перезапись теряла бы улику
    first = quarantine(tmp_path, T0, "api", 200, JSON, "первый")
    second = quarantine(tmp_path, T0, "api", 200, JSON, "второй")
    assert first != second
    assert sorted(_load(f)["body"] for f in _files(tmp_path)) == ["второй", "первый"]


def test_источники_api_и_dom_в_одной_папке_считаются_вместе(tmp_path: Path) -> None:
    for i in range(MAX_FILES + 2):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api" if i % 2 else "dom", 200, JSON, "x")
    assert len(_files(tmp_path)) == MAX_FILES


def test_чужие_не_json_файлы_лимит_не_трогает(tmp_path: Path) -> None:
    keep = tmp_path / "заметка.txt"
    keep.write_text("не карантин", encoding="utf-8")
    for i in range(MAX_FILES + 3):
        quarantine(tmp_path, T0 + timedelta(seconds=i), "api", 200, JSON, "x")
    assert keep.exists()
