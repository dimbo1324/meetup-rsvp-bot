"""Фейковый Telegram: подмена сессии бота и построители обновлений для тестов."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiogram_dialog import setup_dialogs

from meetup_bot.config import Settings
from meetup_bot.dialogs import MODULES as DIALOG_MODULES
from meetup_bot.handlers import build_router
from meetup_bot.telegram.attendance_ui import AttendanceUI
from meetup_bot.telegram.notifier import TelegramNotifier
from meetup_bot.telegram.post_updater import PostUpdater
from tests.helpers import App

_MESSAGE_METHODS = {
    "SendMessage",
    "EditMessageText",
    "SendDocument",
    "EditMessageCaption",
    "SendPhoto",
}


class FakeSession(BaseSession):
    """Сессия бота без сети: записывает вызовы Bot API и отвечает правдоподобно.

    `fail` — функция `(имя_метода, данные) -> исключение | None` для имитации ошибок Telegram.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.msg_id = 100
        self.last_markup: dict[int, dict[str, Any]] = {}
        self.fail: Any = None

    async def close(self) -> None: ...

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        data = method.model_dump(exclude_none=True)
        name = type(method).__name__
        self.calls.append((name, data))
        if self.fail is not None:
            error = self.fail(name, data)
            if error is not None:
                raise error
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="Bot", username="mybot")
        if name in _MESSAGE_METHODS:
            if name.startswith("Send"):
                self.msg_id += 1
                mid = self.msg_id
            else:
                mid = data.get("message_id", self.msg_id)
            if data.get("reply_markup"):
                self.last_markup[mid] = data["reply_markup"]
            return Message(
                message_id=mid,
                date=datetime.now(UTC),
                chat=Chat(id=data.get("chat_id", 0) or 0, type="private"),
                text=data.get("text", "x"),
            )
        if name == "EditMessageReplyMarkup" and data.get("reply_markup"):
            self.last_markup[data.get("message_id", 0)] = data["reply_markup"]
        return True

    def of(self, name: str) -> list[dict[str, Any]]:
        """Данные всех вызовов метода Bot API с таким именем (в порядке вызова)."""
        return [data for call, data in self.calls if call == name]

    def buttons(self, message_id: int) -> list[tuple[str, str]]:
        """Кнопки последней inline-клавиатуры сообщения: (текст, callback_data)."""
        markup = self.last_markup.get(message_id)
        if not markup:
            return []
        return [
            (b["text"], b.get("callback_data", ""))
            for row in markup["inline_keyboard"]
            for b in row
        ]

    @property
    def last_message_id(self) -> int:
        return max(self.last_markup) if self.last_markup else 0


def tg_user(user_id: int, name: str | None = None) -> User:
    first_name = name or f"Люди{user_id}"
    return User(id=user_id, is_bot=False, first_name=first_name, username=f"u{user_id}")


_counter = [1000]


def next_id() -> int:
    _counter[0] += 1
    return _counter[0]


def chat(chat_id: int) -> Chat:
    return Chat(id=chat_id, type="private")


def command_update(user_id: int, text: str) -> Update:
    entity = {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=chat(user_id),
            from_user=tg_user(user_id),
            text=text,
            entities=[entity],
        ),
    )


def text_update(user_id: int, text: str) -> Update:
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=chat(user_id),
            from_user=tg_user(user_id),
            text=text,
        ),
    )


def callback_update(
    user_id: int,
    data: str,
    *,
    inline_message_id: str | None = None,
    message_id: int | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> Update:
    """Нажатие кнопки: под inline-постом (`inline_message_id`) либо под сообщением в личке."""
    message = None
    if message_id is not None:
        message = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=chat(user_id),
            reply_markup=reply_markup,
        )
    return Update(
        update_id=next_id(),
        callback_query=CallbackQuery(
            id=str(next_id()),
            from_user=tg_user(user_id),
            chat_instance="ci",
            data=data,
            inline_message_id=inline_message_id,
            message=message,
        ),
    )


_HANDLER_MODULES = (
    "meetup_bot.handlers.attendance",
    "meetup_bot.handlers.errors",
    "meetup_bot.handlers.inline",
    "meetup_bot.handlers.signup",
    "meetup_bot.handlers.start",
)


def detach_module_routers() -> None:
    """Отвязывает роутеры хендлеров и диалогов от прежнего родителя.

    Роутеры в приложении — модульные синглтоны, а aiogram разрешает подключить роутер
    к родителю только один раз. В бою корень строится один раз, тестам нужен свежий
    диспетчер на каждый тест.
    """
    for name in (*_HANDLER_MODULES, *DIALOG_MODULES):
        module = importlib.import_module(name)
        found: list[Router] = [v for v in vars(module).values() if isinstance(v, Router)]
        found.extend(getattr(module, "DIALOGS", []))
        for router in found:
            router._parent_router = None


class Harness:
    """Диспетчер с настоящими роутерами и диалогами поверх фейковой сессии бота."""

    def __init__(self, app: App, *, env: str = "dev") -> None:
        self.app = app
        self.session = FakeSession()
        self.bot = Bot(
            "123456:TEST-TOKEN",
            session=self.session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.notifier = TelegramNotifier(self.bot, app.sessions, app.clock, -100500)
        self.updater = PostUpdater(
            self.bot, app.sessions, app.clock, app.services.settings, debounce=0.05, pause=0
        )
        self.attendance_ui = AttendanceUI(self.bot, app.sessions, app.clock, app.services)
        self.notifier.attach_attendance_ui(self.attendance_ui)
        self.config = Settings(
            _env_file=None,  # type: ignore[call-arg]
            bot_token="123456:TEST-TOKEN",
            owner_ids="1",  # type: ignore[arg-type]
            service_chat_id=-100500,
            env=env,  # type: ignore[arg-type]
        )
        self.dispatcher = Dispatcher()
        self.dispatcher.workflow_data.update(
            services=app.services,
            sessions=app.sessions,
            post_updater=self.updater,
            bot_username="mybot",
            app_config=self.config,
            clock=app.clock,
            notifier=self.notifier,
            attendance_ui=self.attendance_ui,
        )
        detach_module_routers()
        self.dispatcher.include_router(build_router())
        setup_dialogs(self.dispatcher)

    async def feed(self, update: Update) -> Any:
        return await self.dispatcher.feed_update(self.bot, update)

    async def click(
        self, user_id: int, message_id: int, label: str, *, expect: bool = True
    ) -> bool:
        """Нажимает кнопку диалога по подстроке подписи; False, если такой кнопки нет."""
        found = [data for text, data in self.session.buttons(message_id) if label in text]
        if not found:
            assert not expect, (
                f"нет кнопки «{label}»: {[t for t, _ in self.session.buttons(message_id)]}"
            )
            return False
        await self.feed(
            callback_update(
                user_id,
                found[0],
                message_id=message_id,
                reply_markup=self.session.last_markup.get(message_id),
            )
        )
        return True

    async def close(self) -> None:
        await self.updater.aclose()
