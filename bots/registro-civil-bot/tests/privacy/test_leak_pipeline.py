"""Сквозная проверка утечек (SPEC 0.3 п. 6, 2.6, 7; инвариант 6): «канарейки» через весь бот.

Сценарий (`scenario.py`) прогоняет вход с ошибкой, ответы сайта с эхом секретов, карантин,
рассылку по каналам, журнал на рабочем столе и БД. Затем читается каждый файл и каждое
сообщение канала: канарейки там быть не должно. Тест с отключённым фильтром обязан падать,
иначе он ничего не проверяет.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcbot.application.messages import CALL_TEXT_RU
from rcbot.infrastructure.logging.redaction import Redactor
from tests.fakes import LocalTlsServer
from tests.privacy.helpers import Canaries, find_in_tree, sqlite_text
from tests.privacy.scenario import Artifacts, run_scenario

C = Canaries()
# Владельцу в Telegram номер заявки и ссылка с номером этапа разрешены (SPEC 2.4); текст причины
# сбоя формирует код бота (решение владельца проекта), поэтому секреты из чужих текстов ошибок
# в Telegram-тексты отдельно не проверяются, в отличие от ntfy, звонка, журнала и БД


@pytest.fixture
async def art(
    tmp_path: Path, paths: object, tls_server: LocalTlsServer, monkeypatch: pytest.MonkeyPatch
) -> Artifacts:
    return await run_scenario(tmp_path, paths, tls_server, monkeypatch)  # type: ignore[arg-type]


def _leaks(root: Path, values: list[str]) -> list[str]:
    return find_in_tree(root, values)


def test_сценарий_прошёл_все_ветки_и_оставил_следы(art: Artifacts) -> None:
    """Без этого «чисто» могло бы значить «ничего не произошло»."""
    assert "backoff" in art.states
    assert "reauth" in art.states or "running" in art.states
    assert len(list(art.paths.quarantine.glob("*.json"))) == 2
    assert art.paths.log_file.stat().st_size > 0
    journal = art.journal_dir
    assert list((journal / "checks").glob("*.jsonl"))
    assert list((journal / "events").glob("*.jsonl"))
    assert list((journal / "logs").glob("*.log"))
    status = json.loads((journal / "status.json").read_text(encoding="utf-8"))
    assert status["last_error"]
    assert art.database_files()
    assert art.ntfy
    assert art.call
    assert len(art.api) == 3
    assert any(C.tramite in text for text in art.owner_texts)


def test_лог_в_каталоге_бота_без_канареек(art: Artifacts) -> None:
    assert _leaks(art.paths.logs, C.all_values()) == []


def test_журнал_на_рабочем_столе_без_канареек(art: Artifacts) -> None:
    assert _leaks(art.journal_dir, C.all_values()) == []


def test_карантин_без_канареек_и_без_заголовков(art: Artifacts) -> None:
    files = sorted(art.paths.quarantine.glob("*.json"))
    assert _leaks(art.paths.quarantine, C.all_values()) == []
    for file in files:
        record = json.loads(file.read_text(encoding="utf-8"))
        assert set(record) == {"at", "source", "status", "content_type", "body"}


def test_status_json_без_канареек_но_с_диагностикой(art: Artifacts) -> None:
    text = (art.journal_dir / "status.json").read_text(encoding="utf-8")
    assert [v for v in C.all_values() if v in text] == []
    status = json.loads(text)
    assert status["state"]
    assert "вход не удался" in status["last_error"] or status["last_error"]


def test_весь_каталог_бота_и_tmp_кроме_бд_без_канареек(art: Artifacts) -> None:
    found = [
        f
        for f in find_in_tree(art.root, C.all_values())
        if "sqlite3" not in f and "bot-home\\.cache" not in f
    ]
    assert found == []


SECRETS_ONLY = [C.doc_number, C.password, C.telegram_token, C.ntfy_topic, C.call_user]


def _db_leaks(art: Artifacts, values: list[str]) -> list[str]:
    text = sqlite_text(art.paths.database)
    in_rows = [v[:12] for v in values if v in text]
    in_bytes = [
        f for f in find_in_tree(art.paths.data, values) if "sqlite3" in f and "quarantine" not in f
    ]
    return in_rows + in_bytes


def test_бд_не_содержит_секретов_из_текстов_ошибок(art: Artifacts) -> None:
    assert _db_leaks(art, SECRETS_ONLY) == []


def test_бд_не_хранит_номер_этапа_и_заявки(art: Artifacts) -> None:
    assert _db_leaks(art, [C.etapa, C.tramite]) == []


def test_бд_не_содержит_cookie_сессии(art: Artifacts) -> None:
    assert C.cookie_value not in sqlite_text(art.paths.database)


def test_ntfy_получает_только_даты_и_общую_фразу(art: Artifacts) -> None:
    assert art.ntfy
    for request in art.ntfy:
        payload = json.loads(request.body)
        assert payload["topic"] == C.ntfy_topic  # адрес доставки — единственное разрешённое
        visible = {k: v for k, v in payload.items() if k not in ("topic", "click")}
        text = json.dumps(visible, ensure_ascii=False)
        assert [v for v in C.all_values() if v in text] == []
        assert set(payload) <= {"topic", "title", "message", "priority", "tags", "click"}
        assert "Свободные даты" in payload["message"] or "тест" in payload["message"].lower()
        assert payload["priority"] == 5
        lowered = {k.lower() for k in request.headers}
        assert not lowered & {"cookie", "authorization", "set-cookie"}


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T8: ntfy click = SPEC 2.4, а в нём номер этапа на стороннем сервисе; "
    "открытый вопрос Q-M4 (решает владелец)",
)
def test_ntfy_click_не_содержит_номера_этапа(art: Artifacts) -> None:
    for request in art.ntfy:
        assert C.etapa not in json.dumps(json.loads(request.body), ensure_ascii=False)


def test_ntfy_click_сейчас_равен_ссылке_из_spec_и_только_она(art: Artifacts) -> None:
    """Фиксирует текущее поведение Q-M4: утечка ограничена одной ссылкой на шаг «Agenda»."""
    clicks = {json.loads(r.body).get("click") for r in art.ntfy}
    assert clicks == {f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{C.etapa}/1"}


def test_звонок_callmebot_содержит_только_общую_фразу_и_адресата(art: Artifacts) -> None:
    assert art.call
    for request in art.call:
        assert set(request.query) == {"user", "text", "lang", "rpt"}
        assert request.query["user"] == C.call_user  # без него звонок невозможен
        assert request.query["text"] in (
            CALL_TEXT_RU,
            "Это проверка бота Регистро Сивиль. Мест нет, ничего делать не нужно.",
        )
        rest = {k: v for k, v in request.query.items() if k != "user"}
        assert [v for v in C.all_values() if v in json.dumps(rest, ensure_ascii=False)] == []
        assert len(request.query["text"]) <= 256
        lowered = {k.lower() for k in request.headers}
        assert not lowered & {"cookie", "authorization"}


def test_владельцу_в_telegram_уходят_номер_заявки_и_ссылка(art: Artifacts) -> None:
    joined = "\n".join(art.owner_texts)
    assert C.tramite in joined
    assert f"/etapas/ejecutar/{C.etapa}/1" in joined
    assert C.cookie_value not in joined


def test_запросы_к_сайту_не_несут_лишних_данных(art: Artifacts) -> None:
    for request in art.api:
        assert request.method == "GET"
        assert not request.body
        text = json.dumps(request.headers) + json.dumps(request.query)
        assert [
            v for v in (C.doc_number, C.password, C.telegram_token, C.ntfy_topic) if v in text
        ] == []


# --- отрицательный контроль ---


async def test_контроль_без_фильтра_канарейки_видны_и_тесты_выше_упали_бы(
    tmp_path: Path,
    paths: object,
    tls_server: LocalTlsServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если отключить редактор, канарейки обязаны появиться в логе, журнале и карантине."""
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    art = await run_scenario(tmp_path, paths, tls_server, monkeypatch)  # type: ignore[arg-type]
    assert _leaks(art.paths.logs, C.all_values()), "лог чист без фильтра: проверка слепа"
    assert _leaks(art.journal_dir, C.all_values()), "журнал чист без фильтра: проверка слепа"
    assert _leaks(art.paths.quarantine, C.all_values()), "карантин чист без фильтра: проверка слепа"
    status = (art.journal_dir / "status.json").read_text(encoding="utf-8")
    assert any(v in status for v in C.all_values()), "status.json чист без фильтра"
