"""Сборка и запуск бота целиком: `rcbot run` и `rcbot simulate`.

Порядок остановки (SPEC 3.4): опрос → сообщение «остановлен» → фоновые задачи → подписчики
событий → рассылка → источники и браузер → сторож и каналы → Telegram → БД.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher

from rcbot.application.alerting import AlertOrchestrator, RepeatPolicy
from rcbot.application.bus import EventBus
from rcbot.application.control import Controller
from rcbot.application.health import HealthMonitor
from rcbot.application.housekeeping import Housekeeping
from rcbot.application.incidents import IncidentBook
from rcbot.application.journal_feed import JournalFeed
from rcbot.application.liveness import LivenessMonitor
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import NoopCoordinator, Notifier
from rcbot.application.runtime import RuntimeStore
from rcbot.application.supervisor import Supervisor
from rcbot.bootstrap import (
    Core,
    build_session,
    build_sources,
    ensure_target,
    open_storage,
)
from rcbot.bot import texts
from rcbot.bot.app import build_dispatcher, run_polling
from rcbot.config.secrets import SecretKey
from rcbot.config.settings import CALLMEBOT_HOST
from rcbot.domain.detector import Detector
from rcbot.domain.events import (
    ManualActionRequired,
    PollerStateChanged,
    SlotsAppeared,
    SlotsChanged,
    SlotsGone,
    SourceSwitched,
    UnconfirmedSlotsSeen,
)
from rcbot.domain.models import Channel, DeliveryResult
from rcbot.infrastructure.files import FileJanitor
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.notify.callmebot import CALLMEBOT_TIMEOUT_SEC, CallMeBotNotifier
from rcbot.infrastructure.notify.ntfy import NTFY_TIMEOUT_SEC, NtfyNotifier
from rcbot.infrastructure.notify.telegram import TelegramNotifier
from rcbot.infrastructure.notify.watchdog import HttpWatchdog

logger = logging.getLogger(__name__)

STOP_MESSAGE_TIMEOUT_SEC = 10.0
STOP_POLL_TIMEOUT_SEC = 5.0
WATCHDOG_TIMEOUT_SEC = 10.0
# Код ошибки настроек — тот же, что rcbot.cli.EXIT_SETTINGS (слой runner не импортирует cli)
EXIT_SETTINGS = 2
AGENDA_NOT_CONFIGURED = (
    "Ошибка настроек в .env: RCBOT_AGENDA_URL — вместо <НОМЕР_ЭТАПА> нужен номер этапа "
    "(впишите его через `rcbot init` или в .env)."
)


@dataclass
class Closers:
    """Что закрыть при остановке: в порядке, обратном добавлению (как ExitStack).

    Закрытие добавляется сразу после создания ресурса: сбой сборки посередине закроет всё, что
    уже открыто, а зависимые ресурсы закроются раньше тех, от которых зависят.
    """

    items: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)

    def add(self, closer: Callable[[], Awaitable[Any]]) -> None:
        self.items.append(closer)

    async def close_all(self) -> None:
        interrupted: asyncio.CancelledError | None = None
        for closer in reversed(self.items):
            try:
                await closer()
            except asyncio.CancelledError as error:
                # Отмена посреди закрытия не должна оставить остальные ресурсы открытыми
                interrupted = error
            except Exception:
                logger.exception("ошибка при закрытии ресурса")
        if interrupted is not None:
            raise interrupted


async def _polling_forever(bot: Bot, dispatcher: Dispatcher) -> None:
    await run_polling(bot, dispatcher)
    # Поллинг не должен возвращаться сам: тихий выход оставил бы бота глухим к владельцу
    raise RuntimeError("long polling Telegram завершился без команды остановки")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _notifiers(core: Core, bot: Bot, chat_id: int) -> dict[Channel, Notifier]:
    # У каждого канала свой список хостов: ошибка в адресе одного не пустит его к хосту другого
    s = core.settings
    notifiers: dict[Channel, Notifier] = {Channel.TELEGRAM: TelegramNotifier(bot, chat_id)}
    if topic := core.secrets.get(SecretKey.NTFY_TOPIC):
        http = SafeHttp([_host(s.ntfy_server)], timeout_sec=NTFY_TIMEOUT_SEC)
        notifiers[Channel.NTFY] = NtfyNotifier(http, s.ntfy_server, topic.get_secret_value())
    if user := core.secrets.get(SecretKey.CALLMEBOT_USER):
        http = SafeHttp([CALLMEBOT_HOST], timeout_sec=CALLMEBOT_TIMEOUT_SEC)
        notifiers[Channel.CALL] = CallMeBotNotifier(http, user.get_secret_value(), s.call_lang)
    return notifiers


def _policies(core: Core) -> dict[Channel, RepeatPolicy]:
    s = core.settings
    return {
        Channel.TELEGRAM: RepeatPolicy(s.tg_repeat_sec, s.tg_repeat_max),
        Channel.NTFY: RepeatPolicy(s.ntfy_repeat_sec, s.ntfy_repeat_max),
        Channel.CALL: RepeatPolicy(s.call_repeat_sec, s.call_repeat_max),
    }


async def run_app(
    core: Core,
    *,
    simulate_wait_sec: int | None = None,
    on_delivery: Callable[[DeliveryResult], None] | None = None,
) -> int:
    s = core.settings
    if not s.agenda_configured:
        # Без номера этапа опрос шёл бы на адрес-метку и бесконечно отступал: это ошибка настроек
        logger.error(AGENDA_NOT_CONFIGURED)
        return EXIT_SETTINGS
    clock = core.clock
    closers = Closers()
    supervisor = Supervisor(clock)
    loop: PollLoop | None = None
    alerts: AlertOrchestrator | None = None
    announce_stop = False
    storage = await open_storage(core)
    try:
        token = core.secrets.require(SecretKey.TELEGRAM_TOKEN).get_secret_value()
        chat_id = int(core.secrets.require(SecretKey.TELEGRAM_CHAT_ID).get_secret_value())
        runtime_store = RuntimeStore(storage.kv)
        runtime = await runtime_store.load(s.poll_interval_sec, s.months_ahead, clock.now())
        # Глубина поиска действует из настроек Telegram (kv): .env не перетирает её при запуске
        target = await ensure_target(core, storage, runtime.months_ahead)

        bot = Bot(token)
        closers.add(bot.session.close)
        notifiers = _notifiers(core, bot, chat_id)
        for notifier in notifiers.values():
            closers.add(notifier.close)
        watchdog = None
        if s.watchdog_url:
            http = SafeHttp([_host(s.watchdog_url)], timeout_sec=WATCHDOG_TIMEOUT_SEC)
            watchdog = HttpWatchdog(http, s.watchdog_url)
            closers.add(watchdog.close)
        session = build_session(core)
        primary, fallback = await build_sources(core)
        sources = [src for src in (primary, fallback) if src is not None]
        for source in sources:
            closers.add(source.close)
            session.subscribe(source.use_cookies)

        book = IncidentBook(storage.incidents, clock, target.id)
        detector = Detector()
        if (restored := await book.restore()) is not None:
            detector.restore(restored.slots)
            logger.info("продолжается открытый инцидент %d", restored.id)

        bus = EventBus()
        composer = MessageComposer(s.tz, s.agenda_url, s.tramite_id)
        alerts = AlertOrchestrator(
            notifiers,
            _policies(core),
            composer,
            book,
            session,
            runtime,
            clock,
            NoopCoordinator(),
            journal=core.journal,
        )
        closers.add(alerts.close)
        # Добавлено после рассылки — закроется раньше неё: события дойдут до ещё открытой рассылки
        closers.add(bus.drain)
        if on_delivery is not None:
            alerts.delivery_listeners.append(on_delivery)
        loop = PollLoop(
            primary=primary,
            fallback=fallback,
            session=session,
            detector=detector,
            bus=bus,
            clock=clock,
            runtime=runtime,
            runtime_store=runtime_store,
            stats=storage.stats,
            event_log=storage.events,
            target=target,
            jitter=s.poll_jitter,
            journal=core.journal,
        )
        poll = loop
        _wire(bus, alerts, book)
        JournalFeed(core.journal, clock).subscribe(bus)

        housekeeping = Housekeeping(
            storage.maintenance, FileJanitor(core.paths), storage.kv, clock, core.journal
        )

        async def restart_poll() -> None:
            # Закрытый браузер прерывает зависшую проверку, затем цикл запускается заново
            await poll.reset_source()
            await supervisor.restart("poll")

        async def liveness_extra() -> dict[str, object]:
            incident = await book.current()
            days = sorted({x.day.isoformat() for x in incident.slots}) if incident else []
            return {
                "incident_open": incident is not None,
                "incident_days": days,
                "alert_repeating": alerts.repeating() if alerts else False,
                "logins_last_hour": session.logins_last_hour(),
            }

        liveness = LivenessMonitor(
            loop, core.journal, clock, alerts.service, restart_poll, liveness_extra
        )
        controller = Controller(
            loop,
            alerts,
            book,
            storage.incidents,
            storage.events,
            session,
            clock,
            cleanup=housekeeping.run_once,
            folder_size=housekeeping.folder_size,
            journal=core.journal,
            journal_size=core.journal.size,
            liveness=liveness.diagnose,
        )
        health = HealthMonitor(
            storage.stats,
            storage.incidents,
            clock,
            s.tz,
            s.daily_report_hour,
            alerts.service,
            loop.current_hour,
            housekeeping.folder_size,
            watchdog,
        )
        dispatcher = build_dispatcher(controller, s.tz, chat_id)

        housekeeping.start()
        supervisor.start("telegram", lambda: _polling_forever(bot, dispatcher))
        supervisor.start("warmup", alerts.keep_warm)
        if simulate_wait_sec is not None:
            announce_stop = True
            return await _simulate(alerts, clock_sleep=clock.sleep, wait_sec=simulate_wait_sec)

        await housekeeping.journal_quota_once()
        supervisor.start("housekeeping", housekeeping.run_forever)
        supervisor.start("journal-quota", housekeeping.journal_quota_forever)
        supervisor.start("reports", health.daily_reports)
        supervisor.start("watchdog", health.watchdog)
        supervisor.start("poll", loop.run)
        supervisor.start("liveness", liveness.run)
        # Фоновые задачи запущены: с этого момента остановка сообщается владельцу. Флаг ставится до
        # отправки «запущен»: отмена, пришедшая во время неё, тоже должна оставить сообщение
        announce_stop = True
        interval = loop.current_interval(clock.now())
        core.journal.event(
            "bot.start", f"бот запущен: источник {primary.kind}, интервал ≈ {interval:.0f} с"
        )
        await alerts.service(
            texts.STARTED.format(source=primary.kind.value.upper(), interval=interval)
        )
        # Работает до остановки процесса (Ctrl+C или Планировщик); упавшие задачи перезапускает
        # супервизор, зависший цикл опроса — монитор живости
        await asyncio.Future()
        return 0
    finally:
        try:
            await _shutdown(loop, alerts if announce_stop else None, supervisor, closers)
        finally:
            # База и запись об остановке — даже если закрытие ресурсов прервано отменой
            await storage.close()
            core.journal.event("bot.stop", "бот остановлен")


def _wire(bus: EventBus, alerts: AlertOrchestrator, book: IncidentBook) -> None:
    # Рассылка и запись в БД — разные подписчики: тревога не ждёт базу (SPEC 2.2, п. 4)
    bus.subscribe(SlotsAppeared, alerts.on_appeared)
    bus.subscribe(SlotsAppeared, book.on_appeared)
    bus.subscribe(SlotsChanged, alerts.on_changed)
    bus.subscribe(SlotsChanged, book.on_changed)
    bus.subscribe(SlotsGone, alerts.on_gone)
    bus.subscribe(SlotsGone, book.on_gone)
    bus.subscribe(ManualActionRequired, alerts.on_manual)
    bus.subscribe(SourceSwitched, alerts.on_source_switched)
    bus.subscribe(PollerStateChanged, alerts.on_state_changed)
    bus.subscribe(UnconfirmedSlotsSeen, alerts.on_unconfirmed)


async def _simulate(
    alerts: AlertOrchestrator, clock_sleep: Callable[[float], Awaitable[None]], wait_sec: int
) -> int:
    """Тестовая тревога со всеми повторами; успех — если владелец нажал «Вижу» до таймаута."""
    logger.info(
        "Тестовая тревога отправлена. Нажмите «✅ Вижу» в Telegram (жду до %d с).", wait_sec
    )
    await alerts.simulate()
    waited = 0
    while alerts.repeating() and waited < wait_sec:
        await clock_sleep(1)
        waited += 1
    if alerts.repeating():
        logger.warning("«Вижу» не нажата: повторы остановлены по таймауту.")
        return 1
    logger.info("«Вижу» получено: повторы во всех каналах остановлены.")
    return 0


async def _shutdown(
    loop: PollLoop | None,
    alerts: AlertOrchestrator | None,
    supervisor: Supervisor,
    closers: Closers,
) -> None:
    if loop is not None:
        loop.stop()
        # Цикл опроса досохраняет статистику часа: отмена посреди записи потеряла бы её
        await supervisor.wait_done("poll", STOP_POLL_TIMEOUT_SEC)
    if alerts is not None:
        with contextlib.suppress(Exception):
            async with asyncio.timeout(STOP_MESSAGE_TIMEOUT_SEC):
                await alerts.service(texts.STOPPED)
    await supervisor.stop()
    await closers.close_all()
