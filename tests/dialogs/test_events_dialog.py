"""Диалог встреч: список, карточка, закрытие записи, отмена, правки, предпросмотр и CSV."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.types import Message, PhotoSize, Update

from meetup_bot.db.models import Event, EventStatus, User
from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.tg_fake import Harness, chat, command_update, next_id, text_update, tg_user

PLACE = "Кафе «Утро»"
LIST_LABEL = "24 октября 17:00 · запись открыта · занято 0 из 3"


# ---- локальные помощники -------------------------------------------------------------


def _inline(tg: Harness) -> int:
    ids = [m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk]
    return max(ids)


def _labels(tg: Harness, mid: int | None = None) -> list[str]:
    return [text for text, _ in tg.session.buttons(mid or _inline(tg))]


def _screen(tg: Harness) -> str:
    for name, data in reversed(tg.session.calls):
        if name in {"SendMessage", "EditMessageText"} and "text" in data:
            return str(data["text"])
    return ""


def _said(tg: Harness, fragment: str) -> bool:
    return any(fragment in str(data.get("text", "")) for _, data in tg.session.calls)


def _alerts(tg: Harness) -> list[str]:
    return [str(d["text"]) for d in tg.session.of("AnswerCallbackQuery") if d.get("text")]


async def _press(tg: Harness, label: str, *, user: int = OWNER_ID, mid: int | None = None) -> None:
    await tg.click(user, mid or _inline(tg), label)


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


async def _event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
    assert event is not None
    return event


@pytest.fixture
async def event_id(app: App) -> int:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, capacity=3, place=PLACE, place_url="https://maps.example.com/cafe"
    )
    return created.event_id


async def _allow_dm(app: App, user_id: int) -> None:
    """Личные сообщения уходят только тем, кто нажимал /start у бота."""
    async with app.sessions() as session, session.begin():
        user = await session.get(User, user_id)
        assert user is not None
        user.dm_available = True


async def _open_card(tg: Harness) -> int:
    """/menu -> «Встречи» -> первая встреча; возвращает id сообщения диалога."""
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    await tg.click(OWNER_ID, mid, "занято")
    return mid


async def _open_edit(tg: Harness, section: str) -> None:
    await _open_card(tg)
    await _press(tg, "Редактировать")
    await _press(tg, section)


# ---- список --------------------------------------------------------------------------


async def test_список_подписывает_дату_статус_и_занятые_места(tg: Harness, event_id: int) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    assert LIST_LABEL in _labels(tg, mid)


async def test_список_считает_занятые_места(app: App, tg: Harness, event_id: int) -> None:
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    await tg.feed(command_update(OWNER_ID, "/menu"))
    await tg.click(OWNER_ID, _inline(tg), "Встречи")
    assert "24 октября 17:00 · запись открыта · занято 2 из 3" in _labels(tg)


async def test_пустой_список_сообщает_об_отсутствии_встреч(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    await tg.click(OWNER_ID, _inline(tg), "Встречи")
    assert "Открытых и недавних встреч нет." in _screen(tg)


async def test_список_показывает_закрытую_запись(app: App, tg: Harness, event_id: int) -> None:
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    await tg.feed(command_update(OWNER_ID, "/menu"))
    await tg.click(OWNER_ID, _inline(tg), "Встречи")
    assert "24 октября 17:00 · запись закрыта · занято 0 из 3" in _labels(tg)


async def test_кнопка_обновить_отвечает_обновлено(tg: Harness, event_id: int) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    await tg.click(OWNER_ID, mid, "Обновить")
    assert _alerts(tg)[-1] == "Обновлено"


async def test_назад_из_списка_возвращает_в_меню(tg: Harness, event_id: int) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    await tg.click(OWNER_ID, mid, "Назад")
    assert "Меню администратора" in _screen(tg)


# ---- карточка ------------------------------------------------------------------------


async def test_карточка_показывает_данные_встречи(tg: Harness, event_id: int) -> None:
    await _open_card(tg)
    text = _screen(tg)
    assert "суббота, 24 октября, 17:00" in text
    assert PLACE in text
    assert "Статус: запись открыта" in text
    assert "Занято: 0 из 3, в очереди: 0" in text
    assert "Дедлайн отмены: пятница, 23 октября, 20:00" in text


async def test_карточка_содержит_кнопки_действий(tg: Harness, event_id: int) -> None:
    await _open_card(tg)
    labels = " | ".join(_labels(tg))
    for expected in (
        "Участники",
        "Редактировать",
        "Предпросмотр",
        "Закрыть запись",
        "Отметить явку",
        "Выгрузить CSV",
        "Отменить встречу",
        "К списку",
    ):
        assert expected in labels
    assert "Открыть запись" not in labels


async def test_карточка_считает_очередь(app: App, tg: Harness, event_id: int) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    for n in range(1, 5):
        await app.services.registration.register(event_id, person(n))
    await _open_card(tg)
    assert "Занято: 3 из 3, в очереди: 1" in _screen(tg)


async def test_к_списку_возвращает_к_встречам(tg: Harness, event_id: int) -> None:
    await _open_card(tg)
    await _press(tg, "К списку")
    assert LIST_LABEL in _labels(tg)


# ---- закрытие и открытие записи -------------------------------------------------------


async def test_закрыть_запись_меняет_статус_в_базе(app: App, tg: Harness, event_id: int) -> None:
    await _open_card(tg)
    await _press(tg, "Закрыть запись")
    assert (await _event(app, event_id)).status == EventStatus.CLOSED.value
    assert "Запись закрыта." in _screen(tg)
    labels = " | ".join(_labels(tg))
    assert "Открыть запись" in labels
    assert "Закрыть запись" not in labels


async def test_открыть_запись_возвращает_статус_open(app: App, tg: Harness, event_id: int) -> None:
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    await _open_card(tg)
    await _press(tg, "Открыть запись")
    assert (await _event(app, event_id)).status == EventStatus.OPEN.value
    assert "Запись открыта." in _screen(tg)


async def test_закрытие_записи_обновляет_пост(app: App, tg: Harness, event_id: int) -> None:
    await _open_card(tg)
    await _press(tg, "Закрыть запись")
    assert event_id in app.refresher.scheduled


async def test_закрыть_запись_без_прав_отклонено(app: App, tg: Harness, event_id: int) -> None:
    await tg.feed(command_update(ADMIN_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(ADMIN_ID, mid, "Встречи")
    await tg.click(ADMIN_ID, mid, "занято")
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, mid, "Закрыть запись")
    assert _alerts(tg)[-1] == "Нет доступа."
    assert (await _event(app, event_id)).status == EventStatus.OPEN.value


# ---- отмена встречи ------------------------------------------------------------------


async def test_отмена_встречи_с_причиной_ставит_cancelled_и_шлёт_уведомления(
    app: App, tg: Harness, event_id: int
) -> None:
    await app.services.registration.register(event_id, person(1))
    await _allow_dm(app, person(1).id)
    await _open_card(tg)
    await _press(tg, "Отменить встречу")
    await _say(tg, "Ведущий заболел")
    assert "Причина: Ведущий заболел" in _screen(tg)
    await _press(tg, "Да, отменить")
    event = await _event(app, event_id)
    assert event.status == EventStatus.CANCELLED.value
    assert event.cancel_reason == "Ведущий заболел"
    dms = app.notifier.dms_to(person(1).id)
    assert dms
    assert "Ведущий заболел" in dms[-1]
    assert "Встреча отменена, записанным отправлено уведомление." in _screen(tg)


async def test_отмена_встречи_без_причины_по_кнопке(app: App, tg: Harness, event_id: int) -> None:
    await _open_card(tg)
    await _press(tg, "Отменить встречу")
    await _press(tg, "Без причины")
    assert "Причина не указана." in _screen(tg)
    await _press(tg, "Да, отменить")
    event = await _event(app, event_id)
    assert event.status == EventStatus.CANCELLED.value
    assert event.cancel_reason is None


async def test_после_отмены_карточка_не_предлагает_правок(tg: Harness, event_id: int) -> None:
    await _open_card(tg)
    await _press(tg, "Отменить встречу")
    await _press(tg, "Без причины")
    await _press(tg, "Да, отменить")
    labels = " | ".join(_labels(tg))
    assert "Статус: отменена" in _screen(tg)
    assert "Редактировать" not in labels
    assert "Отменить встречу" not in labels
    assert "Закрыть запись" not in labels


async def test_причина_отмены_обрезается_до_300_символов(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_card(tg)
    await _press(tg, "Отменить встречу")
    await _say(tg, "П" * 500)
    await _press(tg, "Да, отменить")
    event = await _event(app, event_id)
    assert event.cancel_reason == "П" * 300


async def test_назад_из_подтверждения_возвращает_к_причине(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_card(tg)
    await _press(tg, "Отменить встречу")
    await _press(tg, "Без причины")
    await _press(tg, "Назад")
    assert "Напишите причину отмены" in _screen(tg)
    assert (await _event(app, event_id)).status == EventStatus.OPEN.value


async def test_подтверждение_отмены_без_прав_отклонено(
    app: App, tg: Harness, event_id: int
) -> None:
    await tg.feed(command_update(ADMIN_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(ADMIN_ID, mid, "Встречи")
    await tg.click(ADMIN_ID, mid, "занято")
    await tg.click(ADMIN_ID, mid, "Отменить встречу")
    await tg.click(ADMIN_ID, mid, "Без причины")
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, mid, "Да, отменить")
    assert _alerts(tg)[-1] == "Нет доступа."
    assert (await _event(app, event_id)).status == EventStatus.OPEN.value


# ---- отметка явки ---------------------------------------------------------------------


async def test_отметить_явку_до_начала_встречи_отклонено(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_card(tg)
    sent_before = len(tg.session.of("SendMessage"))
    await _press(tg, "Отметить явку")
    assert _alerts(tg)[-1] == "Встреча ещё не началась."
    assert len(tg.session.of("SendMessage")) == sent_before


async def test_отметить_явку_после_начала_присылает_список_в_личку(
    app: App, tg: Harness, event_id: int
) -> None:
    await app.services.registration.register(event_id, person(1))
    starts_at = (await _event(app, event_id)).starts_at
    app.clock.advance(starts_at - app.clock.now() + timedelta(hours=1))
    await _open_card(tg)
    await _press(tg, "Отметить явку")
    assert _alerts(tg)[-1] == "Список отправлен вам в личные сообщения."
    sheet = tg.session.of("SendMessage")[-1]
    assert sheet["chat_id"] == OWNER_ID
    assert "Явка: встреча" in sheet["text"]


async def test_отметить_явку_отменённой_встречи_сообщает_об_отмене(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_card(tg)
    await _press(tg, "Отменить встречу")
    await _press(tg, "Без причины")
    await _press(tg, "Да, отменить")
    # Кнопка явки остаётся в карточке отменённой встречи, но сервис ответит отказом
    await _press(tg, "Отметить явку")
    assert _alerts(tg)[-1] == "Встреча отменена."


async def test_отметить_явку_если_личка_закрыта_подсказывает_старт(
    app: App, tg: Harness, event_id: int
) -> None:
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    starts_at = (await _event(app, event_id)).starts_at
    app.clock.advance(starts_at - app.clock.now() + timedelta(hours=1))
    await _open_card(tg)

    def deny(name: str, data: dict[str, Any]) -> Exception | None:
        if name == "SendMessage":
            return TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "Forbidden")
        return None

    tg.session.fail = deny
    await _press(tg, "Отметить явку")
    assert _alerts(tg)[-1] == "Не удалось отправить список (напишите боту /start)."


# ---- предпросмотр и CSV ---------------------------------------------------------------


async def test_предпросмотр_показывает_пост_как_увидят_участники(
    tg: Harness, event_id: int
) -> None:
    await _open_card(tg)
    await _press(tg, "Предпросмотр")
    text = _screen(tg)
    assert PLACE in text
    assert "0" in text
    await _press(tg, "К встрече")
    assert "Статус: запись открыта" in _screen(tg)


async def test_выгрузка_csv_отправляет_документ_с_bom(app: App, tg: Harness, event_id: int) -> None:
    await app.services.registration.register(event_id, person(1))
    await _open_card(tg)
    await _press(tg, "Выгрузить CSV")
    document = tg.session.of("SendDocument")[-1]
    assert document["chat_id"] == OWNER_ID
    payload = document["document"]
    assert payload.filename == "event-2026-10-24.csv"
    assert payload.data.startswith(b"\xef\xbb\xbf")
    assert "Участник1" in payload.data.decode("utf-8-sig")
    assert _alerts(tg)[-1] == "Файл отправлен."


# ---- правка: описание, фото, место, ссылка ---------------------------------------------


async def test_правка_описания_сохраняется_и_возвращает_в_карточку(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_edit(tg, "Описание")
    await _say(tg, "Приходите все")
    assert (await _event(app, event_id)).description_html == "Приходите все"
    assert "Описание обновлено." in _screen(tg)
    assert "Статус: запись открыта" in _screen(tg)


async def test_очистка_описания(app: App, tg: Harness, event_id: int) -> None:
    await app.services.events.update_details(OWNER_ID, event_id, description_html="Старый текст")
    await _open_edit(tg, "Описание")
    await _press(tg, "Очистить")
    assert (await _event(app, event_id)).description_html == ""
    assert "Описание очищено." in _screen(tg)


async def test_правка_описания_не_текстом_даёт_подсказку(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_edit(tg, "Описание")
    await tg.feed(_photo_update(OWNER_ID, "file-1"))
    assert _said(tg, "Пришлите текст описания")
    assert (await _event(app, event_id)).description_html == ""


async def test_правка_фото_и_удаление(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Фото")
    await tg.feed(_photo_update(OWNER_ID, "photo-xyz"))
    assert (await _event(app, event_id)).photo_file_id == "photo-xyz"
    assert "Фото обновлено." in _screen(tg)
    await _press(tg, "Редактировать")
    await _press(tg, "Фото")
    await _press(tg, "Убрать фото")
    assert (await _event(app, event_id)).photo_file_id is None
    assert "Фото убрано." in _screen(tg)


async def test_правка_фото_текстом_даёт_подсказку(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Фото")
    await _say(tg, "не фото")
    assert _said(tg, "Пришлите фото")
    assert (await _event(app, event_id)).photo_file_id is None


async def test_правка_места(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Место")
    await _say(tg, "Парк Родо")
    assert (await _event(app, event_id)).place == "Парк Родо"
    assert "Место обновлено." in _screen(tg)
    assert "Парк Родо" in _screen(tg)


async def test_правка_ссылки_и_её_удаление(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Ссылка на место")
    await _say(tg, "https://maps.example.com/new")
    assert (await _event(app, event_id)).place_url == "https://maps.example.com/new"
    assert "Ссылка обновлена." in _screen(tg)
    await _press(tg, "Редактировать")
    await _press(tg, "Ссылка на место")
    await _press(tg, "Убрать ссылку")
    assert (await _event(app, event_id)).place_url == ""
    assert "Ссылка убрана." in _screen(tg)


async def test_некорректная_ссылка_даёт_подсказку_и_окно_остаётся(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_edit(tg, "Ссылка на место")
    await _say(tg, "ftp://bad")
    assert _said(tg, "Ссылка должна начинаться с https://")
    assert (await _event(app, event_id)).place_url == "https://maps.example.com/cafe"
    # Диалог не закрыт: следующий корректный ввод принимается
    await _say(tg, "https://maps.example.com/ok")
    assert (await _event(app, event_id)).place_url == "https://maps.example.com/ok"


async def test_назад_из_правки_возвращает_в_меню_правок(tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Место")
    await _press(tg, "Назад")
    assert "Что изменить?" in _screen(tg)
    await _press(tg, "К встрече")
    assert "Статус: запись открыта" in _screen(tg)


# ---- правка: дата и время --------------------------------------------------------------


async def test_перенос_встречи_пересчитывает_моменты_и_уведомляет(
    app: App, tg: Harness, event_id: int
) -> None:
    await app.services.registration.register(event_id, person(1))
    await _allow_dm(app, person(1).id)
    await _open_edit(tg, "Дата и время")
    await _say(tg, "31.10.2026 18:30")
    event = await _event(app, event_id)
    # 18:30 в Монтевидео (UTC-3) = 21:30 UTC
    assert event.starts_at == datetime(2026, 10, 31, 21, 30, tzinfo=UTC)
    assert "Встреча перенесена, записанным отправлено уведомление." in _screen(tg)
    assert app.notifier.dms_to(person(1).id)


async def test_неверный_формат_даты_даёт_подсказку_и_не_закрывает_диалог(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_edit(tg, "Дата и время")
    before = (await _event(app, event_id)).starts_at
    await _say(tg, "в субботу вечером")
    assert _said(tg, "ДД.ММ.ГГГГ ЧЧ:ММ")
    assert (await _event(app, event_id)).starts_at == before
    await _say(tg, "31.10.2026 18:30")
    assert (await _event(app, event_id)).starts_at != before


async def test_дата_в_прошлом_отклоняется(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Дата и время")
    before = (await _event(app, event_id)).starts_at
    await _say(tg, "01.01.2020 10:00")
    assert _said(tg, "Моменты времени не согласованы")
    assert (await _event(app, event_id)).starts_at == before


@pytest.mark.xfail(
    strict=True,
    reason="ДЕФЕКТ: дата 01.01.0001 00:00 доходит до сервиса и роняет его OverflowError "
    "(в compute_times), админ видит «Ошибка: OverflowError» вместо подсказки",
)
async def test_кривая_дата_год_1_не_роняет_диалог(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Дата и время")
    before = (await _event(app, event_id)).starts_at
    await _say(tg, "01.01.0001 00:00")
    assert (await _event(app, event_id)).starts_at == before
    assert "OverflowError" not in _screen(tg)
    assert _said(tg, "ДД.ММ.ГГГГ ЧЧ:ММ") or _said(tg, "Моменты времени не согласованы")


async def test_кривая_дата_год_9999_не_роняет_диалог(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Дата и время")
    before = (await _event(app, event_id)).starts_at
    await _say(tg, "31.12.9999 23:59")
    assert (await _event(app, event_id)).starts_at == before
    assert _said(tg, "ДД.ММ.ГГГГ ЧЧ:ММ") or _said(tg, "Моменты времени не согласованы")


async def test_перенос_закрытой_встречи_сообщает_об_открытии_записи(
    app: App, tg: Harness, event_id: int
) -> None:
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    await _open_edit(tg, "Дата и время")
    await _say(tg, "07.11.2026 18:00")
    assert "Запись открыта заново." in _screen(tg)
    assert (await _event(app, event_id)).status == EventStatus.OPEN.value


# ---- правка: лимит -------------------------------------------------------------------


async def test_смена_лимита_сохраняется(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Лимит мест")
    await _say(tg, "12")
    assert (await _event(app, event_id)).capacity == 12
    assert "Лимит мест: 12." in _screen(tg)


async def test_лимит_ниже_занятых_мест_ошибка_и_окно_остаётся(
    app: App, tg: Harness, event_id: int
) -> None:
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    await _open_edit(tg, "Лимит мест")
    await _say(tg, "1")
    assert _said(tg, "Лимит не может быть ниже числа занятых мест")
    assert (await _event(app, event_id)).capacity == 3
    assert "Лимит мест" in _screen(tg)
    await _say(tg, "2")
    assert (await _event(app, event_id)).capacity == 2


async def test_лимит_не_число_даёт_подсказку(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Лимит мест")
    await _say(tg, "много")
    assert _said(tg, "Введите целое число, например 12.")
    assert (await _event(app, event_id)).capacity == 3


async def test_лимит_вне_диапазона_отклоняется(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Лимит мест")
    await _say(tg, "500")
    assert _said(tg, "Лимит мест должен быть числом от 1 до 200.")
    assert (await _event(app, event_id)).capacity == 3


# ---- правка: моменты ------------------------------------------------------------------


async def test_правка_момента_запроса_явки(app: App, tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Моменты")
    await _press(tg, "Запрос явки")
    assert "Запрос явки" in _screen(tg)
    await _say(tg, "24.10.2026 23:00")
    event = await _event(app, event_id)
    assert event.attendance_prompt_at == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    assert "Запрос явки обновлено." in _screen(tg)


async def test_несогласованный_момент_отклоняется_без_закрытия_диалога(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open_edit(tg, "Моменты")
    await _press(tg, "Закрытие записи")
    before = (await _event(app, event_id)).registration_closes_at
    await _say(tg, "25.10.2026 10:00")
    assert _said(tg, "Моменты времени не согласованы")
    assert (await _event(app, event_id)).registration_closes_at == before


async def test_момент_с_неверным_форматом_даёт_подсказку(tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Моменты")
    await _press(tg, "Напоминание")
    await _say(tg, "завтра")
    assert _said(tg, "ДД.ММ.ГГГГ ЧЧ:ММ")


async def test_ввод_момента_без_выбора_просит_выбрать(tg: Harness, event_id: int) -> None:
    await _open_edit(tg, "Моменты")
    await _say(tg, "24.10.2026 10:00")
    assert _said(tg, "Сначала выберите, какой момент менять.")


async def test_назад_в_моментах_сначала_снимает_выбор_затем_уходит_в_меню(
    tg: Harness, event_id: int
) -> None:
    await _open_edit(tg, "Моменты")
    await _press(tg, "Напоминание")
    await _press(tg, "Назад")
    assert "Выберите, что изменить." in _screen(tg)
    await _press(tg, "Назад")
    assert "Что изменить?" in _screen(tg)


# ---- запуск с id и мастер -------------------------------------------------------------


async def test_новая_встреча_открывает_мастер(tg: Harness, event_id: int) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    await tg.click(OWNER_ID, mid, "Новая встреча")
    assert "Новая встреча — шаг 1 из 6" in _screen(tg)


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _свежие_роутеры() -> None:
    """Роутеры модулей создаются при импорте один раз, а Harness собирает корень заново.

    Без отсоединения второй тест падает с «Router is already attached».
    """
    import importlib

    from meetup_bot.dialogs import MODULES
    from meetup_bot.handlers import attendance, errors, inline, signup, start

    routers = [errors.router, signup.router, attendance.router, inline.router, start.router]
    for name in MODULES:
        module = importlib.import_module(name)
        if getattr(module, "router", None) is not None:
            routers.append(module.router)
        routers.extend(getattr(module, "DIALOGS", []))
    for router in routers:
        router._parent_router = None
