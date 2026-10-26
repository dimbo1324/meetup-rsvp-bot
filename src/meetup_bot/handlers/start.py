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
from meetup_bot.telegram.commands import set_admin_commands
from meetup_bot.telegram.common import START_ABOUT, START_REMIND, check_admin
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
# Человек мог нажать «Записаться», не успев прочитать всплывающее окно с итогом записи
REMIND_STATUS_HINT = (
    "Если вы только что записались, запись сохранена. Чтобы проверить её статус, "
    "нажмите «Записаться» под постом ещё раз: повторное нажатие ничего не изменит."
)
# Сюда приходят уже нажавшие /start, поэтому просить его нажать ещё раз не нужно
ABOUT_TEXT = (
    "Я бот записи на встречи клуба. Пост со встречей приходит в чат, а запись и отмена "
    "делаются кнопками под ним. Напоминания включены: я напишу вам перед встречей."
)
HELP_HEAD = "Как пользоваться ботом:\n"
HELP_JOIN = "• Под постом встречи нажмите «Записаться».\n"
HELP_JOIN_PLUS_ONE = "• Под постом встречи нажмите «Записаться» (или «Записаться +1», с гостем).\n"
HELP_TAIL = (
    "• Передумали — нажмите «Отменить запись» до указанного срока.\n"
    "• Нажмите /start, чтобы получать напоминания в личные сообщения."
)
HELP_USER = HELP_HEAD + HELP_JOIN + HELP_TAIL
HELP_ADMIN_EXTRA = (
    "\n\nДля администраторов:\n"
    "• Наберите @{bot} в любом чате, чтобы разослать пост открытой встречи.\n"
    "• /menu — меню администратора, /cancel — выйти из текущего диалога."
)
HELP_ADMIN = HELP_USER + HELP_ADMIN_EXTRA


def build_help(*, is_admin: bool, bot: str, plus_one_enabled: bool) -> str:
    """Справка; про «+1» говорим только когда кнопка действительно есть под постами."""
    text = HELP_HEAD + (HELP_JOIN_PLUS_ONE if plus_one_enabled else HELP_JOIN) + HELP_TAIL
    return text + HELP_ADMIN_EXTRA.format(bot=bot) if is_admin else text


@private.message(Command("start"))
async def cmd_start(
    message: Message,
    command: CommandObject,
    user_info: UserInfo,
    services: Services,
    sessions: async_sessionmaker[AsyncSession],
    bot_username: str,
    app_config: Settings,
) -> None:
    await services.users.set_dm_available(user_info.id, True)
    is_admin = await check_admin(sessions, user_info.id)
    if is_admin and message.bot is not None:
        # Если при запуске бота чата с админом ещё не было, меню команд включаем сейчас
        await set_admin_commands(message.bot, user_info.id, is_admin=True)

    param = (command.args or "").strip()
    if param == START_REMIND:
        text = f"{REMIND_ON}\n\n{REMIND_STATUS_HINT}"
    elif param == START_ABOUT:
        text = ABOUT_TEXT
    else:
        text = WELCOME_USER.format(name=html.escape(user_info.first_name, quote=False))
        if is_admin:
            text += WELCOME_ADMIN_EXTRA.format(bot=bot_username)
    if app_config.is_dev:
        text += f"\n\nID пользователя: <code>{user_info.id}</code>"
    await message.answer(text)


@private.message(Command("help"))
async def cmd_help(
    message: Message,
    user_info: UserInfo,
    services: Services,
    sessions: async_sessionmaker[AsyncSession],
    bot_username: str,
) -> None:
    await message.answer(
        build_help(
            is_admin=await check_admin(sessions, user_info.id),
            bot=bot_username,
            plus_one_enabled=services.settings.options_now().plus_one_enabled,
        )
    )


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
    if not app_config.is_dev or not await check_admin(sessions, user_info.id):
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
        if app_config.is_dev:
            await event.answer(f"ID этого чата: <code>{event.chat.id}</code>")
