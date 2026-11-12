"""Подставной сайт для браузерных тестов: локальный HTTPS-сервер вместо bpmgob и ID Uruguay.

Chromium запускается с правилом `--host-resolver-rules`: имена bpmgob.mec.gub.uy и
mi.iduruguay.gub.uy отображаются на локальный сервер, ВСЕ остальные имена не разрешаются вовсе.
Так настоящие переадресации 302, Set-Cookie и заголовки работают по-настоящему, а до живого сайта
запрос дойти не может (SPEC 6.4, инвариант 9). Сертификат самоподписанный: контексту передаётся
`ignore_https_errors`.
"""

# ruff: noqa: E501  (строки HTML-макетов)
from __future__ import annotations

import json
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import trustme
from aiohttp import web
from playwright.async_api import Browser, BrowserContext, Playwright

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

    def header(self, name: str) -> str:
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), "")

    @property
    def path(self) -> str:
        return urlparse(self.url).path

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""


@dataclass
class FakeSite:
    handlers: dict[tuple[str, str], Handler] = field(default_factory=dict)
    requests: list[Seen] = field(default_factory=list)
    port: int = 0
    browsers: list[Browser] = field(default_factory=list)
    _runner: web.AppRunner | None = None

    # --- описание страниц ---

    @staticmethod
    def _key(url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        return (parsed.hostname or "", parsed.path)

    def page(self, url: str, body: str, status: int = 200, **headers: str) -> None:
        reply = (status, body, {"Content-Type": "text/html; charset=utf-8", **headers})
        self.handlers[self._key(url)] = lambda _: reply

    def redirect(self, url: str, to: str, status: int = 302) -> None:
        self.handlers[self._key(url)] = lambda _: (status, "", {"Location": to})

    def json(self, url: str, payload: object, status: int = 200) -> None:
        reply = (status, json.dumps(payload), {"Content-Type": "application/json"})
        self.handlers[self._key(url)] = lambda _: reply

    def on(self, url: str, handler: Handler) -> None:
        self.handlers[self._key(url)] = handler

    # --- что видел сервер ---

    def gets(self) -> list[str]:
        return [r.url for r in self.requests if r.method == "GET"]

    def non_get(self) -> list[Seen]:
        return [r for r in self.requests if r.method != "GET"]

    def hosts(self) -> set[str]:
        return {r.host for r in self.requests}

    # --- сервер ---

    @property
    def chromium_args(self) -> list[str]:
        rules = (
            f"MAP {BPM_HOST} 127.0.0.1:{self.port}, MAP {ID_HOST} 127.0.0.1:{self.port}, "
            "MAP * ~NOTFOUND"
        )
        return [f"--host-resolver-rules={rules}"]

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        host = request.host.split(":")[0].lower()
        seen = Seen(
            request.method,
            f"https://{host}{request.path_qs}",
            dict(request.headers),
            (await request.read()).decode("utf-8", errors="replace"),
        )
        self.requests.append(seen)
        handler = self.handlers.get((host, request.path))
        if handler is None:
            return web.Response(status=404, text="no encontrado")
        reply = handler(seen)
        if not isinstance(reply, tuple):
            reply = await reply
        status, body, headers = reply
        if status < 0:  # обрыв соединения: сеть недоступна
            request.transport.abort()  # type: ignore[union-attr]
            return web.Response()
        response = web.Response(status=status, body=body.encode("utf-8"))
        for name, value in headers.items():
            if name.lower() == "set-cookie":
                response.headers.add(name, value)
            else:
                response.headers[name] = value
        return response

    async def __aenter__(self) -> FakeSite:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        trustme.CA().issue_cert(BPM_HOST, ID_HOST).configure_cert(server_ssl)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=server_ssl)
        await site.start()
        self.port = int(site._server.sockets[0].getsockname()[1])  # type: ignore[union-attr]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


def routed_launch(site: FakeSite) -> Callable[[Playwright, Any], Awaitable[Browser]]:
    """Замена `launch` из browser.py: тот же Chromium, но имена сайта ведут на `site`."""

    async def launch(pw: Playwright, options: Any) -> Browser:
        browser = await pw.chromium.launch(headless=options.headless, args=site.chromium_args)
        site.browsers.append(browser)
        real = browser.new_context

        async def new_context(*args: Any, **kwargs: Any) -> BrowserContext:
            kwargs.setdefault("ignore_https_errors", True)
            return await real(*args, **kwargs)

        browser.new_context = new_context  # type: ignore[method-assign]
        return browser

    return launch


# --- макет страницы входа ID Uruguay ---

LOGIN_ID_RETURN = f"https://{ID_HOST}/login?process_state=TEST&redirect_uri=https://{BPM_HOST}/"
HOME_PAGE = f"https://{BPM_HOST}/etapas/home"


def id_uruguay_page(
    *,
    omit: str = "",
    password_hidden: bool = False,
    return_to_bpmgob: bool = True,
    custom_select: bool = False,
) -> str:
    """Шаги из SPEC 1.3. `omit` убирает элемент: gubuy, nodoc, pais, tipo, num, continuar1.

    `password_hidden`: поле пароля существует, но не показывается (шаг «завис»).
    `return_to_bpmgob=False`: форма принята, но возврата на bpmgob нет (например, пароль неверный).
    Заполненное уходит POST-запросом `/api/login` в виде JSON вместе с порядком шагов.
    """

    def part(name: str, html: str) -> str:
        return "" if omit == name else html

    pais = '<label for="pais">País emisor</label><select id="pais"><option>Uruguay</option><option>Rusia</option></select>'
    tipo = '<label for="tipo">Tipo de documento</label><select id="tipo"><option>Cédula</option><option>Pasaporte</option></select>'
    if custom_select:  # раскрывающийся список вместо <select>

        def box(name: str, label: str, options: tuple[str, ...]) -> str:
            items = "".join(f'<li role="option">{o}</li>' for o in options)
            return (
                f'<div id="{name}" role="combobox" aria-label="{label}" tabindex="0">elegir</div>'
                f'<ul id="{name}-list" hidden>{items}</ul>'
            )

        pais = box("pais", "País emisor", ("Uruguay", "Rusia"))
        tipo = box("tipo", "Tipo de documento", ("Cédula", "Pasaporte"))
    leave = f"location.href = 'https://{BPM_HOST}/etapas/home';" if return_to_bpmgob else ""
    return f"""<html><head><meta charset="utf-8"></head><body>
<h1>Elige un método para identificarte en ID Uruguay</h1>
<div id="s1">{part("gubuy", '<button id="gubuy">Usuario Gub.uy</button>')}</div>
<div id="s2" hidden>{part("nodoc", '<button id="nodoc">No tengo documento uruguayo</button>')}</div>
<div id="s3" hidden>
 {part("pais", pais)}
 {part("tipo", tipo)}
 {part("num", '<label for="num">Número</label><input id="num">')}
 {part("continuar1", '<button id="c1">Continuar</button>')}
</div>
<div id="s4" hidden>
 <label for="pw">Ingresá tu contraseña</label><input type="password" id="pw">
 <button id="c2">Continuar</button>
</div>
<script>
const steps = [];
const $ = (id) => document.getElementById(id);
const show = (a, b) => {{ $(a).hidden = true; $(b).hidden = false; }};
if ($('gubuy')) $('gubuy').addEventListener('click', () => {{ steps.push('gubuy'); show('s1', 's2'); }});
if ($('nodoc')) $('nodoc').addEventListener('click', () => {{ steps.push('nodoc'); show('s2', 's3'); }});
if ($('c1')) $('c1').addEventListener('click', () => {{
  steps.push('continuar1');
  $('s3').hidden = true;
  $('s4').hidden = {str(password_hidden).lower()};
}});
for (const id of ['pais', 'tipo']) {{
  const box = $(id); if (!box || box.tagName !== 'DIV') continue;
  box.addEventListener('click', () => {{ $(id + '-list').hidden = false; }});
  $(id + '-list').addEventListener('click', (e) => {{
    box.dataset.value = e.target.textContent; box.textContent = e.target.textContent;
    $(id + '-list').hidden = true;
  }});
}}
$('c2').addEventListener('click', () => {{
  steps.push('continuar2');
  const field = (id) => ($(id) ? ($(id).value ?? $(id).dataset.value) : null);
  fetch('/api/login', {{method: 'POST', body: JSON.stringify({{
    pais: field('pais'), tipo: field('tipo'), num: field('num'), pw: field('pw'), steps }})}})
    .then(() => {{ {leave} }});
}});
</script></body></html>"""


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
    extra_script: str = "",
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
{extra_script}
</script></body></html>"""
