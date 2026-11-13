"""CLI `rcbot` (SPEC 4): разбор аргументов и выбор команды. Сами команды — в модулях пакета."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from rcbot import __version__
from rcbot.cli.exit_codes import EXIT_INTERRUPTED, EXIT_RUNNING, EXIT_SETTINGS
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretMissing

logger = logging.getLogger(__name__)

Handler = Callable[[argparse.Namespace], int]

__all__ = ["EXIT_INTERRUPTED", "EXIT_RUNNING", "EXIT_SETTINGS", "main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rcbot", description="Наблюдатель за записью в Registro Civil (только чтение)."
    )
    parser.add_argument("--version", action="version", version=f"rcbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="команда")

    sub.add_parser("init", help="мастер: секреты в Credential Manager, тема ntfy, chat_id")
    doctor = sub.add_parser("doctor", help="проверка окружения, браузера, секретов, хостов")
    doctor.add_argument("--full", action="store_true", help="ещё и pip-audit зависимостей")
    doctor.add_argument("--offline", action="store_true", help="без проверки доступа к хостам")
    sub.add_parser("discover", help="разведка: записать очищенные запросы шага «Agenda»")
    login = sub.add_parser("login", help="войти на сайт")
    login.add_argument(
        "--check", action="store_true", help="только проверить вход и доступ к шагу 2"
    )
    check = sub.add_parser("check", help="проверить календарь")
    # Флаг оставлен для совместимости со скриптами и SPEC 4.5: проверка и так одна
    check.add_argument(
        "--once", action="store_true", help="одна проверка — по умолчанию; флаг не обязателен"
    )
    sub.add_parser("run", help="постоянная работа")
    simulate = sub.add_parser("simulate", help="тестовый инцидент во все каналы")
    simulate.add_argument("--wait", type=int, default=600, help="сколько ждать «Вижу», секунд")
    sub.add_parser("purge", help="удалить логи, временные файлы, карантин, снимки, кеш")
    sub.add_parser("uninstall", help="удалить секреты и задание Планировщика")
    return parser


def _handlers() -> dict[str, Handler]:
    # Импорт по требованию: `rcbot doctor` не тянет Playwright и aiogram без нужды
    from rcbot.cli import maintenance, operate, setup

    return {
        "init": setup.cmd_init,
        "doctor": setup.cmd_doctor,
        "discover": operate.cmd_discover,
        "login": operate.cmd_login,
        "check": operate.cmd_check,
        "run": operate.cmd_run,
        "simulate": operate.cmd_simulate,
        "purge": maintenance.cmd_purge,
        "uninstall": maintenance.cmd_uninstall,
    }


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # pythonw.exe: потоков нет; консоль Windows: вывод по-русски без падения на cp1252
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        return _handlers()[args.command](args)
    except SecretMissing as error:
        _fail(f"Ошибка: {error}")
        return EXIT_SETTINGS
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        _fail(f"Ошибка настроек в .env: {problems}")
        return EXIT_SETTINGS
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as error:
        # Под pythonw.exe (Планировщик) консоли нет: причина падения должна остаться в логе
        logger.critical("необработанная ошибка в команде %s", args.command, exc_info=True)
        print(
            f"Неожиданная ошибка {type(error).__name__}: подробности в {Paths.default().log_file}",
            file=sys.stderr,
        )
        return 1


def _fail(message: str) -> None:
    logger.error(message)
    print(message, file=sys.stderr)
