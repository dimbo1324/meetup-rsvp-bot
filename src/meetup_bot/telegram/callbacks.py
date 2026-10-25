"""Данные кнопок поста. Формат задаёт renderer.post (`signup_callback`), здесь — разбор."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from meetup_bot.renderer.post import SIGNUP_PREFIX, SIGNUP_SEP


class SignupCallback(CallbackData, prefix=SIGNUP_PREFIX, sep=SIGNUP_SEP):
    event_id: int
    action: str
