"""Фейковый Telegram: подмена сессии бота и построители обновлений для тестов."""

from __future__ import annotations

import html
import importlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import Default, DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, Update, User
from aiogram_dialog import setup_dialogs

from meetup_bot.config import Settings
from meetup_bot.dialogs import MODULES as DIALOG_MODULES
from meetup_bot.handlers import build_router
from meetup_bot.telegram.attendance_ui import AttendanceUI
from meetup_bot.telegram.notifier import TelegramNotifier
from meetup_bot.telegram.post_updater import PostUpdater
from meetup_bot.texts.render import telegram_length
from tests.helpers import App

_SEND_METHODS = {"SendMessage", "SendDocument", "SendPhoto"}
_EDIT_METHODS = {
    "EditMessageText",
    "EditMessageCaption",
    "EditMessageMedia",
    "EditMessageReplyMarkup",
}
_MESSAGE_METHODS = _SEND_METHODS | _EDIT_METHODS

# Лимиты Bot API
CALLBACK_DATA_LIMIT = 64  # байт UTF-8
TEXT_LIMIT = 4096  # единиц UTF-16, после разбора разметки
CAPTION_LIMIT = 1024
ANSWER_LIMIT = 200

# Момент по умолчанию совпадает с часами фикстуры `clock` (четверг 12:00 UTC)
DEFAULT_NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

_TAG_RE = re.compile(r"<[^>]*>")


def chat_type(chat_id: int) -> str:
    """Тип чата по знаку id: личка у положительных, супергруппа у отрицательных."""
    return "private" if chat_id > 0 else "supergroup"


def _visible_length(text: str, parse_mode: Any) -> int:
    """Длина текста, как её считает Telegram: после разбора HTML, в единицах UTF-16."""
    if str(getattr(parse_mode, "value", parse_mode)).upper() == "HTML":
        text = html.unescape(_TAG_RE.sub("", text))
    return telegram_length(text)


class FakeSession(BaseSession):
    """Сессия бота без сети: записывает вызовы Bot API и отвечает как настоящий Telegram.

    `fail` — функция `(имя_метода, данные) -> исключение | None` для имитации ошибок Telegram.
    `now` — источник времени для `Message.date` (тесты не зависят от реальных часов).
    Лимиты Bot API проверяются: нарушение даёт `TelegramBadRequest`, как у Telegram.
    """

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.msg_id = 100
        self.last_markup: dict[int, dict[str, Any]] = {}
        self.removed_markup: set[int] = set()
        self.fail: Any = None
        self._now = now or (lambda: DEFAULT_NOW)

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
        self._check_limits(method, name, data, bot)
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="Bot", username="mybot")
        if name in _MESSAGE_METHODS:
            return self._message_response(name, data)
        return True

    @staticmethod
    def _reject(method: Any, text: str) -> TelegramBadRequest:
        return TelegramBadRequest(method=method, message=f"Bad Request: {text}")

    def _check_limits(self, method: Any, name: str, data: dict[str, Any], bot: Bot) -> None:
        default_mode = bot.default.parse_mode
        mode = data.get("parse_mode")
        if isinstance(mode, Default):
            mode = default_mode
        text = data.get("text")
        is_text_method = name in {"SendMessage", "EditMessageText"}
        if is_text_method and isinstance(text, str) and _visible_length(text, mode) > TEXT_LIMIT:
            raise self._reject(method, "message is too long")
        caption = data.get("caption")
        media = data.get("media")
        if name == "EditMessageMedia" and isinstance(media, dict):
            caption = media.get("caption")
            media_mode = media.get("parse_mode")
            mode = default_mode if isinstance(media_mode, Default) else media_mode
        if isinstance(caption, str) and _visible_length(caption, mode) > CAPTION_LIMIT:
            raise self._reject(method, "message caption is too long")
        markup = data.get("reply_markup")
        if isinstance(markup, dict):
            for row in markup.get("inline_keyboard", []):
                for button in row:
                    cb = button.get("callback_data")
                    if cb is not None and not 1 <= len(cb.encode("utf-8")) <= CALLBACK_DATA_LIMIT:
                        raise self._reject(method, "BUTTON_DATA_INVALID")
        if name == "AnswerCallbackQuery":
            answer = data.get("text")
            if isinstance(answer, str) and telegram_length(answer) > ANSWER_LIMIT:
                raise self._reject(method, "MESSAGE_TOO_LONG: текст ответа на callback > 200")

    def _message_response(self, name: str, data: dict[str, Any]) -> Message | bool:
        """Ответ на Send*/Edit* по типу метода; правка по inline_message_id даёт True."""
        if name in _SEND_METHODS:
            self.msg_id += 1
            mid = self.msg_id
        else:
            if "inline_message_id" in data:
                return True
            mid = data.get("message_id", self.msg_id)
        markup = data.get("reply_markup")
        if markup:
            self.last_markup[mid] = markup
            self.removed_markup.discard(mid)
        elif name in _EDIT_METHODS and self.last_markup.pop(mid, None) is not None:
            # правка без клавиатуры снимает прежнюю — так делает и Telegram
            self.removed_markup.add(mid)
        chat_id = data.get("chat_id", 0)
        chat_id = chat_id if isinstance(chat_id, int) else 0
        fields: dict[str, Any] = {}
        if name in {"SendMessage", "EditMessageText"}:
            fields["text"] = data.get("text", "")
        elif name in {"SendPhoto", "SendDocument", "EditMessageCaption"}:
            fields["caption"] = data.get("caption")
        if name == "SendPhoto":
            fields["photo"] = [self._photo(data.get("photo"))]
        elif name == "SendDocument":
            fields["document"] = self._document(data.get("document"))
        elif name == "EditMessageMedia":
            media = data.get("media", {})
            fields["caption"] = media.get("caption")
            if media.get("type") == "photo":
                fields["photo"] = [self._photo(media.get("media"))]
            else:
                fields["document"] = self._document(media.get("media"))
        inline = markup if isinstance(markup, dict) and "inline_keyboard" in markup else None
        return Message(
            message_id=mid,
            date=self._now(),
            chat=Chat(id=chat_id, type=chat_type(chat_id)),
            reply_markup=inline,
            **fields,
        )

    @staticmethod
    def _photo(source: Any) -> PhotoSize:
        file_id = source if isinstance(source, str) else "photo-file"
        return PhotoSize(file_id=file_id, file_unique_id=f"u-{file_id}", width=10, height=10)

    @staticmethod
    def _document(source: Any) -> Document:
        file_id = source if isinstance(source, str) else "document-file"
        return Document(file_id=file_id, file_unique_id=f"u-{file_id}")

    def of(self, name: str) -> list[dict[str, Any]]:
        """Данные всех вызовов метода Bot API с таким именем (в порядке вызова)."""
        return [data for call, data in self.calls if call == name]

    def calls_with(self, key: str) -> list[dict[str, Any]]:
        """Данные всех вызовов Bot API, в которых есть поле `key` (например, `caption`)."""
        return [data for _, data in self.calls if key in data]

    def buttons(self, message_id: int) -> list[tuple[str, str]]:
        """Кнопки последней inline-клавиатуры сообщения: (текст, callback_data)."""
        markup = self.last_markup.get(message_id)
        if not markup:
            return []
        return [
            (b["text"], b.get("callback_data", ""))
            for row in markup.get("inline_keyboard", [])
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
    return Chat(id=chat_id, type=chat_type(chat_id))


def command_update(user_id: int, text: str) -> Update:
    entity = {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=DEFAULT_NOW,
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
            date=DEFAULT_NOW,
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
            date=DEFAULT_NOW,
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
        self.session = FakeSession(app.clock.now)
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
        buttons = self.session.buttons(message_id)
        found = [data for text, data in buttons if label in text]
        if not found:
            assert not expect, (
                f"нет кнопки «{label}» у сообщения {message_id}: {[t for t, _ in buttons]}"
                + (
                    " (клавиатуру сняла правка сообщения)"
                    if message_id in self.session.removed_markup
                    else ""
                )
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
