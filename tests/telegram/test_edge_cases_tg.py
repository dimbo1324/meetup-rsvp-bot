"""Спорные сценарии Telegram-слоя: лимиты в UTF-16, подделанные кнопки, отзыв прав."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meetup_bot.db.models import EventStatus
from meetup_bot.renderer.post import (
    CAPTION_LIMIT,
    Participant,
    PostData,
    render_post,
    visible_length,
)
from meetup_bot.telegram.common import ALERT_LIMIT, fit_alert, telegram_length
from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import Harness, callback_update, command_update


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_длина_считается_как_в_telegram_в_utf16() -> None:
    assert telegram_length("абв") == 3
    assert telegram_length("🎉") == 2
    assert telegram_length("👩‍👩‍👧") == _utf16("👩‍👩‍👧")


def test_всплывающее_окно_из_эмодзи_не_превышает_лимит() -> None:
    assert telegram_length(fit_alert("🎉" * 150)) <= ALERT_LIMIT


def test_подпись_поста_с_эмодзи_в_именах_не_превышает_лимит_telegram() -> None:
    names = [Participant("🎉🔥" * 15 + f" {i}") for i in range(10)]
    post = render_post(
        PostData(
            event_id=1,
            status=EventStatus.OPEN.value,
            starts_at=datetime(2026, 10, 24, 20, 0, tzinfo=UTC),
            timezone="America/Montevideo",
            capacity=10,
            seats_taken=10,
            participants=names,
            description_html="🌟 " * 300,
            has_photo=True,
        )
    )
    assert visible_length(post.text) <= CAPTION_LIMIT


@pytest.mark.parametrize(
    "data",
    ["r:99999999999999999999999:j", "r:-5:j", "r:1:zz", "r::j", "r:1", "a:1:999999999"],
)
async def test_подделанные_кнопки_не_роняют_бота_и_получают_ответ(
    app: App, tg: Harness, data: str
) -> None:
    await tg.feed(callback_update(OWNER_ID, data, inline_message_id="x"))
    assert tg.session.of("AnswerCallbackQuery"), data
    assert not [d for d in tg.session.of("SendMessage") if d.get("chat_id") == -100500]


async def test_админ_лишённый_прав_посреди_диалога_получает_отказ(app: App, tg: Harness) -> None:
    await tg.feed(command_update(ADMIN_ID, "/menu"))
    mid = tg.session.last_message_id
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)

    await tg.click(ADMIN_ID, mid, "Настройки")

    alerts = [d.get("text", "") for d in tg.session.of("AnswerCallbackQuery")]
    assert any("Нет доступа" in text for text in alerts)


async def test_inline_не_больше_50_результатов(app: App, tg: Harness) -> None:
    from datetime import timedelta

    from aiogram.types import InlineQuery, Update

    from tests.services.test_attendance import FIRST_START
    from tests.tg_fake import next_id, tg_user

    for n in range(55):
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=FIRST_START + timedelta(hours=n)
        )
    await tg.feed(
        Update(
            update_id=next_id(),
            inline_query=InlineQuery(id="q", from_user=tg_user(OWNER_ID), query="", offset=""),
        )
    )
    answer = tg.session.of("AnswerInlineQuery")[-1]
    assert len(answer["results"]) <= 50
