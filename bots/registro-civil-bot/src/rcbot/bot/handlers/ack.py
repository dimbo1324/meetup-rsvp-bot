"""Подтверждение тревоги («✅ Вижу» или слово «ок») и «Готово» после шага 1 (SPEC 2.4, D3)."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from rcbot.application.control import Controller
from rcbot.bot import texts
from rcbot.infrastructure.notify.telegram import CALLBACK_ACK, CALLBACK_MANUAL_DONE

router = Router(name="ack")

OK_WORDS = re.compile(r"^\s*(ок|окей|ok|okay|вижу|да)[.!]*\s*$", re.I)


@router.callback_query(F.data == CALLBACK_ACK)
async def on_ack_button(callback: CallbackQuery, controller: Controller) -> None:
    acked = await controller.ack()
    await callback.answer("Принято" if acked else "Тревоги нет")
    if acked and isinstance(callback.message, Message):
        await callback.message.answer(texts.ACKED)


@router.callback_query(F.data == CALLBACK_MANUAL_DONE)
async def on_manual_done(callback: CallbackQuery, controller: Controller) -> None:
    controller.manual_done()
    await callback.answer(texts.MANUAL_THANKS)


@router.message(F.text.regexp(OK_WORDS))
async def on_ok_word(message: Message, controller: Controller) -> None:
    await message.answer(texts.ACKED if await controller.ack() else texts.NOT_ACKED)
