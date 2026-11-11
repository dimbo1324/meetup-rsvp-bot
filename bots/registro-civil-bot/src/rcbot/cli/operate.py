"""Рабочие команды: discover, login, check, run, simulate."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from playwright.async_api import Error as PlaywrightError

from rcbot.application.ports import AvailabilitySource
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
from rcbot.domain.models import AvailabilitySnapshot, Slot
from rcbot.infrastructure.gubuy.discovery import DiscoveryRecorder
from rcbot.infrastructure.gubuy.pages import PageKind
from rcbot.infrastructure.gubuy.probe import probe_agenda

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


def _not_yet(name: str) -> int:
    print(f"Команда `rcbot {name}` ещё не реализована (этап 1 в работе).", file=sys.stderr)
    return 1


def cmd_discover(_: argparse.Namespace) -> int:
    core = build_core()
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


async def _check_once(core: Core) -> AvailabilitySnapshot:
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
            return await source.fetch(target)
        except SessionExpired:
            session.mark_expired()
            await session.relogin("сессия истекла во время проверки")
            return await source.fetch(target)
    finally:
        await source.close()


def cmd_login(args: argparse.Namespace) -> int:
    core = build_core()
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
    core = build_core()
    try:
        snapshot = asyncio.run(_check_once(core))
    except ManualStepRequired:
        print("Сайт требует шаг 1: пройдите его в своём браузере и повторите.", file=sys.stderr)
        return 1
    except RcbotError as error:
        print(f"Проверка не удалась: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    _print_snapshot(snapshot)
    return 0


def cmd_run(_: argparse.Namespace) -> int:
    return _not_yet("run")


def cmd_simulate(_: argparse.Namespace) -> int:
    return _not_yet("simulate")
