"""Точка входа: `python -m meetup_bot`."""

from __future__ import annotations

import logging

from meetup_bot import __version__
from meetup_bot.config import load_settings


def main() -> int:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper())
    log = logging.getLogger("meetup_bot")
    log.info("meetup-rsvp-bot %s: каркас готов, обработчики появятся в вехе M2", __version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
