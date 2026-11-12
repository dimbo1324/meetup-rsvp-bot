"""Подтверждение тревоги («✅ Вижу» или слово «ок») и «Готово» после шага 1 (SPEC 2.4, D3)."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from rcbot.application.control import Controller
from rcbot.bot import texts
from rcbot.infrastructure.notify.telegram import CALLBACK_ACK, CALLBACK_MANUAL_DONE

OK_WORDS = re.compile(r"^\s*(ок|окей|ok|okay|вижу|да)[.!]*\s*$", re.I)


async def on_ack_button(callback: CallbackQuery, controller: Controller) -> None:
    acked = await controller.ack()
    await callback.answer("Принято" if acked else "Тревоги нет")
    if acked and isinstance(callback.message, Message):
        await callback.message.answer(texts.ACKED)


async def on_manual_done(callback: CallbackQuery, controller: Controller) -> None:
    controller.manual_done()
    await callback.answer(texts.MANUAL_THANKS)


async def on_ok_word(message: Message, controller: Controller) -> None:
    await message.answer(texts.ACKED if await controller.ack() else texts.NOT_ACKED)


def build_router() -> Router:
    """Новый роутер на каждый Dispatcher: один роутер нельзя подключить к двум."""
    router = Router(name="ack")
    router.callback_query.register(on_ack_button, F.data == CALLBACK_ACK)
    router.callback_query.register(on_manual_done, F.data == CALLBACK_MANUAL_DONE)
    router.message.register(on_ok_word, F.text.regexp(OK_WORDS))
    return router
