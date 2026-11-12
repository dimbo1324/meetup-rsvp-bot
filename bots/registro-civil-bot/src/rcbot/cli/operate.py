"""Рабочие команды: discover, login, check, run, simulate."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from collections import defaultdict
from collections.abc import Callable

from playwright.async_api import Error as PlaywrightError

from rcbot.application.ports import AvailabilitySource, CheckRecord
from rcbot.bootstrap import (
    Core,
    browser_options,
    build_api_source,
    build_authenticator,
    build_core,
    build_dom_source,
    build_session,
    transient_target,
)
from rcbot.config.secrets import SecretKey
from rcbot.domain.errors import ManualStepRequired, RcbotError, SessionExpired
from rcbot.domain.models import AvailabilitySnapshot, DeliveryResult, Slot, SourceKind
from rcbot.infrastructure.gubuy.discovery import DiscoveryRecorder
from rcbot.infrastructure.gubuy.pages import PageKind
from rcbot.infrastructure.gubuy.probe import probe_agenda
from rcbot.infrastructure.win.power import KeepAwake
from rcbot.infrastructure.win.single_instance import AlreadyRunning, SingleInstance
from rcbot.runner import run_app

PROBE_MESSAGES = {
    PageKind.AGENDA: (True, "Шаг «Agenda» открыт напрямую: календарь доступен."),
    PageKind.STEP1: (
        False,
        "Сайт открыл шаг 1 с контрольным вопросом. Бот его не решает (D3): после каждого\n"
        "такого входа он будет звать вас кнопкой «Готово» в Telegram.",
    ),
    PageKind.LOGIN: (False, "Сайт вернул страницу входа: cookies не приняты."),
    PageKind.BLOCKED: (False, "Сайт ответил отказом защитного шлюза: подождите и не повторяйте."),
    PageKind.UNKNOWN: (False, "Страница не распознана: признаки шага «Agenda» не найдены."),
}


def cmd_discover(_: argparse.Namespace) -> int:
    core = build_core(command="discover")
    s = core.settings
    core.secrets.get(SecretKey.DOC_NUMBER)  # регистрирует номер документа в фильтре очистки
    recorder = DiscoveryRecorder(
        s.login_url,
        s.agenda_url,
        s.location_text,
        s.months_ahead,
        core.paths.discovery,
        browser_options(core, headless=False, channel=s.discover_channel),
    )
    try:
        result = asyncio.run(recorder.run(core.clock.now()))
    except (RcbotError, PlaywrightError) as error:
        print(f"Разведка не завершилась: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"\nЗаписано запросов: {len(result.records)} → {result.path}")
    print("\n".join(result.summary()))
    print("Файл очищен от cookies и личных данных; хранится 7 дней.")
    return 0


def _print_snapshot(snapshot: AvailabilitySnapshot) -> None:
    print(f"Источник: {snapshot.source.value}, проверка заняла {snapshot.latency_ms:.0f} мс.")
    if not snapshot.slots:
        print("Свободных мест нет.")
        return
    by_month: dict[tuple[int, int], list[Slot]] = defaultdict(list)
    for slot in sorted(snapshot.slots):
        by_month[(slot.day.year, slot.day.month)].append(slot)
    for (year, month), slots in sorted(by_month.items()):
        print(f"{month:02}.{year}: " + ", ".join(s.label() for s in slots))


async def _check_once(core: Core) -> tuple[SourceKind, AvailabilitySnapshot | RcbotError]:
    """Одна проверка; вид источника нужен и при ошибке: запись в журнале не должна его путать."""
    session = build_session(core)
    api = build_api_source(core)
    source: AvailabilitySource = api if api.configured else build_dom_source(core)
    if source is not api:
        await api.close()
    session.subscribe(source.use_cookies)
    target = transient_target(core)
    try:
        await session.ensure()
        try:
            return source.kind, await source.fetch(target)
        except SessionExpired:
            session.mark_expired()
            await session.relogin("сессия истекла во время проверки")
            return source.kind, await source.fetch(target)
    except RcbotError as error:
        return source.kind, error
    finally:
        await source.close()


def cmd_login(args: argparse.Namespace) -> int:
    core = build_core(command="login")
    try:
        cookies = asyncio.run(build_authenticator(core).login())
    except RcbotError as error:
        print(f"Вход не удался: {error}", file=sys.stderr)
        return 1
    print(f"Вход выполнен: {len(cookies)} cookies сессии (значения не показываются).")
    if not args.check:
        return 0
    kind = asyncio.run(probe_agenda(core.settings.agenda_url, cookies, browser_options(core)))
    ok, message = PROBE_MESSAGES[kind]
    print(message)
    return 0 if ok else 1


def cmd_check(_: argparse.Namespace) -> int:
    core = build_core(command="check")
    kind, outcome = asyncio.run(_check_once(core))
    if isinstance(outcome, RcbotError):
        core.journal.check(
            CheckRecord(
                core.clock.now(),
                kind,
                False,
                frozenset(),
                0.0,
                "check-once",
                0.0,
                type(outcome).__name__,
                str(outcome),
            )
        )
        if isinstance(outcome, ManualStepRequired):
            print("Сайт требует шаг 1: пройдите его в своём браузере и повторите.", file=sys.stderr)
        else:
            print(f"Проверка не удалась: {type(outcome).__name__}: {outcome}", file=sys.stderr)
        return 1
    core.journal.check(
        CheckRecord(
            outcome.observed_at,
            outcome.source,
            True,
            outcome.slots,
            outcome.latency_ms,
            "check-once",
            0.0,
        )
    )
    _print_snapshot(outcome)
    return 0


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """uvloop — только на Linux; на Windows нужен стандартный ProactorEventLoop (SPEC 2.1)."""
    if sys.platform != "linux":
        return None
    try:
        # Динамический импорт: на Windows пакета нет, и проверка типов не должна его искать
        uvloop = importlib.import_module("uvloop")
    except ImportError:
        return None
    factory: Callable[[], asyncio.AbstractEventLoop] = uvloop.new_event_loop
    return factory


def _exclusive(core: Core, body: Callable[[], int]) -> int:
    lock = SingleInstance(core.paths.lock_file)
    try:
        lock.acquire()
    except AlreadyRunning as error:
        print(f"{error}. Остановите работающий экземпляр.", file=sys.stderr)
        return 3
    try:
        with KeepAwake():
            return body()
    finally:
        lock.release()


def cmd_run(_: argparse.Namespace) -> int:
    # pythonw.exe (Планировщик) запускается без консоли: пишем только в файл лога
    core = build_core(console=sys.stderr is not None, command="run")
    return _exclusive(core, lambda: asyncio.run(run_app(core), loop_factory=_loop_factory()))


def _print_delivery(result: DeliveryResult) -> None:
    mark = "✅" if result.ok else "❌"
    error = f" — {result.error}" if result.error else ""
    channel, attempt = result.channel.value, result.attempt
    print(f"{mark} {channel}, попытка {attempt}: {result.latency_ms:.0f} мс{error}")


def cmd_simulate(args: argparse.Namespace) -> int:
    core = build_core(command="simulate")

    def body() -> int:
        return asyncio.run(
            run_app(core, simulate_wait_sec=args.wait, on_delivery=_print_delivery),
            loop_factory=_loop_factory(),
        )

    return _exclusive(core, body)
