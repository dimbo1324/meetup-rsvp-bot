"""Точка входа: `python -m meetup_bot`."""

from __future__ import annotations

import asyncio
import logging
import sys

from meetup_bot import __version__
from meetup_bot.app import run
from meetup_bot.config import load_settings


def main() -> int:
    config = load_settings()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("meetup_bot").info("meetup-rsvp-bot %s: запуск (%s)", __version__, config.env)
    loop_factory = None
    if sys.platform == "linux":
        try:
            import uvloop

            loop_factory = uvloop.new_event_loop
        except ImportError:
            pass
    try:
        asyncio.run(run(config), loop_factory=loop_factory)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
