"""Сквозной сценарий для проверки утечек: трогает вход, источник, карантин, журнал, БД и каналы.

Собирается настоящий цикл опроса, рассылка, журнал на рабочем столе, БД SQLite и ApiSource на
локальном TLS-сервере; Telegram — фейк, который запоминает текст владельцу. Во все места, откуда
текст попадает наружу, подсовываются «канарейки» (`helpers.Canaries`): в тексты ошибок, в тела
ответов сайта, в заголовки. Кто поймал канарейку — тот нашёл утечку.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from rcbot.application.alerting import AlertOrchestrator, RepeatPolicy
from rcbot.application.bus import EventBus
from rcbot.application.incidents import IncidentBook
from rcbot.application.journal_feed import JournalFeed
from rcbot.application.liveness import LivenessMonitor
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import NoopCoordinator, SessionCookie
from rcbot.application.runtime import RuntimeSettings, RuntimeStore
from rcbot.application.session_manager import SessionManager
from rcbot.bootstrap import Core, build_core, ensure_target, open_storage
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretStore
from rcbot.domain.detector import Detector
from rcbot.domain.errors import LoginFailed, SessionExpired, SiteUnavailable
from rcbot.domain.models import AvailabilitySnapshot, Channel, SourceKind, WatchTarget
from rcbot.infrastructure.gubuy.api_source import ApiSource
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.logging.redaction import REDACTOR
from rcbot.infrastructure.notify import callmebot
from rcbot.infrastructure.notify.callmebot import CallMeBotNotifier
from rcbot.infrastructure.notify.ntfy import NtfyNotifier
from rcbot.runner import _wire
from tests.fakes import (
    T0,
    FakeAuthenticator,
    FakeClock,
    FakeNotifier,
    LocalTlsServer,
    RecordedRequest,
    settle,
    slots,
    snapshot,
)
from tests.privacy.helpers import Canaries

C = Canaries()
Step = Callable[[WatchTarget], Awaitable[AvailabilitySnapshot]]


@dataclass
class Artifacts:
    root: Path
    paths: Paths
    core: Core
    owner_texts: list[str]
    ntfy: list[RecordedRequest]
    call: list[RecordedRequest]
    api: list[RecordedRequest]
    states: list[str] = field(default_factory=list)

    @property
    def journal_dir(self) -> Path:
        return Path(self.core.settings.journal_dir)

    def database_files(self) -> list[Path]:
        return sorted(self.paths.data.glob("rcbot.sqlite3*"))


class ChainSource:
    """Источник, который отвечает по списку шагов: ответ сайта, ошибка, снимок с местами."""

    kind = SourceKind.API

    def __init__(self, steps: list[Step]) -> None:
        self._steps = steps

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        return await self._steps.pop(0)(target)

    def use_cookies(self, cookies: object) -> None:
        """Cookies сессии источнику-заглушке не нужны."""

    async def close(self) -> None:
        """Ресурсов не держит."""


def hostile_html() -> str:
    """Страница, в которую сайт «эхом» вернул всё секретное: куки, токены, документ, контакты."""
    return (
        "<html><body>Error\n"
        f"Set-Cookie: simple_bpm_session={C.cookie_value}; Path=/\n"
        f"Cookie: TS0191f197={C.cookie_value}\n"
        f"token={C.telegram_token}\n"
        f"documento {C.doc_number} contraseña={C.password}\n"
        f"заявка {C.tramite}, этап /etapas/ejecutar/{C.etapa}/1, тема {C.ntfy_topic}\n"
        "correo ivan.canary@example.com, +598 99 123 456, 1234567890123\n"
        "</body></html>"
    )


def prime_redactor(store: SecretStore | None = None) -> None:
    """Как при запуске: чтение секретов из keyring регистрирует их в фильтре."""
    store = store or SecretStore(on_read=REDACTOR.register)
    store.credentials()
    for key in C.secrets():
        store.require(key)
    REDACTOR.register_many([C.tramite, C.etapa])


async def run_scenario(
    tmp_path: Path,
    paths: Paths,
    server: LocalTlsServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Artifacts:
    monkeypatch.setenv("RCBOT_AGENDA_URL", f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{C.etapa}/1")
    monkeypatch.setenv("RCBOT_TRAMITE_ID", C.tramite)
    store = SecretStore()
    for key, value in C.secrets().items():
        store.set(key, value)
    core = build_core(console=False, level="DEBUG", command="run")
    prime_redactor(core.secrets)
    settings = core.settings
    clock = FakeClock()

    # ответы «сайта»: страница с эхом секретов, 429 с cookie в заголовке, JSON неожиданного формата
    answers = [
        (200, "text/html; charset=utf-8", hostile_html(), {}),
        (
            429,
            "text/plain",
            f"slow down {C.password}",
            {"Set-Cookie": f"simple_bpm_session={C.cookie_value}"},
        ),
        (
            200,
            "application/json",
            json.dumps(
                {"ok": False, "note": f"Cookie: simple_bpm_session={C.cookie_value}"}
                | {"doc": C.doc_number}
            ),
            {},
        ),
    ]
    served = {"n": 0}

    async def api_handler(request):  # type: ignore[no-untyped-def]
        from aiohttp import web

        status, ctype, body, headers = answers[min(served["n"], len(answers) - 1)]
        served["n"] += 1
        return web.Response(
            status=status, text=body, content_type=ctype.split(";")[0], headers=headers
        )

    server.route("GET", "/api/disp", api_handler)
    server.respond("POST", "/", text="ok")
    server.respond("GET", "/start.php", text="Call queued")
    monkeypatch.setattr(callmebot, "API_URL", server.url("/start.php"))

    api_http = SafeHttp({"127.0.0.1"}, 5, ssl_context=server.client_ssl)
    api = ApiSource(api_http, server.url("/api/disp"), settings.agenda_url, paths.quarantine, clock)

    storage = await open_storage(core)
    target = await ensure_target(core, storage)
    runtime = RuntimeSettings(settings.poll_interval_sec, settings.months_ahead, T0)
    book = IncidentBook(storage.incidents, clock, target.id)
    bus = EventBus()

    telegram = FakeNotifier(Channel.TELEGRAM)
    ntfy_http = SafeHttp({"127.0.0.1"}, 5, ssl_context=server.client_ssl)
    call_http = SafeHttp({"127.0.0.1"}, 5, ssl_context=server.client_ssl)
    notifiers = {
        Channel.TELEGRAM: telegram,
        Channel.NTFY: NtfyNotifier(ntfy_http, server.url("").rstrip("/"), C.ntfy_topic),
        Channel.CALL: CallMeBotNotifier(call_http, C.call_user, settings.call_lang),
    }
    policies = {c: RepeatPolicy(60.0, 0) for c in Channel}
    authenticator = FakeAuthenticator(
        cookies=[SessionCookie("simple_bpm_session", C.cookie_value, "bpmgob.mec.gub.uy")],
        failures=[LoginFailed(f"неверный пароль {C.password} для документа {C.doc_number}")],
    )
    session = SessionManager(authenticator, clock, settings.relogin_max_per_hour)
    composer = MessageComposer(settings.tz, settings.agenda_url, settings.tramite_id)
    alerts = AlertOrchestrator(
        notifiers,
        policies,
        composer,
        book,
        session,
        runtime,
        clock,
        NoopCoordinator(),
        core.journal,
    )
    _wire(bus, alerts, book)
    JournalFeed(core.journal, clock).subscribe(bus)

    async def via_api(t: WatchTarget) -> AvailabilitySnapshot:
        return await api.fetch(t)

    def fail(error: Exception) -> Step:
        async def step(_: WatchTarget) -> AvailabilitySnapshot:
            raise error

        return step

    async def empty(_: WatchTarget) -> AvailabilitySnapshot:
        return snapshot(frozenset())

    async def found(_: WatchTarget) -> AvailabilitySnapshot:
        return snapshot(slots(23, 24))

    source = ChainSource(
        [
            via_api,  # HTML с эхом секретов → карантин
            via_api,  # 429 + Set-Cookie
            via_api,  # JSON неожиданного формата → карантин
            empty,  # успешная проверка: состояние снова RUNNING, дальше сбои дадут BACKOFF
            fail(SiteUnavailable(f"таймаут {C.telegram_token} тема {C.ntfy_topic}")),
            fail(SiteUnavailable(f"сеть недоступна для {C.password}")),
            fail(SessionExpired(f"страница входа, документ {C.doc_number}")),
            fail(SessionExpired("страница входа")),
            found,
        ]
    )
    loop = PollLoop(
        primary=source,  # type: ignore[arg-type]
        fallback=None,
        session=session,
        detector=Detector(),
        bus=bus,
        clock=clock,
        runtime=runtime,
        runtime_store=RuntimeStore(storage.kv),
        stats=storage.stats,
        event_log=storage.events,
        target=target,
        jitter=0.0,
        journal=core.journal,
    )
    states: list[str] = []
    for _ in range(9):
        await loop._tick()
        states.append(loop.status.state.value)
        clock.advance(20)
        await settle()
    await bus.drain(10)
    await loop._flush_hour()
    await alerts.close()
    liveness = LivenessMonitor(loop, core.journal, clock, alerts.service, _noop)
    await liveness.tick()

    logging.getLogger("rcbot.scenario").error("итог сценария: %s", loop.status.last_error)
    await api.close()
    for notifier in (notifiers[Channel.NTFY], notifiers[Channel.CALL]):
        await notifier.close()
    await storage.close()
    core.close()
    logging.shutdown()
    requests = server.requests
    return Artifacts(
        root=tmp_path,
        paths=paths,
        core=core,
        owner_texts=[alert.text for alert, _ in telegram.sent],
        ntfy=[r for r in requests if r.method == "POST"],
        call=[r for r in requests if r.path == "/start.php"],
        api=[r for r in requests if r.path == "/api/disp"],
        states=states,
    )


async def _noop() -> None:
    return None
