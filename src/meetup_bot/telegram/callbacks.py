"""Данные кнопок поста. Формат `r:<event_id>:<action>` совпадает с renderer.post.signup_callback."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

ACTION_JOIN = "j"
ACTION_JOIN_PLUS_ONE = "j1"
ACTION_CANCEL = "c"


class SignupCallback(CallbackData, prefix="r", sep=":"):
    event_id: int
    action: str
