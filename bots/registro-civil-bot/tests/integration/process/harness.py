"""Стенд для `run_app`: настоящие база, цикл опроса, супервизор и рассылка; снаружи — фейки.

Подменяются только границы с внешним миром: Telegram (`Bot`, `run_polling`), каналы, источник
календаря, вход на сайт. Всё запоминает порядок событий в `rig.order` — по нему проверяется
порядок запуска и остановки. Стенд работает и внутри теста (через `monkeypatch.setattr`), и в
дочернем процессе (через обычный `setattr`).
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import keyring
from tests.fakes import (
    FakeAuthenticator,
    FakeNotifier,
    FakeSource,
    ManualClock,
    RecordingJournal,
)

from rcbot import runner
from rcbot.application.alerting import AlertOrchestrator
from rcbot.application.bus import EventBus
from rcbot.application.session_manager import SessionManager
from rcbot.application.supervisor import Supervisor
from rcbot.bootstrap import Core, Storage, build_core
from rcbot.config.secrets import SecretKey, SecretStore
from rcbot.domain.models import Alert, AlertKind, Channel, DeliveryResult

TOKEN = "123456789:AAH-synthetic-token-abcdefghijklmnopqrstuvwxyz0123"
CHAT_ID = "777"

Patch = Callable[[Any, str, Any], None]


class OrderedJournal(RecordingJournal):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    def event(self, code: str, message: str, level: str = "INFO", **data: object) -> None:
        self._order.append(f"journal:{code}")
        super().event(code, message, level, **data)

    def enforce_quota(self) -> tuple[int, int]:
        self._order.append("journal.quota")
        return super().enforce_quota()


class OrderedNotifier(FakeNotifier):
    def __init__(self, channel: Channel, rig: Rig) -> None:
        super().__init__(channel)
        self._rig = rig

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        if alert.kind is AlertKind.SERVICE:
            self._rig.order.append(f"msg:{alert.text}")
            if self._rig.sigint_on_started and alert.text.startswith("▶️"):
                asyncio.get_running_loop().call_later(0.1, signal.raise_signal, signal.SIGINT)
        return await super().send(alert, attempt)

    async def close(self) -> None:
        self._rig.order.append(f"notifier.close:{self.channel.value}")
        await super().close()


class OrderedSource(FakeSource):
    def __init__(self, rig: Rig, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rig = rig

    async def fetch(self, target: Any) -> Any:
        if not self.calls:
            self._rig.order.append("poll.first-fetch")
        return await super().fetch(target)

    async def close(self) -> None:
        self._rig.order.append("source.close")
        await super().close()


class FakeApi:
    configured = False

    async def close(self) -> None:
        return None


class FakeBot:
    def __init__(self, rig: Rig, token: str) -> None:
        self.token = token
        self.session = type("Session", (), {"close": self._close})()
        self._rig = rig

    async def _close(self) -> None:
        self._rig.order.append("bot.session.close")


class FakeWatchdog:
    def __init__(self, rig: Rig, url: str) -> None:
        self.url = url
        self._rig = rig
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        return True

    async def close(self) -> None:
        self._rig.order.append("watchdog.close")


@dataclass
class Polling:
    """Поведение подставного long polling Telegram: по запуску из списка, затем «висит»."""

    script: list[str] = field(default_factory=list)  # "crash" | "return" | "forever"
    starts: int = 0

    async def run(self, rig: Rig, bot: Any, dispatcher: Any) -> None:
        self.starts += 1
        rig.order.append(f"telegram.start:{self.starts}")
        mode = self.script.pop(0) if self.script else "forever"
        if mode == "crash":
            raise RuntimeError("поллинг упал")
        if mode == "return":
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            rig.order.append("telegram.cancelled")
            raise


@dataclass
class Rig:
    core: Core
    clock: ManualClock
    journal: OrderedJournal
    order: list[str]
    polling: Polling
    dom: OrderedSource = field(init=False)
    notifiers: dict[Channel, OrderedNotifier] = field(default_factory=dict)
    session: SessionManager | None = None
    auth: FakeAuthenticator = field(default_factory=FakeAuthenticator)
    alerts: AlertOrchestrator | None = None
    storage: Storage | None = None
    bots: list[FakeBot] = field(default_factory=list)
    watchdogs: list[FakeWatchdog] = field(default_factory=list)
    sigint_on_started: bool = False
    task: asyncio.Task[Any] | None = None
    dispatcher_args: list[tuple[Any, ...]] = field(default_factory=list)

    async def until(self, condition: Callable[[], bool], what: str, seconds: float = 15.0) -> None:
        """Ждёт настоящим временем (мелкими паузами): база и потоки работают в реальном времени."""
        deadline = time.monotonic() + seconds
        while not condition():
            if self.task is not None and self.task.done() and not self.task.cancelled():
                self.task.result()  # run_app упал: показываем настоящую причину, а не таймаут
            if time.monotonic() > deadline:
                raise AssertionError(f"не дождались: {what}; порядок: {self.order}")
            await asyncio.sleep(0.005)

    async def started(self) -> None:
        await self.until(lambda: "journal:bot.start" in self.order, "запуск бота")
        await self.until(lambda: any(m.startswith("msg:▶️") for m in self.order), "STARTED")

    def tasks(self) -> set[str]:
        return {t.get_name() for t in asyncio.all_tasks() if not t.done()}

    def sent_texts(self, channel: Channel = Channel.TELEGRAM) -> list[str]:
        return [alert.text for alert, _ in self.notifiers[channel].sent]


def build_rig(
    patch: Patch,
    *,
    with_secrets: bool = True,
    script: list[Any] | None = None,
    channels: tuple[Channel, ...] = (Channel.TELEGRAM, Channel.NTFY),
) -> Rig:
    core = build_core(console=False)
    clock = ManualClock()
    order: list[str] = []
    journal = OrderedJournal(order)
    core.clock = clock  # type: ignore[assignment]
    core.journal = journal
    if with_secrets:
        store = SecretStore()
        store.set(SecretKey.TELEGRAM_TOKEN, TOKEN)
        store.set(SecretKey.TELEGRAM_CHAT_ID, CHAT_ID)
    rig = Rig(core, clock, journal, order, Polling())
    rig.dom = OrderedSource(rig, script if script is not None else [frozenset()], clock=clock)
    rig.notifiers = {channel: OrderedNotifier(channel, rig) for channel in channels}

    def make_bot(token: str) -> FakeBot:
        bot = FakeBot(rig, token)
        rig.bots.append(bot)
        return bot

    def make_session(_: Core) -> SessionManager:
        rig.session = SessionManager(rig.auth, clock, 6)
        return rig.session

    class RecordingBus(EventBus):
        async def drain(self, wait_sec: float = 10.0) -> None:
            order.append("bus.drain")
            await super().drain(wait_sec)

    class RecordingAlerts(AlertOrchestrator):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            rig.alerts = self

        async def close(self) -> None:
            order.append("alerts.close")
            await super().close()

    class RecordingSupervisor(Supervisor):
        def start(self, name: str, factory: Any, *, restart: bool = True) -> None:
            order.append(f"start:{name}")
            super().start(name, factory, restart=restart)

    real_open_storage = runner.open_storage

    async def open_storage(core_: Core) -> Storage:
        storage = await real_open_storage(core_)
        real_close = storage.close

        async def close() -> None:
            order.append("storage.close")
            await real_close()

        storage.close = close  # type: ignore[method-assign]
        rig.storage = storage
        return storage

    async def run_polling(bot: Any, dispatcher: Any) -> None:
        await rig.polling.run(rig, bot, dispatcher)

    def make_watchdog(_http: Any, url: str) -> FakeWatchdog:
        watchdog = FakeWatchdog(rig, url)
        rig.watchdogs.append(watchdog)
        return watchdog

    def make_dispatcher(*args: Any) -> object:
        # Настоящий Dispatcher вешает на себя маршрутизаторы уровня модуля: второй вызов в одном
        # процессе невозможен, а сборка Telegram-слоя проверяется в тестах bot/
        rig.dispatcher_args.append(args)
        return object()

    patch(runner, "build_dispatcher", make_dispatcher)
    patch(runner, "Bot", make_bot)
    patch(runner, "run_polling", run_polling)
    patch(runner, "_notifiers", lambda core_, bot, chat_id: rig.notifiers)
    patch(runner, "build_session", make_session)
    patch(runner, "build_api_source", lambda core_: FakeApi())
    patch(runner, "build_dom_source", lambda core_: rig.dom)
    patch(runner, "EventBus", RecordingBus)
    patch(runner, "Supervisor", RecordingSupervisor)
    patch(runner, "AlertOrchestrator", RecordingAlerts)
    patch(runner, "open_storage", open_storage)
    patch(runner, "HttpWatchdog", make_watchdog)
    return rig


def install_memory_keyring() -> None:
    """Для дочернего процесса: настоящий Credential Manager тесты не видят и не меняют."""
    from tests.conftest import MemoryKeyring

    keyring.set_keyring(MemoryKeyring())
