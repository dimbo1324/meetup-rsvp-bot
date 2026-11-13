"""Очистка дампа разведки до записи на диск (SPEC 2.6, 6.2): тело, заголовки, сводка."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.infrastructure.gubuy import discovery
from rcbot.infrastructure.gubuy.discovery import (
    DROP_HEADERS,
    MAX_BODY_CHARS,
    DiscoveryResult,
    _clean_headers,
    clean_body,
    clean_post_data,
    clean_url,
)
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR

JSON = "application/json; charset=utf-8"


def test_персональные_ключи_json_маскируются_на_любой_глубине() -> None:
    payload = {
        "cupos": 3,
        "solicitante": {
            "Nombre": "Тест Тестов",
            "contacto": [{"email": "a@b.example", "telefono": "099111222"}],
            "detalle": {"deep": {"deeper": {"numero_documento": "AB123456", "ok": "fecha"}}},
        },
        "lista": [[{"apellido": "Иванов"}], {"Teléfono": "1"}],
    }
    cleaned = clean_body(JSON, json.dumps(payload))
    assert cleaned["cupos"] == 3
    assert cleaned["solicitante"]["Nombre"] == MASK
    assert cleaned["solicitante"]["contacto"] == [{"email": MASK, "telefono": MASK}]
    assert cleaned["solicitante"]["detalle"]["deep"]["deeper"] == {
        "numero_documento": MASK,
        "ok": "fecha",
    }
    assert cleaned["lista"] == [[{"apellido": MASK}], {"Teléfono": MASK}]
    text = json.dumps(cleaned, ensure_ascii=False)
    for leaked in ("Тест", "a@b.example", "099111222", "AB123456", "Иванов"):
        assert leaked not in text


@pytest.mark.parametrize(
    "key",
    [
        *["nombre", "NOMBRE", "Apellido", "mail", "correo", "teléfono", "telefono", "phone"],
        *["celular", "documento", "cédula", "cedula", "pasaporte", "passport", "usuario", "user"],
        *["dirección", "address", "name", "nombre_completo", "user_id"],
    ],
)
def test_каждый_персональный_ключ_маскируется(key: str) -> None:
    assert clean_body(JSON, json.dumps({key: "значение"})) == {key: MASK}


def test_календарные_поля_не_маскируются() -> None:
    body = {"fecha": "2027-02-23", "hora": "10:30", "cupos": 2, "disponible": True, "x": None}
    assert clean_body(JSON, json.dumps(body)) == body


def test_строковые_значения_проходят_через_редактор() -> None:
    REDACTOR.register("УСЛОВНЫЙ-СЕКРЕТ")
    cleaned = clean_body(JSON, json.dumps({"nota": "см. УСЛОВНЫЙ-СЕКРЕТ и a.b@c.example"}))
    assert cleaned == {"nota": f"см. {MASK} и {MASK}"}


def test_ключи_словаря_тоже_очищаются() -> None:
    # Словарь «адрес почты → данные» не редкость; ключ-значение — тоже утечка
    cleaned = clean_body(JSON, json.dumps({"person.test@example.org": {"fecha": "2027-02-23"}}))
    assert "person.test@example.org" not in json.dumps(cleaned)


@pytest.mark.parametrize("scalar", ["123", "true", "null", '"строка"', "[1, 2]"])
def test_json_верхнего_уровня_не_словарь_разбирается(scalar: str) -> None:
    assert clean_body(JSON, scalar) == json.loads(scalar)


def test_скрипт_календаря_идёт_через_редактор_как_текст() -> None:
    cleaned = clean_body("application/javascript", "// Письмо для test.person@example.org")
    assert cleaned == f"// Письмо для {MASK}"


OWNER_PAGE = "<html><div>Titular: Ana Prueba Testova, pasaporte TEST000000</div></html>"


@pytest.mark.parametrize(
    "content_type", ["text/html", "text/plain; charset=utf-8", "", "application/xml", JSON]
)
def test_страница_не_пишется_только_длина_хеш_и_вид(content_type: str) -> None:
    url = "https://bpmgob.mec.gub.uy/autenticacion/login"
    cleaned = clean_body(content_type, OWNER_PAGE, url=url, status=200)
    assert cleaned == {
        "omitted": True,
        "chars": len(OWNER_PAGE),
        "sha256": hashlib.sha256(OWNER_PAGE.encode()).hexdigest(),
        "page": "login",
    }
    text = json.dumps(cleaned, ensure_ascii=False)
    for leaked in ("Ana", "Prueba", "Testova", "TEST000000", "Titular"):
        assert leaked not in text


async def test_страница_с_именем_владельца_не_попадает_в_файл_дампа(tmp_path: Path) -> None:
    async def headers() -> dict[str, str]:
        return {}

    async def text() -> str:
        return OWNER_PAGE

    request = SimpleNamespace(
        method="GET", resource_type="xhr", all_headers=headers, post_data=None
    )
    response = SimpleNamespace(
        request=request,
        url="https://bpmgob.mec.gub.uy/api/fragmento",
        status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        text=text,
        all_headers=headers,
    )
    recorder = discovery.DiscoveryRecorder(
        "https://l/", "https://a/", "офис", 1, tmp_path, discovery.BrowserOptions()
    )
    await recorder._collect(response)  # type: ignore[arg-type]
    from datetime import UTC, datetime

    raw = recorder._write(datetime(2026, 11, 12, tzinfo=UTC)).path.read_text(encoding="utf-8")
    for leaked in ("Ana", "Prueba", "Testova", "TEST000000", "Titular"):
        assert leaked not in raw
    assert '"omitted": true' in raw


def test_битый_json_с_json_типом_падает_в_редактор_а_не_в_исключение() -> None:
    cleaned = clean_body(JSON, '{"nombre": "x", "mail": "test.person@example.org"')
    assert isinstance(cleaned, str)
    assert "test.person@example.org" not in cleaned


def test_тип_содержимого_json_проверяется_без_учёта_регистра() -> None:
    assert clean_body("Application/JSON", '{"nombre": "x"}') == {"nombre": MASK}


def test_пустое_тело_и_пустой_тип() -> None:
    assert clean_body("", "") == ""
    assert clean_body(JSON, "") == ""


def test_тело_усекается_после_очистки_и_секрет_на_границе_не_протекает() -> None:
    secret = "ДЛИННЫЙ-УСЛОВНЫЙ-СЕКРЕТ-СЕССИИ"
    REDACTOR.register(secret)
    # Слова с пробелами: редактор на длинной строке без разделителей работает квадратично
    body = ("слово " * MAX_BODY_CHARS)[: MAX_BODY_CHARS - 12] + secret + " хвост" * 50
    cleaned = clean_body("application/javascript", body)
    assert isinstance(cleaned, str)
    assert len(cleaned) <= MAX_BODY_CHARS
    assert "ДЛИННЫЙ" not in cleaned


# --- заголовки ---


def test_заголовки_с_личными_данными_удаляются_без_учёта_регистра() -> None:
    headers = {
        "Cookie": "simple_bpm_session=abc",
        "COOKIE": "x=1",
        "Set-Cookie": "TS0191f197=zzz",
        "set-cookie": "a=b",
        "Authorization": "Bearer t",
        "authorization": "Basic x",
        "Proxy-Authorization": "Basic y",
        "PROXY-AUTHORIZATION": "Basic z",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    assert _clean_headers(headers) == {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }


def test_список_удаляемых_заголовков_полный() -> None:
    assert {"cookie", "set-cookie", "authorization", "proxy-authorization"} <= DROP_HEADERS


def test_значения_оставшихся_заголовков_проходят_через_редактор() -> None:
    REDACTOR.register("УСЛОВНЫЙ-СЕКРЕТ")
    cleaned = _clean_headers({"X-Debug": "содержит УСЛОВНЫЙ-СЕКРЕТ", "Referer": "https://h/x"})
    assert cleaned == {"X-Debug": f"содержит {MASK}", "Referer": "https://h/x"}


def test_пустые_заголовки() -> None:
    assert _clean_headers({}) == {}


@given(st.dictionaries(st.text(min_size=1, max_size=12), st.text(max_size=20), max_size=8))
def test_свойство_запрещённые_заголовки_никогда_не_остаются(headers: dict[str, str]) -> None:
    cleaned = _clean_headers(headers)
    assert not {k.lower() for k in cleaned} & DROP_HEADERS


# --- свойство для тела ---


def _no_personal_values(value: Any, personal: set[str]) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return not any(p in text for p in personal)


@given(
    st.recursive(
        st.one_of(st.integers(), st.booleans(), st.none(), st.text(alphabet="abc ", max_size=5)),
        lambda inner: st.one_of(
            st.lists(inner, max_size=3),
            st.dictionaries(
                st.sampled_from(["nombre", "mail", "fecha", "cupos", "Usuario", "x"]),
                inner,
                max_size=3,
            ),
        ),
        max_leaves=12,
    )
)
def test_свойство_значения_под_персональными_ключами_никогда_не_остаются(payload: Any) -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("nombre", "mail", "Usuario"):
                    assert value == MASK
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(clean_body(JSON, json.dumps(payload)))


# --- сводка ---


def _record(url: str, method: str = "GET", status: int = 200, resource: str = "xhr") -> dict:
    return {"method": method, "status": status, "resource": resource, "url": url}


def test_сводка_помечает_api_календаря() -> None:
    result = DiscoveryResult(
        Path("x.json"),
        [
            _record("https://bpmgob.mec.gub.uy/agenda_sae_api_disponibilidades?x=1"),
            _record("https://bpmgob.mec.gub.uy/calendario-sae.js?_=1", resource="script"),
        ],
    )
    lines = result.summary()
    assert lines[0].endswith("  <== API календаря")
    assert "agenda_sae_api_disponibilidades" in lines[0]
    assert "<==" not in lines[1]
    assert "socket.io" not in "\n".join(lines)


def test_сводка_замечает_socket_io() -> None:
    result = DiscoveryResult(
        Path("x.json"), [_record("https://bpmgob.mec.gub.uy/socket.io/?EIO=3", "POST", 200)]
    )
    assert (
        result.summary()[-1] == "Замечен socket.io: календарь может обновляться без перезагрузки."
    )


def test_сводка_пустой_записи_пуста() -> None:
    assert DiscoveryResult(Path("x.json")).summary() == []


def test_сводка_печатает_метод_статус_ресурс_и_адрес() -> None:
    line = DiscoveryResult(Path("x.json"), [_record("https://h/a", "POST", 503, "fetch")]).summary()
    assert line == ["POST 503 fetch  https://h/a"]


# --- запись файла ---


def test_имя_файла_дампа_содержит_метку_до_секунд(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    recorder = discovery.DiscoveryRecorder(
        "https://l/", "https://a/", "офис", 1, tmp_path / "out", discovery.BrowserOptions()
    )
    at = datetime(2026, 11, 12, 15, 4, 5, tzinfo=UTC)
    result = recorder._write(at)
    assert result.path == tmp_path / "out" / "discovery-20261112T150405.json"
    saved = json.loads(result.path.read_text(encoding="utf-8"))
    assert saved == {"recorded_at": at.isoformat(), "records": []}


# --- адрес и тело запроса ---


def test_значения_личных_полей_в_адресе_маскируются() -> None:
    url = "https://bpmgob.mec.gub.uy/api?fecha=2027-02-23&Nombre=Ana&documento=AB123456&x=1"
    cleaned = clean_url(url)
    assert (
        cleaned
        == f"https://bpmgob.mec.gub.uy/api?fecha=2027-02-23&Nombre={MASK}&documento={MASK}&x=1"
    )


def test_адрес_без_запроса_не_меняется_а_секреты_в_нём_вырезаются() -> None:
    assert clean_url("https://bpmgob.mec.gub.uy/a/b.js") == "https://bpmgob.mec.gub.uy/a/b.js"
    REDACTOR.register("УСЛОВНЫЙ-СЕКРЕТ")
    assert "УСЛОВНЫЙ" not in clean_url("https://h/p/УСЛОВНЫЙ-СЕКРЕТ?a=1")


def test_имя_личного_поля_в_адресе_может_быть_закодировано() -> None:
    assert (
        clean_url("https://h/x?n%6Fmbre=Ana&e-mail=a")
        == f"https://h/x?n%6Fmbre={MASK}&e-mail={MASK}"
    )


def test_тело_формы_маскирует_личные_поля() -> None:
    cleaned = clean_post_data("csrf=1&apellido=Иванов&telefono=099&fecha=2027-02-23")
    # csrf — секретное имя редактора (значение привязано к сессии): маскируется вместе с личным
    assert cleaned == f"csrf={MASK}&apellido={MASK}&telefono={MASK}&fecha=2027-02-23"


def test_тело_json_маскирует_личные_поля() -> None:
    cleaned = clean_post_data(json.dumps({"fecha": "2027-02-23", "nombre": "Ана"}))
    assert json.loads(cleaned) == {"fecha": "2027-02-23", "nombre": MASK}


@pytest.mark.parametrize("data", ["", "просто текст без полей"])
def test_пустое_и_произвольное_тело_запроса(data: str) -> None:
    assert clean_post_data(data) == data


# --- сбой записи одного ответа ---


async def test_сбой_записи_ответа_логируется_а_не_теряется_молча(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from playwright.async_api import Error as PlaywrightError

    async def broken_headers() -> dict[str, str]:
        raise PlaywrightError("страница закрыта")

    async def text() -> str:
        return "{}"

    request = SimpleNamespace(
        method="GET", resource_type="xhr", all_headers=broken_headers, post_data=None
    )
    response = SimpleNamespace(
        request=request,
        url="https://bpmgob.mec.gub.uy/agenda_sae_api_disponibilidades",
        status=200,
        headers={"content-type": "application/json"},
        text=text,
    )
    recorder = discovery.DiscoveryRecorder(
        "https://l/", "https://a/", "офис", 1, tmp_path, discovery.BrowserOptions()
    )
    with caplog.at_level(logging.WARNING, logger=discovery.logger.name):
        recorder._on_response(response)  # type: ignore[arg-type]
        await asyncio.gather(*recorder._pending, return_exceptions=True)
        await asyncio.sleep(0)
    assert recorder._pending == set()
    assert any("ответ не записан" in r.getMessage() for r in caplog.records)


async def test_тело_ответа_не_прочиталось_запись_делается_с_пустым_телом(tmp_path: Path) -> None:
    from playwright.async_api import Error as PlaywrightError

    async def headers() -> dict[str, str]:
        return {"x": "1"}

    async def no_body() -> str:
        raise PlaywrightError("тело недоступно")

    request = SimpleNamespace(
        method="GET", resource_type="xhr", all_headers=headers, post_data=None
    )
    response = SimpleNamespace(
        request=request,
        url="https://bpmgob.mec.gub.uy/agenda_sae_api_disponibilidades",
        status=204,
        headers={"content-type": "application/json"},
        text=no_body,
        all_headers=headers,
    )
    recorder = discovery.DiscoveryRecorder(
        "https://l/", "https://a/", "офис", 1, tmp_path, discovery.BrowserOptions()
    )
    await recorder._collect(response)  # type: ignore[arg-type]
    [record] = recorder._records
    assert record["body"] == ""
    assert record["status"] == 204


@pytest.mark.parametrize(
    ("url", "resource", "recorded"),
    [
        ("https://bpmgob.mec.gub.uy/api", "xhr", True),
        ("https://bpmgob.mec.gub.uy/api", "fetch", True),
        ("https://bpmgob.mec.gub.uy/js/calendario-sae.js?_=1", "script", True),
        ("https://bpmgob.mec.gub.uy/js/otro.js", "script", False),
        ("https://bpmgob.mec.gub.uy/calendario.html", "document", False),
        ("https://bpmgob.mec.gub.uy/x.css", "stylesheet", False),
        ("https://mi.iduruguay.gub.uy/api", "xhr", False),
        ("https://bpmgob.mec.gub.uy.evil.com/api", "xhr", False),
        ("https://evil.com/calendario-sae.js", "script", False),
    ],
)
async def test_что_записывается_а_что_нет(
    tmp_path: Path, url: str, resource: str, recorded: bool
) -> None:
    async def headers() -> dict[str, str]:
        return {}

    async def text() -> str:
        return "{}"

    request = SimpleNamespace(
        method="GET", resource_type=resource, all_headers=headers, post_data=None
    )
    response = SimpleNamespace(
        request=request,
        url=url,
        status=200,
        headers={"content-type": "application/json"},
        text=text,
        all_headers=headers,
    )
    recorder = discovery.DiscoveryRecorder(
        "https://l/", "https://a/", "офис", 1, tmp_path, discovery.BrowserOptions()
    )
    await recorder._collect(response)  # type: ignore[arg-type]
    assert bool(recorder._records) is recorded
