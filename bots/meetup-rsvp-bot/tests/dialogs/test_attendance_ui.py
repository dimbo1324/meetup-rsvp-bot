"""Список явки в личке админа: отправка, переключатели, «Готово», исправление отметок."""

from datetime import timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select

from meetup_bot.db.models import Attendance, Ban, Event, Registration
from meetup_bot.telegram.attendance_callbacks import AttendanceCallback
from meetup_bot.telegram.attendance_ui import SheetStatus
from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.tg_fake import Harness, callback_update

# ---- локальные помощники -------------------------------------------------------------


def _labels(tg: Harness, mid: int) -> list[str]:
    return [text for text, _ in tg.session.buttons(mid)]


def _alerts(tg: Harness) -> list[str]:
    return [str(d["text"]) for d in tg.session.of("AnswerCallbackQuery") if d.get("text")]


def _last_sheet_mid(tg: Harness) -> int:
    return tg.session.last_message_id


async def _event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
    assert event is not None
    return event


async def _registrations(app: App, event_id: int) -> list[Registration]:
    async with app.sessions() as session:
        rows = await session.scalars(
            select(Registration).where(Registration.event_id == event_id).order_by(Registration.id)
        )
        return list(rows.all())


async def _bans(app: App) -> list[Ban]:
    async with app.sessions() as session:
        return list((await session.scalars(select(Ban).order_by(Ban.id))).all())


async def _start_meeting(app: App, event_id: int) -> None:
    """Часы -> через час после начала встречи."""
    starts_at = (await _event(app, event_id)).starts_at
    app.clock.advance(starts_at - app.clock.now() + timedelta(hours=1))
    await app.services.lifecycle.finish_due(app.clock.now())


def _forbidden(name: str, data: dict[str, Any]) -> Exception | None:
    if name == "SendMessage":
        return TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "Forbidden: no chat")
    return None


def _bad_request(method_name: str) -> Any:
    def fail(name: str, data: dict[str, Any]) -> Exception | None:
        if name == method_name:
            return TelegramBadRequest(
                SendMessage(chat_id=1, text="x"), "Bad Request: message is not modified"
            )
        return None

    return fail


@pytest.fixture
async def event_id(app: App) -> int:
    created = await app.services.events.create_event_as_admin(OWNER_ID, capacity=5, place="Кафе")
    return created.event_id


@pytest.fixture
async def started(app: App, event_id: int) -> int:
    """Встреча с тремя записанными, уже началась."""
    for n in (1, 2, 3):
        await app.services.registration.register(event_id, person(n))
    await _start_meeting(app, event_id)
    return event_id


async def _sheet(tg: Harness, event_id: int, admin_id: int = OWNER_ID) -> int:
    status = await tg.attendance_ui.send_sheet(admin_id, event_id)
    assert status is SheetStatus.SENT
    return _last_sheet_mid(tg)


# ---- send_sheet: статусы -----------------------------------------------------------------


async def test_список_отправляется_админу_с_кнопками_по_умолчанию_пришёл(
    tg: Harness, started: int
) -> None:
    status = await tg.attendance_ui.send_sheet(OWNER_ID, started)
    assert status is SheetStatus.SENT
    sent = tg.session.of("SendMessage")[-1]
    assert sent["chat_id"] == OWNER_ID
    assert "Явка: встреча пятница, 23 октября, 16:30–18:30, Кафе" in sent["text"]
    assert "Нажмите на имя, чтобы отметить неявку" in sent["text"]
    assert _labels(tg, _last_sheet_mid(tg)) == [
        "✅ Участник1",
        "✅ Участник2",
        "✅ Участник3",
        "Готово",
    ]


async def test_до_начала_встречи_статус_not_started(tg: Harness, event_id: int) -> None:
    assert await tg.attendance_ui.send_sheet(OWNER_ID, event_id) is SheetStatus.NOT_STARTED
    assert not tg.session.of("SendMessage")


async def test_постороннему_статус_no_access(tg: Harness, started: int) -> None:
    assert await tg.attendance_ui.send_sheet(777, started) is SheetStatus.NO_ACCESS
    assert not tg.session.of("SendMessage")


async def test_отменённая_встреча_статус_cancelled(app: App, tg: Harness, started: int) -> None:
    await app.services.events.cancel_event(OWNER_ID, started, "дождь")
    assert await tg.attendance_ui.send_sheet(OWNER_ID, started) is SheetStatus.CANCELLED


async def test_несуществующая_встреча_статус_cancelled(tg: Harness) -> None:
    assert await tg.attendance_ui.send_sheet(OWNER_ID, 99999) is SheetStatus.CANCELLED


async def test_окно_отметки_закрыто_через_семь_суток(app: App, tg: Harness, started: int) -> None:
    app.clock.advance(timedelta(days=7))
    assert await tg.attendance_ui.send_sheet(OWNER_ID, started) is SheetStatus.WINDOW_CLOSED


async def test_граница_окна_ровно_семь_суток_ещё_открыта(
    app: App, tg: Harness, event_id: int
) -> None:
    starts_at = (await _event(app, event_id)).starts_at
    app.clock.advance(starts_at - app.clock.now() + timedelta(days=7))
    await app.services.lifecycle.finish_due(app.clock.now())
    assert await tg.attendance_ui.send_sheet(OWNER_ID, event_id) is SheetStatus.SENT


async def test_ошибка_telegram_при_отправке_даёт_failed(tg: Harness, started: int) -> None:
    tg.session.fail = _forbidden
    assert await tg.attendance_ui.send_sheet(OWNER_ID, started) is SheetStatus.FAILED


async def test_без_участников_список_сообщает_что_их_нет(
    tg: Harness, event_id: int, app: App
) -> None:
    await _start_meeting(app, event_id)
    await _sheet(tg, event_id)
    sent = tg.session.of("SendMessage")[-1]
    assert "Активных участников нет." in sent["text"]
    assert _labels(tg, _last_sheet_mid(tg)) == ["Готово"]


async def test_гость_помечен_в_кнопке(app: App, tg: Harness, event_id: int) -> None:
    await app.services.settings.set("plus_one_enabled", True, OWNER_ID)
    await app.services.registration.register(event_id, person(1), guests=1)
    await _start_meeting(app, event_id)
    mid = await _sheet(tg, event_id)
    assert "✅ Участник1 (+1)" in _labels(tg, mid)


async def test_длинное_имя_в_кнопке_обрезается_многоточием(
    app: App, tg: Harness, event_id: int
) -> None:
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="Д" * 80)
    await _start_meeting(app, event_id)
    mid = await _sheet(tg, event_id)
    label = _labels(tg, mid)[0]
    assert label.startswith("✅ ДДД")
    assert label.endswith("…")
    assert len(label) == len("✅ ") + 40


# ---- переключатели -------------------------------------------------------------------------


async def test_нажатие_на_имя_меняет_только_разметку(app: App, tg: Harness, started: int) -> None:
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник2")
    assert _labels(tg, mid) == ["✅ Участник1", "🚫 Участник2", "✅ Участник3", "Готово"]
    # База не тронута: отметка записывается только по «Готово»
    registrations = await _registrations(app, started)
    assert {r.attendance for r in registrations} == {Attendance.UNKNOWN.value}
    assert (await _event(app, started)).attendance_marked_at is None
    assert tg.session.of("EditMessageReplyMarkup")


async def test_повторное_нажатие_возвращает_отметку_пришёл(tg: Harness, started: int) -> None:
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник2")
    await tg.click(OWNER_ID, mid, "Участник2")
    assert _labels(tg, mid)[1] == "✅ Участник2"


async def test_переключение_не_трогает_остальные_кнопки(tg: Harness, started: int) -> None:
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник1")
    await tg.click(OWNER_ID, mid, "Участник3")
    assert _labels(tg, mid) == ["🚫 Участник1", "✅ Участник2", "🚫 Участник3", "Готово"]


# ---- «Готово» -------------------------------------------------------------------------------


async def test_готово_записывает_явку_и_банит_по_порогу(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник2")
    await tg.click(OWNER_ID, mid, "Готово")
    registrations = await _registrations(app, started)
    assert [r.attendance for r in registrations] == [
        Attendance.ATTENDED.value,
        Attendance.NO_SHOW.value,
        Attendance.ATTENDED.value,
    ]
    event = await _event(app, started)
    assert event.attendance_marked_at is not None
    assert event.attendance_marked_by == OWNER_ID
    edit = tg.session.of("EditMessageText")[-1]
    assert edit["message_id"] == mid
    assert "Явка отмечена ✅. Неявок: 1" in edit["text"]
    assert "Выдано автобанов: 1" in edit["text"]
    assert "reply_markup" not in edit
    bans = await _bans(app)
    assert [(b.user_id, b.active, b.reason) for b in bans] == [(person(2).id, True, "no_show")]


async def test_готово_без_неявок_никого_не_банит(app: App, tg: Harness, started: int) -> None:
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Готово")
    assert "Явка отмечена ✅. Неявок: 0" in tg.session.of("EditMessageText")[-1]["text"]
    registrations = await _registrations(app, started)
    assert {r.attendance for r in registrations} == {Attendance.ATTENDED.value}
    assert await _bans(app) == []


async def test_готово_при_выключенном_автобане_не_пишет_про_баны(
    app: App, tg: Harness, started: int
) -> None:
    await app.services.settings.set("ban_after_no_shows", 0, OWNER_ID)
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник1")
    await tg.click(OWNER_ID, mid, "Готово")
    text = tg.session.of("EditMessageText")[-1]["text"]
    assert "Неявок: 1" in text
    assert "автобанов" not in text
    assert await _bans(app) == []


async def test_после_готово_у_остальных_админов_сообщение_гаснет(
    app: App, tg: Harness, started: int
) -> None:
    owner_mid = await _sheet(tg, started, OWNER_ID)
    admin_mid = await _sheet(tg, started, ADMIN_ID)
    await tg.click(OWNER_ID, owner_mid, "Готово")
    edits = tg.session.of("EditMessageText")
    closed = [e for e in edits if e["chat_id"] == ADMIN_ID and e["message_id"] == admin_mid]
    assert len(closed) == 1
    # Имя берётся из профиля нажавшего (в фейковом обновлении это «Люди1»)
    assert closed[0]["text"] == "Явку отметил(а) Люди1"
    # Собственное сообщение нажавшего гасится один раз — итогом, а не подписью
    own = [e for e in edits if e["chat_id"] == OWNER_ID and e["message_id"] == owner_mid]
    assert len(own) == 1
    assert "Явка отмечена" in own[0]["text"]


async def test_ошибка_при_гашении_чужого_списка_не_мешает_итогу(
    app: App, tg: Harness, started: int
) -> None:
    owner_mid = await _sheet(tg, started, OWNER_ID)
    await _sheet(tg, started, ADMIN_ID)

    def fail_admin_edit(name: str, data: dict[str, Any]) -> Exception | None:
        if name == "EditMessageText" and data.get("chat_id") == ADMIN_ID:
            return TelegramBadRequest(SendMessage(chat_id=1, text="x"), "Bad Request: gone")
        return None

    tg.session.fail = fail_admin_edit
    await tg.click(OWNER_ID, owner_mid, "Готово")
    assert (await _event(app, started)).attendance_marked_at is not None


async def test_вторая_отметка_другим_админом_сообщает_кто_уже_отметил(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started, ADMIN_ID)
    await app.services.attendance.mark(OWNER_ID, started, set())
    await tg.click(ADMIN_ID, mid, "Участник1")
    await tg.click(ADMIN_ID, mid, "Готово")
    edit = tg.session.of("EditMessageText")[-1]
    assert edit["message_id"] == mid
    assert edit["text"].startswith("Явку отметил(а)")
    # Отметка первого админа не перезаписана
    event = await _event(app, started)
    assert event.attendance_marked_by == OWNER_ID
    registrations = await _registrations(app, started)
    assert {r.attendance for r in registrations} == {Attendance.ATTENDED.value}


async def test_повторное_готово_того_же_админа_не_перезаписывает_явку(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started)
    # После «Готово» правка снимает клавиатуру; запоздавший повтор — это нажатие по старым данным
    stale = dict((text, data) for text, data in tg.session.buttons(mid))
    first_row = next(data for text, data in stale.items() if "Участник1" in text)
    second_row = next(data for text, data in stale.items() if "Участник2" in text)
    done = next(data for text, data in stale.items() if "Готово" in text)
    await tg.click(OWNER_ID, mid, "Участник1")
    await tg.click(OWNER_ID, mid, "Готово")
    marked = (await _event(app, started)).attendance_marked_at
    assert first_row and done
    await tg.feed(callback_update(OWNER_ID, second_row, message_id=mid))
    await tg.feed(callback_update(OWNER_ID, done, message_id=mid))
    assert (await _event(app, started)).attendance_marked_at == marked
    registrations = await _registrations(app, started)
    assert [r.attendance for r in registrations] == [
        Attendance.NO_SHOW.value,
        Attendance.ATTENDED.value,
        Attendance.ATTENDED.value,
    ]


async def test_готово_после_закрытия_окна_показывает_ошибку(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started)
    app.clock.advance(timedelta(days=8))
    await tg.click(OWNER_ID, mid, "Готово")
    assert _alerts(tg)[-1] == "Окно отметки явки закрыто (прошло более 7 суток)."
    assert (await _event(app, started)).attendance_marked_at is None


async def test_сбой_пересчёта_банов_честно_предупреждает_админа(
    app: App, tg: Harness, started: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", broken)
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник1")
    await tg.click(OWNER_ID, mid, "Готово")
    text = tg.session.of("EditMessageText")[-1]["text"]
    assert "Явка отмечена ✅. Неявок: 1" in text
    assert "Пересчёт банов отложен для участников: 1. Бот повторит его автоматически." in text
    # Отметка при этом сохранена
    assert (await _event(app, started)).attendance_marked_at is not None


async def test_непредвиденная_ошибка_не_раскрывает_детали(
    app: App, tg: Harness, started: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("секретная трассировка")

    monkeypatch.setattr(app.services.attendance, "mark", broken)
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Готово")
    assert _alerts(tg)[-1] == "Что-то пошло не так, попробуйте ещё раз."


async def test_ошибка_telegram_при_правке_итога_не_роняет_обработчик(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started)
    tg.session.fail = _bad_request("EditMessageText")
    await tg.click(OWNER_ID, mid, "Готово")
    assert (await _event(app, started)).attendance_marked_at is not None


async def test_ошибка_telegram_при_переключении_не_роняет_обработчик(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started)
    tg.session.fail = _bad_request("EditMessageReplyMarkup")
    await tg.click(OWNER_ID, mid, "Участник1")
    assert _alerts(tg) == [] or _alerts(tg)[-1] != "Что-то пошло не так, попробуйте ещё раз."
    assert tg.session.of("AnswerCallbackQuery")


# ---- режим исправления --------------------------------------------------------------------------


async def test_после_отметки_список_открывается_в_режиме_исправления(
    app: App, tg: Harness, started: int
) -> None:
    await app.services.attendance.mark(OWNER_ID, started, set())
    mid = await _sheet(tg, started)
    text = tg.session.of("SendMessage")[-1]["text"]
    assert "Нажатие на имя сразу меняет отметку." in text
    assert "Исправление отметок возможно до" in text
    assert "Готово" not in " | ".join(_labels(tg, mid))


async def test_режим_исправления_показывает_уже_отмеченные_неявки(
    app: App, tg: Harness, started: int
) -> None:
    registrations = await _registrations(app, started)
    await app.services.attendance.mark(OWNER_ID, started, {registrations[1].id})
    mid = await _sheet(tg, started)
    assert _labels(tg, mid) == ["✅ Участник1", "🚫 Участник2", "✅ Участник3"]


async def test_исправление_неявки_на_пришёл_снимает_автобан(
    app: App, tg: Harness, started: int
) -> None:
    registrations = await _registrations(app, started)
    await app.services.attendance.mark(OWNER_ID, started, {registrations[1].id})
    assert [b.active for b in await _bans(app)] == [True]
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник2")
    assert (await _registrations(app, started))[1].attendance == Attendance.ATTENDED.value
    assert _labels(tg, mid)[1] == "✅ Участник2"
    assert [b.active for b in await _bans(app)] == [False]


async def test_исправление_пришёл_на_неявку_выдаёт_автобан(
    app: App, tg: Harness, started: int
) -> None:
    await app.services.attendance.mark(OWNER_ID, started, set())
    mid = await _sheet(tg, started)
    await tg.click(OWNER_ID, mid, "Участник3")
    assert (await _registrations(app, started))[2].attendance == Attendance.NO_SHOW.value
    assert _labels(tg, mid)[2] == "🚫 Участник3"
    bans = await _bans(app)
    assert [(b.user_id, b.active) for b in bans] == [(person(3).id, True)]


async def test_исправление_после_закрытия_окна_отклонено(
    app: App, tg: Harness, started: int
) -> None:
    await app.services.attendance.mark(OWNER_ID, started, set())
    mid = await _sheet(tg, started)
    app.clock.advance(timedelta(days=8))
    await tg.click(OWNER_ID, mid, "Участник1")
    assert _alerts(tg)[-1] == "Окно отметки явки закрыто (прошло более 7 суток)."
    assert _labels(tg, mid)[0] == "✅ Участник1"
    assert (await _registrations(app, started))[0].attendance == Attendance.ATTENDED.value


# ---- права и устаревшие кнопки ----


async def test_постороннему_нажатие_на_кнопки_списка_отклоняется(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started)
    await tg.click(777, mid, "Готово")
    assert _alerts(tg)[-1] == "Нет доступа."
    assert (await _event(app, started)).attendance_marked_at is None


async def test_потерявший_права_админ_не_переключает_кнопки(
    app: App, tg: Harness, started: int
) -> None:
    mid = await _sheet(tg, started, ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, mid, "Участник1")
    assert _alerts(tg)[-1] == "Нет доступа."
    assert _labels(tg, mid)[0] == "✅ Участник1"


async def test_кнопка_без_клавиатуры_в_сообщении_считается_устаревшей(
    app: App, tg: Harness, started: int
) -> None:
    data = AttendanceCallback(event_id=started, target="d").pack()
    await tg.feed(callback_update(OWNER_ID, data, message_id=900))
    assert _alerts(tg)[-1] == "Список устарел. Откройте актуальный."
    assert (await _event(app, started)).attendance_marked_at is None


async def test_кнопка_под_inline_сообщением_считается_устаревшей(
    app: App, tg: Harness, started: int
) -> None:
    data = AttendanceCallback(event_id=started, target="1").pack()
    await tg.feed(callback_update(OWNER_ID, data, inline_message_id="abc"))
    assert (await _event(app, started)).attendance_marked_at is None


# ---- память UI ----


async def test_отправленные_списки_запоминаются_ограниченно(tg: Harness) -> None:
    ui = tg.attendance_ui
    for event_number in range(60):
        ui._remember(event_number, OWNER_ID, 1000 + event_number)
    assert len(ui._sent) == 50
    assert 59 in ui._sent
    assert 0 not in ui._sent


async def test_повторная_отправка_тому_же_админу_заменяет_адрес(tg: Harness) -> None:
    ui = tg.attendance_ui
    ui._remember(1, OWNER_ID, 10)
    ui._remember(1, OWNER_ID, 11)
    assert ui._sent[1] == [(OWNER_ID, 11)]


async def test_исправление_берёт_целевое_значение_с_кнопки_а_не_переключает_бд(
    app: App, tg: Harness, started: int
) -> None:
    await app.services.attendance.mark(OWNER_ID, started, set())
    first = await _sheet(tg, started)
    second = await _sheet(tg, started)
    # Оба админа видят «✅ Участник3» и жмут одновременно: итог один, а не взаимная отмена
    await tg.click(OWNER_ID, first, "Участник3")
    await tg.click(OWNER_ID, second, "Участник3")
    assert (await _registrations(app, started))[2].attendance == Attendance.NO_SHOW.value
