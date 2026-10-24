"""PostUpdater: debounce, повторный проход, правка по inline_message_id, ошибки Telegram."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import EditMessageText

from meetup_bot.db.models import EventPost
from meetup_bot.repositories import event_posts
from meetup_bot.telegram.post_updater import MAX_ATTEMPTS, PostUpdater
from tests.helpers import OWNER_ID, App, person
from tests.tg_fake import Harness

INLINE_ID = "inl-1"
_METHOD = EditMessageText(text="x", inline_message_id="x")
_real_sleep = asyncio.sleep


def bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(_METHOD, message)


async def make_event(app: App, **kwargs: Any) -> int:
    created = await app.services.events.create_event_as_admin(OWNER_ID, capacity=10, **kwargs)
    return created.event_id


async def add_post(app: App, event_id: int, inline_id: str = INLINE_ID) -> int:
    async with app.sessions() as session, session.begin():
        post, _ = await event_posts.register(
            session,
            event_id=event_id,
            inline_message_id=inline_id,
            posted_by=None,
            now=app.clock.now(),
        )
        return post.id


async def get_post(app: App, post_id: int) -> EventPost:
    async with app.sessions() as session:
        post = await session.get(EventPost, post_id)
        assert post is not None
        return post


async def flush(tg: Harness) -> None:
    """Дожидается всех запланированных правок."""
    await tg.updater.aclose()


def fail_first(tg: Harness, *errors: Exception, method: str = "EditMessageText") -> None:
    """Первые вызовы указанного метода падают по очереди с этими ошибками, дальше — успех."""
    queue = list(errors)

    def hook(name: str, _data: dict[str, Any]) -> Exception | None:
        if name == method and queue:
            return queue.pop(0)
        return None

    tg.session.fail = hook


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Подменяет asyncio.sleep: запоминает запрошенные задержки и не ждёт по-настоящему."""
    delays: list[float] = []

    async def fake_sleep(delay: float, result: Any = None) -> Any:
        delays.append(delay)
        await _real_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


async def test_много_schedule_подряд_дают_одну_правку(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    for _ in range(10):
        tg.updater.schedule(event_id)
    await flush(tg)
    assert len(tg.session.of("EditMessageText")) == 1


async def test_правка_идёт_по_inline_message_id_с_кнопками(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    tg.updater.schedule(event_id)
    await flush(tg)
    (call,) = tg.session.of("EditMessageText")
    assert call["inline_message_id"] == INLINE_ID
    assert "Записались: 0 из 10" in call["text"]
    callbacks = [b["callback_data"] for row in call["reply_markup"]["inline_keyboard"] for b in row]
    assert callbacks == [f"r:{event_id}:j", f"r:{event_id}:c"]
    assert call["link_preview_options"]["is_disabled"] is True


async def test_после_правки_сохраняются_хеш_и_время(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    tg.updater.schedule(event_id)
    await flush(tg)
    post = await get_post(app, post_id)
    assert post.last_render_hash
    assert post.last_edited_at == app.clock.now()


async def test_повторная_правка_без_изменений_пропускается(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    tg.updater.schedule(event_id)
    await tg.updater.aclose()
    tg.updater.schedule(event_id)
    await tg.updater.aclose()
    assert len(tg.session.of("EditMessageText")) == 1


async def test_изменение_данных_даёт_новую_правку(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    tg.updater.schedule(event_id)
    await tg.updater.aclose()
    await app.services.registration.register(event_id, person(1))
    tg.updater.schedule(event_id)
    await tg.updater.aclose()
    texts = [c["text"] for c in tg.session.of("EditMessageText")]
    assert len(texts) == 2
    assert "Записались: 1 из 10" in texts[1]


async def test_schedule_во_время_правки_запускает_повторный_проход(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    original = tg.session.make_request
    first = True

    async def racing(bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        nonlocal first
        if type(method).__name__ == "EditMessageText" and first:
            first = False
            # Во время правки приходит новая запись и просьба пересобрать пост
            await app.services.registration.register(event_id, person(1))
            tg.updater.schedule(event_id)
        return await original(bot, method, timeout)

    tg.session.make_request = racing  # type: ignore[method-assign]
    tg.updater.schedule(event_id)
    await flush(tg)
    texts = [c["text"] for c in tg.session.of("EditMessageText")]
    assert len(texts) == 2
    assert "Записались: 0 из 10" in texts[0]
    assert "Записались: 1 из 10" in texts[1]


async def test_фото_правится_через_подпись(app: App, tg: Harness) -> None:
    event_id = await make_event(app, photo_file_id="photo-1")
    await add_post(app, event_id)
    tg.updater.schedule(event_id)
    await flush(tg)
    assert not tg.session.of("EditMessageText")
    (call,) = tg.session.of("EditMessageCaption")
    assert call["inline_message_id"] == INLINE_ID
    assert "Записались: 0 из 10" in call["caption"]


async def test_у_закрытой_встречи_кнопки_снимаются(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    tg.updater.schedule(event_id)
    await flush(tg)
    (call,) = tg.session.of("EditMessageText")
    # Пустая разметка, а не её отсутствие: иначе Telegram оставил бы прежние кнопки
    assert call["reply_markup"] == {"inline_keyboard": []}


async def test_у_отменённой_встречи_кнопки_сняты_и_есть_статус(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    await app.services.events.cancel_event(OWNER_ID, event_id, "дождь")
    tg.updater.schedule(event_id)
    await flush(tg)
    (call,) = tg.session.of("EditMessageText")
    assert call["reply_markup"] == {"inline_keyboard": []}
    assert "Встреча отменена: дождь" in call["text"]


async def test_мёртвые_посты_не_правятся(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    live = await add_post(app, event_id, "live")
    dead = await add_post(app, event_id, "dead")
    async with app.sessions() as session, session.begin():
        await event_posts.mark_dead(session, dead)
    tg.updater.schedule(event_id)
    await flush(tg)
    assert [c["inline_message_id"] for c in tg.session.of("EditMessageText")] == ["live"]
    assert (await get_post(app, live)).last_render_hash


async def test_несколько_постов_одной_встречи_правятся_все(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    for n in range(3):
        await add_post(app, event_id, f"p{n}")
    tg.updater.schedule(event_id)
    await flush(tg)
    ids = sorted(c["inline_message_id"] for c in tg.session.of("EditMessageText"))
    assert ids == ["p0", "p1", "p2"]


async def test_встреча_без_постов_ничего_не_вызывает(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    tg.updater.schedule(event_id)
    await flush(tg)
    assert tg.session.calls == []


async def test_несуществующая_встреча_игнорируется(tg: Harness) -> None:
    tg.updater.schedule(424242)
    await flush(tg)
    assert tg.session.calls == []


async def test_retry_after_ждёт_и_повторяет(app: App, tg: Harness, sleeps: list[float]) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(tg, TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=7))
    tg.updater.schedule(event_id)
    await flush(tg)
    assert len(tg.session.of("EditMessageText")) == 2
    assert 7 in sleeps
    assert (await get_post(app, post_id)).last_render_hash


async def test_retry_after_ограничивается_потолком(
    app: App, tg: Harness, sleeps: list[float]
) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    fail_first(tg, TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=500))
    tg.updater.schedule(event_id)
    await flush(tg)
    assert 30.0 in sleeps
    assert 500 not in sleeps


async def test_бесконечные_лимиты_исчерпывают_попытки_без_сохранения(
    app: App, tg: Harness, sleeps: list[float]
) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    tg.session.fail = lambda name, _d: (
        TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=1)
        if name == "EditMessageText"
        else None
    )
    tg.updater.schedule(event_id)
    await flush(tg)
    assert len(tg.session.of("EditMessageText")) == MAX_ATTEMPTS
    post = await get_post(app, post_id)
    assert post.last_render_hash is None
    assert not post.is_dead


@pytest.mark.parametrize(
    "error",
    [
        TelegramNetworkError(_METHOD, "Request timeout error"),
        TelegramServerError(_METHOD, "Bad Gateway"),
    ],
    ids=["сеть", "5xx"],
)
async def test_сетевой_сбой_повторяется_с_нарастающей_паузой(
    app: App, tg: Harness, sleeps: list[float], error: TelegramAPIError
) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(tg, error, error)
    tg.updater.schedule(event_id)
    await flush(tg)
    assert len(tg.session.of("EditMessageText")) == 3
    assert [d for d in sleeps if d in (1, 2)] == [1, 2]
    assert (await get_post(app, post_id)).last_render_hash


async def test_not_modified_считается_успехом(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(
        tg, bad_request("Bad Request: message is not modified: specified new message content")
    )
    tg.updater.schedule(event_id)
    await flush(tg)
    post = await get_post(app, post_id)
    assert post.last_render_hash
    assert not post.is_dead
    assert len(tg.session.of("EditMessageText")) == 1


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: MESSAGE_ID_INVALID",
        "Bad Request: message to edit not found",
        "Bad Request: message can't be edited",
    ],
)
async def test_недоступное_сообщение_помечает_пост_мёртвым(
    app: App, tg: Harness, message: str
) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(tg, bad_request(message))
    tg.updater.schedule(event_id)
    await flush(tg)
    post = await get_post(app, post_id)
    assert post.is_dead
    assert post.last_render_hash is None
    assert len(tg.session.of("EditMessageText")) == 1


async def test_forbidden_помечает_пост_мёртвым(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(tg, TelegramForbiddenError(_METHOD, "Forbidden: bot was kicked"))
    tg.updater.schedule(event_id)
    await flush(tg)
    assert (await get_post(app, post_id)).is_dead


async def test_мёртвый_пост_больше_не_правится(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    fail_first(tg, bad_request("Bad Request: MESSAGE_ID_INVALID"))
    tg.updater.schedule(event_id)
    await tg.updater.aclose()
    tg.updater.schedule(event_id)
    await tg.updater.aclose()
    assert len(tg.session.of("EditMessageText")) == 1


async def test_прочая_ошибка_запроса_не_убивает_пост_и_не_повторяется(
    app: App, tg: Harness
) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(tg, bad_request("Bad Request: can't parse entities"))
    tg.updater.schedule(event_id)
    await flush(tg)
    post = await get_post(app, post_id)
    assert not post.is_dead
    assert post.last_render_hash is None
    assert len(tg.session.of("EditMessageText")) == 1


async def test_прочая_ошибка_api_не_повторяется(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(tg, TelegramAPIError(_METHOD, "что-то странное"))
    tg.updater.schedule(event_id)
    await flush(tg)
    assert len(tg.session.of("EditMessageText")) == 1
    assert (await get_post(app, post_id)).last_render_hash is None


async def test_ошибка_одного_поста_не_мешает_остальным(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    first = await add_post(app, event_id, "a")
    second = await add_post(app, event_id, "b")
    fail_first(tg, TelegramForbiddenError(_METHOD, "Forbidden"))
    tg.updater.schedule(event_id)
    await flush(tg)
    assert (await get_post(app, first)).is_dead
    assert (await get_post(app, second)).last_render_hash


async def test_пост_без_подписи_правится_текстом_после_смены_типа(app: App, tg: Harness) -> None:
    # Встреча с фото, но разосланное сообщение — текстовое: caption править нельзя
    event_id = await make_event(app, photo_file_id="photo-1")
    post_id = await add_post(app, event_id)
    fail_first(
        tg,
        bad_request("Bad Request: there is no caption in the message to edit"),
        method="EditMessageCaption",
    )
    tg.updater.schedule(event_id)
    await flush(tg)
    assert len(tg.session.of("EditMessageCaption")) == 1
    assert len(tg.session.of("EditMessageText")) == 1
    assert (await get_post(app, post_id)).last_render_hash


async def test_пост_без_текста_правится_подписью_после_смены_типа(app: App, tg: Harness) -> None:
    # Фото убрали у встречи, а разосланное сообщение — фото: text править нельзя
    event_id = await make_event(app)
    post_id = await add_post(app, event_id)
    fail_first(tg, bad_request("Bad Request: there is no text in the message to edit"))
    tg.updater.schedule(event_id)
    await flush(tg)
    assert len(tg.session.of("EditMessageText")) == 1
    assert len(tg.session.of("EditMessageCaption")) == 1
    assert (await get_post(app, post_id)).last_render_hash


async def test_aclose_дожидается_запланированных_правок(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    tg.updater.schedule(event_id)
    # Правка ещё ждёт debounce; aclose обнуляет его и завершает работу
    assert not tg.session.of("EditMessageText")
    await tg.updater.aclose()
    assert len(tg.session.of("EditMessageText")) == 1


async def test_aclose_отменяет_зависшие_правки_по_таймауту(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)

    async def hang(bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        await _real_sleep(30)

    tg.session.make_request = hang  # type: ignore[method-assign]
    tg.updater.schedule(event_id)
    await _real_sleep(0.2)
    await tg.updater.aclose(wait=0.1)
    assert tg.updater._tasks == {}


async def test_aclose_без_задач_не_падает(tg: Harness) -> None:
    await tg.updater.aclose()


def test_schedule_без_цикла_событий_не_падает(app: App, caplog: pytest.LogCaptureFixture) -> None:
    updater = PostUpdater(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    updater.schedule(1)
    assert updater._tasks == {}
    assert "цикл событий не запущен" in caplog.text


async def test_сбой_сборки_поста_не_роняет_задачу(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("сбой БД")

    monkeypatch.setattr("meetup_bot.telegram.post_updater.build_post", boom)
    tg.updater.schedule(event_id)
    await flush(tg)
    assert tg.session.calls == []
    assert "не удалось обновить посты встречи" in caplog.text


async def test_задача_убирает_себя_из_реестра(app: App, tg: Harness) -> None:
    event_id = await make_event(app)
    await add_post(app, event_id)
    tg.updater.schedule(event_id)
    assert event_id in tg.updater._tasks
    await asyncio.wait(list(tg.updater._tasks.values()))
    assert tg.updater._tasks == {}
