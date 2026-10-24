"""Сервис настроек: значения из БД поверх умолчаний из кода (раздел 7 спецификации).

Каждое изменение проверяется схемой, требует прав админа и пишется в audit_log.

Потребители (регистрация, баны, явка…) получают настройки через СИНХРОННЫЕ провайдеры,
потому что вызываются под открытой транзакцией записи: открыть из неё ещё одну сессию к
SQLite значит самоблокироваться. Поэтому весь набор переопределений держится в памяти
(`load()` при старте), а `*_now()` читают только кэш.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.repositories import audit
from meetup_bot.repositories import settings as settings_repo
from meetup_bot.services import access
from meetup_bot.services.errors import DomainError
from meetup_bot.services.rules import RegistrationOptions, RulesSettings
from meetup_bot.services.settings_schema import (
    SAMPLE_VARS,
    SCHEMA,
    SettingSpec,
    SettingsValidationError,
    check_template,
    get_spec,
    parse_hhmm,
    validate,
)
from meetup_bot.services.timing import ScheduleSettings
from meetup_bot.texts.defaults import ALLOWED_PLACEHOLDERS, DEFAULT_TEXTS, TEXT_PREFIX
from meetup_bot.texts.render import fill, text_variables

logger = logging.getLogger(__name__)

__all__ = [
    "DefaultEventParams",
    "SettingsNotLoaded",
    "SettingsService",
    "SettingsValidationError",
]


class SettingsNotLoaded(RuntimeError):
    """Синхронное чтение до `await SettingsService.load()` — ошибка порядка запуска."""

    def __init__(self) -> None:
        super().__init__("настройки не загружены: вызовите `await settings.load()` при старте")


@dataclass(frozen=True)
class DefaultEventParams:
    """Параметры новой встречи по умолчанию (настройки влияют только на новые встречи).

    Имена полей совпадают с `EventDefaults` из services/lifecycle.py.
    """

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
        # Кэш переопределений из БД. None — ещё не загружен. После записи обновляется по
        # месту (а не сбрасывается), чтобы синхронные читатели всегда видели актуальное.
        self._overrides: dict[str, Any] | None = None
        # Растёт при каждой записи: загрузка, начатая до записи, не должна затереть кэш
        # устаревшим словарём
        self._version = 0
        self._load_lock = asyncio.Lock()
        # Записи сериализуем, чтобы порядок в БД и в кэше совпадал
        self._write_lock = asyncio.Lock()

    # ---- загрузка и кэш -------------------------------------------------------------

    async def load(self) -> None:
        """Прогревает кэш (вызывать при старте до первой работы потребителей)."""
        await self._load()

    async def _load(self) -> dict[str, Any]:
        async with self._load_lock:
            while self._overrides is None:
                version = self._version
                async with self._sessions() as session:
                    stored = await settings_repo.get_all(session)
                if version != self._version:
                    continue  # пока читали, кто-то записал: читаем заново
                # Ключи, удалённые из схемы в новых версиях, игнорируем
                self._overrides = {k: v for k, v in stored.items() if k in SCHEMA}
            return self._overrides

    def _cache(self) -> dict[str, Any]:
        if self._overrides is None:
            raise SettingsNotLoaded
        return self._overrides

    # ---- синхронное чтение из кэша --------------------------------------------------

    def get_now(self, key: str) -> Any:
        spec = get_spec(key)
        return self._cache().get(key, spec.default)

    def _all_now(self) -> dict[str, Any]:
        overrides = self._cache()
        return {key: overrides.get(key, spec.default) for key, spec in SCHEMA.items()}

    def timezone_now(self) -> ZoneInfo:
        return ZoneInfo(str(self.get_now("timezone")))

    def schedule_now(self) -> ScheduleSettings:
        v = self._all_now()
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

    def options_now(self) -> RegistrationOptions:
        v = self._all_now()
        return RegistrationOptions(
            waitlist_enabled=v["waitlist_enabled"], plus_one_enabled=v["plus_one_enabled"]
        )

    def rules_now(self) -> RulesSettings:
        v = self._all_now()
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

    def defaults_now(self) -> DefaultEventParams:
        v = self._all_now()
        return DefaultEventParams(
            capacity=v["capacity"],
            place=v["default_place"],
            place_url=v["default_place_url"],
            description_html=v["default_description"],
            photo_file_id=v["default_photo_file_id"],
        )

    def render_text_now(self, key: str, *, escape_html: bool = True, **variables: object) -> str:
        """Подставляет значения в текст `text.*`.

        `escape_html=True` — для текстов, уходящих сообщением в parse_mode=HTML (значения
        вроде имени или причины иначе сломали бы разметку); для всплывающих окон передают
        False, там HTML не интерпретируется. Испорченный в БД шаблон не должен ронять
        уведомление: логируем и берём текст по умолчанию. Отсутствующие подстановки
        заменяются пустой строкой, лишние — игнорируются.
        """
        if not key.startswith(TEXT_PREFIX):
            key = TEXT_PREFIX + key
        if key not in DEFAULT_TEXTS:
            raise SettingsValidationError(key, f"«{key}» не является текстом бота.")
        unknown = set(variables) - ALLOWED_PLACEHOLDERS
        if unknown:
            logger.warning("текст %s: лишние подстановки проигнорированы: %s", key, sorted(unknown))
        values = {
            name: str(value)
            for name, value in variables.items()
            if name in ALLOWED_PLACEHOLDERS and value is not None
        }
        return self._render(key, values, escape_html=escape_html)

    def render_message(self, key: str, params: dict[str, Any]) -> str:
        """Текст-уведомление из сырых параметров сервиса (моменты, место, имя, причина)."""
        if not key.startswith(TEXT_PREFIX):
            key = TEXT_PREFIX + key
        return self._render(key, text_variables(params, self.timezone_now()), escape_html=True)

    def _render(self, key: str, values: dict[str, str], *, escape_html: bool) -> str:
        if key not in DEFAULT_TEXTS:
            raise SettingsValidationError(key, f"«{key}» не является текстом бота.")
        template = str(self.get_now(key))
        try:
            check_template(key, template)
            return fill(template, values, escape_html=escape_html)
        except SettingsValidationError, ValueError, IndexError, KeyError:
            logger.exception("шаблон %s испорчен, используется текст по умолчанию", key)
            return fill(DEFAULT_TEXTS[key], values, escape_html=escape_html)

    # ---- асинхронный интерфейс (обёртки над теми же данными) ------------------------

    async def get(self, key: str) -> Any:
        await self._load()
        return self.get_now(key)

    async def get_all(self) -> dict[str, Any]:
        await self._load()
        return self._all_now()

    def spec(self, key: str) -> SettingSpec:
        return get_spec(key)

    async def is_default(self, key: str) -> bool:
        get_spec(key)
        return key not in await self._load()

    async def set(self, key: str, value: Any, actor_id: int) -> list[str]:
        """Сохраняет значение; возвращает предупреждения (ошибки — SettingsValidationError)."""
        await self._load()
        async with self._write_lock:
            now = self._clock.now()
            async with self._sessions() as session, session.begin():
                # Права проверяем первыми: не-админ не должен узнавать детали схемы
                await access.require_admin(session, actor_id)
                spec = get_spec(key)
                normalized, warnings = validate(spec, value)
                old = self.get_now(key)
                await settings_repo.upsert(session, key, normalized, actor_id=actor_id, now=now)
                await audit.add(
                    session,
                    action="settings.update",
                    now=now,
                    actor_id=actor_id,
                    payload={"key": key, "old": old, "new": normalized},
                )
            # Только после успешного коммита; кэш уже загружен, обновляем по месту
            self._version += 1
            self._cache()[key] = normalized
        return warnings

    async def reset(self, key: str, actor_id: int) -> None:
        """Возвращает настройку к значению по умолчанию (удаляет строку из БД)."""
        await self._load()
        async with self._write_lock:
            now = self._clock.now()
            async with self._sessions() as session, session.begin():
                await access.require_admin(session, actor_id)
                spec = get_spec(key)
                old = self.get_now(key)
                if await settings_repo.delete_key(session, key):
                    await audit.add(
                        session,
                        action="settings.reset",
                        now=now,
                        actor_id=actor_id,
                        payload={"key": key, "old": old, "new": spec.default},
                    )
            self._version += 1
            self._cache().pop(key, None)

    async def render_text(self, key: str, *, escape_html: bool = True, **variables: object) -> str:
        await self._load()
        return self.render_text_now(key, escape_html=escape_html, **variables)

    async def timezone(self) -> ZoneInfo:
        await self._load()
        return self.timezone_now()

    async def schedule_settings(self) -> ScheduleSettings:
        await self._load()
        return self.schedule_now()

    async def registration_options(self) -> RegistrationOptions:
        await self._load()
        return self.options_now()

    async def rules_settings(self) -> RulesSettings:
        await self._load()
        return self.rules_now()

    async def default_event_params(self) -> DefaultEventParams:
        await self._load()
        return self.defaults_now()


# SAMPLE_VARS реэкспортируется для UI предпросмотра текстов
SAMPLE_TEXT_VARS = SAMPLE_VARS
