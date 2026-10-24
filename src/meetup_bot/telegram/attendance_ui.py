"""Интерфейс отметки явки в личке админа (раздел 5.8, сценарий 4.6).

Состояние переключателей хранится только в разметке самого сообщения (префикс ✅/🚫 на
кнопке), поэтому переживает перезапуск бота. В памяти держим лишь адреса отправленных
списков, чтобы после «Готово» погасить их у остальных админов; после перезапуска такие
списки остаются «живыми», но безопасными: сервис ответит `AlreadyMarked`.
"""

from __future__ import annotations

import enum
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.container import Services
from meetup_bot.db.models import Attendance, EventStatus
from meetup_bot.repositories import admins, events
from meetup_bot.services.attendance import ATTENDANCE_WINDOW, SheetRow
from meetup_bot.telegram.attendance_callbacks import TARGET_DONE, AttendanceCallback
from meetup_bot.texts.render import format_moment

logger = logging.getLogger(__name__)

MARK_ATTENDED = "✅"
MARK_NO_SHOW = "🚫"
# Лимит Telegram — 100 кнопок; одну занимает «Готово», остальным оставлен запас
MAX_ROWS = 90
MAX_BUTTON_NAME = 40
# Рассылки в памяти не копим бесконечно: встреч мало, но словарь не должен расти вечно
MAX_TRACKED_EVENTS = 50


class SheetStatus(enum.StrEnum):
    SENT = "sent"
    NOT_STARTED = "not_started"
    WINDOW_CLOSED = "window_closed"
    CANCELLED = "cancelled"
    NO_ACCESS = "no_access"
    FAILED = "failed"


def row_button(event_id: int, row: SheetRow, no_show: bool) -> InlineKeyboardButton:
    name = row.name
    if len(name) > MAX_BUTTON_NAME:
        name = name[: MAX_BUTTON_NAME - 1] + "…"
    guest = " (+1)" if row.guests else ""
    mark = MARK_NO_SHOW if no_show else MARK_ATTENDED
    return InlineKeyboardButton(
        text=f"{mark} {name}{guest}",
        callback_data=AttendanceCallback(event_id=event_id, target=str(row.registration_id)).pack(),
    )


def build_keyboard(
    event_id: int, rows: list[SheetRow], no_show_ids: set[int], *, with_done: bool
) -> InlineKeyboardMarkup:
    keyboard = [
        [row_button(event_id, row, row.registration_id in no_show_ids)] for row in rows[:MAX_ROWS]
    ]
    if with_done:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="Готово",
                    callback_data=AttendanceCallback(event_id=event_id, target=TARGET_DONE).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


class AttendanceUI:
    def __init__(
        self,
        bot: Bot,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        services: Services,
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._clock = clock
        self._services = services
        self._sent: dict[int, list[tuple[int, int]]] = {}

    async def send_sheet(self, admin_id: int, event_id: int) -> SheetStatus:
        """Отправляет админу список участников с переключателями ✅/🚫.

        Режим определяется по состоянию встречи: явка не отмечена — обычная отметка с
        кнопкой «Готово»; уже отмечена и окно 7 суток открыто — режим исправления.
        """
        try:
            async with self._sessions() as session:
                if not await admins.is_admin(session, admin_id):
                    return SheetStatus.NO_ACCESS
                event = await events.get(session, event_id)
                if event is None or event.status == EventStatus.CANCELLED.value:
                    return SheetStatus.CANCELLED
                now = self._clock.now()
                if event.status != EventStatus.FINISHED.value and event.starts_at > now:
                    return SheetStatus.NOT_STARTED
                if now > event.starts_at + ATTENDANCE_WINDOW:
                    return SheetStatus.WINDOW_CLOSED
                starts_at, place = event.starts_at, event.place
                correction = event.attendance_marked_at is not None
            rows = await self._services.attendance.get_sheet(admin_id, event_id)
        except Exception:
            logger.exception("не удалось подготовить список явки (event_id=%s)", event_id)
            return SheetStatus.FAILED

        tz = self._services.settings.timezone_now()
        title = f"Явка: встреча {format_moment(starts_at, tz)}"
        if place:
            title += f", {place}"
        lines = [html.escape(title)]
        if not rows:
            lines.append("Активных участников нет.")
        elif correction:
            until = format_moment(starts_at + ATTENDANCE_WINDOW, tz)
            lines.append("Нажатие на имя сразу меняет отметку.")
            lines.append(f"Исправление отметок возможно до {until}.")
        else:
            lines.append("Нажмите на имя, чтобы отметить неявку (🚫), затем «Готово».")
        if len(rows) > MAX_ROWS:
            lines.append(f"Показаны первые {MAX_ROWS} из {len(rows)} участников.")
        no_show_ids = {r.registration_id for r in rows if r.attendance == Attendance.NO_SHOW.value}
        markup = build_keyboard(
            event_id, rows, no_show_ids if correction else set(), with_done=not correction
        )
        try:
            message = await self._bot.send_message(
                admin_id, "\n".join(lines), parse_mode="HTML", reply_markup=markup
            )
        except TelegramAPIError as exc:
            # Обычно админ не нажимал /start: это нормально, персональные данные не логируем
            logger.warning("список явки не доставлен: %s", exc.message)
            return SheetStatus.FAILED
        self._remember(event_id, admin_id, message.message_id)
        return SheetStatus.SENT

    def _remember(self, event_id: int, chat_id: int, message_id: int) -> None:
        sent = self._sent.setdefault(event_id, [])
        sent[:] = [item for item in sent if item[0] != chat_id] + [(chat_id, message_id)]
        while len(self._sent) > MAX_TRACKED_EVENTS:
            del self._sent[next(iter(self._sent))]

    async def close_others(self, event_id: int, except_chat_id: int, marked_by: str) -> None:
        """Гасит списки остальных админов: «Явку отметил(а) …» без клавиатуры.

        `marked_by` — сырое имя: экранирует `render_text_now`.
        """
        text = self._services.settings.render_text_now("text.attendance_marked_by", name=marked_by)
        for chat_id, message_id in self._sent.pop(event_id, []):
            if chat_id == except_chat_id:
                continue
            try:
                await self._bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, parse_mode="HTML"
                )
            except TelegramAPIError as exc:
                logger.warning("не удалось погасить список явки: %s", exc.message)
