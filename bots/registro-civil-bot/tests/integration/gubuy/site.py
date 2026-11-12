"""Подставной сайт для браузерных тестов: страницы отдаются через `context.route`, сети нет.

Вся «сеть» Chromium — это обработчик `FakeSite.handle`: запросы к bpmgob и mi.iduruguay получают
подставные страницы, любой другой адрес отклоняется и попадает в `foreign` (тест проверяет, что
он пуст). Так ни один тест не ходит на живой сайт (SPEC 6.4).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Route

BPM_HOST = "bpmgob.mec.gub.uy"
ID_HOST = "mi.iduruguay.gub.uy"
AGENDA = f"https://{BPM_HOST}/etapas/ejecutar/TEST-ETAPA/1"
HOME = f"https://{BPM_HOST}/"
LOGIN_BPM = (
    f"https://{BPM_HOST}/autenticacion/login?redirect=https://{BPM_HOST}/tramites/iniciar/TEST"
)
LOGIN_ID = f"https://{ID_HOST}/login?process_state=TEST"
OFFICE = "Oficina Tramite Extranjeros"

GREEN = "rgb(0, 160, 0)"
GREY = "rgb(238, 238, 238)"

Reply = tuple[int, str, dict[str, str]]
Handler = Callable[["Seen"], Reply | Awaitable[Reply]]


@dataclass(frozen=True)
class Seen:
    method: str
    url: str
    headers: dict[str, str]
    body: str

    @property
    def path(self) -> str:
        return urlparse(self.url).path


@dataclass
class FakeSite:
    handlers: dict[str, Handler] = field(default_factory=dict)
    requests: list[Seen] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)

    def page(self, url: str, body: str, status: int = 200, **headers: str) -> None:
        reply = (status, body, {"content-type": "text/html; charset=utf-8", **headers})
        self.handlers[url] = lambda _: reply

    def redirect(self, url: str, to: str, status: int = 302) -> None:
        self.handlers[url] = lambda _: (status, "", {"location": to})

    def json(self, url: str, payload: object, status: int = 200) -> None:
        reply = (status, json.dumps(payload), {"content-type": "application/json"})
        self.handlers[url] = lambda _: reply

    def on(self, url: str, handler: Handler) -> None:
        self.handlers[url] = handler

    def gets(self) -> list[str]:
        return [r.url for r in self.requests if r.method == "GET"]

    def non_get(self) -> list[Seen]:
        return [r for r in self.requests if r.method != "GET"]

    async def handle(self, route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.hostname not in (BPM_HOST, ID_HOST):
            self.foreign.append(request.url)
            await route.abort()
            return
        seen = Seen(
            request.method, request.url, await request.all_headers(), request.post_data or ""
        )
        self.requests.append(seen)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        handler = self.handlers.get(request.url) or self.handlers.get(key)
        if handler is None:
            await route.fulfill(status=404, body="no encontrado", content_type="text/plain")
            return
        reply = handler(seen)
        if not isinstance(reply, tuple):
            reply = await reply
        status, body, headers = reply
        await route.fulfill(status=status, body=body, headers=headers)


def attach(site: FakeSite, browser: Browser) -> None:
    """Каждый новый контекст браузера получает подставной сайт (код сам создаёт контексты)."""
    real = browser.new_context

    async def new_context(*args: Any, **kwargs: Any) -> BrowserContext:
        context = await real(*args, **kwargs)
        await context.route("**/*", site.handle)
        return context

    browser.new_context = new_context  # type: ignore[method-assign]


def routed_launch(site: FakeSite, real_launch: Callable[..., Awaitable[Browser]]) -> Any:
    async def launch(pw: Any, options: Any) -> Browser:
        browser = await real_launch(pw, options)
        attach(site, browser)
        return browser

    return launch


def routed_temporary_context(site: FakeSite, real: Callable[..., Any]) -> Any:
    @asynccontextmanager
    async def temporary_context(options: Any) -> Any:
        async with real(options) as context:
            await context.route("**/*", site.handle)
            yield context

    return temporary_context


# --- макет календаря jQuery UI Datepicker ---


def td(
    text: object,
    *,
    td_bg: str | None = None,
    a_bg: str | None = None,
    span_bg: str | None = None,
    cls: str = "",
    tag: str = "a",
) -> str:
    def style(bg: str | None) -> str:
        return f' style="background-color: {bg}"' if bg else ""

    inner = f'<{tag} class="ui-state-default"{style(a_bg)}>{text}</{tag}>'
    if span_bg:
        inner = f"<span{style(span_bg)}>{inner}</span>" if tag == "a" else inner
    return f'<td class="{cls}"{style(td_bg)}>{inner}</td>'


def green(day: int, where: str = "a") -> str:
    if where == "td":
        return td(day, td_bg=GREEN, a_bg=GREY)
    if where == "span":
        return (
            f'<td><span style="background-color: {GREEN}">{day}</span></td>' if day else "<td></td>"
        )
    return td(day, a_bg=GREEN)


def grey(day: int) -> str:
    return td(day, a_bg=GREY)


def rows(*cells: str) -> str:
    return "".join(f"<tr>{''.join(cells[i : i + 7])}</tr>" for i in range(0, len(cells), 7))


def calendar_markup(
    title: str, body: str, *, next_disabled: bool = False, banner: str | None = None
) -> str:
    disabled = " ui-state-disabled" if next_disabled else ""
    note = f'<div id="banner" style="color:red">{banner}</div>' if banner else ""
    return (
        '<div class="ui-datepicker"><div class="ui-datepicker-header">'
        '<a class="ui-datepicker-prev ui-corner-all ui-state-disabled"><span>Prev</span></a>'
        f'<a class="ui-datepicker-next ui-corner-all{disabled}"><span>Next</span></a>'
        f'<div class="ui-datepicker-title">{title}</div></div>'
        f'<table class="ui-datepicker-calendar"><tbody>{body}</tbody></table></div>{note}'
    )


def standalone(title: str, body: str, **kwargs: Any) -> str:
    return f"<html><body>{calendar_markup(title, body, **kwargs)}</body></html>"


@dataclass(frozen=True)
class MonthSpec:
    title: str
    body: str
    next_disabled: bool = False


EVENT_LOG_JS = """
window.__events = [];
for (const name of ['click','dblclick','mousedown','mouseup','submit','keydown','keypress',
                    'input','change','contextmenu','dragstart']) {
  document.addEventListener(name, (e) => {
    const t = e.target;
    const cls = typeof t.className === 'string' ? t.className.trim().replace(/\\s+/g, '.') : '';
    window.__events.push(name + ' ' + (t.id ? '#' + t.id : '') + (cls ? '.' + cls : '')
      + ' <' + t.tagName.toLowerCase() + '> ' + (t.textContent || '').trim().slice(0, 24));
  }, true);
}
"""


def agenda_page(
    months: list[MonthSpec],
    *,
    radio: bool = True,
    flip: str = "normal",
    banner: str | None = None,
    show_calendar_without_radio: bool = False,
    office: str = OFFICE,
) -> str:
    """Шаг «Agenda»: радиокнопка офиса, после её выбора появляется календарь.

    `flip`: normal — стрелка листает месяц; none — стрелка ничего не меняет; same_month — меняется
    только регистр заголовка (тот же месяц). Кнопки внизу и форма записывают события, но ничего не
    делают: если бот их нажмёт, это будет видно в `window.__events` и в запросах.
    """
    data = json.dumps(
        [{"title": m.title, "body": m.body, "nextDisabled": m.next_disabled} for m in months]
    )
    radio_html = (
        f'<label><input type="radio" name="loc" id="loc1"> {office}</label>' if radio else ""
    )
    shown = "block" if show_calendar_without_radio else "none"
    return f"""<html><head><meta charset="utf-8"><title>Agenda</title>
<style>#personal {{ height: 700px; background: #fdd; }}</style></head><body>
<h2>Paso 2 de 3</h2>
<div id="personal">Titular: Ana Prueba, pasaporte TEST000000, correo ana.prueba@example.org</div>
<form action="/etapas/ejecutar/TEST-ETAPA/guardar" method="post">
 <fieldset>{radio_html}</fieldset>
 <p>Seleccionar día</p>
 <div id="holder" style="display:{shown}">
  <div class="ui-datepicker" id="dp"><div class="ui-datepicker-header">
   <a class="ui-datepicker-prev ui-corner-all ui-state-disabled"><span>Prev</span></a>
   <a class="ui-datepicker-next ui-corner-all" id="next"><span>Next</span></a>
   <div class="ui-datepicker-title" id="title"></div></div>
   <table class="ui-datepicker-calendar"><tbody id="cal"></tbody></table></div>
  {f'<div style="color:red">{banner}</div>' if banner else ""}
 </div>
 <button type="submit" id="volver" name="a" value="volver">Volver</button>
 <button type="submit" id="guardar" name="a" value="guardar">Guardar y Cerrar</button>
 <button type="submit" id="siguiente" name="a" value="siguiente">Siguiente</button>
</form>
<script>
{EVENT_LOG_JS}
const MONTHS = {data}; let i = 0;
const title = document.getElementById('title'), cal = document.getElementById('cal');
const next = document.getElementById('next');
function render() {{
  title.textContent = MONTHS[i].title; cal.innerHTML = MONTHS[i].body;
  next.classList.toggle('ui-state-disabled', MONTHS[i].nextDisabled || i >= MONTHS.length - 1);
}}
render();
const radio = document.getElementById('loc1');
if (radio) radio.addEventListener('change', () => {{
  document.getElementById('holder').style.display = 'block';
}});
next.addEventListener('click', (e) => {{
  e.preventDefault();
  if (next.classList.contains('ui-state-disabled')) return;
  if ('{flip}' === 'normal') {{ i += 1; render(); }}
  else if ('{flip}' === 'same_month') {{ title.textContent = title.textContent.toUpperCase(); }}
}});
</script></body></html>"""
