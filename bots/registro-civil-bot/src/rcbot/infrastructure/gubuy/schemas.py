"""Модель ответа `agenda_sae_api_disponibilidades` — заглушка до разведки (решение D1, R5).

Формат ответа станет известен после `rcbot discover` на этапе 3. До тех пор `ApiSource`
выключен (пустой `RCBOT_API_URL`), а бот работает через `DomSource`. Разбор нарочно отказывает:
лучше переход на запасной источник, чем тревога по неправильно понятому ответу (SPEC 6.3).
"""

from __future__ import annotations

from typing import Any

from rcbot.domain.errors import UnexpectedResponse
from rcbot.domain.models import Slot


def parse_disponibilidades(payload: Any) -> frozenset[Slot]:
    raise UnexpectedResponse("формат ответа API ещё не разобран: нужен дамп разведки (этап 3)")
