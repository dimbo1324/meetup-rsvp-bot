"""SettingsService: set/reset, права, аудит, кэш, синхронные `*_now`, рендер текстов."""

import asyncio
import logging
from datetime import time
from typing import Any

import pytest
from sqlalchemy import select

from meetup_bot.clock import FakeClock
from meetup_bot.db.models import AuditLog, Setting
from meetup_bot.repositories import settings as settings_repo
from meetup_bot.services.errors import DomainError, PermissionDenied
from meetup_bot.services.settings import (
    DefaultEventParams,
    SettingsNotLoaded,
    SettingsService,
    SettingsValidationError,
)
from meetup_bot.services.settings_schema import SCHEMA
from meetup_bot.texts.defaults import DEFAULT_TEXTS
from tests.helpers import ADMIN_ID, OWNER_ID, App

OUTSIDER_ID = 777


@pytest.fixture
def settings(app: App) -> SettingsService:
    return app.services.settings


async def audit_rows(app: App, action: str) -> list[AuditLog]:
    async with app.sessions() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        )
        return list(rows.all())


async def stored(app: App) -> dict[str, Any]:
    async with app.sessions() as session:
        return await settings_repo.get_all(session)


# ---- до load() ------------------------------------------------------------------------


@pytest.fixture
def fresh(app: App, clock: FakeClock) -> SettingsService:
    """Сервис, у которого load() ещё не вызывали."""
    return SettingsService(app.sessions, clock)


@pytest.mark.parametrize(
    "reader",
    [
        lambda s: s.get_now("capacity"),
        lambda s: s.timezone_now(),
        lambda s: s.schedule_now(),
        lambda s: s.options_now(),
        lambda s: s.rules_now(),
        lambda s: s.defaults_now(),
        lambda s: s.render_text_now("text.full"),
    ],
    ids=[
        "get_now",
        "timezone_now",
        "schedule_now",
        "options_now",
        "rules_now",
        "defaults_now",
        "render_text_now",
    ],
)
def test_синхронное_чтение_до_load_это_ошибка(fresh: SettingsService, reader: Any) -> None:
    with pytest.raises(SettingsNotLoaded):
        reader(fresh)


async def test_после_load_синхронное_чтение_работает(fresh: SettingsService) -> None:
    await fresh.load()
    assert fresh.get_now("capacity") == 10


async def test_асинхронное_чтение_само_загружает_кэш(fresh: SettingsService) -> None:
    assert await fresh.get("capacity") == 10
    assert fresh.get_now("capacity") == 10


async def test_load_игнорирует_ключи_удалённые_из_схемы(app: App, clock: FakeClock) -> None:
    async with app.sessions() as session, session.begin():
        await settings_repo.upsert(session, "старый_ключ", 1, actor_id=None, now=clock.now())
        await settings_repo.upsert(session, "capacity", 33, actor_id=None, now=clock.now())
    service = SettingsService(app.sessions, clock)
    await service.load()
    assert service.get_now("capacity") == 33
    assert "старый_ключ" not in await service.get_all()


async def test_запись_во_время_загрузки_не_затирается_устаревшим_кэшем(
    app: App, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SettingsService(app.sessions, clock)
    original = settings_repo.get_all
    calls = 0

    async def get_all_with_concurrent_write(session: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        result = await original(session)
        if calls == 1:
            service._version += 1  # имитация записи, прошедшей, пока мы читали
        return result

    monkeypatch.setattr(settings_repo, "get_all", get_all_with_concurrent_write)
    await service.load()
    assert calls == 2


# ---- значения по умолчанию -------------------------------------------------------------


async def test_get_all_без_переопределений_равен_умолчаниям(settings: SettingsService) -> None:
    assert await settings.get_all() == {key: spec.default for key, spec in SCHEMA.items()}


async def test_все_ключи_без_переопределений_считаются_умолчанием(
    settings: SettingsService,
) -> None:
    assert await settings.is_default("capacity")


async def test_is_default_неизвестный_ключ_это_ошибка(settings: SettingsService) -> None:
    with pytest.raises(SettingsValidationError):
        await settings.is_default("нет_такого")


async def test_spec_возвращает_описание_настройки(settings: SettingsService) -> None:
    assert settings.spec("capacity").default == 10


# ---- set ------------------------------------------------------------------------------


async def test_set_сохраняет_значение_и_обновляет_кэш(app: App, settings: SettingsService) -> None:
    await settings.set("capacity", 25, ADMIN_ID)
    assert settings.get_now("capacity") == 25
    assert await settings.get("capacity") == 25
    assert (await stored(app))["capacity"] == 25
    assert not await settings.is_default("capacity")


async def test_set_значение_видно_после_перезагрузки_из_БД(
    app: App, clock: FakeClock, settings: SettingsService
) -> None:
    await settings.set("event_time", "18:30", OWNER_ID)
    other = SettingsService(app.sessions, clock)
    await other.load()
    assert other.get_now("event_time") == "18:30"


async def test_set_пишет_аудит_со_старым_и_новым_значением(
    app: App, settings: SettingsService
) -> None:
    await settings.set("capacity", 25, ADMIN_ID)
    await settings.set("capacity", 30, ADMIN_ID)
    rows = await audit_rows(app, "settings.update")
    assert [(r.actor_id, r.payload) for r in rows] == [
        (ADMIN_ID, {"key": "capacity", "old": 10, "new": 25}),
        (ADMIN_ID, {"key": "capacity", "old": 25, "new": 30}),
    ]
    assert rows[0].created_at == app.clock.now()


async def test_set_пишет_кто_и_когда_изменил(app: App, settings: SettingsService) -> None:
    await settings.set("capacity", 25, OWNER_ID)
    async with app.sessions() as session:
        row = await session.get(Setting, "capacity")
    assert row is not None
    assert (row.updated_by, row.updated_at) == (OWNER_ID, app.clock.now())


async def test_set_владелец_тоже_может(settings: SettingsService) -> None:
    await settings.set("capacity", 12, OWNER_ID)
    assert settings.get_now("capacity") == 12


async def test_set_не_админ_получает_PermissionDenied(app: App, settings: SettingsService) -> None:
    with pytest.raises(PermissionDenied):
        await settings.set("capacity", 25, OUTSIDER_ID)
    assert settings.get_now("capacity") == 10
    assert await stored(app) == {}
    assert await audit_rows(app, "settings.update") == []


async def test_set_права_проверяются_раньше_схемы(settings: SettingsService) -> None:
    # Посторонний не должен узнавать, какие ключи бывают
    with pytest.raises(PermissionDenied):
        await settings.set("нет_такого", 1, OUTSIDER_ID)


async def test_set_неизвестный_ключ_у_админа_это_ошибка_валидации(
    settings: SettingsService,
) -> None:
    with pytest.raises(SettingsValidationError) as info:
        await settings.set("нет_такого", 1, ADMIN_ID)
    assert info.value.key == "нет_такого"


async def test_set_неверное_значение_не_меняет_кэш_БД_и_аудит(
    app: App, settings: SettingsService
) -> None:
    with pytest.raises(SettingsValidationError):
        await settings.set("capacity", 0, ADMIN_ID)
    assert settings.get_now("capacity") == 10
    assert await stored(app) == {}
    assert await audit_rows(app, "settings.update") == []


async def test_set_текст_с_неизвестной_подстановкой_отклонён(  # кейс 20
    app: App, settings: SettingsService
) -> None:
    with pytest.raises(SettingsValidationError, match="подстановка"):
        await settings.set("text.full", "Привет, {unknown}", ADMIN_ID)
    assert settings.get_now("text.full") == DEFAULT_TEXTS["text.full"]
    assert await stored(app) == {}


@pytest.mark.parametrize("template", ["{date.__class__}", "{0}", "{x!r}"])
async def test_set_формат_инъекции_отклонены(  # кейс 20
    app: App, settings: SettingsService, template: str
) -> None:
    with pytest.raises(SettingsValidationError):
        await settings.set("text.reminder", template, ADMIN_ID)
    assert await stored(app) == {}
    assert await audit_rows(app, "settings.update") == []


async def test_set_всплывающий_текст_длиннее_200_в_худшем_случае_отклонён(  # кейс 20
    settings: SettingsService,
) -> None:
    with pytest.raises(SettingsValidationError, match="слишком длинный"):
        await settings.set("text.full", "{place}{name}", ADMIN_ID)


async def test_set_возвращает_предупреждения(settings: SettingsService) -> None:
    warnings = await settings.set("default_description", "а" * 701, ADMIN_ID)
    assert len(warnings) == 1


async def test_set_без_предупреждений_возвращает_пустой_список(
    settings: SettingsService,
) -> None:
    assert await settings.set("capacity", 11, ADMIN_ID) == []


async def test_set_нормализует_значение_перед_сохранением(
    app: App, settings: SettingsService
) -> None:
    await settings.set("default_place", "  Кафе  ", ADMIN_ID)
    assert settings.get_now("default_place") == "Кафе"
    assert (await stored(app))["default_place"] == "Кафе"


async def test_set_часовой_пояс_обновляет_timezone_now(settings: SettingsService) -> None:
    await settings.set("timezone", "UTC", ADMIN_ID)
    assert settings.timezone_now().key == "UTC"
    assert (await settings.timezone()).key == "UTC"


async def test_set_некорректный_часовой_пояс_отклонён(settings: SettingsService) -> None:
    with pytest.raises(SettingsValidationError):
        await settings.set("timezone", "Mars/Phobos", ADMIN_ID)
    assert settings.timezone_now().key == "America/Montevideo"


async def test_параллельные_set_разных_ключей_согласуют_кэш_и_БД(
    app: App, settings: SettingsService
) -> None:
    await asyncio.gather(
        settings.set("capacity", 21, ADMIN_ID),
        settings.set("event_time", "19:00", ADMIN_ID),
        settings.set("plus_one_enabled", True, OWNER_ID),
    )
    assert await stored(app) == {"capacity": 21, "event_time": "19:00", "plus_one_enabled": True}
    assert settings.get_now("capacity") == 21
    assert len(await audit_rows(app, "settings.update")) == 3


async def test_параллельные_set_одного_ключа_последний_в_кэше_равен_БД(
    app: App, settings: SettingsService
) -> None:
    await asyncio.gather(*(settings.set("capacity", 20 + n, ADMIN_ID) for n in range(5)))
    assert (await stored(app))["capacity"] == settings.get_now("capacity")


# ---- reset ----------------------------------------------------------------------------


async def test_reset_возвращает_умолчание_в_кэше_и_БД(app: App, settings: SettingsService) -> None:
    await settings.set("capacity", 25, ADMIN_ID)
    await settings.reset("capacity", ADMIN_ID)
    assert settings.get_now("capacity") == 10
    assert await settings.is_default("capacity")
    assert await stored(app) == {}


async def test_reset_пишет_аудит(app: App, settings: SettingsService) -> None:
    await settings.set("capacity", 25, ADMIN_ID)
    await settings.reset("capacity", OWNER_ID)
    rows = await audit_rows(app, "settings.reset")
    assert [(r.actor_id, r.payload) for r in rows] == [
        (OWNER_ID, {"key": "capacity", "old": 25, "new": 10})
    ]


async def test_reset_без_переопределения_не_пишет_аудит(
    app: App, settings: SettingsService
) -> None:
    await settings.reset("capacity", ADMIN_ID)
    assert await audit_rows(app, "settings.reset") == []
    assert settings.get_now("capacity") == 10


async def test_reset_не_админ_получает_PermissionDenied(
    app: App, settings: SettingsService
) -> None:
    await settings.set("capacity", 25, ADMIN_ID)
    with pytest.raises(PermissionDenied):
        await settings.reset("capacity", OUTSIDER_ID)
    assert settings.get_now("capacity") == 25
    assert (await stored(app))["capacity"] == 25


async def test_reset_неизвестный_ключ_это_ошибка_валидации(settings: SettingsService) -> None:
    with pytest.raises(SettingsValidationError):
        await settings.reset("нет_такого", ADMIN_ID)


async def test_reset_только_одного_ключа_не_трогает_остальные(
    settings: SettingsService,
) -> None:
    await settings.set("capacity", 25, ADMIN_ID)
    await settings.set("event_time", "18:00", ADMIN_ID)
    await settings.reset("capacity", ADMIN_ID)
    assert settings.get_now("event_time") == "18:00"


# ---- снимки настроек ------------------------------------------------------------------


async def test_schedule_now_отражает_изменения(settings: SettingsService) -> None:
    await settings.set("event_time", "18:30", ADMIN_ID)
    await settings.set("event_weekday", 4, ADMIN_ID)
    await settings.set("registration_close_hours_before", 5, ADMIN_ID)
    schedule = settings.schedule_now()
    assert schedule.event_time == time(18, 30)
    assert schedule.event_weekday == 4
    assert schedule.registration_close_hours_before == 5
    assert schedule.cancel_deadline_time == time(20, 0)
    assert await settings.schedule_settings() == schedule


async def test_rules_now_отражает_изменения(settings: SettingsService) -> None:
    await settings.set("ban_after_no_shows", 3, ADMIN_ID)
    await settings.set("reminder_enabled", False, ADMIN_ID)
    await settings.set("auto_create_time", "09:15", ADMIN_ID)
    rules = settings.rules_now()
    assert rules.ban_after_no_shows == 3
    assert rules.reminder_enabled is False
    assert rules.auto_create_time == time(9, 15)
    assert rules.auto_create_weekday == 6
    assert await settings.rules_settings() == rules


async def test_options_now_отражает_изменения(settings: SettingsService) -> None:
    assert not settings.options_now().waitlist_enabled
    await settings.set("waitlist_enabled", True, ADMIN_ID)
    await settings.set("plus_one_enabled", True, ADMIN_ID)
    options = settings.options_now()
    assert (options.waitlist_enabled, options.plus_one_enabled) == (True, True)
    assert await settings.registration_options() == options


async def test_defaults_now_отражает_изменения(settings: SettingsService) -> None:
    assert settings.defaults_now() == DefaultEventParams(10, "", "", "", None)
    await settings.set("capacity", 15, ADMIN_ID)
    await settings.set("default_place", "Кафе", ADMIN_ID)
    await settings.set("default_place_url", "https://maps.example/x", ADMIN_ID)
    await settings.set("default_description", "<b>Привет</b>", ADMIN_ID)
    await settings.set("default_photo_file_id", "AgAC123", ADMIN_ID)
    expected = DefaultEventParams(15, "Кафе", "https://maps.example/x", "<b>Привет</b>", "AgAC123")
    assert settings.defaults_now() == expected
    assert await settings.default_event_params() == expected


async def test_испорченное_время_в_БД_падает_явно(
    app: App, clock: FakeClock, settings: SettingsService
) -> None:
    async with app.sessions() as session, session.begin():
        await settings_repo.upsert(session, "event_time", "25:99", actor_id=None, now=clock.now())
    broken = SettingsService(app.sessions, clock)
    await broken.load()
    with pytest.raises(DomainError, match="некорректное время"):
        broken.schedule_now()


# ---- render_text_now ------------------------------------------------------------------


def test_render_подставляет_значения(settings: SettingsService) -> None:
    text = settings.render_text_now(
        "text.registered",
        weekday="суббота",
        date="1 ноября",
        time="17:00",
        deadline="пятница, 20:00",
    )
    assert (
        text == "Вы записаны на суббота, 1 ноября, 17:00. "
        "Если планы изменятся, отмените запись до пятница, 20:00."
    )


def test_render_принимает_ключ_без_префикса(settings: SettingsService) -> None:
    assert settings.render_text_now("already") == DEFAULT_TEXTS["text.already"]


def test_render_экранирует_html_по_умолчанию(settings: SettingsService) -> None:
    text = settings.render_text_now("text.attendance_marked_by", name="<b>Вася & Ко</b>")
    assert text == "Явку отметил(а) &lt;b&gt;Вася &amp; Ко&lt;/b&gt;"


def test_render_без_экранирования_для_всплывающих_окон(settings: SettingsService) -> None:
    text = settings.render_text_now("text.attendance_marked_by", escape_html=False, name="<b>&")
    assert text == "Явку отметил(а) <b>&"


def test_render_кавычки_не_экранируются(settings: SettingsService) -> None:
    text = settings.render_text_now("text.attendance_marked_by", name='Ян "Бык" О\'Нил')
    assert text == 'Явку отметил(а) Ян "Бык" О\'Нил'


def test_render_отсутствующая_подстановка_даёт_пустую_строку(settings: SettingsService) -> None:
    text = settings.render_text_now("text.event_cancelled", date="25.10")
    assert text == "Встреча 25.10 отменена. "


def test_render_значение_none_считается_отсутствующим(settings: SettingsService) -> None:
    text = settings.render_text_now("text.event_cancelled", date="25.10", reason=None)
    assert text == "Встреча 25.10 отменена. "


def test_render_лишние_подстановки_игнорируются_и_логируются(
    settings: SettingsService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        text = settings.render_text_now("text.already", лишнее="x", other=1)
    assert text == DEFAULT_TEXTS["text.already"]
    assert "лишние подстановки" in caplog.text


def test_render_число_подставляется_как_строка(settings: SettingsService) -> None:
    assert settings.render_text_now("text.already_waitlist", position=3) == (
        "Вы уже в листе ожидания, номер 3."
    )


def test_render_значение_не_подставляется_повторно_как_шаблон(settings: SettingsService) -> None:
    # Пользовательский ввод «{name}» не должен раскрываться второй раз
    text = settings.render_text_now("text.attendance_marked_by", name="{name}{reason}")
    assert text == "Явку отметил(а) {name}{reason}"


def test_render_неизвестный_ключ_это_ошибка(settings: SettingsService) -> None:
    with pytest.raises(SettingsValidationError):
        settings.render_text_now("text.нет_такого")


def test_render_не_текстовый_ключ_это_ошибка(settings: SettingsService) -> None:
    with pytest.raises(SettingsValidationError):
        settings.render_text_now("capacity")


async def test_render_использует_текст_админа_после_set(settings: SettingsService) -> None:
    await settings.set("text.already", "Уже есть, {name}!", ADMIN_ID)
    assert settings.render_text_now("text.already", name="Аня") == "Уже есть, Аня!"
    await settings.reset("text.already", ADMIN_ID)
    assert settings.render_text_now("text.already") == DEFAULT_TEXTS["text.already"]


async def test_render_async_совпадает_с_синхронным(settings: SettingsService) -> None:
    assert await settings.render_text("text.already_waitlist", position=2) == (
        settings.render_text_now("text.already_waitlist", position=2)
    )


@pytest.mark.parametrize(
    "broken", ["{unknown} вместо", "{", "Одинокая } скобка", "{date.__class__}"]
)
async def test_render_испорченный_шаблон_откатывается_на_умолчание(
    app: App,
    clock: FakeClock,
    broken: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with app.sessions() as session, session.begin():
        await settings_repo.upsert(
            session, "text.already_waitlist", broken, actor_id=None, now=clock.now()
        )
    service = SettingsService(app.sessions, clock)
    await service.load()
    with caplog.at_level(logging.ERROR):
        text = service.render_text_now("text.already_waitlist", position=4)
    assert text == "Вы уже в листе ожидания, номер 4."
    assert "испорчен" in caplog.text
