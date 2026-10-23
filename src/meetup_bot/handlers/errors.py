"""Глобальный обработчик ошибок диспетчера."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Router
from aiogram.types import ErrorEvent
from aiogram_dialog.api.exceptions import (
    InvalidStackIdError,
    OutdatedIntent,
    UnknownIntent,
    UnknownState,
)

from meetup_bot.clock import Clock
from meetup_bot.services.ports import Notifier
from meetup_bot.telegram.common import GENERIC_ERROR_TEXT

logger = logging.getLogger(__name__)

router = Router(name="errors")

STALE_DIALOG_TEXT = "Это меню устарело. Откройте его заново командой /menu."
# Состояние диалогов в памяти: после перезапуска старые кнопки дают эти ошибки — это не сбой
STALE_DIALOG_ERRORS = (UnknownIntent, OutdatedIntent, UnknownState, InvalidStackIdError)

REPORT_INTERVAL = timedelta(minutes=1)
# Когда в служебный чат в последний раз писали про ошибку этого типа
_last_report: dict[str, datetime] = {}


@router.errors()
async def on_error(event: ErrorEvent, notifier: Notifier, clock: Clock) -> bool:
    exception = event.exception
    kind = type(exception).__name__
    if isinstance(exception, STALE_DIALOG_ERRORS):
        logger.info("устаревшая кнопка диалога: %s", kind)
        stale = event.update.callback_query
        if stale is not None:
            try:
                await stale.answer(text=STALE_DIALOG_TEXT, show_alert=True)
            except Exception:
                logger.debug("не удалось ответить на устаревшую кнопку", exc_info=True)
        return True
    # Трейсбек идёт только в лог: в нём могут быть данные пользователей, а токен в него не попадает
    logger.error("необработанная ошибка в обработчике: %s", kind, exc_info=exception)

    now = clock.now()
    last = _last_report.get(kind)
    if last is None or now - last >= REPORT_INTERVAL:
        _last_report[kind] = now
        try:
            await notifier.send_service(f"⚠️ Ошибка: {kind}")
        except Exception:
            logger.exception("не удалось отправить сообщение об ошибке в служебный чат")

    callback = event.update.callback_query
    if callback is not None:
        try:
            await callback.answer(text=GENERIC_ERROR_TEXT, show_alert=True)
        except Exception:
            # Уже отвечено или запрос устарел — пользователю это не поможет, просто пропускаем
            logger.debug("повторный ответ на callback не удался", exc_info=True)
    return True
