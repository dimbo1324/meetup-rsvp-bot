"""Inline-режим: выдача списка встреч админу и регистрация отправленного поста."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.types import ChosenInlineResult, InlineQuery, Update
from sqlalchemy import select

from meetup_bot.db.models import EventPost, User
from meetup_bot.handlers.inline import MAX_RESULTS
from meetup_bot.services.errors import DomainError
from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.tg_fake import Harness, next_id, tg_user

STRANGER = 4242
INLINE_ID = "inline-msg-1"
# Пятница, 30 октября 2026, 16:30 в Монтевидео (UTC-3)
FRIDAY_START = datetime(2026, 10, 30, 19, 30, tzinfo=UTC)


def inline_query_update(user_id: int, query: str = "") -> Update:
    return Update(
        update_id=next_id(),
        inline_query=InlineQuery(
            id=str(next_id()), from_user=tg_user(user_id), query=query, offset=""
        ),
    )


def chosen_update(user_id: int, result_id: str, inline_message_id: str | None) -> Update:
    return Update(
        update_id=next_id(),
        chosen_inline_result=ChosenInlineResult(
            result_id=result_id,
            from_user=tg_user(user_id),
            query="",
            inline_message_id=inline_message_id,
        ),
    )


async def make_event(app: App, **kwargs: Any) -> int:
    created = await app.services.events.create_event_as_admin(OWNER_ID, capacity=10, **kwargs)
    return created.event_id


async def query_answer(tg: Harness, user_id: int) -> dict[str, Any]:
    await tg.feed(inline_query_update(user_id))
    (answer,) = tg.session.of("AnswerInlineQuery")
    return answer


async def all_posts(app: App) -> list[EventPost]:
    async with app.sessions() as session:
        return list((await session.scalars(select(EventPost))).all())


async def test_не_админу_пустой_список_и_кнопка_знакомства(app: App, tg: Harness) -> None:
    await make_event(app)
    answer = await query_answer(tg, STRANGER)
    assert answer["results"] == []
    assert answer["button"] == {"text": "Бот записи на встречи", "start_parameter": "about"}


async def test_не_админу_ответ_персональный_и_без_кэша(app: App, tg: Harness) -> None:
    answer = await query_answer(tg, STRANGER)
    assert answer["cache_time"] == 0
    assert answer["is_personal"] is True


async def test_inline_запрос_постороннего_не_пишет_его_в_базу(app: App, tg: Harness) -> None:
    await query_answer(tg, STRANGER)
    async with app.sessions() as session:
        assert await session.get(User, STRANGER) is None


async def test_админ_получает_встречу_статьёй_с_кнопками(app: App, tg: Harness) -> None:
    event_id = await make_event(app, starts_at=FRIDAY_START)
    answer = await query_answer(tg, ADMIN_ID)
    (result,) = answer["results"]
    assert result["type"] == "article"
    assert result["id"] == f"event:{event_id}"
    assert result["title"] == "Встреча: пятница, 30 октября, 16:30–18:30"
    assert result["description"] == "Занято 0 из 10 мест"
    callbacks = [
        b["callback_data"] for row in result["reply_markup"]["inline_keyboard"] for b in row
    ]
    assert callbacks == [f"r:{event_id}:j", f"r:{event_id}:c"]
    content = result["input_message_content"]
    assert "Записались: 0 из 10" in content["message_text"]
    assert content["link_preview_options"]["is_disabled"] is True


async def test_админу_ответ_персональный_и_без_кэша(app: App, tg: Harness) -> None:
    await make_event(app)
    answer = await query_answer(tg, ADMIN_ID)
    assert answer["cache_time"] == 0
    assert answer["is_personal"] is True
    assert "button" not in answer


async def test_в_описании_видно_занятые_места(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    answer = await query_answer(tg, ADMIN_ID)
    assert answer["results"][0]["description"] == "Занято 1 из 10 мест"


async def test_без_встреч_список_пуст_без_кнопки(app: App, tg: Harness) -> None:
    answer = await query_answer(tg, ADMIN_ID)
    assert answer["results"] == []
    assert "button" not in answer


async def test_в_списке_только_открытые_встречи(app: App, tg: Harness) -> None:
    open_id = await make_event(app, starts_at=app.clock.now() + timedelta(days=3))
    closed_id = await make_event(app, starts_at=app.clock.now() + timedelta(days=4))
    cancelled_id = await make_event(app, starts_at=app.clock.now() + timedelta(days=5))
    await app.services.events.set_registration_open(OWNER_ID, closed_id, False)
    await app.services.events.cancel_event(OWNER_ID, cancelled_id)
    answer = await query_answer(tg, OWNER_ID)
    assert [r["id"] for r in answer["results"]] == [f"event:{open_id}"]


async def test_встреча_с_фото_выдаётся_фотографией_с_подписью(app: App, tg: Harness) -> None:
    event_id = await make_event(app, photo_file_id="photo-file-1")
    answer = await query_answer(tg, ADMIN_ID)
    (result,) = answer["results"]
    assert result["type"] == "photo"
    # Суффикс нужен, чтобы chosen_inline_result узнал вид разосланного поста
    assert result["id"] == f"event:{event_id}:photo"
    assert result["photo_file_id"] == "photo-file-1"
    assert result["title"].startswith("Встреча:")
    assert result["description"] == "Занято 0 из 10 мест"
    assert "Записались: 0 из 10" in result["caption"]
    assert result["reply_markup"]["inline_keyboard"]


async def test_результатов_не_больше_лимита_telegram(app: App, tg: Harness) -> None:
    for n in range(MAX_RESULTS + 3):
        await make_event(app, starts_at=app.clock.now() + timedelta(days=2 + n))
    answer = await query_answer(tg, ADMIN_ID)
    assert len(answer["results"]) == MAX_RESULTS


async def test_отказ_сервиса_даёт_пустой_ответ(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_a: Any, **_k: Any) -> None:
        raise DomainError("нет прав")

    monkeypatch.setattr(app.services.events, "list_events", refuse)
    answer = await query_answer(tg, ADMIN_ID)
    assert answer["results"] == []
    assert answer["cache_time"] == 0


async def test_права_проверяются_на_каждый_запрос(app: App, tg: Harness) -> None:
    await make_event(app)
    assert (await query_answer(tg, ADMIN_ID))["results"]
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.feed(inline_query_update(ADMIN_ID))
    assert tg.session.of("AnswerInlineQuery")[-1]["results"] == []


# ---- chosen_inline_result ----


async def test_выбранный_результат_регистрирует_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await tg.feed(chosen_update(ADMIN_ID, f"event:{event_id}", INLINE_ID))
    (post,) = await all_posts(app)
    assert (post.event_id, post.inline_message_id, post.posted_by) == (
        event_id,
        INLINE_ID,
        ADMIN_ID,
    )
    assert post.created_at == app.clock.now()


async def test_после_выбора_пост_пересобирается_актуальными_данными(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await tg.feed(chosen_update(ADMIN_ID, f"event:{event_id}", INLINE_ID))
    await tg.updater.aclose()
    (edit,) = tg.session.of("EditMessageText")
    assert edit["inline_message_id"] == INLINE_ID


async def test_повторный_выбор_того_же_сообщения_не_дублирует_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    for _ in range(2):
        await tg.feed(chosen_update(ADMIN_ID, f"event:{event_id}", INLINE_ID))
    assert len(await all_posts(app)) == 1


async def test_выбор_не_пишет_пользователя_в_базу(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await tg.feed(chosen_update(STRANGER, f"event:{event_id}", INLINE_ID))
    async with app.sessions() as session:
        assert await session.get(User, STRANGER) is None


@pytest.mark.parametrize(
    "result_id",
    ["что-то-другое", "event:", "event:abc", "event:1.5", "ev:1"],
)
async def test_некорректный_идентификатор_результата_игнорируется(
    app: App, tg: Harness, result_id: str
) -> None:
    await make_event(app)
    await tg.feed(chosen_update(ADMIN_ID, result_id, INLINE_ID))
    assert await all_posts(app) == []


async def test_выбор_без_inline_message_id_игнорируется(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await tg.feed(chosen_update(ADMIN_ID, f"event:{event_id}", None))
    assert await all_posts(app) == []


async def test_выбор_несуществующей_встречи_не_роняет_диспетчер(app: App, tg: Harness) -> None:
    # Внешний ключ не даст создать пост: ошибка логируется, исключение наружу не выходит
    await tg.feed(chosen_update(ADMIN_ID, "event:9999", INLINE_ID))
    assert tg.session.of("EditMessageText") == []


async def test_выбор_фото_результата_запоминает_фото_пост(app: App, tg: Harness) -> None:
    event_id = await make_event(app, photo_file_id="photo-file-1")
    await tg.feed(chosen_update(ADMIN_ID, f"event:{event_id}:photo", INLINE_ID))
    (post,) = await all_posts(app)
    assert post.has_photo is True
    assert post.last_photo_file_id == "photo-file-1"


async def test_выбор_текстового_результата_запоминает_текстовый_пост_даже_с_фото_у_встречи(
    app: App, tg: Harness
) -> None:
    # Фото добавили между выдачей списка и выбором: разосланный пост остался текстовым
    event_id = await make_event(app, photo_file_id="photo-file-1")
    await tg.feed(chosen_update(ADMIN_ID, f"event:{event_id}", INLINE_ID))
    (post,) = await all_posts(app)
    assert post.has_photo is False
    await tg.updater.aclose()
    assert tg.session.of("EditMessageText")
    assert not tg.session.of("EditMessageCaption")
