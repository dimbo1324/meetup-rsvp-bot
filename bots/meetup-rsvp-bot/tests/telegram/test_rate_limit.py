"""Ограничение частоты: не больше RATE_LIMIT_EVENTS событий за RATE_LIMIT_WINDOW на человека."""

from __future__ import annotations

from datetime import timedelta

from meetup_bot.clock import FakeClock
from meetup_bot.middlewares.rate_limit import (
    MAX_TRACKED_USERS,
    RATE_LIMIT_EVENTS,
    RATE_LIMIT_TEXT,
    RATE_LIMIT_WINDOW,
    RateLimiter,
    install_rate_limit,
)
from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import Harness, callback_update, command_update, text_update

USER = 7001
OTHER = 7002
WINDOW = timedelta(seconds=RATE_LIMIT_WINDOW)


async def make_event(app: App) -> int:
    created = await app.services.events.create_event_as_admin(OWNER_ID, capacity=10)
    return created.event_id


def callback_texts(tg: Harness) -> list[str | None]:
    return [a.get("text") for a in tg.session.of("AnswerCallbackQuery")]


async def press(tg: Harness, user: int, event_id: int) -> None:
    await tg.feed(callback_update(user, f"r:{event_id}:c", inline_message_id="inl"))


async def test_лишние_нажатия_получают_ответ_и_не_доходят_до_обработчика(
    app: App, tg: Harness
) -> None:
    install_rate_limit(tg.dispatcher, app.clock)
    event_id = await make_event(app)
    for _ in range(RATE_LIMIT_EVENTS + 2):
        await press(tg, USER, event_id)
    texts = callback_texts(tg)
    assert len(texts) == RATE_LIMIT_EVENTS + 2, "на каждый callback ровно один ответ"
    assert RATE_LIMIT_TEXT not in texts[:RATE_LIMIT_EVENTS]
    assert texts[RATE_LIMIT_EVENTS:] == [RATE_LIMIT_TEXT] * 2


async def test_после_окна_нажатия_снова_обрабатываются(app: App, tg: Harness) -> None:
    install_rate_limit(tg.dispatcher, app.clock)
    event_id = await make_event(app)
    for _ in range(RATE_LIMIT_EVENTS + 1):
        await press(tg, USER, event_id)
    assert callback_texts(tg)[-1] == RATE_LIMIT_TEXT
    app.clock.advance(WINDOW + timedelta(milliseconds=1))
    await press(tg, USER, event_id)
    assert callback_texts(tg)[-1] != RATE_LIMIT_TEXT


async def test_лимит_у_каждого_человека_свой(app: App, tg: Harness) -> None:
    install_rate_limit(tg.dispatcher, app.clock)
    event_id = await make_event(app)
    for _ in range(RATE_LIMIT_EVENTS + 1):
        await press(tg, USER, event_id)
    await press(tg, OTHER, event_id)
    assert callback_texts(tg)[-1] != RATE_LIMIT_TEXT


async def test_админы_не_освобождаются(app: App, tg: Harness) -> None:
    install_rate_limit(tg.dispatcher, app.clock)
    event_id = await make_event(app)
    for _ in range(RATE_LIMIT_EVENTS + 1):
        await press(tg, ADMIN_ID, event_id)
    assert callback_texts(tg)[-1] == RATE_LIMIT_TEXT


async def test_лишние_сообщения_в_личке_игнорируются(app: App, tg: Harness) -> None:
    install_rate_limit(tg.dispatcher, app.clock)
    for _ in range(RATE_LIMIT_EVENTS + 3):
        await tg.feed(command_update(USER, "/help"))
    assert len(tg.session.of("SendMessage")) == RATE_LIMIT_EVENTS


async def test_обычный_текст_тоже_считается(app: App, tg: Harness) -> None:
    install_rate_limit(tg.dispatcher, app.clock)
    for _ in range(RATE_LIMIT_EVENTS):
        await tg.feed(text_update(USER, "привет"))
    before = len(tg.session.of("SendMessage"))
    await tg.feed(command_update(USER, "/help"))
    assert len(tg.session.of("SendMessage")) == before


def test_память_ограничена_числом_отслеживаемых_людей(clock: FakeClock) -> None:
    limiter = RateLimiter(clock, max_users=3)
    for user_id in range(10):
        assert limiter.allow(user_id) is True
    assert limiter.tracked <= 3
    assert MAX_TRACKED_USERS >= 1000


def test_старые_отметки_забываются_по_ttl(clock: FakeClock) -> None:
    limiter = RateLimiter(clock)
    for _ in range(RATE_LIMIT_EVENTS):
        assert limiter.allow(USER) is True
    assert limiter.allow(USER) is False
    clock.advance(WINDOW + timedelta(milliseconds=1))
    assert limiter.allow(USER) is True


def test_отклонённые_события_не_продлевают_блокировку(clock: FakeClock) -> None:
    limiter = RateLimiter(clock)
    for _ in range(RATE_LIMIT_EVENTS):
        limiter.allow(USER)
    for _ in range(50):
        assert limiter.allow(USER) is False
    clock.advance(WINDOW + timedelta(milliseconds=1))
    assert limiter.allow(USER) is True
