"""DomSource в настоящем Chromium на подставном сайте (SPEC 2.3, 2.6; инвариант 0.3: чтение).

Страницы отдаёт локальный HTTPS-сервер (см. site.py): имена bpmgob и ID Uruguay ведут на него,
остальные имена в Chromium не разрешаются вовсе — до живого сайта запрос дойти не может.
"""

from __future__ import annotations

import dataclasses
import logging
import struct
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from tests.fakes import TARGET, FakeClock
from tests.integration.gubuy.site import (
    AGENDA,
    LOGIN_ID,
    OFFICE,
    FakeSite,
    MonthSpec,
    agenda_page,
    green,
    grey,
    routed_launch,
    rows,
)

from rcbot.application.ports import SessionCookie
from rcbot.domain.errors import (
    ManualStepRequired,
    RateLimited,
    SessionExpired,
    SiteUnavailable,
    UnexpectedResponse,
)
from rcbot.domain.models import SourceKind
from rcbot.infrastructure.gubuy import dom_source
from rcbot.infrastructure.gubuy.browser import BrowserOptions, launch
from rcbot.infrastructure.gubuy.dom_source import MAX_SHOTS, DomSource

pytestmark = pytest.mark.browser

NOV = MonthSpec("Noviembre 2026", rows(grey(1), green(5), green(12), grey(20)))
DIC = MonthSpec("Diciembre 2026", rows(grey(1), green(3)))
ENE = MonthSpec("Enero 2027", rows(green(7)))
FEB = MonthSpec("Febrero 2027", rows(grey(1)))
MAR = MonthSpec("Marzo 2027", rows(green(30)))
EMPTY = MonthSpec("Noviembre 2026", rows(grey(1), grey(2)))
STEP1_HTML = "<html><b>Verificación de seguridad</b> ¿Qué número está antes del 1?</html>"
BLOCK_HTML = "<html>The requested URL was rejected. Please consult with your administrator.</html>"
ID_LOGIN_HTML = "<html><h1>Elige un método para identificarte en ID Uruguay</h1></html>"

Make = Callable[..., DomSource]


class Env:
    def __init__(self, site: FakeSite, make: Make, shots: Path, clock: FakeClock) -> None:
        self.site = site
        self.make = make
        self.shots = shots
        self.clock = clock


@pytest.fixture
async def env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> AsyncIterator[Env]:
    async with FakeSite() as site:
        monkeypatch.setattr(dom_source, "launch", routed_launch(site))
        monkeypatch.setattr(dom_source, "STEP_TIMEOUT_MS", 1500)  # провалы ждём 1,5 с, а не 20 с
        created: list[DomSource] = []
        shots = tmp_path / "shots"

        def make(location: str = OFFICE, shots_dir: Path | None = None) -> DomSource:
            source = DomSource(
                AGENDA, location, BrowserOptions(headless=True), shots_dir or shots, clock
            )
            created.append(source)
            return source

        yield Env(site, make, shots, clock)
        for source in created:
            await source.close()
        assert site.hosts() <= {"bpmgob.mec.gub.uy", "mi.iduruguay.gub.uy"}


def target(months: int) -> object:
    return dataclasses.replace(TARGET, months_ahead=months)


async def events(source: DomSource) -> list[str]:
    assert source._page is not None
    return list(await source._page.evaluate("window.__events"))


def png_size(path: Path) -> tuple[int, int]:
    width, height = struct.unpack(">II", path.read_bytes()[16:24])
    return width, height


# --- чтение месяцев ---


async def test_один_месяц_читается_без_нажатий_на_стрелку(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV, DIC]))
    source = env.make()
    snapshot = await source.fetch(target(1))  # type: ignore[arg-type]
    assert {s.day.isoformat() for s in snapshot.slots} == {"2026-11-05", "2026-11-12"}
    assert snapshot.source is SourceKind.DOM
    assert snapshot.target_id == TARGET.id
    assert snapshot.observed_at == env.clock.now()
    assert snapshot.latency_ms > 0
    assert not [e for e in await events(source) if "next" in e.lower()]


async def test_несколько_месяцев_и_переход_через_год(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV, DIC, ENE, FEB]))
    snapshot = await env.make().fetch(target(3))  # type: ignore[arg-type]
    assert {s.day.isoformat() for s in snapshot.slots} == {
        "2026-11-05",
        "2026-11-12",
        "2026-12-03",
        "2027-01-07",
    }


async def test_чтение_останавливается_на_months_ahead(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV, DIC, ENE, FEB, MAR]))
    source = env.make()
    snapshot = await source.fetch(target(2))  # type: ignore[arg-type]
    assert {s.day.month for s in snapshot.slots} == {11, 12}
    clicks = [e for e in await events(source) if e.startswith("click") and "next" in e.lower()]
    assert len(clicks) == 1  # два месяца — одно нажатие стрелки


async def test_чтение_останавливается_когда_стрелка_отключена(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV, DIC]))  # второй месяц последний: стрелка отключена
    source = env.make()
    snapshot = await source.fetch(target(6))  # type: ignore[arg-type]
    assert {s.day.month for s in snapshot.slots} == {11, 12}
    clicks = [e for e in await events(source) if e.startswith("click") and "next" in e.lower()]
    assert len(clicks) == 1


async def test_стрелка_отключена_уже_на_первом_месяце(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([MonthSpec("Noviembre 2026", NOV.body, next_disabled=True)]))
    source = env.make()
    snapshot = await source.fetch(target(6))  # type: ignore[arg-type]
    assert len(snapshot.slots) == 2
    assert not [e for e in await events(source) if "next" in e.lower()]


async def test_нет_мест_это_пустое_множество_и_плашка_не_мешает(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([EMPTY], banner="No hay cupos disponibles"))
    snapshot = await env.make().fetch(target(1))  # type: ignore[arg-type]
    assert snapshot.slots == frozenset()


async def test_календарь_не_перелистнулся_это_unexpected_response(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV, DIC], flip="none"))
    with pytest.raises(UnexpectedResponse, match="не перелистнулся"):
        await env.make().fetch(target(2))  # type: ignore[arg-type]


async def test_заголовок_сменился_но_месяц_тот_же_это_unexpected_response(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV, DIC], flip="same_month"))
    with pytest.raises(UnexpectedResponse, match="не перелистнулся"):
        await env.make().fetch(target(2))  # type: ignore[arg-type]


async def test_радиокнопки_офиса_нет_это_unexpected_response(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV], radio=False))
    with pytest.raises(UnexpectedResponse, match="переключатель офиса"):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_другой_офис_не_находится(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV], office="Otra Oficina"))
    with pytest.raises(UnexpectedResponse, match="переключатель офиса"):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_офис_ищется_без_учёта_регистра(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV], office="OFICINA TRAMITE EXTRANJEROS"))
    assert len((await env.make().fetch(target(1))).slots) == 2  # type: ignore[arg-type]


async def test_календарь_не_появился_после_выбора_офиса(env: Env) -> None:
    html = (
        "<h2>Paso 2 de 3</h2><label><input type='radio' name='l'>"
        f" {OFFICE}</label><p>Seleccionar día</p>"
    )
    env.site.page(AGENDA, html)
    with pytest.raises(SiteUnavailable, match="браузер не прочитал страницу"):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_офис_уже_выбран_повторно_не_нажимается(env: Env) -> None:
    html = agenda_page([NOV], show_calendar_without_radio=True).replace(
        'id="loc1">', 'id="loc1" checked>'
    )
    env.site.page(AGENDA, html)
    source = env.make()
    assert len((await source.fetch(target(1))).slots) == 2  # type: ignore[arg-type]
    assert not [e for e in await events(source) if "loc1" in e]


async def test_офис_находится_и_по_подписи_если_роли_radio_нет(env: Env) -> None:
    html = f"""<h2>Paso 2 de 3</h2><input type="checkbox" id="o"><label for="o">{OFFICE}</label>
<div id="h" style="display:none"><div class="ui-datepicker">
<div class="ui-datepicker-title">Noviembre 2026</div>
<a class="ui-datepicker-next ui-state-disabled"></a>
<table class="ui-datepicker-calendar"><tbody>{NOV.body}</tbody></table></div></div>
<script>document.getElementById('o').addEventListener('change',
() => {{ document.getElementById('h').style.display = 'block'; }});</script>"""
    env.site.page(AGENDA, html)
    assert len((await env.make().fetch(target(1))).slots) == 2  # type: ignore[arg-type]


# --- распознавание страниц ---


async def test_переадресация_на_вход_это_session_expired(env: Env) -> None:
    env.site.redirect(AGENDA, LOGIN_ID)
    env.site.page(LOGIN_ID, ID_LOGIN_HTML)
    with pytest.raises(SessionExpired):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_страница_autenticacion_это_session_expired(env: Env) -> None:
    login = "https://bpmgob.mec.gub.uy/autenticacion/login"
    env.site.redirect(AGENDA, login)
    env.site.page(login, "<html>Ingresar</html>")
    with pytest.raises(SessionExpired):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_шаг_один_это_manual_step_required(env: Env) -> None:
    env.site.page(AGENDA, STEP1_HTML)
    with pytest.raises(ManualStepRequired):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [403, 429])
async def test_статусы_блокировки_это_rate_limited(env: Env, status: int) -> None:
    env.site.page(AGENDA, "<html>обычная страница</html>", status=status)
    with pytest.raises(RateLimited):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_страница_блокировки_это_rate_limited(env: Env) -> None:
    env.site.page(AGENDA, BLOCK_HTML)
    with pytest.raises(RateLimited, match="блокировки"):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_это_site_unavailable(env: Env, status: int) -> None:
    env.site.page(AGENDA, "<html>Service Unavailable</html>", status=status)
    with pytest.raises(SiteUnavailable, match=str(status)):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_5xx_со_страницей_agenda_всё_равно_site_unavailable(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]), status=503)
    with pytest.raises(SiteUnavailable, match="503"):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_страница_без_признаков_это_unexpected_response(env: Env) -> None:
    env.site.page(AGENDA, "<html><body>Hola</body></html>")
    with pytest.raises(UnexpectedResponse, match="признаки шага"):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_ответ_404_без_признаков_это_unexpected_response(env: Env) -> None:
    env.site.page(AGENDA, "<html>No existe</html>", status=404)
    with pytest.raises(UnexpectedResponse):
        await env.make().fetch(target(1))  # type: ignore[arg-type]


async def test_обрыв_сети_это_site_unavailable_без_деталей(env: Env) -> None:
    env.site.on(AGENDA, lambda _: (-1, "", {}))
    with pytest.raises(SiteUnavailable) as info:
        await env.make().fetch(target(1))  # type: ignore[arg-type]
    assert "bpmgob" not in str(
        info.value
    )  # адрес и текст ошибки Playwright не попадают в сообщение


# --- cookies и контекст ---


async def test_cookies_попадают_в_контекст_и_уходят_на_сайт(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    source.use_cookies([SessionCookie("simple_bpm_session", "TEST-VALUE-1", "bpmgob.mec.gub.uy")])
    await source.fetch(target(1))  # type: ignore[arg-type]
    assert source._context is not None
    stored = {c["name"]: c["value"] for c in await source._context.cookies()}
    assert stored == {"simple_bpm_session": "TEST-VALUE-1"}
    assert "simple_bpm_session=TEST-VALUE-1" in env.site.requests[0].header("cookie")


async def test_use_cookies_пересоздаёт_контекст_а_без_него_контекст_общий(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    source.use_cookies([SessionCookie("simple_bpm_session", "TEST-OLD", "bpmgob.mec.gub.uy")])
    await source.fetch(target(1))  # type: ignore[arg-type]
    first_context, first_browser = source._context, source._browser
    await source.fetch(target(1))  # type: ignore[arg-type]
    assert source._context is first_context  # cookies не менялись: контекст и вкладка прежние

    source.use_cookies([SessionCookie("simple_bpm_session", "TEST-NEW", "bpmgob.mec.gub.uy")])
    await source.fetch(target(1))  # type: ignore[arg-type]
    assert source._context is not first_context
    assert source._browser is first_browser  # браузер тот же, пересоздан только контекст
    assert first_context is not None
    with pytest.raises(Exception, match="closed"):
        await first_context.new_page()  # старый контекст закрыт: его cookies не живут
    assert source._context is not None
    values = [c["value"] for c in await source._context.cookies()]
    assert values == ["TEST-NEW"]


async def test_профиль_на_диск_не_пишется(env: Env, tmp_path: Path) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    await source.fetch(target(1))  # type: ignore[arg-type]
    # Контекст создан без user_data_dir: временный, в памяти
    assert source._context is not None
    assert source._context.browser is source._browser


# --- снимки ---


async def test_снимок_только_элемента_календаря_и_только_при_слотах(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    await source.fetch(target(1))  # type: ignore[arg-type]
    [shot] = list(env.shots.glob("*.png"))
    assert shot.name == f"{env.clock.now():%Y%m%dT%H%M%S}.png"
    assert source._page is not None
    box = await source._page.locator(".ui-datepicker").first.bounding_box()
    assert box is not None
    width, height = png_size(shot)
    assert abs(width - box["width"]) <= 1
    assert abs(height - box["height"]) <= 1
    full = await source._page.screenshot(full_page=True)
    full_width, full_height = struct.unpack(">II", full[16:24])
    assert height < full_height / 2  # весь экран с данными владельца в файл не попадает
    assert width <= full_width


async def test_без_слотов_снимка_нет_и_папка_не_создаётся(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([EMPTY]))
    await env.make().fetch(target(1))  # type: ignore[arg-type]
    assert not env.shots.exists()


async def test_снимков_не_больше_двадцати_старые_удаляются(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    await source.fetch(target(1))  # type: ignore[arg-type]
    assert source._page is not None
    start = env.clock.now()
    for i in range(1, MAX_SHOTS + 6):
        await source._shot(source._page, start + timedelta(seconds=i))
    files = sorted(p.name for p in env.shots.glob("*.png"))
    assert len(files) == MAX_SHOTS == 20
    assert files[0] == f"{start + timedelta(seconds=6):%Y%m%dT%H%M%S}.png"
    assert files[-1] == f"{start + timedelta(seconds=25):%Y%m%dT%H%M%S}.png"


async def test_сбой_снимка_не_теряет_найденные_слоты(
    env: Env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Диск не принял снимок (здесь вместо папки лежит файл), но слоты уже найдены:
    # тревога важнее снимка
    blocked = tmp_path / "занято"
    blocked.write_text("файл на месте папки", encoding="utf-8")
    env.site.page(AGENDA, agenda_page([NOV]))
    with caplog.at_level(logging.WARNING, logger=dom_source.logger.name):
        snapshot = await env.make(shots_dir=blocked).fetch(target(1))  # type: ignore[arg-type]
    assert len(snapshot.slots) == 2
    assert any("снимок календаря не сохранён" in r.getMessage() for r in caplog.records)


# --- жизненный цикл ---


async def test_close_закрывает_браузер_идемпотентен_и_источник_работает_снова(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    await source.close()  # до первого запуска: ничего не открыто
    await source.fetch(target(1))  # type: ignore[arg-type]
    browser = source._browser
    assert browser is not None and browser.is_connected()
    await source.close()
    assert not browser.is_connected()
    assert source._browser is source._context is source._page is source._pw is None
    await source.close()  # повторный вызов безопасен
    assert len((await source.fetch(target(1))).slots) == 2  # type: ignore[arg-type]
    assert source._browser is not browser


async def test_после_падения_браузера_следующая_проверка_запускает_новый(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    await source.fetch(target(1))  # type: ignore[arg-type]
    dead = source._browser
    assert dead is not None
    await dead.close()  # как будто процесс Chromium завершился
    assert len((await source.fetch(target(1))).slots) == 2  # type: ignore[arg-type]
    assert source._browser is not dead
    assert source._browser is not None and source._browser.is_connected()


async def test_закрытая_вкладка_пересоздаётся(env: Env) -> None:
    env.site.page(AGENDA, agenda_page([NOV]))
    source = env.make()
    await source.fetch(target(1))  # type: ignore[arg-type]
    page = source._page
    assert page is not None
    await page.close()
    assert len((await source.fetch(target(1))).slots) == 2  # type: ignore[arg-type]
    assert source._page is not page


async def test_ошибка_проверки_не_ломает_следующую(env: Env) -> None:
    env.site.page(AGENDA, "<html>Service Unavailable</html>", status=503)
    source = env.make()
    with pytest.raises(SiteUnavailable):
        await source.fetch(target(1))  # type: ignore[arg-type]
    env.site.page(AGENDA, agenda_page([NOV]))
    assert len((await source.fetch(target(1))).slots) == 2  # type: ignore[arg-type]


async def test_вид_источника(env: Env) -> None:
    assert env.make().kind is SourceKind.DOM


async def test_chromium_запускается_без_профиля_на_диске() -> None:
    # Прямая проверка модуля browser: launch без channel и контекст без постоянного каталога
    async with async_playwright() as pw:
        browser = await launch(pw, BrowserOptions(headless=True))
        try:
            context = await browser.new_context()
            assert context.browser is browser  # контекст принадлежит браузеру, а не профилю
            await context.close()
        finally:
            await browser.close()
