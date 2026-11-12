"""Инвариант 10 (SPEC 2.4, 2.6): Telegram отвечает только владельцу.

Устройство: фильтр стоит внешним middleware диспетчера (для message и callback_query) и ни один
обработчик не зарегистрирован мимо него. Поведение: фаззинг обновлений от чужих user_id и
chat_id не вызывает ни одного метода `Controller` и не порождает ни одного ответа.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from hypothesis import given
from hypothesis import strategies as st

from rcbot.bot.app import ALLOWED_UPDATES, build_dispatcher
from rcbot.bot.middlewares import OwnerOnlyMiddleware

OWNER = 5_550_142
TZ = ZoneInfo("America/Montevideo")
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
TOKEN = "123456789" + ":" + "A" * 35


class CountingController:
    """Любой метод считается и возвращает «успех»: тест видит, кто что вызвал."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        async def method(*_: object, **__: object) -> Any:
            self.calls.append(name)
            return True

        return method


class RecordingSession(BaseSession):
    """Сессия без сети: запоминает вызванные методы Bot API."""

    def __init__(self) -> None:
        super().__init__()
        self.methods: list[str] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.methods.append(type(method).__name__)
        if isinstance(method, SendMessage):
            return Message(message_id=1, date=NOW, chat=Chat(id=OWNER, type="private"))
        return True

    async def close(self) -> None:
        """Соединений нет."""

    async def stream_content(self, *_: object, **__: object) -> Any:
        raise NotImplementedError


def _detach_routers() -> None:
    """Роутеры — объекты уровня модуля и крепятся к диспетчеру один раз за процесс (см. ниже)."""
    from rcbot.bot.dialogs.menu import menu_dialog
    from rcbot.bot.handlers import ack, commands

    for router in (ack.router, commands.router, menu_dialog):
        router._parent_router = None


def build() -> tuple[Dispatcher, Bot, CountingController, RecordingSession]:
    _detach_routers()
    controller = CountingController()
    dispatcher = build_dispatcher(controller, TZ, OWNER)  # type: ignore[arg-type]
    session = RecordingSession()
    return dispatcher, Bot(TOKEN, session=session), controller, session


def message_update(user_id: int | None, chat_id: int, text: str, update_id: int = 1) -> Update:
    sender = User(id=user_id, is_bot=False, first_name="x") if user_id is not None else None
    message = Message(
        message_id=7,
        date=NOW,
        chat=Chat(id=chat_id, type="private" if chat_id > 0 else "group"),
        from_user=sender,
        text=text,
    )
    return Update(update_id=update_id, message=message)


def callback_update(user_id: int, chat_id: int | None, data: str) -> Update:
    message = (
        Message(message_id=7, date=NOW, chat=Chat(id=chat_id, type="private"), text="тревога")
        if chat_id is not None
        else None
    )
    query = CallbackQuery(
        id="cb1",
        from_user=User(id=user_id, is_bot=False, first_name="x"),
        chat_instance="ci",
        message=message,
        data=data,
    )
    return Update(update_id=2, callback_query=query)


# --- устройство ---


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T8: build_dispatcher нельзя вызвать дважды в одном процессе: роутеры ack, "
    "commands и menu_dialog — объекты уровня модуля, и aiogram отказывает «Router is already "
    "attached» (bot/app.py:~37). Мешает тестам и перезапуску Telegram-части без процесса",
)
def test_build_dispatcher_можно_вызвать_повторно() -> None:
    from rcbot.bot.app import build_dispatcher as make

    make(CountingController(), TZ, OWNER)  # type: ignore[arg-type]
    make(CountingController(), TZ, OWNER)  # type: ignore[arg-type]


def test_фильтр_владельца_стоит_внешним_middleware_для_message_и_callback_query() -> None:
    dispatcher, *_ = build()
    for observer in (dispatcher.message, dispatcher.callback_query):
        owners = [m for m in observer.outer_middleware if isinstance(m, OwnerOnlyMiddleware)]
        assert len(owners) == 1
        assert owners[0]._owner == OWNER


def test_разрешены_только_message_и_callback_query() -> None:
    assert ALLOWED_UPDATES == ["message", "callback_query"]


# Внутренние наблюдатели фреймворков (диспетчер aiogram и aiogram-dialog): не обработчики бота
FRAMEWORK_OBSERVERS = {"update", "aiogd_update", "error"}


def _stray_handlers() -> list[str]:
    dispatcher, *_ = build()
    routers = list(dispatcher.chain_tail)
    assert len(routers) >= 4
    allowed = {"message", "callback_query"} | FRAMEWORK_OBSERVERS
    return [
        f"{router.name}.{name}"
        for router in routers
        for name, observer in router.observers.items()
        if observer.handlers and name not in allowed
    ]


def test_обработчики_только_у_message_и_callback_query_кроме_известной_business_message() -> None:
    """Ratchet: набор «чужих» обработчиков не растёт сверх известного отступления."""
    assert {s.split(".", 1)[1] for s in _stray_handlers()} <= {"business_message"}


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T8: aiogram-dialog регистрирует обработчики business_message (меню), а фильтр "
    "владельца стоит только на message и callback_query (bot/app.py:~34). От чужих такие "
    "обновления спасает только allowed_updates; ожидается фильтр на dispatcher.update "
    "(outer middleware по Update) либо на business_message",
)
def test_нет_обработчиков_вне_фильтра_владельца() -> None:
    assert _stray_handlers() == []


def test_все_обработчики_лежат_под_диспетчером_с_фильтром() -> None:
    dispatcher, *_ = build()
    for router in dispatcher.chain_tail:
        if router is dispatcher:
            continue
        parents = []
        node = router.parent_router
        while node is not None:
            parents.append(node)
            node = node.parent_router
        assert dispatcher in parents, router.name


def test_у_подчинённых_роутеров_нет_собственных_обходов_фильтра() -> None:
    """Фильтр владельца единственный внешний: подмена `data` или пропуск не регистрируются."""
    dispatcher, *_ = build()
    for router in dispatcher.chain_tail:
        if router is dispatcher:
            continue
        for name in ("message", "callback_query"):
            for mw in router.observers[name].outer_middleware:
                assert not isinstance(mw, OwnerOnlyMiddleware)


# --- поведение ---

COMMANDS = [
    "/start",
    "/help",
    "/status",
    "/pause 30",
    "/pause",
    "/resume",
    "/test",
    "/last",
    "/interval 5",
    "/interval 20",
    "/relogin",
    "/cleanup",
    "/menu",
    "ок",
    "вижу",
    "да",
    "random text",
]
CALLBACKS = ["ack", "manual_done", "x", "", "iv:20", "menu:status"]
ids = st.integers(min_value=-(2**40), max_value=2**40).filter(lambda n: n != OWNER)


async def _feed(update: Update) -> tuple[CountingController, RecordingSession]:
    dispatcher, bot, controller, session = build()
    await dispatcher.feed_update(bot, update)
    return controller, session


@given(user=ids, chat=ids, text=st.sampled_from(COMMANDS), update_id=st.integers(1, 10**6))
async def test_чужое_сообщение_не_вызывает_controller_и_не_получает_ответа(
    user: int, chat: int, text: str, update_id: int
) -> None:
    controller, session = await _feed(message_update(user, chat, text, update_id))
    assert controller.calls == []
    assert session.methods == []


@given(chat=ids, text=st.sampled_from(COMMANDS))
async def test_владелец_в_чужом_чате_например_в_группе_отвергается(chat: int, text: str) -> None:
    controller, session = await _feed(message_update(OWNER, chat, text))
    assert controller.calls == [] and session.methods == []


@given(user=ids, text=st.sampled_from(COMMANDS))
async def test_чужой_пользователь_в_чате_владельца_отвергается(user: int, text: str) -> None:
    controller, session = await _feed(message_update(user, OWNER, text))
    assert controller.calls == [] and session.methods == []


@pytest.mark.parametrize("text", COMMANDS)
async def test_сообщение_без_отправителя_отвергается(text: str) -> None:
    controller, session = await _feed(message_update(None, OWNER, text))
    assert controller.calls == [] and session.methods == []


@given(user=ids, chat=st.one_of(st.none(), ids), data=st.sampled_from(CALLBACKS))
async def test_чужая_кнопка_не_вызывает_controller_и_не_получает_ответа(
    user: int, chat: int | None, data: str
) -> None:
    controller, session = await _feed(callback_update(user, chat, data))
    assert controller.calls == [] and session.methods == []


@pytest.mark.parametrize("data", ["ack", "manual_done"])
async def test_кнопка_владельца_в_чужом_чате_отвергается(data: str) -> None:
    controller, session = await _feed(callback_update(OWNER, 99, data))
    assert controller.calls == [] and session.methods == []


def _other_updates() -> dict[str, Update]:
    base = dict(message_id=9, date=NOW, chat=Chat(id=777, type="private"), text="/status")
    stranger = User(id=777, is_bot=False, first_name="x")
    return {
        "edited_message": Update(update_id=3, edited_message=Message(**base, from_user=stranger)),
        "channel_post": Update(update_id=4, channel_post=Message(**base)),
        "business_message": Update(
            update_id=5,
            business_message=Message(**base, from_user=stranger, business_connection_id="bc"),
        ),
    }


@pytest.mark.parametrize("kind", ["edited_message", "channel_post", "business_message"])
async def test_обновления_других_типов_от_чужих_не_вызывают_controller_и_ответов(
    kind: str,
) -> None:
    dispatcher, bot, controller, session = build()
    await dispatcher.feed_update(bot, _other_updates()[kind])
    assert controller.calls == [] and session.methods == []


# --- контроль: владельца фильтр пропускает ---


async def test_контроль_владелец_получает_ответ_на_help() -> None:
    controller, session = await _feed(message_update(OWNER, OWNER, "/help"))
    assert "SendMessage" in session.methods
    assert controller.calls == []


@pytest.mark.parametrize("text", ["ок", "вижу"])
async def test_контроль_слово_подтверждения_от_владельца_доходит_до_controller(text: str) -> None:
    controller, session = await _feed(message_update(OWNER, OWNER, text))
    assert controller.calls == ["ack"]
    assert "SendMessage" in session.methods


async def test_контроль_кнопка_вижу_от_владельца_доходит_до_controller() -> None:
    controller, session = await _feed(callback_update(OWNER, OWNER, "ack"))
    assert controller.calls == ["ack"]
    assert AnswerCallbackQuery.__name__ in session.methods


async def test_контроль_сканер_слеп_без_фильтра() -> None:
    """Отрицательный контроль: без OwnerOnlyMiddleware чужой доходит до controller."""
    _detach_routers()
    controller = CountingController()
    dispatcher = Dispatcher(controller=controller, tz=TZ)
    from rcbot.bot.handlers import ack

    dispatcher.include_router(ack.router)
    bot = Bot(TOKEN, session=RecordingSession())
    await dispatcher.feed_update(bot, message_update(12345, 12345, "ок"))
    assert controller.calls == ["ack"]
