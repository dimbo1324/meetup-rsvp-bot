"""Пользователи Telegram и посты встреч: записи, которые нужны Telegram-слою.

Обработчики не пишут в БД сами (инвариант 5): актуализация имени, флаг личных сообщений
и регистрация отправленного поста идут через этот сервис.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import User
from meetup_bot.repositories import admins, event_posts, users, users_known
from meetup_bot.repositories.users import UserInfo


def _info(user: User) -> UserInfo:
    return UserInfo(user.id, user.first_name, user.last_name, user.username)


class UserService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock

    async def touch(self, info: UserInfo) -> None:
        """Сохраняет свежие имя и @username при каждом взаимодействии с ботом."""
        async with self._sessions() as session, session.begin():
            await users.upsert(session, info, self._clock.now())

    async def set_dm_available(self, user_id: int, value: bool) -> None:
        """True после /start, False после блокировки бота пользователем (раздел 8.4)."""
        async with self._sessions() as session, session.begin():
            await users.set_dm_available(session, user_id, value, self._clock.now())

    async def is_admin(self, user_id: int) -> bool:
        """Админ или владелец: для меню и кнопок (права действий проверяют сервисы сами)."""
        async with self._sessions() as session:
            return await admins.is_admin(session, user_id)

    async def is_owner(self, user_id: int) -> bool:
        async with self._sessions() as session:
            return await admins.is_owner(session, user_id)

    async def list_known(
        self, limit: int, offset: int, exclude_ids: Collection[int] = ()
    ) -> list[UserInfo]:
        """Известные боту люди (с именем), недавно активные первыми: для выбора и бана."""
        async with self._sessions() as session:
            found = await users_known.list_known(session, limit, offset, exclude_ids)
            return [_info(user) for user in found]

    async def count_known(self, exclude_ids: Collection[int] = ()) -> int:
        async with self._sessions() as session:
            return await users_known.count_known(session, exclude_ids)

    async def get_many(self, user_ids: Collection[int]) -> dict[int, UserInfo]:
        """Данные известных пользователей по ID; неизвестных в результате нет."""
        async with self._sessions() as session:
            found = await users_known.get_many(session, user_ids)
            return {user_id: _info(user) for user_id, user in found.items()}

    async def register_post(
        self, event_id: int, inline_message_id: str, posted_by: int | None
    ) -> bool:
        """Запоминает отправленный inline-пост встречи; True, если пост новый.

        Идемпотентно: и `chosen_inline_result`, и первое нажатие кнопки (lazy registration)
        регистрируют один и тот же `inline_message_id` без дублей.
        """
        async with self._sessions() as session, session.begin():
            _, created = await event_posts.register(
                session,
                event_id=event_id,
                inline_message_id=inline_message_id,
                posted_by=posted_by,
                now=self._clock.now(),
            )
            return created
