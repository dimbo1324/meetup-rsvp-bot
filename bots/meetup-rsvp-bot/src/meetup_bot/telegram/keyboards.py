"""Преобразование клавиатуры из рендера (чистые данные) в разметку aiogram."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from meetup_bot.renderer.post import RenderedPost


def to_markup(post: RenderedPost) -> InlineKeyboardMarkup | None:
    """None, если кнопок нет: при правке сообщения вызывающий передаёт пустую разметку."""
    if not post.keyboard:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.callback_data) for b in row]
            for row in post.keyboard
        ]
    )
