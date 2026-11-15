"""`SaeSource` (решение R12): публичная страница записи SAE на локальном HTTPS-сервере.

Страницы синтетические, по снимку экрана владельца: надпись «no hay cupos disponibles», выбор
офиса «Ubicación». Реальный HTML бот сохраняет сам при первой проверке (снимок `sae-*.html`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp import web
from tests.fakes import TARGET, FakeClock, LocalTlsServer

from rcbot.application.ports import SessionCookie
from rcbot.domain.errors import RateLimited, SiteUnavailable, UnexpectedResponse
from rcbot.domain.models import SourceKind
from rcbot.infrastructure.gubuy import MAX_STORED_FILES, api_source
from rcbot.infrastructure.gubuy.sae_source import SaeSource, seats_open, visible_text
from rcbot.infrastructure.http.client import SafeHttp

PAGE_PATH = "/sae/agendarReserva/Paso1.xhtml"

EMPTY_PAGE = """<!DOCTYPE html><html><head><title>SAE - Sistema de Agenda Electrónica</title>
<script>var msg = "otra cosa";</script></head><body>
<h1>Inscripción Extranjeros Exclusiva para Titulares</h1>
<div class="alerta"><h2>En la oficina seleccionada no hay cupos disponibles</h2>
<p>A la brevedad se añadirán cupos</p></div>
<fieldset><legend>Formulario</legend><label>Ubicación:</label>
<input type="radio" checked> Oficina Tramite Extranjeros - Av. Uruguay 933</fieldset>
</body></html>"""

OPEN_PAGE = """<!DOCTYPE html><html><head><title>SAE - Sistema de Agenda Electrónica</title>
<script>var aviso = "No hay cupos disponibles";</script></head><body>
<h1>Inscripción Extranjeros Exclusiva para Titulares</h1>
<fieldset><label>Ubicaci&oacute;n:</label>
<input type="radio" checked> Oficina Tramite Extranjeros - Av. Uruguay 933</fieldset>
<div class="calendario">Diciembre 2026</div>
</body></html>"""

OTHER_PAGE = "<html><body><h1>Error interno</h1><p>Intente más tarde</p></body></html>"

MakeSource = Callable[..., SaeSource]


@pytest.fixture
def make(tls_server: LocalTlsServer, tmp_path: Path, clock: FakeClock) -> MakeSource:
    def factory(snapshots: Path | None = None) -> SaeSource:
        http = SafeHttp({"127.0.0.1"}, 5.0, ssl_context=tls_server.client_ssl)
        return SaeSource(http, tls_server.url(PAGE_PATH), snapshots or tmp_path / "shots", clock)

    return factory


# --- разбор страницы ---


def test_видимый_текст_без_скриптов_регистра_и_ударений() -> None:
    text = visible_text("<p>Ubicación:</p><script>Скрытое</script><style>.x{}</style><b>ÁÉ</b>")
    assert text == "ubicacion: ae"


def test_надпись_нет_мест_значит_мест_нет() -> None:
    assert seats_open(EMPTY_PAGE) is False


def test_надпись_в_скрипте_не_считается_надписью() -> None:
    # Шаблон текста в JavaScript страницы не должен прятать появившиеся места
    assert seats_open(OPEN_PAGE) is True


def test_регистр_и_ударения_в_надписи_не_мешают() -> None:
    page = "<div>Ubicación</div><div>NO HAY CUPOS DISPONIBLES</div>"
    assert seats_open(page) is False


def test_не_страница_записи_не_распознаётся() -> None:
    assert seats_open(OTHER_PAGE) is None
    assert seats_open("") is None


@pytest.mark.parametrize(
    "hidden_attr",
    ['style="display:none"', 'style="display: none;"', "hidden", 'aria-hidden="true"'],
)
def test_скрытая_надпись_нет_мест_не_прячет_появившиеся_места(hidden_attr: str) -> None:
    # Блок с надписью может остаться в разметке скрытым, когда места появятся
    page = (
        f'<div id="form:warnSinCupos" {hidden_attr}><h4>En la oficina seleccionada '
        "no hay cupos disponibles</h4></div><fieldset><span>Ubicación</span><input type=radio>"
        "</fieldset>"
    )
    assert seats_open(page) is True


def test_незакрытые_теги_не_ломают_видимость() -> None:
    page = '<div style="display:none"><p>скрыто<p>тоже</div><span>Ubicación</span>'
    assert visible_text(page) == "ubicacion"


def test_настоящий_снимок_страницы_без_мест() -> None:
    """Форма блока с надписью — как в снимке живой страницы SAE от 2026-11-14 (без лишнего)."""
    page = """<form><div id="form:warnSinCupos" class="ui-outputpanel ui-widget" style="">
    <div class="ui-messages-warn"><h4 class="ui-messages-warn-title">En la oficina seleccionada
    no hay cupos disponibles
    </h4><ul><li><span>A la brevedad se añadirán cupos</span></li></ul></div></div>
    <fieldset><legend>Formulario</legend><div id="recurso"><span class="label">Ubicación</span>
    <div role="radiogroup"><input type="radio" checked="checked"/></div></div>
    <!-- Calendario completo --></fieldset></form>"""
    assert seats_open(page) is False


# --- источник ---


async def test_нет_мест_пустой_снимок_источника_sae(
    make: MakeSource, tls_server: LocalTlsServer
) -> None:
    tls_server.respond("GET", PAGE_PATH, text=EMPTY_PAGE, content_type="text/html")
    source = make()
    snapshot = await source.fetch(TARGET)
    await source.close()
    assert source.kind is SourceKind.SAE
    assert snapshot.source is SourceKind.SAE
    assert snapshot.slots == frozenset() and snapshot.is_open is False


async def test_места_появились_снимок_открыт_без_дат(
    make: MakeSource, tls_server: LocalTlsServer
) -> None:
    tls_server.respond("GET", PAGE_PATH, text=OPEN_PAGE, content_type="text/html")
    source = make()
    snapshot = await source.fetch(TARGET)
    await source.close()
    assert snapshot.is_open and snapshot.dates_unknown and snapshot.slots == frozenset()


async def test_запрос_только_get_без_cookies_и_без_тела(
    make: MakeSource, tls_server: LocalTlsServer
) -> None:
    tls_server.respond("GET", PAGE_PATH, text=EMPTY_PAGE, content_type="text/html")
    source = make()
    source.use_cookies([SessionCookie("simple_bpm_session", "TEST", "bpmgob.mec.gub.uy")])
    await source.fetch(TARGET)
    await source.close()
    [request] = tls_server.requests
    assert request.method == "GET" and request.body == b""
    assert "Cookie" not in request.headers  # вход в аккаунт SAE не нужен (R12)


@pytest.mark.parametrize("status", [403, 429])
async def test_блокировка_это_замедление(
    make: MakeSource, tls_server: LocalTlsServer, status: int
) -> None:
    tls_server.respond("GET", PAGE_PATH, text="no", status=status)
    source = make()
    with pytest.raises(RateLimited):
        await source.fetch(TARGET)
    await source.close()


async def test_ошибка_сервера_это_недоступность(
    make: MakeSource, tls_server: LocalTlsServer
) -> None:
    tls_server.respond("GET", PAGE_PATH, text="down", status=503)
    source = make()
    with pytest.raises(SiteUnavailable):
        await source.fetch(TARGET)
    await source.close()


async def test_переадресация_не_выполняется_и_это_неожиданный_ответ(
    make: MakeSource, tls_server: LocalTlsServer
) -> None:
    tls_server.respond(
        "GET", PAGE_PATH, text="", status=302, headers={"Location": "https://evil.example/"}
    )
    source = make()
    with pytest.raises(UnexpectedResponse):
        await source.fetch(TARGET)
    await source.close()
    assert len(tls_server.requests) == 1  # чужой адрес не открывался


async def test_чужая_страница_неожиданный_ответ_и_снимок_для_разбора(
    make: MakeSource, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    tls_server.respond("GET", PAGE_PATH, text=OTHER_PAGE, content_type="text/html")
    source = make()
    with pytest.raises(UnexpectedResponse, match="не распознана"):
        await source.fetch(TARGET)
    await source.close()
    assert [p.name.endswith("-unknown.html") for p in (tmp_path / "shots").iterdir()] == [True]


async def test_слишком_большой_ответ_не_читается(
    make: MakeSource, tls_server: LocalTlsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_source, "MAX_RESPONSE_BYTES", 100)
    tls_server.respond("GET", PAGE_PATH, text=EMPTY_PAGE, content_type="text/html")
    source = make()
    with pytest.raises(UnexpectedResponse):
        await source.fetch(TARGET)
    await source.close()


async def test_снимок_сохраняется_при_первой_проверке_и_при_смене_состояния(
    make: MakeSource, tls_server: LocalTlsServer, tmp_path: Path, clock: FakeClock
) -> None:
    pages = [EMPTY_PAGE, EMPTY_PAGE, OPEN_PAGE, OPEN_PAGE, EMPTY_PAGE]
    served: list[str] = []

    async def handler(_: web.Request) -> web.StreamResponse:
        page = pages[len(served)]
        served.append(page)
        return web.Response(text=page, content_type="text/html")

    tls_server.route("GET", PAGE_PATH, handler)
    source = make()
    for _ in pages:
        await source.fetch(TARGET)
        await clock.sleep(1)
    await source.close()
    labels = sorted(p.name.rsplit("-", 1)[1] for p in (tmp_path / "shots").iterdir())
    # Первая проверка, переход «нет → есть», переход «есть → нет»; повторы не пишутся
    assert labels == ["empty.html", "empty.html", "open.html"]


async def test_снимков_не_больше_предела(
    make: MakeSource, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    for i in range(MAX_STORED_FILES + 5):
        (shots / f"sae-20260101T0000{i:02d}-empty.html").write_text("x", encoding="utf-8")
    tls_server.respond("GET", PAGE_PATH, text=EMPTY_PAGE, content_type="text/html")
    source = make()
    await source.fetch(TARGET)
    await source.close()
    assert len(list(shots.glob("sae-*.html"))) == MAX_STORED_FILES


async def test_сбой_записи_снимка_не_мешает_проверке(
    make: MakeSource, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("файл вместо каталога", encoding="utf-8")
    tls_server.respond("GET", PAGE_PATH, text=EMPTY_PAGE, content_type="text/html")
    source = make(snapshots=blocker)
    snapshot = await source.fetch(TARGET)
    await source.close()
    assert snapshot.is_open is False
