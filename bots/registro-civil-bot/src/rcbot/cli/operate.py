"""Рабочие команды: discover, login, check, run, simulate."""

from __future__ import annotations

import argparse
import asyncio
import sys

from rcbot.bootstrap import browser_options, build_authenticator, build_core
from rcbot.domain.errors import RcbotError
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
    return _not_yet("discover")


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
    return _not_yet("check")


def cmd_run(_: argparse.Namespace) -> int:
    return _not_yet("run")


def cmd_simulate(_: argparse.Namespace) -> int:
    return _not_yet("simulate")
