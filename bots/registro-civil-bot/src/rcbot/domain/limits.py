"""Жёсткие ограничения SPEC 0.3 (правило 3) и 2.3: не настраиваются ни `.env`, ни Telegram.

Живут в домене, чтобы одни и те же значения видели настройки (`config`) и опрос (`application`).
"""

from __future__ import annotations

MIN_POLL_INTERVAL_SEC = 10.0
MIN_DOM_POLL_INTERVAL_SEC = 60.0
MAX_POLL_INTERVAL_SEC = 3600.0
MIN_MONTHS_AHEAD = 1
MAX_MONTHS_AHEAD = 12
