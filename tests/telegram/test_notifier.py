"""TelegramNotifier: личные и служебные сообщения, нарезка текста, запрос отметки явки."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from meetup_bot.db.models import User
from meetup_bot.repositories import users
from meetup_bot.telegram import notifier as notifier_module
from meetup_bot.telegram.attendance_ui import SheetStatus
from meetup_bot.telegram.notifier import (
    CHUNK_LIMIT,
    TelegramNotifier,
    split_text,
)
from meetup_bot.texts.render import telegram_length
from meetup_bot.texts.service import BACKUP_CAPTION, SERVICE_CHAT_UNAVAILABLE_TEXT
from meetup_bot.user_info import UserInfo
from tests.helpers import App
from tests.tg_fake import Harness

SERVICE_CHAT = -100500
_METHOD = SendMessage(chat_id=1, text="x")
_real_sleep = asyncio.sleep


async def make_user(app: App, user_id: int = 77, *, dm: bool = True) -> None:
    async with app.sessions() as session, session.begin():
        await users.upsert(session, UserInfo(user_id, "Анна"), app.clock.now())
        await users.set_dm_available(session, user_id, dm, app.clock.now())


async def dm_flag(app: App, user_id: int) -> bool:
    async with app.sessions() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user.dm_available


def fail_with(tg: Harness, *errors: Exception) -> None:
    queue = list(errors)

    def hook(name: str, _data: dict[str, Any]) -> Exception | None:
        if name == "SendMessage" and queue:
            return queue.pop(0)
        return None

    tg.session.fail = hook


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(delay: float, result: Any = None) -> Any:
        delays.append(delay)
        await _real_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


class StubSheetUI:
    def __init__(self, status: SheetStatus) -> None:
        self.status = status
        self.calls: list[tuple[int, int]] = []

    async def send_sheet(self, admin_id: int, event_id: int) -> SheetStatus:
        self.calls.append((admin_id, event_id))
        return self.status


# ---- split_text ----


def test_короткий_текст_остаётся_одним_куском() -> None:
    assert split_text("привет") == ["привет"]


def test_пустой_текст_даёт_один_пустой_кусок() -> None:
    assert split_text("") == [""]


def test_длинный_текст_режется_по_переносам_строк() -> None:
    text = "\n".join(["а" * 30] * 10)
    chunks = split_text(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks) == text
    assert len(chunks) > 1


def test_слишком_длинная_строка_режется_жёстко() -> None:
    chunks = split_text("б" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_жёсткая_нарезка_сохраняет_предыдущие_строки() -> None:
    chunks = split_text("коротко\n" + "в" * 150, limit=100)
    assert chunks[0] == "коротко"
    assert "".join(chunks[1:]) == "в" * 150


# ---- send_dm ----


async def test_личное_сообщение_доставляется(app: App, tg: Harness) -> None:
    await make_user(app)
    assert await tg.notifier.send_dm(77, "Привет") is True
    (call,) = tg.session.of("SendMessage")
    assert (call["chat_id"], call["text"]) == (77, "Привет")


async def test_forbidden_отключает_личку_в_базе(app: App, tg: Harness) -> None:
    await make_user(app)
    fail_with(tg, TelegramForbiddenError(_METHOD, "Forbidden: bot was blocked by the user"))
    assert await tg.notifier.send_dm(77, "Привет") is False
    assert await dm_flag(app, 77) is False


async def test_forbidden_для_неизвестного_пользователя_не_падает(tg: Harness) -> None:
    fail_with(tg, TelegramForbiddenError(_METHOD, "Forbidden"))
    assert await tg.notifier.send_dm(555, "Привет") is False


async def test_retry_after_повторяет_и_доставляет(
    app: App, tg: Harness, sleeps: list[float]
) -> None:
    fail_with(tg, TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=4))
    assert await tg.notifier.send_dm(77, "Привет") is True
    assert len(tg.session.of("SendMessage")) == 2
    assert 4 in sleeps


async def test_retry_after_ограничивается_потолком(
    app: App, tg: Harness, sleeps: list[float]
) -> None:
    fail_with(tg, TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=900))
    assert await tg.notifier.send_dm(77, "Привет") is True
    assert 30.0 in sleeps


async def test_повторный_retry_after_сдаётся(app: App, tg: Harness, sleeps: list[float]) -> None:
    await make_user(app)
    fail_with(
        tg,
        TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=1),
        TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=1),
    )
    assert await tg.notifier.send_dm(77, "Привет") is False
    assert len(tg.session.of("SendMessage")) == 2
    # Лимит — не блокировка: флаг личных сообщений остаётся
    assert await dm_flag(app, 77) is True


async def test_прочая_ошибка_api_возвращает_false_без_повтора(app: App, tg: Harness) -> None:
    await make_user(app)
    fail_with(tg, TelegramBadRequest(_METHOD, "Bad Request: chat not found"))
    assert await tg.notifier.send_dm(77, "Привет") is False
    assert len(tg.session.of("SendMessage")) == 1
    assert await dm_flag(app, 77) is True


async def test_общая_ошибка_api_возвращает_false(tg: Harness) -> None:
    fail_with(tg, TelegramAPIError(_METHOD, "сбой"))
    assert await tg.notifier.send_dm(1, "x") is False


# ---- send_service ----


async def test_служебное_сообщение_уходит_в_служебный_чат(tg: Harness) -> None:
    await tg.notifier.send_service("Бот запущен")
    (call,) = tg.session.of("SendMessage")
    assert (call["chat_id"], call["text"]) == (SERVICE_CHAT, "Бот запущен")


async def test_длинное_служебное_сообщение_режется_на_части(tg: Harness) -> None:
    line = "ж" * 1000
    text = "\n".join([line] * 9)
    await tg.notifier.send_service(text)
    sent = [c["text"] for c in tg.session.of("SendMessage")]
    assert len(sent) > 1
    assert all(len(part) <= CHUNK_LIMIT for part in sent)
    assert "\n".join(sent) == text


async def test_ошибка_служебного_сообщения_не_пробрасывается(tg: Harness) -> None:
    fail_with(tg, TelegramForbiddenError(_METHOD, "Forbidden: bot was kicked"))
    await tg.notifier.send_service("текст")


async def test_ошибка_одной_части_не_мешает_остальным(tg: Harness) -> None:
    fail_with(tg, TelegramBadRequest(_METHOD, "Bad Request: can't parse entities"))
    await tg.notifier.send_service("\n".join(["з" * 3000] * 2))
    assert len(tg.session.of("SendMessage")) == 2


async def test_служебное_сообщение_повторяется_после_retry_after(
    tg: Harness, sleeps: list[float]
) -> None:
    fail_with(tg, TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=2))
    await tg.notifier.send_service("текст")
    assert len(tg.session.of("SendMessage")) == 2
    assert 2 in sleeps


async def test_повторная_ошибка_после_retry_after_не_пробрасывается(
    tg: Harness, sleeps: list[float]
) -> None:
    fail_with(
        tg,
        TelegramRetryAfter(_METHOD, "Too Many Requests", retry_after=2),
        TelegramBadRequest(_METHOD, "Bad Request"),
    )
    await tg.notifier.send_service("текст")
    assert len(tg.session.of("SendMessage")) == 2


# ---- send_attendance_prompt ----


async def test_запрос_явки_без_привязанного_интерфейса_возвращает_false(
    app: App, tg: Harness
) -> None:
    notifier = TelegramNotifier(tg.bot, app.sessions, app.clock, SERVICE_CHAT)
    assert await notifier.send_attendance_prompt(2, 1) is False
    assert tg.session.calls == []


async def test_запрос_явки_делегирует_привязанному_интерфейсу(app: App, tg: Harness) -> None:
    ui = StubSheetUI(SheetStatus.SENT)
    notifier = TelegramNotifier(tg.bot, app.sessions, app.clock, SERVICE_CHAT)
    notifier.attach_attendance_ui(ui)  # type: ignore[arg-type]
    assert await notifier.send_attendance_prompt(2, 5) is True
    assert ui.calls == [(2, 5)]


@pytest.mark.parametrize(
    "status", [SheetStatus.NO_ACCESS, SheetStatus.FAILED, SheetStatus.NOT_STARTED]
)
async def test_запрос_явки_false_если_список_не_отправлен(
    app: App, tg: Harness, status: SheetStatus
) -> None:
    notifier = TelegramNotifier(tg.bot, app.sessions, app.clock, SERVICE_CHAT)
    notifier.attach_attendance_ui(StubSheetUI(status))  # type: ignore[arg-type]
    assert await notifier.send_attendance_prompt(2, 5) is False


# ---- документы в служебный чат (бэкапы) ----


async def test_бэкап_уходит_документом_в_служебный_чат(tg: Harness, tmp_path: Path) -> None:
    backup = tmp_path / "bot-2026-10-30.db"
    backup.write_bytes(b"sqlite")
    await tg.notifier.send_service_document(backup)
    (call,) = tg.session.of("SendDocument")
    assert call["chat_id"] == SERVICE_CHAT
    assert call["caption"] == BACKUP_CAPTION.format(name=backup.name)


async def test_файл_больше_лимита_не_отправляется(
    tg: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "bot-2026-10-30.db"
    backup.write_bytes(b"0123456789")
    monkeypatch.setattr(notifier_module, "UPLOAD_LIMIT_BYTES", 5)
    await tg.notifier.send_service_document(backup)
    assert tg.session.of("SendDocument") == []


async def test_ошибка_отправки_документа_пробрасывается(tg: Harness, tmp_path: Path) -> None:
    backup = tmp_path / "bot-2026-10-30.db"
    backup.write_bytes(b"sqlite")
    tg.session.fail = lambda name, _data: (
        TelegramAPIError(_METHOD, "chat not found") if name == "SendDocument" else None
    )
    # Обработку и лог берёт на себя задача бэкапа: локальная копия уже есть
    with pytest.raises(TelegramAPIError):
        await tg.notifier.send_service_document(backup)


# ---- split_text: UTF-16 и HTML ----


def test_длина_куска_считается_в_единицах_utf16() -> None:
    # Эмодзи вне BMP — две единицы: 3000 штук — это 6000, а не 3000
    chunks = split_text("😀" * 3000)
    assert len(chunks) > 1
    assert all(telegram_length(chunk) <= CHUNK_LIMIT for chunk in chunks)
    assert "".join(chunks) == "😀" * 3000


def test_строки_с_эмодзи_режутся_по_переносам_в_пределах_лимита() -> None:
    text = "\n".join(["😀" * 900] * 4)
    chunks = split_text(text)
    assert all(telegram_length(chunk) <= CHUNK_LIMIT for chunk in chunks)
    assert "\n".join(chunks) == text


def test_длинная_строка_не_режется_посреди_тега_и_сущности() -> None:
    line = "".join(f'<a href="https://example.com/{n}">ссылка &amp; {n}</a> ' for n in range(60))
    chunks = split_text(line, limit=100)
    assert len(chunks) > 1
    assert "".join(chunks) == line
    for chunk in chunks:
        assert chunk.count("<") == chunk.count(">")
        assert chunk.count("&") == chunk.count(";")
        assert telegram_length(chunk) <= 100


# ---- служебный чат недоступен ----


def make_notifier(tg: Harness, app: App, owners: tuple[int, ...]) -> TelegramNotifier:
    return TelegramNotifier(tg.bot, app.sessions, app.clock, SERVICE_CHAT, owner_ids=owners)


def sent_to(tg: Harness, chat_id: int) -> list[str]:
    return [c["text"] for c in tg.session.of("SendMessage") if c["chat_id"] == chat_id]


async def test_миграция_чата_пишет_в_лог_новый_id_и_совет(
    app: App, tg: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = make_notifier(tg, app, (1,))
    fail_with(tg, TelegramMigrateToChat(_METHOD, "Bad Request: group migrated", -100777))
    delivered = await notifier.send_service("текст")
    assert delivered is False
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("-100777" in m and "SERVICE_CHAT_ID" in m for m in errors)


async def test_миграция_чата_дублирует_сообщение_владельцам(app: App, tg: Harness) -> None:
    notifier = make_notifier(tg, app, (1, 2))
    fail_with(tg, TelegramMigrateToChat(_METHOD, "Bad Request: group migrated", -100777))
    await notifier.send_service("важное")
    for owner in (1, 2):
        (text,) = sent_to(tg, owner)
        assert "важное" in text
        assert "-100777" in text


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(_METHOD, "Forbidden: bot was kicked from the supergroup chat"),
        TelegramBadRequest(_METHOD, "Bad Request: chat not found"),
    ],
)
async def test_недоступный_служебный_чат_дублируется_владельцам(
    app: App, tg: Harness, error: Exception
) -> None:
    notifier = make_notifier(tg, app, (1, 2))
    fail_with(tg, error)
    delivered = await notifier.send_service("бэкап готов")
    assert delivered is False
    for owner in (1, 2):
        (text,) = sent_to(tg, owner)
        assert text.startswith(SERVICE_CHAT_UNAVAILABLE_TEXT)
        assert "бэкап готов" in text


async def test_успешная_отправка_владельцам_не_дублируется(app: App, tg: Harness) -> None:
    notifier = make_notifier(tg, app, (1,))
    assert await notifier.send_service("всё хорошо") is True
    assert sent_to(tg, 1) == []


async def test_прочая_ошибка_отправки_не_дублируется_владельцам(app: App, tg: Harness) -> None:
    notifier = make_notifier(tg, app, (1,))
    fail_with(tg, TelegramBadRequest(_METHOD, "Bad Request: can't parse entities"))
    assert await notifier.send_service("текст") is False
    assert sent_to(tg, 1) == []


async def test_дублирование_владельцам_не_зацикливается_без_владельцев(
    app: App, tg: Harness
) -> None:
    notifier = make_notifier(tg, app, ())
    fail_with(tg, TelegramForbiddenError(_METHOD, "Forbidden: bot was kicked"))
    assert await notifier.send_service("текст") is False
    assert len(tg.session.of("SendMessage")) == 1
