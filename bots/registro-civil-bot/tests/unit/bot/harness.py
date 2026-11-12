"""Харнесс Telegram-тестов: фейковая сессия aiogram, настоящий Dispatcher и настоящий Controller.

Сеть не нужна: `FakeSession` вместо HTTP-сессии записывает каждый вызов Bot API и отвечает
заготовленным JSON, который проходит ту же проверку `check_response`, что и боевой ответ, —
поэтому ошибки Telegram (429, 400, 403…) приходят настоящими исключениями aiogram.
`Controller` собран на настоящем `PollLoop`, `AlertOrchestrator`, `IncidentBook`, `SessionManager`
и фейках из `tests/fakes.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, get_args
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from rcbot.application.alerting import AlertOrchestrator, RepeatPolicy
from rcbot.application.bus import EventBus
from rcbot.application.control import Controller
from rcbot.application.incidents import IncidentBook
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import NoopCoordinator
from rcbot.application.runtime import RuntimeSettings, RuntimeStore
from rcbot.application.session_manager import SessionManager
from rcbot.bot.app import build_dispatcher
from rcbot.domain.detector import Detector
from rcbot.domain.models import Channel
from tests.fakes import (
    AGENDA_URL,
    TARGET,
    FakeAuthenticator,
    FakeNotifier,
    FakeSource,
    ManualClock,
    MemoryEventLog,
    MemoryIncidentRepository,
    MemoryKV,
    MemoryStatsRepository,
    RecordingJournal,
    settle,
)

OWNER = 1_000_001
STRANGER = 2_000_002
GROUP = -1_003_000_003
TZ = ZoneInfo("America/Montevideo")
FAKE_TOKEN = "123456:TEST-TOKEN-NOT-REAL"
T = TypeVar("T", bound=TelegramMethod[Any])


@dataclass(frozen=True)
class ApiFailure:
    """Ответ Bot API с ошибкой: превращается в настоящее исключение aiogram."""

    status: int
    description: str
    retry_after: int | None = None


@dataclass(frozen=True)
class Call:
    method: TelegramMethod[Any]
    timeout: int | None


class FakeSession(BaseSession):
    """Вместо HTTP: записывает вызовы и отвечает по сценарию; в сеть не ходит."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Call] = []
        self.closed = False
        self._script: dict[type, list[BaseException | ApiFailure | None]] = {}
        self._next_id = 500

    def script(self, method_type: type, *outcomes: BaseException | ApiFailure | None) -> None:
        """Исходы первых вызовов метода по порядку; `None` — обычный успех."""
        self._script.setdefault(method_type, []).extend(outcomes)

    def of(self, method_type: type[T]) -> list[T]:
        return [c.method for c in self.calls if isinstance(c.method, method_type)]  # type: ignore[misc]

    @property
    def sent(self) -> list[SendMessage]:
        return self.of(SendMessage)

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return self.of(AnswerCallbackQuery)

    @property
    def sent_texts(self) -> list[str]:
        return [m.text for m in self.sent]

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура задана BaseSession
    ) -> Any:
        self.calls.append(Call(method, timeout))
        await asyncio.sleep(0)  # как настоящая сеть: отдаёт управление циклу событий
        queue = self._script.get(type(method))
        outcome = queue.pop(0) if queue else None
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, ApiFailure):
            parameters = (
                {"parameters": {"retry_after": outcome.retry_after}}
                if outcome.retry_after is not None
                else {}
            )
            content = {
                "ok": False,
                "error_code": outcome.status,
                "description": outcome.description,
            }
            self.check_response(bot, method, outcome.status, json.dumps(content | parameters))
        return self.check_response(
            bot, method, 200, json.dumps({"ok": True, "result": self._result(method)})
        ).result

    def _result(self, method: TelegramMethod[Any]) -> Any:
        returning = method.__returning__
        if returning is Message or Message in get_args(returning):
            self._next_id += 1
            chat_id = getattr(method, "chat_id", OWNER)
            return {
                "message_id": getattr(method, "message_id", None) or self._next_id,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "private"},
                "text": getattr(method, "text", ""),
            }
        if getattr(returning, "__origin__", None) is list:
            return []
        if returning is User:
            return {"id": 123456, "is_bot": True, "first_name": "rcbot", "username": "rcbot_test"}
        return True

    async def close(self) -> None:
        self.closed = True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("в тестах файлы не скачиваются")


def make_bot(session: FakeSession | None = None) -> tuple[Bot, FakeSession]:
    session = session or FakeSession()
    return Bot(FAKE_TOKEN, session=session), session


# --- управление на настоящих классах приложения и фейках ---


@dataclass
class Rig:
    """Всё, что нужно тестам команд, кнопок и меню."""

    controller: Controller
    loop: PollLoop
    alerts: AlertOrchestrator
    book: IncidentBook
    runtime: RuntimeSettings
    kv: MemoryKV
    clock: ManualClock
    source: FakeSource
    auth: FakeAuthenticator
    notifiers: dict[Channel, FakeNotifier]
    incidents: MemoryIncidentRepository
    event_log: MemoryEventLog
    journal: RecordingJournal
    session: SessionManager
    cleanup_calls: list[int]
    liveness: list[str]


async def make_rig(
    *,
    cleanup: dict[str, int] | None = None,
    folder_bytes: int | None = 5 * 1024 * 1024,
    journal_bytes: int | None = 2048,
    relogin_failures: tuple[BaseException, ...] = (),
) -> Rig:
    clock = ManualClock()
    kv = MemoryKV()
    store = RuntimeStore(kv)
    runtime = await store.load(20.0, 6, clock.now())
    source = FakeSource([frozenset()], clock=clock)
    auth = FakeAuthenticator(failures=relogin_failures)
    session = SessionManager(auth, clock, 6)
    journal = RecordingJournal()
    incidents = MemoryIncidentRepository()
    event_log = MemoryEventLog()
    loop = PollLoop(
        primary=source,
        fallback=None,
        session=session,
        detector=Detector(),
        bus=EventBus(),
        clock=clock,
        runtime=runtime,
        runtime_store=store,
        stats=MemoryStatsRepository(),
        event_log=event_log,
        target=TARGET,
        jitter=0.0,
        journal=journal,
    )
    notifiers = {c: FakeNotifier(c) for c in Channel}
    book = IncidentBook(incidents, clock, TARGET.id)
    alerts = AlertOrchestrator(
        dict(notifiers),  # type: ignore[arg-type]
        {c: RepeatPolicy(60.0, 3) for c in Channel},
        MessageComposer(TZ, AGENDA_URL, "TEST-TRAMITE"),
        book,
        session,
        runtime,
        clock,
        NoopCoordinator(),
        journal,
    )
    cleanup_calls: list[int] = []
    liveness = [""]

    async def do_cleanup() -> dict[str, int]:
        cleanup_calls.append(1)
        return dict(cleanup or {})

    controller = Controller(
        loop,
        alerts,
        book,
        incidents,
        event_log,
        session,
        clock,
        cleanup=do_cleanup,
        folder_size=(lambda: folder_bytes) if folder_bytes is not None else None,
        journal=journal,
        journal_size=(lambda: journal_bytes) if journal_bytes is not None else None,
        liveness=lambda: liveness[0],
    )
    return Rig(
        controller=controller,
        loop=loop,
        alerts=alerts,
        book=book,
        runtime=runtime,
        kv=kv,
        clock=clock,
        source=source,
        auth=auth,
        notifiers=notifiers,
        incidents=incidents,
        event_log=event_log,
        journal=journal,
        session=session,
        cleanup_calls=cleanup_calls,
        liveness=liveness,
    )


def snapshot_of_state(rig: Rig) -> dict[str, object]:
    """Всё, что команда способна изменить: по нему видно, что чужой не изменил ничего."""
    runtime = rig.runtime
    return {
        "kv": dict(rig.kv.data),
        "interval": runtime.base_interval_sec,
        "months": runtime.months_ahead,
        "paused_until": runtime.paused_until,
        "channels_off": set(runtime.channels_off),
        "journal": list(rig.journal.events),
        "logins": rig.auth.logins,
        "cleanup": list(rig.cleanup_calls),
        "sent": {c: list(n.sent) for c, n in rig.notifiers.items()},
        "wake": rig.loop._wake.is_set(),
        "repeating": rig.alerts.repeating(),
        "incidents": dict(rig.incidents.incidents),
    }


# --- Telegram-обновления ---


class Chatter:
    """Собирает входящие апдейты от имени пользователя и скармливает их Dispatcher."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        *,
        user_id: int = OWNER,
        chat_id: int | None = None,
        chat_type: str = "private",
    ) -> None:
        self.dp = dispatcher
        self.bot = bot
        self.user = User(id=user_id, is_bot=False, first_name="Тест")
        self.chat = Chat(id=user_id if chat_id is None else chat_id, type=chat_type)
        self._update_id = 0
        self._message_id = 0
        self.last_callback: CallbackQuery | None = None

    def _next_update(self) -> int:
        self._update_id += 1
        return self._update_id

    def message(self, text: str | None) -> Message:
        self._message_id += 1
        return Message(
            message_id=self._message_id,
            date=1_700_000_000,  # type: ignore[arg-type]
            chat=self.chat,
            from_user=self.user,
            text=text,
        )

    async def say(self, text: str | None) -> Any:
        update = Update(update_id=self._next_update(), message=self.message(text))
        return await self.dp.feed_update(self.bot, update)

    async def press(
        self,
        data: str,
        *,
        on: Message | None = None,
        with_message: bool = True,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> Any:
        """Нажатие inline-кнопки. `on` — сообщение с клавиатурой (по умолчанию — условное)."""
        message = on if on is not None else self.message("клавиатура")
        if keyboard is not None:
            message = message.model_copy(update={"reply_markup": keyboard})
        self._update_id += 1
        callback = CallbackQuery(
            id=f"cb{self._update_id}",
            from_user=self.user,
            chat_instance="test",
            message=message if with_message else None,
            data=data,
        )
        self.last_callback = callback
        return await self.dp.feed_update(
            self.bot, Update(update_id=self._update_id, callback_query=callback)
        )


@dataclass
class Setup:
    rig: Rig
    bot: Bot
    session: FakeSession
    dp: Dispatcher
    owner: Chatter


async def make_setup(**rig_kwargs: Any) -> Setup:
    rig = await make_rig(**rig_kwargs)
    bot, session = make_bot()
    dp = build_dispatcher(rig.controller, TZ, OWNER)
    return Setup(rig, bot, session, dp, Chatter(dp, bot))


def stranger_of(setup: Setup, **kwargs: Any) -> Chatter:
    kwargs.setdefault("user_id", STRANGER)
    return Chatter(setup.dp, setup.bot, **kwargs)


async def settled() -> None:
    await settle()


Awaiter = Callable[[], Awaitable[Any]]
