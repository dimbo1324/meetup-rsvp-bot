"""Личка: /start, /help, /menu для не-админа, /dev_shift и события членства бота."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from aiogram.types import (
    Chat,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
)

from meetup_bot.clock import ShiftableClock
from meetup_bot.db.models import User
from meetup_bot.handlers.fallback import UNKNOWN_ADMIN_TEXT, UNKNOWN_USER_TEXT
from meetup_bot.handlers.start import (
    ABOUT_TEXT,
    HELP_ADMIN,
    HELP_USER,
    REMIND_ON,
    REMIND_STATUS_HINT,
    WELCOME_ADMIN_EXTRA,
)
from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import Harness, command_update, next_id, text_update, tg_user

USER = 3001
BOT_USER = tg_user(999, "Bot")


def sent_texts(tg: Harness) -> list[str]:
    return [c["text"] for c in tg.session.of("SendMessage")]


async def get_user(app: App, user_id: int) -> User | None:
    async with app.sessions() as session:
        return await session.get(User, user_id)


def command_in_group(user_id: int, text: str) -> Update:
    private = command_update(user_id, text)
    assert private.message is not None
    message: Message = private.message.model_copy(
        update={"chat": Chat(id=-1001, type="supergroup")}
    )
    return Update(update_id=next_id(), message=message)


def member_update(chat: Chat, status: str, user_id: int = USER) -> Update:
    old = ChatMemberMember(user=BOT_USER)
    new: ChatMemberBanned | ChatMemberLeft | ChatMemberMember
    if status == "kicked":
        new = ChatMemberBanned(user=BOT_USER, until_date=datetime.fromtimestamp(0, UTC))
    elif status == "left":
        new = ChatMemberLeft(user=BOT_USER)
    else:
        new = ChatMemberMember(user=BOT_USER)
    return Update(
        update_id=next_id(),
        my_chat_member=ChatMemberUpdated(
            chat=chat,
            from_user=tg_user(user_id),
            date=datetime.now(UTC),
            old_chat_member=old,
            new_chat_member=new,
        ),
    )


# ---- /start ----


async def test_start_создаёт_пользователя_с_доступной_личкой(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    user = await get_user(app, USER)
    assert user is not None
    assert user.dm_available is True


async def test_start_включает_личку_заблокировавшему_ранее(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    await tg.feed(member_update(Chat(id=USER, type="private"), "kicked"))
    await tg.feed(command_update(USER, "/start"))
    user = await get_user(app, USER)
    assert user is not None
    assert user.dm_available is True


async def test_приветствие_участника_обращается_по_имени(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    (text,) = sent_texts(tg)
    assert text.startswith("Здравствуйте, Люди3001! Я помогаю записываться на встречи клуба.")
    assert "Вы администратор" not in text


async def test_приветствие_админа_объясняет_рассылку_поста(tg: Harness) -> None:
    await tg.feed(command_update(ADMIN_ID, "/start"))
    (text,) = sent_texts(tg)
    assert WELCOME_ADMIN_EXTRA.format(bot="mybot") in text
    assert "@mybot" in text


async def test_имя_в_приветствии_экранируется(app: App, tg: Harness) -> None:
    update = command_update(USER, "/start")
    assert update.message is not None
    message = update.message.model_copy(update={"from_user": tg_user(USER, "<b>Ян</b>")})
    await tg.feed(Update(update_id=next_id(), message=message))
    (text,) = sent_texts(tg)
    assert "Здравствуйте, &lt;b&gt;Ян&lt;/b&gt;!" in text


async def test_в_dev_показывается_id_пользователя(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    assert sent_texts(tg)[0].endswith(f"ID пользователя: <code>{USER}</code>")


async def test_в_проде_id_пользователя_не_показывается(
    make_harness: Callable[..., Harness],
) -> None:
    prod = make_harness(env="prod")
    await prod.feed(command_update(USER, "/start"))
    assert "ID пользователя" not in sent_texts(prod)[0]


async def test_deep_link_remind(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start remind"))
    assert sent_texts(tg)[0].startswith(REMIND_ON)


async def test_deep_link_remind_не_теряет_информацию_о_записи(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start remind"))
    assert REMIND_STATUS_HINT in sent_texts(tg)[0]


async def test_deep_link_remind_включает_личку(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start remind"))
    user = await get_user(app, USER)
    assert user is not None
    assert user.dm_available is True


def test_о_боте_не_просит_нажать_start() -> None:
    assert "/start" not in ABOUT_TEXT


async def test_deep_link_about(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start about"))
    assert sent_texts(tg)[0].startswith(ABOUT_TEXT)


async def test_неизвестный_параметр_start_даёт_обычное_приветствие(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start чтото"))
    assert sent_texts(tg)[0].startswith("Здравствуйте, ")


async def test_start_в_группе_игнорируется(app: App, tg: Harness) -> None:
    await tg.feed(command_in_group(USER, "/start"))
    assert tg.session.calls == []


# ---- /help и /menu ----


async def test_справка_участника(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/help"))
    assert sent_texts(tg) == [HELP_USER]


async def test_справка_не_упоминает_плюс_один_пока_кнопка_выключена(app: App, tg: Harness) -> None:
    await app.services.settings.set("plus_one_enabled", False, OWNER_ID)
    await tg.feed(command_update(USER, "/help"))
    assert "+1" not in sent_texts(tg)[0]


async def test_справка_упоминает_плюс_один_когда_кнопка_включена(app: App, tg: Harness) -> None:
    await app.services.settings.set("plus_one_enabled", True, OWNER_ID)
    await tg.feed(command_update(USER, "/help"))
    assert "«Записаться +1»" in sent_texts(tg)[0]


async def test_справка_админа_включает_раздел_для_админов(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/help"))
    assert sent_texts(tg) == [HELP_ADMIN.format(bot="mybot")]


async def test_menu_не_админу_отказ(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/menu"))
    assert sent_texts(tg) == ["Нет доступа."]


async def test_cancel_не_админу_отказ(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/cancel"))
    assert sent_texts(tg) == ["Нет доступа."]


# ---- /dev_shift ----


async def test_dev_shift_в_проде_молчит(make_harness: Callable[..., Harness]) -> None:
    prod = make_harness(env="prod")
    prod.dispatcher.workflow_data["clock"] = ShiftableClock(prod.app.clock)
    await prod.feed(command_update(OWNER_ID, "/dev_shift 60"))
    assert prod.session.calls == []
    assert prod.dispatcher.workflow_data["clock"].offset == timedelta(0)


async def test_dev_shift_не_админу_молчит(tg: Harness) -> None:
    shifted = ShiftableClock(tg.app.clock)
    tg.dispatcher.workflow_data["clock"] = shifted
    await tg.feed(command_update(USER, "/dev_shift 60"))
    assert tg.session.calls == []
    assert shifted.offset == timedelta(0)


async def test_dev_shift_на_обычных_часах_сообщает_что_сдвиг_недоступен(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/dev_shift 60"))
    assert sent_texts(tg) == ["Часы не поддерживают сдвиг."]


async def test_dev_shift_сдвигает_часы_и_показывает_время(tg: Harness) -> None:
    shifted = ShiftableClock(tg.app.clock)
    tg.dispatcher.workflow_data["clock"] = shifted
    await tg.feed(command_update(OWNER_ID, "/dev_shift 60"))
    assert shifted.offset == timedelta(minutes=60)
    assert sent_texts(tg) == ["Часы сдвинуты на 60 мин. Сейчас: четверг, 22 октября, 10:00."]


async def test_dev_shift_допускает_отрицательное_значение(tg: Harness) -> None:
    shifted = ShiftableClock(tg.app.clock)
    tg.dispatcher.workflow_data["clock"] = shifted
    await tg.feed(command_update(OWNER_ID, "/dev_shift -30"))
    assert shifted.offset == timedelta(minutes=-30)


async def test_dev_shift_сдвиги_накапливаются(tg: Harness) -> None:
    shifted = ShiftableClock(tg.app.clock)
    tg.dispatcher.workflow_data["clock"] = shifted
    await tg.feed(command_update(OWNER_ID, "/dev_shift 60"))
    await tg.feed(command_update(OWNER_ID, "/dev_shift 30"))
    assert shifted.offset == timedelta(minutes=90)


async def test_dev_shift_без_числа_подсказывает_использование(tg: Harness) -> None:
    shifted = ShiftableClock(tg.app.clock)
    tg.dispatcher.workflow_data["clock"] = shifted
    await tg.feed(command_update(OWNER_ID, "/dev_shift"))
    await tg.feed(command_update(OWNER_ID, "/dev_shift abc"))
    assert shifted.offset == timedelta(0)
    assert all(t.startswith("Использование: /dev_shift &lt;минуты&gt;") for t in sent_texts(tg))
    assert len(sent_texts(tg)) == 2


# ---- my_chat_member ----


async def test_блокировка_бота_отключает_личку(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    await tg.feed(member_update(Chat(id=USER, type="private"), "kicked"))
    user = await get_user(app, USER)
    assert user is not None
    assert user.dm_available is False


async def test_выход_из_личного_чата_отключает_личку(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    await tg.feed(member_update(Chat(id=USER, type="private"), "left"))
    user = await get_user(app, USER)
    assert user is not None
    assert user.dm_available is False


async def test_разблокировка_бота_сама_личку_не_включает(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/start"))
    await tg.feed(member_update(Chat(id=USER, type="private"), "kicked"))
    await tg.feed(member_update(Chat(id=USER, type="private"), "member"))
    user = await get_user(app, USER)
    assert user is not None
    assert user.dm_available is False


async def test_блокировка_неизвестным_пользователем_не_падает(app: App, tg: Harness) -> None:
    await tg.feed(member_update(Chat(id=555, type="private"), "kicked"))
    assert await get_user(app, 555) is None


async def test_добавление_в_группу_в_dev_сообщает_id_чата(tg: Harness) -> None:
    await tg.feed(member_update(Chat(id=-1007, type="supergroup"), "member"))
    (call,) = tg.session.of("SendMessage")
    assert call["chat_id"] == -1007
    assert call["text"] == "ID этого чата: <code>-1007</code>"


async def test_добавление_в_группу_в_проде_молчит(make_harness: Callable[..., Harness]) -> None:
    prod = make_harness(env="prod")
    await prod.feed(member_update(Chat(id=-1007, type="supergroup"), "member"))
    assert prod.session.calls == []


async def test_удаление_из_группы_ничего_не_отправляет(tg: Harness) -> None:
    await tg.feed(member_update(Chat(id=-1007, type="supergroup"), "kicked"))
    assert tg.session.calls == []


# ---- неопознанные сообщения ----


async def test_непонятное_сообщение_админу_подсказывает_меню(tg: Harness) -> None:
    await tg.feed(text_update(OWNER_ID, "что это"))
    assert sent_texts(tg) == [UNKNOWN_ADMIN_TEXT]
    assert "/menu" in UNKNOWN_ADMIN_TEXT


async def test_непонятное_сообщение_участнику_подсказывает_справку(tg: Harness) -> None:
    await tg.feed(text_update(USER, "что это"))
    assert sent_texts(tg) == [UNKNOWN_USER_TEXT]
    assert "/menu" not in UNKNOWN_USER_TEXT


async def test_неизвестная_команда_тоже_получает_подсказку(tg: Harness) -> None:
    await tg.feed(command_update(USER, "/nonexistent"))
    assert sent_texts(tg) == [UNKNOWN_USER_TEXT]


async def test_непонятное_сообщение_в_группе_игнорируется(tg: Harness) -> None:
    update = text_update(USER, "что это")
    assert update.message is not None
    message = update.message.model_copy(update={"chat": Chat(id=-1001, type="supergroup")})
    await tg.feed(Update(update_id=next_id(), message=message))
    assert tg.session.calls == []
