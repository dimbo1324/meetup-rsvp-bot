"""Пользователи Telegram и посты встреч: записи, которые нужны Telegram-слою.

Обработчики не пишут в БД сами (инвариант 5): актуализация имени, флаг личных сообщений
и регистрация отправленного поста идут через этот сервис.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.repositories import event_posts, users
from meetup_bot.repositories.users import UserInfo


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
