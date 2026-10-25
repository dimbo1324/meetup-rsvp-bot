"""Диалог настроек: группы, переключатели, ввод чисел/времени/текстов, сброс, часовой пояс."""

from datetime import UTC, datetime, timedelta

from aiogram.types import Message, PhotoSize, Update

from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import (
    Harness,
    callback_update,
    chat,
    command_update,
    next_id,
    text_update,
    tg_user,
)

# ---- локальные помощники -------------------------------------------------------------


def _inline(tg: Harness) -> int:
    ids = [m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk]
    return max(ids)


def _labels(tg: Harness) -> list[str]:
    return [text for text, _ in tg.session.buttons(_inline(tg))]


def _screen(tg: Harness) -> str:
    for name, data in reversed(tg.session.calls):
        if name in {"SendMessage", "EditMessageText"} and "text" in data:
            return str(data["text"])
    return ""


async def _press(tg: Harness, label: str, *, user: int = OWNER_ID) -> None:
    await tg.click(user, _inline(tg), label)


async def _press_exact(tg: Harness, label: str, *, user: int = OWNER_ID) -> None:
    mid = _inline(tg)
    data = next(d for t, d in tg.session.buttons(mid) if t == label)
    await tg.feed(
        callback_update(user, data, message_id=mid, reply_markup=tg.session.last_markup[mid])
    )


async def _say(tg: Harness, text: str, *, user: int = OWNER_ID) -> None:
    await tg.feed(text_update(user, text))


def _photo_update(user_id: int, file_id: str) -> Update:
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=chat(user_id),
            from_user=tg_user(user_id),
            photo=[PhotoSize(file_id=file_id, file_unique_id="u", width=10, height=10)],
        ),
    )


async def _open(tg: Harness, *path: str, user: int = OWNER_ID) -> None:
    """/menu -> «Настройки» -> дальше по подписям кнопок."""
    await tg.feed(command_update(user, "/menu"))
    await tg.click(user, _inline(tg), "Настройки")
    for label in path:
        await tg.click(user, _inline(tg), label)


# ---- навигация ---------------------------------------------------------------------------


async def test_меню_настроек_показывает_разделы(tg: Harness) -> None:
    await _open(tg)
    labels = " | ".join(_labels(tg))
    for expected in (
        "Встречи по умолчанию",
        "Расписание",
        "Правила",
        "Тексты сообщений",
        "Часовой пояс",
    ):
        assert expected in labels


async def test_группа_правила_показывает_значения_и_пометку_по_умолчанию(tg: Harness) -> None:
    await _open(tg, "Правила")
    assert "Имена в посте: выключено · по умолч." in _labels(tg)
    assert "Автобан по неявкам: порог 1 · по умолч." in _labels(tg)


async def test_назад_из_группы_и_выход_в_главное_меню(tg: Harness) -> None:
    await _open(tg, "Правила")
    await _press(tg, "Назад")
    assert "Настройки" in _screen(tg)
    await _press(tg, "В меню")
    assert "Меню администратора" in _screen(tg)


async def test_назад_из_редактирования_возвращает_в_группу(tg: Harness) -> None:
    await _open(tg, "Правила", "Имена в посте")
    await _press(tg, "Назад")
    assert "Выберите настройку, чтобы изменить." in _screen(tg)


# ---- переключатели -------------------------------------------------------------------------


async def test_переключение_флага_видно_в_сервисе_настроек(app: App, tg: Harness) -> None:
    await _open(tg, "Правила", "Имена в посте")
    assert app.services.settings.get_now("show_names_in_post") is False
    await _press(tg, "Включить")
    assert app.services.settings.get_now("show_names_in_post") is True
    assert "✅ Сохранено." in _screen(tg)
    assert "Сейчас: <b>включено</b>" in _screen(tg)
    await _press(tg, "Выключить")
    assert app.services.settings.get_now("show_names_in_post") is False


async def test_сброс_флага_возвращает_значение_по_умолчанию(app: App, tg: Harness) -> None:
    await _open(tg, "Правила", "Имена в посте")
    await _press(tg, "Включить")
    await _press(tg, "Сбросить по умолчанию")
    assert app.services.settings.get_now("show_names_in_post") is False
    assert "Возвращено значение по умолчанию." in _screen(tg)
    assert "(по умолчанию)" in _screen(tg)


async def test_текст_в_настройке_с_кнопками_не_принимается(app: App, tg: Harness) -> None:
    await _open(tg, "Правила", "Имена в посте")
    await _say(tg, "да")
    assert "Для этой настройки используйте кнопки." in _screen(tg)
    assert app.services.settings.get_now("show_names_in_post") is False


async def test_выбор_дня_недели_кнопкой(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "День встречи")
    assert "✅ пт" in _labels(tg)
    await _press_exact(tg, "вс")
    assert app.services.settings.get_now("event_weekday") == 6
    assert "Сейчас: <b>воскресенье</b>" in _screen(tg)


# ---- числа ---------------------------------------------------------------------------------


async def test_ввод_числа_сохраняется(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Лимит мест")
    await _say(tg, "15")
    assert app.services.settings.get_now("capacity") == 15
    assert "✅ Сохранено." in _screen(tg)
    assert "Сейчас: <b>15 мест</b>" in _screen(tg)


async def test_не_число_отклоняется_и_значение_не_меняется(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Лимит мест")
    await _say(tg, "много")
    assert "Нужно целое число от 1 до 200." in _screen(tg)
    assert app.services.settings.get_now("capacity") == 10


async def test_число_вне_диапазона_отклоняется(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Лимит мест")
    await _say(tg, "500")
    assert "Значение должно быть от 1 до 200." in _screen(tg)
    assert app.services.settings.get_now("capacity") == 10


async def test_нулевой_порог_автобана_допустим(app: App, tg: Harness) -> None:
    await _open(tg, "Правила", "Автобан по неявкам")
    await _say(tg, "0")
    assert app.services.settings.get_now("ban_after_no_shows") == 0
    assert "Сейчас: <b>выключен</b>" in _screen(tg)


# ---- время ---------------------------------------------------------------------------------


async def test_ввод_времени_сохраняется(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Время начала")
    await _say(tg, "18:30")
    assert app.services.settings.get_now("event_time") == "18:30"
    assert "✅ Сохранено." in _screen(tg)


async def test_неверное_время_отклоняется(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Время начала")
    await _say(tg, "25:00")
    assert "Время должно быть в формате ЧЧ:ММ, например 17:00." in _screen(tg)
    assert app.services.settings.get_now("event_time") == "16:30"


async def test_новое_время_применяется_к_ближайшей_встрече(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Время начала")
    await _say(tg, "19:00")
    created = await app.services.events.create_event_as_admin(OWNER_ID)
    async with app.sessions() as session:
        from meetup_bot.db.models import Event

        event = await session.get(Event, created.event_id)
    assert event is not None
    # Пятница 23.10, 19:00 в Монтевидео (UTC-3) = 22:00 UTC
    assert event.starts_at == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


# ---- моменты встречи: длительность, закрытие записи, запрос явки, напоминание о посте ------


async def test_новые_ключи_видны_в_группах_с_единицами(tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию")
    assert "Длительность встречи: 120 мин · по умолч." in _labels(tg)
    await _press(tg, "Назад")
    await _press(tg, "Расписание")
    labels = _labels(tg)
    for expected in (
        "Закрытие записи: 60 мин после начала · по умолч.",
        "Запрос явки через: 90 мин после начала · по умолч.",
        "Напоминание разослать пост: 2 дн. до встречи · по умолч.",
        "Время напоминания о посте: 10:00 · по умолч.",
    ):
        assert expected in labels


async def test_закрытие_записи_меняется_и_сбрасывается(app: App, tg: Harness) -> None:
    await _open(tg, "Расписание", "Закрытие записи")
    assert "Сейчас: <b>60 мин после начала</b> (по умолчанию)" in _screen(tg)
    await _say(tg, "30")
    assert app.services.settings.get_now("registration_close_minutes_after_start") == 30
    assert "Сейчас: <b>30 мин после начала</b>" in _screen(tg)
    await _say(tg, "0")
    assert "Сейчас: <b>в момент начала</b>" in _screen(tg)
    await _press(tg, "Сбросить по умолчанию")
    assert app.services.settings.get_now("registration_close_minutes_after_start") == 60


async def test_закрытие_записи_вне_диапазона_отклоняется(app: App, tg: Harness) -> None:
    await _open(tg, "Расписание", "Закрытие записи")
    await _say(tg, "300")
    assert "Значение должно быть от 0 до 240." in _screen(tg)
    assert app.services.settings.get_now("registration_close_minutes_after_start") == 60


async def test_запрос_явки_в_минутах(app: App, tg: Harness) -> None:
    await _open(tg, "Расписание", "Запрос явки через")
    await _say(tg, "120")
    assert app.services.settings.get_now("attendance_prompt_minutes_after") == 120
    assert "Сейчас: <b>120 мин после начала</b>" in _screen(tg)


async def test_длительность_встречи_применяется_к_новой_встрече(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Длительность встречи")
    await _say(tg, "90")
    assert app.services.settings.get_now("event_duration_minutes") == 90
    assert "Сейчас: <b>90 мин</b>" in _screen(tg)
    created = await app.services.events.create_event_as_admin(OWNER_ID)
    async with app.sessions() as session:
        from meetup_bot.db.models import Event

        event = await session.get(Event, created.event_id)
    assert event is not None
    assert event.ends_at - event.starts_at == timedelta(minutes=90)


async def test_напоминание_о_посте_выключается_нулём(app: App, tg: Harness) -> None:
    await _open(tg, "Расписание", "Напоминание разослать пост")
    await _say(tg, "0")
    assert app.services.settings.get_now("post_reminder_days_before") == 0
    assert "Сейчас: <b>выключено</b>" in _screen(tg)


async def test_время_напоминания_о_посте(app: App, tg: Harness) -> None:
    await _open(tg, "Расписание", "Время напоминания о посте")
    await _say(tg, "25:00")
    assert app.services.settings.get_now("post_reminder_time") == "10:00"
    await _say(tg, "09:15")
    assert app.services.settings.get_now("post_reminder_time") == "09:15"
    await _press(tg, "Сбросить по умолчанию")
    assert app.services.settings.get_now("post_reminder_time") == "10:00"


# ---- строки, ссылки, описание, фото ------------------------------------------------------------


async def test_место_по_умолчанию_сохраняется_и_очищается(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Место")
    await _say(tg, "Кафе «Утро»")
    assert app.services.settings.get_now("default_place") == "Кафе «Утро»"
    await _press(tg, "Очистить")
    assert app.services.settings.get_now("default_place") == ""
    assert "✅ Сохранено." in _screen(tg)


async def test_ссылка_без_https_отклоняется(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Ссылка на карту")
    await _say(tg, "http://maps.example.com")
    assert "Ссылка должна начинаться с https://" in _screen(tg)
    assert app.services.settings.get_now("default_place_url") == ""
    await _say(tg, "https://maps.example.com/x")
    assert app.services.settings.get_now("default_place_url") == "https://maps.example.com/x"


async def test_описание_по_умолчанию_сохраняется(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Описание в посте")
    await _say(tg, "Приходите вовремя")
    assert app.services.settings.get_now("default_description") == "Приходите вовремя"


async def test_фото_по_умолчанию_принимает_только_фото(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Фото в посте")
    await _say(tg, "текст вместо фото")
    assert "Отправьте именно фото" in _screen(tg)
    assert app.services.settings.get_now("default_photo_file_id") is None
    await tg.feed(_photo_update(OWNER_ID, "photo-1"))
    assert app.services.settings.get_now("default_photo_file_id") == "photo-1"
    await _press(tg, "Очистить")
    assert app.services.settings.get_now("default_photo_file_id") is None


# ---- тексты сообщений --------------------------------------------------------------------------


async def test_список_текстов_содержит_все_шаблоны(tg: Harness) -> None:
    await _open(tg, "Тексты сообщений")
    labels = _labels(tg)
    assert "Успешная запись" in labels
    assert "Встреча отменена" in labels


async def test_окно_текста_показывает_допустимые_подстановки_и_предпросмотр(tg: Harness) -> None:
    await _open(tg, "Тексты сообщений", "Успешная запись")
    text = _screen(tg)
    assert "(по умолчанию)" in text
    assert "{weekday}" in text
    assert "Предпросмотр с примерными значениями:" in text


async def test_новый_текст_сохраняется_и_помечается_в_списке(app: App, tg: Harness) -> None:
    await _open(tg, "Тексты сообщений", "Успешная запись")
    await _say(tg, "Ура, вы записаны на {weekday}!")
    assert app.services.settings.get_now("text.registered") == "Ура, вы записаны на {weekday}!"
    assert "✅ Сохранено." in _screen(tg)
    await _press(tg, "Назад")
    assert "Успешная запись ✏️" in _labels(tg)


async def test_неизвестная_подстановка_отклоняется(app: App, tg: Harness) -> None:
    await _open(tg, "Тексты сообщений", "Успешная запись")
    await _say(tg, "Привет, {nickname}!")
    assert "Неизвестная подстановка" in _screen(tg)
    assert "{nickname}" in _screen(tg)
    assert app.services.settings.get_now("text.registered") != "Привет, {nickname}!"


async def test_текст_всплывающего_окна_длиннее_200_символов_отклоняется(
    app: App, tg: Harness
) -> None:
    await _open(tg, "Тексты сообщений", "Мест нет")
    await _say(tg, "я" * 250)
    assert "Текст слишком длинный" in _screen(tg)


async def test_сброс_текста_возвращает_шаблон_по_умолчанию(app: App, tg: Harness) -> None:
    from meetup_bot.texts.defaults import DEFAULT_TEXTS

    await _open(tg, "Тексты сообщений", "Успешная запись")
    await _say(tg, "Другой текст")
    await _press(tg, "Сбросить по умолчанию")
    assert app.services.settings.get_now("text.registered") == DEFAULT_TEXTS["text.registered"]
    assert "Возвращено значение по умолчанию." in _screen(tg)


async def test_текст_вместо_фото_в_тексте_сообщения_не_принимается(app: App, tg: Harness) -> None:
    await _open(tg, "Тексты сообщений", "Успешная запись")
    await tg.feed(_photo_update(OWNER_ID, "p"))
    # Фото игнорируется окном (принимает только текст): значение не изменилось
    from meetup_bot.texts.defaults import DEFAULT_TEXTS

    assert app.services.settings.get_now("text.registered") == DEFAULT_TEXTS["text.registered"]


# ---- часовой пояс --------------------------------------------------------------------------------


async def test_окно_часового_пояса_отмечает_текущий(tg: Harness) -> None:
    await _open(tg, "Часовой пояс")
    assert "✅ America/Montevideo" in _labels(tg)
    assert "Сейчас: <b>America/Montevideo</b> (по умолчанию)" in _screen(tg)


async def test_выбор_пояса_из_списка_меняет_пояс_сервисов(app: App, tg: Harness) -> None:
    await _open(tg, "Часовой пояс")
    await _press_exact(tg, "Europe/Moscow")
    assert app.services.settings.get_now("timezone") == "Europe/Moscow"
    assert app.services.settings.timezone_now().key == "Europe/Moscow"
    assert "✅ Сохранено." in _screen(tg)


async def test_пояс_вводится_названием_iana(app: App, tg: Harness) -> None:
    await _open(tg, "Часовой пояс")
    await _say(tg, "America/Argentina/Cordoba")
    assert app.services.settings.get_now("timezone") == "America/Argentina/Cordoba"


async def test_неизвестный_пояс_отклоняется(app: App, tg: Harness) -> None:
    await _open(tg, "Часовой пояс")
    await _say(tg, "Нигде/Никогда")
    assert "Неизвестный часовой пояс" in _screen(tg)
    assert app.services.settings.get_now("timezone") == "America/Montevideo"


async def test_сброс_пояса(app: App, tg: Harness) -> None:
    await _open(tg, "Часовой пояс")
    await _press_exact(tg, "UTC")
    await _press(tg, "Сбросить по умолчанию")
    assert app.services.settings.get_now("timezone") == "America/Montevideo"


async def test_смена_пояса_меняет_время_в_списке_встреч(app: App, tg: Harness) -> None:
    await app.services.events.create_event_as_admin(OWNER_ID)
    await _open(tg, "Часовой пояс")
    await _press_exact(tg, "Europe/Moscow")
    await _press(tg, "Назад")
    await _press(tg, "В меню")
    await _press(tg, "Встречи")
    # 16:30 в Монтевидео = 19:30 UTC = 22:30 в Москве
    assert any(t.startswith("23 октября 22:30") for t in _labels(tg))


# ---- права ---------------------------------------------------------------------------------------


async def test_админ_потерявший_права_не_меняет_настройку(app: App, tg: Harness) -> None:
    await _open(tg, "Правила", "Имена в посте", user=ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _press(tg, "Включить", user=ADMIN_ID)
    assert app.services.settings.get_now("show_names_in_post") is False


async def test_админ_потерявший_права_не_пишет_числа(app: App, tg: Harness) -> None:
    await _open(tg, "Встречи по умолчанию", "Лимит мест", user=ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _say(tg, "77", user=ADMIN_ID)
    assert app.services.settings.get_now("capacity") == 10


async def test_админ_потерявший_права_не_открывает_группу(app: App, tg: Harness) -> None:
    await _open(tg, user=ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _press(tg, "Правила", user=ADMIN_ID)
    assert "Выберите настройку, чтобы изменить." not in _screen(tg)
