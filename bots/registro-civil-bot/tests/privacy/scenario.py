"""Сквозной сценарий для проверки утечек: источник, снимки страницы, журнал, БД и каналы.

Собирается настоящий цикл опроса, рассылка, журнал на рабочем столе, БД SQLite и `SaeSource` на
локальном TLS-сервере; Telegram — фейк, который запоминает текст владельцу. Во все места, откуда
текст попадает наружу, подсовываются «канарейки» (`helpers.Canaries`): в тексты ошибок, в тела
ответов сайта, в заголовки. Кто поймал канарейку — тот нашёл утечку.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import web

from rcbot.application.alerting import AlertOrchestrator, RepeatPolicy
from rcbot.application.bus import EventBus
from rcbot.application.incidents import IncidentBook
from rcbot.application.journal_feed import JournalFeed
from rcbot.application.liveness import LivenessMonitor
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import NoopCoordinator
from rcbot.application.runtime import RuntimeSettings, RuntimeStore
from rcbot.bootstrap import Core, build_core, ensure_target, open_storage
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretStore
from rcbot.domain.detector import Detector
from rcbot.domain.errors import SiteUnavailable, UnexpectedResponse
from rcbot.domain.models import AvailabilitySnapshot, Channel, SourceKind, WatchTarget
from rcbot.infrastructure.gubuy.sae_source import SaeSource
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.logging.redaction import REDACTOR
from rcbot.infrastructure.notify import callmebot
from rcbot.infrastructure.notify.callmebot import CallMeBotNotifier
from rcbot.infrastructure.notify.ntfy import NtfyNotifier
from rcbot.runner import _wire
from tests.fakes import (
    T0,
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
SAE_PATH = "/sae/agendarReserva/Paso1.xhtml"


@dataclass
class Artifacts:
    root: Path
    paths: Paths
    core: Core
    owner_texts: list[str]
    ntfy: list[RecordedRequest]
    call: list[RecordedRequest]
    site: list[RecordedRequest]
    states: list[str] = field(default_factory=list)

    @property
    def journal_dir(self) -> Path:
        return Path(self.core.settings.journal_dir)

    def database_files(self) -> list[Path]:
        return sorted(self.paths.data.glob("rcbot.sqlite3*"))


class ChainSource:
    """Источник, который отвечает по списку шагов: ответ сайта, ошибка, снимок с местами."""

    kind = SourceKind.SAE

    def __init__(self, steps: list[Step]) -> None:
        self._steps = steps

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        return await self._steps.pop(0)(target)

    async def close(self) -> None:
        """Ресурсов не держит."""


def hostile_html() -> str:
    """Страница, в которую сайт «эхом» вернул всё секретное: куки, токены, номера, контакты."""
    return (
        "<html><body>Error\n"
        f"Set-Cookie: JSESSIONID={C.cookie_value}; Path=/\n"
        f"Cookie: TS0191f197={C.cookie_value}\n"
        f"token={C.telegram_token}\n"
        f"usuario {C.call_user}, chat {C.chat_id}\n"
        f"заявка {C.tramite}, этап /etapas/ejecutar/{C.etapa}/1, тема {C.ntfy_topic}\n"
        "correo ivan.canary@example.com, +598 99 123 456, 1234567890123\n"
        "</body></html>"
    )


def prime_redactor(store: SecretStore | None = None) -> None:
    """Как при запуске: чтение секретов из keyring регистрирует их в фильтре."""
    store = store or SecretStore(on_read=REDACTOR.register)
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

    # Ответы «сайта»: чужая страница с эхом секретов, 429 с cookie в заголовке, 503 с секретом
    answers = [
        (200, "text/html", hostile_html(), {}),
        (
            429,
            "text/plain",
            f"slow down {C.telegram_token}",
            {"Set-Cookie": f"JSESSIONID={C.cookie_value}"},
        ),
        (503, "text/html", f"<p>caído {C.ntfy_topic}</p>", {}),
    ]
    served = {"n": 0}

    async def site_handler(_: web.Request) -> web.StreamResponse:
        status, ctype, body, headers = answers[min(served["n"], len(answers) - 1)]
        served["n"] += 1
        return web.Response(status=status, text=body, content_type=ctype, headers=headers)

    server.route("GET", SAE_PATH, site_handler)
    server.respond("POST", "/", text="ok")
    server.respond("GET", "/start.php", text="Call queued")
    monkeypatch.setattr(callmebot, "API_URL", server.url("/start.php"))

    sae_http = SafeHttp({"127.0.0.1"}, 5, ssl_context=server.client_ssl)
    sae = SaeSource(sae_http, server.url(SAE_PATH), paths.shots, clock)

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
    composer = MessageComposer(settings.tz, settings.agenda_url, settings.tramite_id)
    alerts = AlertOrchestrator(
        notifiers,
        policies,
        composer,
        book,
        runtime,
        clock,
        NoopCoordinator(),
        core.journal,
    )
    _wire(bus, alerts, book)
    JournalFeed(core.journal, clock).subscribe(bus)

    async def via_site(t: WatchTarget) -> AvailabilitySnapshot:
        return await sae.fetch(t)

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
            via_site,  # чужая страница с эхом секретов → снимок для разбора
            via_site,  # 429 + Set-Cookie
            via_site,  # 503 с секретом в теле
            empty,  # успешная проверка: состояние снова RUNNING, дальше сбои дадут BACKOFF
            fail(SiteUnavailable(f"таймаут {C.telegram_token} тема {C.ntfy_topic}")),
            fail(SiteUnavailable(f"сеть недоступна для {C.call_user}")),
            fail(UnexpectedResponse("страница не распознана")),
            fail(UnexpectedResponse(f"странный ответ, чат {C.chat_id}")),
            found,
        ]
    )
    loop = PollLoop(
        source=source,  # type: ignore[arg-type]
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
    await sae.close()
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
        site=[r for r in requests if r.path == SAE_PATH],
        states=states,
    )


async def _noop() -> None:
    return None
