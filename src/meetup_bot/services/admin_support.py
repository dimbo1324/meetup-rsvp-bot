"""Общие мелочи сервисов: рендер уведомлений, провайдеры настроек, рассылка после коммита."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from meetup_bot.services.defaults import DEFAULT_TIMEZONE
from meetup_bot.services.ports import Notifier
from meetup_bot.services.safe import safe_send_dm
from meetup_bot.services.waitlist import Promotion
from meetup_bot.texts.defaults import DEFAULT_TEXTS, TEXT_PREFIX
from meetup_bot.texts.render import fill, text_variables

# (ключ текста без «text.», сырые параметры) -> готовая HTML-строка уведомления.
# Параметры: starts_at/deadline_at — моменты, place/name/reason/position — строки.
RenderText = Callable[[str, dict[str, Any]], str]

# Значение или синхронный провайдер: провайдер вызывается при каждой операции,
# поэтому смена настроек в админке подхватывается без перезапуска
type Provided[T] = T | Callable[[], T]

# Лимит Telegram — 4096 символов; с запасом на разметку
MESSAGE_CHUNK = 3500


def default_render_text(key: str, params: dict[str, Any]) -> str:
    """Рендер по текстам по умолчанию — для сборки без сервиса настроек (тесты, утилиты)."""
    template = DEFAULT_TEXTS[TEXT_PREFIX + key]
    return fill(template, text_variables(params, DEFAULT_TIMEZONE), escape_html=True)


def resolve[T](value: T | Callable[[], T]) -> T:
    """Значение либо результат синхронного провайдера (датаклассы настроек не вызываемы)."""
    return value() if callable(value) else value


def chunk_lines(lines: Iterable[str], limit: int = MESSAGE_CHUNK) -> list[str]:
    """Склеивает строки в сообщения не длиннее `limit`; строки не рвутся посередине."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def send_dms(notifier: Notifier, user_ids: Iterable[int], text: str) -> None:
    """Рассылка после коммита: сбой отправки не должен ломать уже сохранённую операцию."""
    for user_id in user_ids:
        await safe_send_dm(notifier, user_id, text)


async def notify_promotions(
    notifier: Notifier, render: RenderText, promotions: Sequence[Promotion]
) -> None:
    """Пишет продвинутым из очереди (5.5) — только тем, кто разрешил личные сообщения."""
    for promotion in promotions:
        if promotion.user_id is None or not promotion.dm_available:
            continue
        text = render(
            "promoted",
            {
                "starts_at": promotion.starts_at,
                "ends_at": promotion.ends_at,
                "place": promotion.place,
            },
        )
        await safe_send_dm(notifier, promotion.user_id, text)
