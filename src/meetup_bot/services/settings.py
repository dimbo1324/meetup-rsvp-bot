"""Сервис настроек: значения из БД поверх умолчаний из кода (раздел 7 спецификации).

Каждое изменение проверяется схемой, требует прав админа и пишется в audit_log.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.repositories import admins, audit
from meetup_bot.repositories import settings as settings_repo
from meetup_bot.services.errors import DomainError
from meetup_bot.services.registration import RegistrationOptions
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.settings_schema import (
    SAMPLE_VARS,
    SCHEMA,
    SettingSpec,
    SettingsValidationError,
    get_spec,
    parse_hhmm,
    validate,
)
from meetup_bot.services.timing import ScheduleSettings
from meetup_bot.texts.defaults import ALLOWED_PLACEHOLDERS

__all__ = [
    "DefaultEventParams",
    "SettingsService",
    "SettingsValidationError",
]


@dataclass(frozen=True)
class DefaultEventParams:
    """Параметры новой встречи по умолчанию (настройки влияют только на новые встречи)."""

    capacity: int
    place: str
    place_url: str
    description_html: str
    photo_file_id: str | None


def _to_time(value: str) -> time:
    parsed = parse_hhmm(value)
    if parsed is None:  # значение из БД испорчено вручную: падаем явно, а не молча
        raise DomainError(f"некорректное время в настройках: {value!r}")
    return time(*parsed)


class SettingsService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock
        # Кэш переопределений из БД; процесс один, поэтому достаточно сбрасывать его при записи
        self._overrides: dict[str, Any] | None = None
        self._load_lock = asyncio.Lock()

    async def _load(self) -> dict[str, Any]:
        overrides = self._overrides
        if overrides is not None:
            return overrides
        async with self._load_lock:
            if self._overrides is None:
                async with self._sessions() as session:
                    stored = await settings_repo.get_all(session)
                # Ключи, удалённые из схемы в новых версиях, игнорируем
                self._overrides = {k: v for k, v in stored.items() if k in SCHEMA}
            return self._overrides

    async def get(self, key: str) -> Any:
        spec = get_spec(key)
        overrides = await self._load()
        return overrides.get(key, spec.default)

    async def get_all(self) -> dict[str, Any]:
        overrides = await self._load()
        return {key: overrides.get(key, spec.default) for key, spec in SCHEMA.items()}

    def spec(self, key: str) -> SettingSpec:
        return get_spec(key)

    async def is_default(self, key: str) -> bool:
        get_spec(key)
        return key not in await self._load()

    async def set(self, key: str, value: Any, actor_id: int) -> list[str]:
        """Сохраняет значение; возвращает предупреждения (ошибки — SettingsValidationError)."""
        spec = get_spec(key)
        normalized, warnings = validate(spec, value)
        old = await self.get(key)
        now = self._clock.now()
        try:
            async with self._sessions() as session, session.begin():
                await admins.require_admin(session, actor_id)
                await settings_repo.upsert(session, key, normalized, actor_id=actor_id, now=now)
                await audit.add(
                    session,
                    action="settings.update",
                    now=now,
                    actor_id=actor_id,
                    payload={"key": key, "old": old, "new": normalized},
                )
        finally:
            self._overrides = None
        return warnings

    async def reset(self, key: str, actor_id: int) -> None:
        """Возвращает настройку к значению по умолчанию (удаляет строку из БД)."""
        get_spec(key)
        old = await self.get(key)
        now = self._clock.now()
        try:
            async with self._sessions() as session, session.begin():
                await admins.require_admin(session, actor_id)
                await settings_repo.delete_key(session, key)
                await audit.add(
                    session,
                    action="settings.reset",
                    now=now,
                    actor_id=actor_id,
                    payload={"key": key, "old": old, "new": SCHEMA[key].default},
                )
        finally:
            self._overrides = None

    async def render_text(self, key: str, **variables: object) -> str:
        """Подставляет значения в текст `text.*`; неизвестная подстановка — ошибка."""
        if not key.startswith("text.") or key not in SCHEMA:
            raise SettingsValidationError(key, f"«{key}» не является текстом бота.")
        unknown = set(variables) - ALLOWED_PLACEHOLDERS
        if unknown:
            raise SettingsValidationError(
                key, f"Неизвестные подстановки: {', '.join(sorted(unknown))}."
            )
        template = str(await self.get(key))
        # Пустую подстановку за потребителя не додумываем: это скрыло бы его ошибку
        try:
            return template.format(**variables)
        except KeyError as exc:
            raise SettingsValidationError(
                key, f"Для текста не передана подстановка {{{exc.args[0]}}}."
            ) from exc

    async def timezone(self) -> ZoneInfo:
        return ZoneInfo(str(await self.get("timezone")))

    async def schedule_settings(self) -> ScheduleSettings:
        v = await self.get_all()
        return ScheduleSettings(
            timezone=v["timezone"],
            event_weekday=v["event_weekday"],
            event_time=_to_time(v["event_time"]),
            cancel_deadline_days_before=v["cancel_deadline_days_before"],
            cancel_deadline_time=_to_time(v["cancel_deadline_time"]),
            registration_close_hours_before=v["registration_close_hours_before"],
            reminder_hours_before=v["reminder_hours_before"],
            attendance_prompt_hours_after=v["attendance_prompt_hours_after"],
        )

    async def registration_options(self) -> RegistrationOptions:
        v = await self.get_all()
        return RegistrationOptions(
            waitlist_enabled=v["waitlist_enabled"], plus_one_enabled=v["plus_one_enabled"]
        )

    async def rules_settings(self) -> RulesSettings:
        v = await self.get_all()
        return RulesSettings(
            ban_after_no_shows=v["ban_after_no_shows"],
            remove_banned_from_future_events=v["remove_banned_from_future_events"],
            notify_user_on_ban=v["notify_user_on_ban"],
            late_promotion_hours=v["late_promotion_hours"],
            notify_admins_on_full=v["notify_admins_on_full"],
            notify_admins_on_cancel=v["notify_admins_on_cancel"],
            reminder_enabled=v["reminder_enabled"],
            show_names_in_post=v["show_names_in_post"],
            auto_create_enabled=v["auto_create_enabled"],
            auto_create_weekday=v["auto_create_weekday"],
            auto_create_time=_to_time(v["auto_create_time"]),
        )

    async def default_event_params(self) -> DefaultEventParams:
        v = await self.get_all()
        return DefaultEventParams(
            capacity=v["capacity"],
            place=v["default_place"],
            place_url=v["default_place_url"],
            description_html=v["default_description"],
            photo_file_id=v["default_photo_file_id"],
        )


# SAMPLE_VARS реэкспортируется для UI предпросмотра текстов
SAMPLE_TEXT_VARS = SAMPLE_VARS
