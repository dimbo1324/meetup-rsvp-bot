"""Личка с ботом: /start, /help, dev-команда и события членства бота."""

from __future__ import annotations

import html
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock, ShiftableClock
from meetup_bot.config import Settings
from meetup_bot.container import Services
from meetup_bot.repositories.users import UserInfo
from meetup_bot.telegram.common import check_admin
from meetup_bot.texts.render import format_moment

logger = logging.getLogger(__name__)

router = Router(name="start")
private = Router(name="start_private")
router.include_router(private)
private.message.filter(F.chat.type == "private")

WELCOME_USER = (
    "Здравствуйте, {name}! Я помогаю записываться на встречи клуба.\n\n"
    "Пост со встречей приходит в чат от администратора — нажмите «Записаться» под ним. "
    "Здесь, в личке, я буду присылать напоминания и сообщать, если освободится место."
)
WELCOME_ADMIN_EXTRA = (
    "\n\nВы администратор. Чтобы разослать пост, наберите @{bot} в нужном чате и выберите "
    "встречу. Меню администратора — команда /menu."
)
REMIND_ON = "Напоминания включены. Я напишу вам перед встречей и если освободится место."
ABOUT_TEXT = (
    "Я бот записи на встречи клуба. Пост со встречей приходит в чат, а запись и отмена "
    "делаются кнопками под ним. Нажмите /start, чтобы я мог присылать вам напоминания."
)
HELP_USER = (
    "Как пользоваться ботом:\n"
    "• Под постом встречи нажмите «Записаться» (или «Записаться +1», если доступно).\n"
    "• Передумали — нажмите «Отменить запись» до указанного срока.\n"
    "• Нажмите /start, чтобы получать напоминания в личные сообщения."
)
HELP_ADMIN = (
    HELP_USER + "\n\nДля администраторов:\n"
    "• Наберите @{bot} в любом чате, чтобы разослать пост открытой встречи.\n"
    "• /menu — меню администратора, /cancel — выйти из текущего диалога.\n"
    "Меню появится в одной из следующих версий."
)


@private.message(Command("start"))
async def cmd_start(
    message: Message,
    command: CommandObject,
    user_info: UserInfo,
    services: Services,
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    bot_username: str,
    app_config: Settings,
) -> None:
    await services.users.set_dm_available(user_info.id, True)
    is_admin = await check_admin(sessions, user_info.id)

    param = (command.args or "").strip()
    if param == "remind":
        text = REMIND_ON
    elif param == "about":
        text = ABOUT_TEXT
    else:
        text = WELCOME_USER.format(name=html.escape(user_info.first_name, quote=False))
        if is_admin:
            text += WELCOME_ADMIN_EXTRA.format(bot=bot_username)
    if app_config.env == "dev":
        text += f"\n\nID пользователя: <code>{user_info.id}</code>"
    await message.answer(text)


@private.message(Command("help"))
async def cmd_help(
    message: Message,
    user_info: UserInfo,
    sessions: async_sessionmaker[AsyncSession],
    bot_username: str,
) -> None:
    if await check_admin(sessions, user_info.id):
        await message.answer(HELP_ADMIN.format(bot=bot_username))
    else:
        await message.answer(HELP_USER)


@private.message(Command("dev_shift"))
async def cmd_dev_shift(
    message: Message,
    command: CommandObject,
    user_info: UserInfo,
    services: Services,
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    app_config: Settings,
) -> None:
    # В бою команда молчит: пользователь не должен узнать, что она вообще существует
    if app_config.env != "dev" or not await check_admin(sessions, user_info.id):
        return
    if not isinstance(clock, ShiftableClock):
        await message.answer("Часы не поддерживают сдвиг.")
        return
    try:
        minutes = int((command.args or "").strip())
    except ValueError:
        await message.answer("Использование: /dev_shift &lt;минуты&gt;, например /dev_shift 60")
        return
    clock.shift(timedelta(minutes=minutes))
    now = format_moment(clock.now(), services.settings.timezone_now())
    await message.answer(f"Часы сдвинуты на {minutes} мин. Сейчас: {now}.")


@router.my_chat_member()
async def on_my_chat_member(
    event: ChatMemberUpdated, services: Services, app_config: Settings
) -> None:
    status = event.new_chat_member.status
    if event.chat.type == "private":
        # Пользователь заблокировал бота: личные сообщения ему больше не доходят
        if status in ("kicked", "left"):
            await services.users.set_dm_available(event.chat.id, False)
        return
    if status in ("member", "administrator"):
        logger.info("бот добавлен в чат: chat_id=%s", event.chat.id)
        if app_config.env == "dev":
            await event.answer(f"ID этого чата: <code>{event.chat.id}</code>")
