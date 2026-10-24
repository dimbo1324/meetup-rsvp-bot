"""Интерфейс отметки явки в личке админа (раздел 5.8, сценарий 4.6).

Список разбит на страницы (у встречи может быть до 200 человек, а у сообщения — не больше
100 кнопок). Неявки до «Готово» копятся в черновике на стороне бота под блокировкой
сообщения: два быстрых нажатия не затирают друг друга. После перезапуска черновик пуст, и
отметки текущей страницы восстанавливаются из разметки сообщения (префикс ✅/🚫 на кнопке).
"""

from __future__ import annotations

import asyncio
import enum
import html
import logging
from collections import OrderedDict, defaultdict
from collections.abc import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.container import Services
from meetup_bot.db.models import Attendance, EventStatus
from meetup_bot.repositories import admins, events
from meetup_bot.services.attendance import ATTENDANCE_WINDOW, SheetRow
from meetup_bot.telegram.attendance_callbacks import (
    PAGE_PREFIX,
    TARGET_DONE,
    TARGET_NOOP,
    AttendanceCallback,
)
from meetup_bot.texts.render import format_moment

logger = logging.getLogger(__name__)

MARK_ATTENDED = "✅"
MARK_NO_SHOW = "🚫"
# Строк на странице: вместе с навигацией и «Готово» далеко от лимита Telegram в 100 кнопок
PAGE_SIZE = 25
MAX_BUTTON_NAME = 40
# Рассылки и черновики в памяти не копим бесконечно
MAX_TRACKED_EVENTS = 50
MAX_DRAFTS = 200

type MessageKey = tuple[int, int]


class SheetStatus(enum.StrEnum):
    SENT = "sent"
    NOT_STARTED = "not_started"
    WINDOW_CLOSED = "window_closed"
    CANCELLED = "cancelled"
    NO_ACCESS = "no_access"
    FAILED = "failed"


def _callback(event_id: int, target: str) -> str:
    return AttendanceCallback(event_id=event_id, target=target).pack()


def row_button(event_id: int, row: SheetRow, no_show: bool) -> InlineKeyboardButton:
    name = row.name
    if len(name) > MAX_BUTTON_NAME:
        name = name[: MAX_BUTTON_NAME - 1] + "…"
    guest = " (+1)" if row.guests else ""
    mark = MARK_NO_SHOW if no_show else MARK_ATTENDED
    return InlineKeyboardButton(
        text=f"{mark} {name}{guest}", callback_data=_callback(event_id, str(row.registration_id))
    )


def page_count(rows: Sequence[SheetRow]) -> int:
    return max(1, -(-len(rows) // PAGE_SIZE))


def _nav_row(event_id: int, page: int, pages: int) -> list[InlineKeyboardButton]:
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀", callback_data=_callback(event_id, f"{PAGE_PREFIX}{page - 1}")
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{pages}", callback_data=_callback(event_id, TARGET_NOOP)
        )
    )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="▶", callback_data=_callback(event_id, f"{PAGE_PREFIX}{page + 1}")
            )
        )
    return nav


def build_keyboard(
    event_id: int,
    rows: Sequence[SheetRow],
    no_show_ids: set[int],
    *,
    with_done: bool,
    page: int = 0,
) -> InlineKeyboardMarkup:
    pages = page_count(rows)
    page = min(max(page, 0), pages - 1)
    chunk = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    keyboard = [[row_button(event_id, row, row.registration_id in no_show_ids)] for row in chunk]
    if pages > 1:
        keyboard.append(_nav_row(event_id, page, pages))
    if with_done:
        keyboard.append(
            [InlineKeyboardButton(text="Готово", callback_data=_callback(event_id, TARGET_DONE))]
        )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _buttons(
    markup: InlineKeyboardMarkup,
) -> list[tuple[InlineKeyboardButton, AttendanceCallback]]:
    found: list[tuple[InlineKeyboardButton, AttendanceCallback]] = []
    for line in markup.inline_keyboard:
        for button in line:
            data = button.callback_data
            if not data or not data.startswith("a:"):
                continue
            try:
                found.append((button, AttendanceCallback.unpack(data)))
            except ValueError, TypeError:
                continue
    return found


def has_done(markup: InlineKeyboardMarkup) -> bool:
    """Обычный режим отметки (есть «Готово») или режим исправления."""
    return any(data.target == TARGET_DONE for _, data in _buttons(markup))


def shown_registration_ids(markup: InlineKeyboardMarkup) -> list[int]:
    return [int(data.target) for _, data in _buttons(markup) if data.target.isdigit()]


def marked_in_markup(markup: InlineKeyboardMarkup) -> set[int]:
    """Неявки, видимые на текущей странице (восстановление черновика после перезапуска)."""
    return {
        int(data.target)
        for button, data in _buttons(markup)
        if data.target.isdigit() and button.text.startswith(MARK_NO_SHOW)
    }


def current_page(rows: Sequence[SheetRow], markup: InlineKeyboardMarkup) -> int:
    shown = shown_registration_ids(markup)
    ids = [row.registration_id for row in rows]
    if not shown or shown[0] not in ids:
        return 0
    return ids.index(shown[0]) // PAGE_SIZE


def _db_no_shows(rows: Sequence[SheetRow]) -> set[int]:
    return {r.registration_id for r in rows if r.attendance == Attendance.NO_SHOW.value}


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
        self._drafts: OrderedDict[MessageKey, set[int]] = OrderedDict()
        self._locks: defaultdict[MessageKey, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, chat_id: int, message_id: int) -> asyncio.Lock:
        """Нажатия в одном списке обрабатываются по очереди: черновик не теряет изменений."""
        return self._locks[(chat_id, message_id)]

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
        if page_count(rows) > 1:
            lines.append(f"Участников: {len(rows)}; листайте страницы кнопками ◀ ▶.")
        marked = _db_no_shows(rows) if correction else set()
        markup = build_keyboard(event_id, rows, marked, with_done=not correction)
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

    # ---- черновик обычного режима ---------------------------------------------------------

    def draft(self, chat_id: int, message_id: int, markup: InlineKeyboardMarkup) -> set[int]:
        key = (chat_id, message_id)
        if key not in self._drafts:
            self._drafts[key] = marked_in_markup(markup)
            while len(self._drafts) > MAX_DRAFTS:
                self._drafts.popitem(last=False)
        return self._drafts[key]

    def forget(self, chat_id: int, message_id: int) -> None:
        self._drafts.pop((chat_id, message_id), None)

    async def toggle(
        self,
        admin_id: int,
        chat_id: int,
        message_id: int,
        markup: InlineKeyboardMarkup,
        event_id: int,
        registration_id: int,
    ) -> InlineKeyboardMarkup:
        """Обычный режим: меняет отметку в черновике (в БД ничего не пишется)."""
        rows = await self._services.attendance.get_sheet(admin_id, event_id)
        draft = self.draft(chat_id, message_id, markup)
        if registration_id in draft:
            draft.discard(registration_id)
        elif registration_id in {row.registration_id for row in rows}:
            draft.add(registration_id)
        page = current_page(rows, markup)
        return build_keyboard(event_id, rows, draft, with_done=True, page=page)

    async def correct(
        self,
        admin_id: int,
        markup: InlineKeyboardMarkup,
        event_id: int,
        registration_id: int,
    ) -> InlineKeyboardMarkup:
        """Режим исправления: отметка сразу пишется в БД (обратная текущей по данным БД)."""
        rows = await self._services.attendance.get_sheet(admin_id, event_id)
        current = {row.registration_id: row.attendance for row in rows}
        new = (
            Attendance.ATTENDED
            if current.get(registration_id) == Attendance.NO_SHOW.value
            else Attendance.NO_SHOW
        )
        await self._services.attendance.correct(admin_id, event_id, registration_id, new)
        rows = await self._services.attendance.get_sheet(admin_id, event_id)
        page = current_page(rows, markup)
        return build_keyboard(event_id, rows, _db_no_shows(rows), with_done=False, page=page)

    async def page(
        self,
        admin_id: int,
        chat_id: int,
        message_id: int,
        markup: InlineKeyboardMarkup,
        event_id: int,
        page: int,
    ) -> InlineKeyboardMarkup:
        rows = await self._services.attendance.get_sheet(admin_id, event_id)
        if has_done(markup):
            marked = self.draft(chat_id, message_id, markup)
            return build_keyboard(event_id, rows, marked, with_done=True, page=page)
        return build_keyboard(event_id, rows, _db_no_shows(rows), with_done=False, page=page)

    # ---- рассылка админам -----------------------------------------------------------------

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
            self.forget(chat_id, message_id)
            if chat_id == except_chat_id:
                continue
            try:
                await self._bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, parse_mode="HTML"
                )
            except TelegramAPIError as exc:
                logger.warning("не удалось погасить список явки: %s", exc.message)
