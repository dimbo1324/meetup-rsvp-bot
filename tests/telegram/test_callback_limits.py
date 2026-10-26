"""Лимит Telegram на callback_data: не больше 64 байт (клавиатуры поста и листа явки)."""

from __future__ import annotations

from datetime import UTC, datetime

from meetup_bot.db.models import Attendance, EventStatus
from meetup_bot.renderer.post import PostData, render_post
from meetup_bot.services.attendance import SheetRow
from meetup_bot.telegram.attendance_ui import PAGE_SIZE, build_keyboard

CALLBACK_LIMIT_BYTES = 64
# Заведомо больше любых реальных идентификаторов (SQLite — 64-битные целые)
HUGE_ID = 2**63 - 1


def test_клавиатура_поста_укладывается_в_лимит_callback_data() -> None:
    data = PostData(
        event_id=HUGE_ID,
        status=EventStatus.OPEN.value,
        starts_at=datetime(2026, 10, 30, 19, 30, tzinfo=UTC),
        timezone="America/Montevideo",
        capacity=10,
        seats_taken=0,
        participants=[],
        plus_one_enabled=True,
    )
    buttons = [b for row in render_post(data).keyboard for b in row]
    assert len(buttons) == 3, "ожидались «Записаться», «Отменить запись», «Записаться +1»"
    for button in buttons:
        assert len(button.callback_data.encode()) <= CALLBACK_LIMIT_BYTES


def test_лист_явки_укладывается_в_лимит_callback_data_на_любой_странице() -> None:
    rows = [
        SheetRow(HUGE_ID - n, 1, f"Участник {n}", 0, Attendance.UNKNOWN.value)
        for n in range(PAGE_SIZE * 3)
    ]
    for page in range(3):
        markup = build_keyboard(HUGE_ID, rows, set(), with_done=True, page=page)
        callbacks = [b.callback_data for line in markup.inline_keyboard for b in line]
        assert callbacks
        for data in callbacks:
            assert data is not None
            assert len(data.encode()) <= CALLBACK_LIMIT_BYTES
