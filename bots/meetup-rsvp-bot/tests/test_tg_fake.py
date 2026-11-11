"""Тесты самого фейкового Telegram: он не должен прятать ошибки и нарушения лимитов."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from tests.tg_fake import FakeSession, Harness, callback_update, chat_type

CHAT = 7


def kb(*data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"b{i}", callback_data=d)] for i, d in enumerate(data)
        ]
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession(lambda: datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return Bot(
        "123456:TEST-TOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def test_send_photo_returns_photo_with_caption(bot: Bot) -> None:
    msg = await bot.send_photo(CHAT, "file-1", caption="подпись")
    assert msg.photo and msg.photo[-1].file_id == "file-1"
    assert msg.caption == "подпись"
    assert msg.text is None


async def test_send_document_returns_document(bot: Bot) -> None:
    msg = await bot.send_document(CHAT, "doc-1", caption="c")
    assert msg.document and msg.document.file_id == "doc-1"
    assert msg.caption == "c"


async def test_edit_text_and_caption_return_message_by_type(bot: Bot) -> None:
    sent = await bot.send_message(CHAT, "a")
    edited = await bot.edit_message_text("b", chat_id=CHAT, message_id=sent.message_id)
    assert not isinstance(edited, bool) and edited.text == "b"
    photo = await bot.send_photo(CHAT, "f", caption="x")
    cap = await bot.edit_message_caption(chat_id=CHAT, message_id=photo.message_id, caption="y")
    assert not isinstance(cap, bool) and cap.caption == "y"


async def test_edit_media_returns_message_and_true_for_inline(bot: Bot) -> None:
    photo = await bot.send_photo(CHAT, "f", caption="x")
    media = InputMediaPhoto(media="new-file", caption="z")
    edited = await bot.edit_message_media(media, chat_id=CHAT, message_id=photo.message_id)
    assert not isinstance(edited, bool)
    assert edited.photo and edited.photo[-1].file_id == "new-file"
    assert edited.caption == "z"
    assert await bot.edit_message_media(media, inline_message_id="inl") is True


async def test_inline_edits_return_true(bot: Bot) -> None:
    assert await bot.edit_message_text("t", inline_message_id="i") is True
    assert await bot.edit_message_caption(inline_message_id="i", caption="c") is True
    assert await bot.edit_message_reply_markup(inline_message_id="i") is True


async def test_message_date_comes_from_injected_clock(bot: Bot) -> None:
    msg = await bot.send_message(CHAT, "a")
    assert msg.date == datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


async def test_chat_type_by_sign(bot: Bot) -> None:
    assert (await bot.send_message(CHAT, "a")).chat.type == "private"
    assert (await bot.send_message(-100500, "a")).chat.type == "supergroup"
    assert chat_type(5) == "private"
    assert chat_type(-5) == "supergroup"


async def test_edit_without_markup_removes_buttons(bot: Bot, session: FakeSession) -> None:
    sent = await bot.send_message(CHAT, "a", reply_markup=kb("x"))
    mid = sent.message_id
    assert session.buttons(mid) == [("b0", "x")]
    await bot.edit_message_text("b", chat_id=CHAT, message_id=mid)
    assert session.buttons(mid) == []
    assert mid in session.removed_markup


async def test_edit_markup_none_and_empty_remove_buttons(bot: Bot, session: FakeSession) -> None:
    mid = (await bot.send_message(CHAT, "a", reply_markup=kb("x"))).message_id
    await bot.edit_message_reply_markup(chat_id=CHAT, message_id=mid)
    assert session.buttons(mid) == []
    await bot.edit_message_reply_markup(chat_id=CHAT, message_id=mid, reply_markup=kb("y"))
    assert session.buttons(mid) == [("b0", "y")]
    await bot.edit_message_caption(chat_id=CHAT, message_id=mid, caption="c")
    assert session.buttons(mid) == []


async def test_edit_media_without_markup_removes_buttons(bot: Bot, session: FakeSession) -> None:
    mid = (await bot.send_photo(CHAT, "f", reply_markup=kb("x"))).message_id
    await bot.edit_message_media(InputMediaPhoto(media="g"), chat_id=CHAT, message_id=mid)
    assert session.buttons(mid) == []


async def test_edit_keeps_new_buttons(bot: Bot, session: FakeSession) -> None:
    mid = (await bot.send_message(CHAT, "a", reply_markup=kb("x"))).message_id
    await bot.edit_message_text("b", chat_id=CHAT, message_id=mid, reply_markup=kb("z"))
    assert session.buttons(mid) == [("b0", "z")]


async def test_click_on_vanished_button_fails_clearly(tg: Harness) -> None:
    bot = tg.bot
    mid = (await bot.send_message(CHAT, "a", reply_markup=kb("x"))).message_id
    await bot.edit_message_text("b", chat_id=CHAT, message_id=mid)
    with pytest.raises(AssertionError, match="клавиатуру сняла правка"):
        await tg.click(CHAT, mid, "b0")
    assert await tg.click(CHAT, mid, "b0", expect=False) is False


async def test_callback_data_limit_in_bytes(bot: Bot) -> None:
    await bot.send_message(CHAT, "a", reply_markup=kb("x" * 64))
    with pytest.raises(TelegramBadRequest, match="BUTTON_DATA_INVALID"):
        await bot.send_message(CHAT, "a", reply_markup=kb("x" * 65))
    # 33 кириллические буквы = 66 байт, хотя символов меньше 64
    with pytest.raises(TelegramBadRequest, match="BUTTON_DATA_INVALID"):
        await bot.send_message(CHAT, "a", reply_markup=kb("я" * 33))


async def test_text_limit_utf16_and_html_stripped(bot: Bot) -> None:
    await bot.send_message(CHAT, "a" * 4096)
    # теги не считаются: видимая длина ровно 4096
    await bot.send_message(CHAT, "<b>" + "a" * 4096 + "</b>")
    with pytest.raises(TelegramBadRequest, match="message is too long"):
        await bot.send_message(CHAT, "a" * 4097)
    # эмодзи вне BMP — 2 единицы
    with pytest.raises(TelegramBadRequest, match="message is too long"):
        await bot.send_message(CHAT, "😀" * 2049)
    mid = (await bot.send_message(CHAT, "a")).message_id
    with pytest.raises(TelegramBadRequest, match="message is too long"):
        await bot.edit_message_text("a" * 4097, chat_id=CHAT, message_id=mid)


async def test_caption_limit(bot: Bot) -> None:
    await bot.send_photo(CHAT, "f", caption="a" * 1024)
    with pytest.raises(TelegramBadRequest, match="caption is too long"):
        await bot.send_photo(CHAT, "f", caption="a" * 1025)
    with pytest.raises(TelegramBadRequest, match="caption is too long"):
        await bot.send_document(CHAT, "d", caption="😀" * 513)
    mid = (await bot.send_photo(CHAT, "f")).message_id
    with pytest.raises(TelegramBadRequest, match="caption is too long"):
        await bot.edit_message_caption(chat_id=CHAT, message_id=mid, caption="a" * 1025)
    with pytest.raises(TelegramBadRequest, match="caption is too long"):
        await bot.edit_message_media(
            InputMediaPhoto(media="g", caption="a" * 1025), chat_id=CHAT, message_id=mid
        )


async def test_answer_callback_limit(bot: Bot) -> None:
    assert await bot.answer_callback_query("1", text="a" * 200) is True
    with pytest.raises(TelegramBadRequest):
        await bot.answer_callback_query("1", text="a" * 201)


async def test_injected_failure_has_priority(bot: Bot, session: FakeSession) -> None:
    error = TelegramBadRequest(method=None, message="Bad Request: boom")  # type: ignore[arg-type]
    session.fail = lambda name, _data: error if name == "SendMessage" else None
    with pytest.raises(TelegramBadRequest, match="boom"):
        await bot.send_message(CHAT, "a")


def test_callback_update_builds_without_clock() -> None:
    assert callback_update(1, "x").callback_query is not None
