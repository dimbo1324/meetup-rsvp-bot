"""Точка входа: `python -m meetup_bot`.

Коды выхода: 0 — штатная остановка, 1 — Telegram отклонил токен или недоступен,
2 — неполная или неверная конфигурация (`.env`).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from typing import Any

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from pydantic import ValidationError

from meetup_bot import __version__
from meetup_bot.app import run
from meetup_bot.config import Settings, load_settings

logger = logging.getLogger("meetup_bot")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def describe_config_error(error: ValidationError) -> str:
    """Список проблем `.env` без значений: в них могут быть секреты."""
    problems = []
    for item in error.errors():
        name = str(item["loc"][0]).upper() if item["loc"] else "?"
        reason = "не задана" if item["type"] == "missing" else item["msg"]
        problems.append(f"{name}: {reason}")
    return "Конфигурация неполная или неверная (.env): " + "; ".join(problems)


def _loop_factory() -> Callable[[], Any] | None:
    # Проверка платформы должна быть видна mypy статически: на Windows uvloop не ставится
    if sys.platform == "linux":
        try:
            import uvloop

            return uvloop.new_event_loop
        except ImportError:
            return None
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        config: Settings = load_settings()
    except ValidationError as error:
        logger.error(describe_config_error(error))
        return 2
    logging.getLogger().setLevel(config.log_level.upper())
    logger.info("meetup-rsvp-bot %s: запуск (%s)", __version__, config.env)
    try:
        asyncio.run(run(config), loop_factory=_loop_factory())
    except KeyboardInterrupt:
        return 0
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил BOT_TOKEN: проверьте токен в .env (BotFather → /token).")
        return 1
    except TelegramNetworkError as error:
        logger.error("Нет связи с Telegram: %s", error.message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
