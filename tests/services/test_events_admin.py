"""EventService: админские действия над встречами (5.2, 5.7, 5.10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import Attendance, Event, EventStatus, Registration, User
from meetup_bot.repositories import bans as bans_repo
from meetup_bot.repositories import events
from meetup_bot.services.errors import InvalidOperation, NotFound, PermissionDenied
from meetup_bot.services.events import UNSET
from meetup_bot.services.registration import RegisterStatus
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

STRANGER = 777
NEXT_SATURDAY = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)  # суббота 17:00 в Монтевидео


async def make_event(app: App, capacity: int = 10) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)).event_id


async def load(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        return event


async def allow_dm(app: App, *user_ids: int) -> None:
    async with app.sessions() as session, session.begin():
        for user_id in user_ids:
            user = await session.get(User, user_id)
            assert user is not None
            user.dm_available = True


async def statuses(app: App, event_id: int) -> dict[int, str]:
    async with app.sessions() as session:
        rows = await session.execute(
            select(Registration.user_id, Registration.status).where(
                Registration.event_id == event_id
            )
        )
        return {user_id: status for user_id, status in rows.all()}


# ---- создание ----


async def test_создание_админом_сохраняет_поля_и_моменты(app: App) -> None:
    created = await app.services.events.create_event_as_admin(
        ADMIN_ID,
        capacity=7,
        description_html="<b>Привет</b>",
        place="Кафе",
        place_url="https://example.com/map",
        photo_file_id="AgAD",
    )
    event = await load(app, created.event_id)
    now = app.clock.now()
    assert (event.capacity, event.place, event.photo_file_id) == (7, "Кафе", "AgAD")
    assert event.place_url == "https://example.com/map"
    assert event.description_html == "<b>Привет</b>"
    assert event.status == EventStatus.OPEN.value
    assert event.created_by == ADMIN_ID
    assert event.starts_at == now + timedelta(hours=32)
    assert event.cancel_deadline_at == now + timedelta(hours=11)
    assert event.registration_closes_at == now + timedelta(hours=32)  # закрытие в начале встречи
    assert event.reminder_at == now + timedelta(hours=8)
    assert event.attendance_prompt_at == now + timedelta(hours=37)
    assert created.warnings == []


async def test_создание_не_админом_запрещено(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.events.create_event_as_admin(STRANGER)


async def test_права_проверяются_раньше_валидации(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.events.create_event_as_admin(STRANGER, capacity=0)


@pytest.mark.parametrize("capacity", [0, -5, 201])
async def test_создание_с_неверным_лимитом(app: App, capacity: int) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)
    assert exc.value.code == "invalid_capacity"


async def test_создание_с_лимитом_не_целым_числом(app: App) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(OWNER_ID, capacity=True)
    assert exc.value.code == "invalid_capacity"


@pytest.mark.parametrize("html", ["<b>не закрыт", "a & b", "<script>x</script>"])
async def test_создание_с_неверным_html_описания(app: App, html: str) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(OWNER_ID, description_html=html)
    assert exc.value.code == "invalid_description_html"


async def test_слишком_длинное_описание_отклоняется(app: App) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(OWNER_ID, description_html="x" * 3501)
    assert exc.value.code == "invalid_description_html"


async def test_длинное_описание_создаёт_встречу_с_предупреждением(app: App) -> None:
    created = await app.services.events.create_event_as_admin(OWNER_ID, description_html="x" * 701)
    assert len(created.warnings) == 1


async def test_создание_со_слишком_длинным_местом(app: App) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(OWNER_ID, place="м" * 201)
    assert exc.value.code == "invalid_place"


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://a b.com", "javascript:alert(1)", "https://" + "a" * 2000]
)
async def test_создание_с_неверной_ссылкой(app: App, url: str) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(OWNER_ID, place_url=url)
    assert exc.value.code == "invalid_place_url"


async def test_ссылка_с_управляющим_символом_отклоняется(app: App) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(
            OWNER_ID, place_url="https://example.com/\x07"
        )
    assert exc.value.code == "invalid_place_url"


@pytest.mark.parametrize("photo", ["", "   ", "я" * 257])
async def test_создание_с_неверным_идентификатором_фото(app: App, photo: str) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(OWNER_ID, photo_file_id=photo)
    assert exc.value.code == "invalid_photo_file_id"


async def test_создание_на_прошедшее_время_отклоняется(app: App) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=app.clock.now() - timedelta(days=1)
        )
    assert exc.value.code == "invalid_times"


async def test_создание_без_часового_пояса_отклоняется(app: App) -> None:
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=datetime(2026, 11, 7, 20, 0)
        )
    assert exc.value.code == "invalid_times"


async def test_создание_пишет_аудит(app: App) -> None:
    event_id = await make_event(app)
    from meetup_bot.db.models import AuditLog

    async with app.sessions() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.event_id == event_id))
    assert row is not None
    assert (row.action, row.actor_id) == ("event.create", OWNER_ID)


async def test_системное_создание_не_проверяет_права(app: App) -> None:
    event_id = await app.services.events.create_event(capacity=3)
    assert (await load(app, event_id)).capacity == 3


# ---- правка деталей ----


async def test_update_details_меняет_только_переданные_поля(app: App) -> None:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, place="Кафе", description_html="Старое", photo_file_id="AgAD"
    )
    await app.services.events.update_details(OWNER_ID, created.event_id, description_html="Новое")
    event = await load(app, created.event_id)
    assert (event.description_html, event.place, event.photo_file_id) == ("Новое", "Кафе", "AgAD")


async def test_update_details_none_убирает_фото(app: App) -> None:
    created = await app.services.events.create_event_as_admin(OWNER_ID, photo_file_id="AgAD")
    await app.services.events.update_details(OWNER_ID, created.event_id, photo_file_id=None)
    assert (await load(app, created.event_id)).photo_file_id is None


async def test_update_details_unset_не_трогает_фото(app: App) -> None:
    created = await app.services.events.create_event_as_admin(OWNER_ID, photo_file_id="AgAD")
    await app.services.events.update_details(OWNER_ID, created.event_id, photo_file_id=UNSET)
    assert (await load(app, created.event_id)).photo_file_id == "AgAD"


async def test_update_details_меняет_место_и_ссылку(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.update_details(
        OWNER_ID, event_id, place="Парк", place_url="https://example.com/p"
    )
    event = await load(app, event_id)
    assert (event.place, event.place_url) == ("Парк", "https://example.com/p")


async def test_update_details_возвращает_предупреждение_о_длинном_описании(app: App) -> None:
    event_id = await make_event(app)
    warnings = await app.services.events.update_details(
        OWNER_ID, event_id, description_html="я" * 701
    )
    assert len(warnings) == 1
    assert (await load(app, event_id)).description_html == "я" * 701


async def test_update_details_неверный_html_не_меняет_встречу(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation):
        await app.services.events.update_details(OWNER_ID, event_id, description_html="<i>")
    assert (await load(app, event_id)).description_html == ""


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("place", "м" * 201, "invalid_place"),
        ("place_url", "ftp://x", "invalid_place_url"),
        ("photo_file_id", " ", "invalid_photo_file_id"),
    ],
)
async def test_update_details_валидирует_поля(app: App, field: str, value: str, code: str) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.update_details(OWNER_ID, event_id, **{field: value})
    assert exc.value.code == code


async def test_update_details_запрещён_не_админу(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.update_details(STRANGER, event_id, place="X")


async def test_update_details_несуществующей_встречи(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.events.update_details(OWNER_ID, 404, place="X")


async def test_update_details_отменённой_встречи_запрещён(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.update_details(OWNER_ID, event_id, place="X")
    assert exc.value.code == "bad_status"


async def test_update_details_пересобирает_посты(app: App) -> None:
    event_id = await make_event(app)
    app.refresher.scheduled.clear()
    await app.services.events.update_details(OWNER_ID, event_id, place="Парк")
    assert app.refresher.scheduled == [event_id]


# ---- перенос ----


async def test_перенос_пересчитывает_все_моменты(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    event = await load(app, event_id)
    assert event.starts_at == NEXT_SATURDAY
    assert event.cancel_deadline_at == NEXT_SATURDAY - timedelta(hours=21)
    assert event.registration_closes_at == NEXT_SATURDAY
    assert event.reminder_at == NEXT_SATURDAY - timedelta(hours=24)
    assert event.attendance_prompt_at == NEXT_SATURDAY + timedelta(hours=5)


async def test_перенос_сбрасывает_отметки_отправленных_напоминаний(app: App) -> None:
    event_id = await make_event(app)
    async with app.sessions() as session, session.begin():
        event = await events.get(session, event_id)
        assert event is not None
        sent = app.clock.now()
        event.reminder_sent_at = sent
        event.attendance_prompt_sent_at = sent
        event.attendance_reminder_sent_at = sent
    await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    event = await load(app, event_id)
    assert event.reminder_sent_at is None
    assert event.attendance_prompt_sent_at is None
    assert event.attendance_reminder_sent_at is None


async def test_перенос_уведомляет_записанных_с_доступными_лс(app: App) -> None:
    event_id = await make_event(app)
    for n in range(3):
        await app.services.registration.register(event_id, person(n))
    await allow_dm(app, 1000, 1001)
    await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    assert len(app.notifier.dms_to(1000)) == 1
    assert len(app.notifier.dms_to(1001)) == 1
    assert app.notifier.dms_to(1002) == []


async def test_перенос_уведомляет_и_очередь(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    await allow_dm(app, 1000, 1001)
    await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    assert len(app.notifier.dms_to(1001)) == 1


async def test_перенос_не_уведомляет_отменивших_запись(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(0))
    await allow_dm(app, 1000)
    await app.services.registration.cancel(event_id, 1000)
    await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    assert app.notifier.dms_to(1000) == []


async def test_перенос_пересобирает_посты(app: App) -> None:
    event_id = await make_event(app)
    app.refresher.scheduled.clear()
    await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    assert app.refresher.scheduled == [event_id]


async def test_перенос_на_прошедшее_время_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.reschedule(OWNER_ID, event_id, app.clock.now())
    assert exc.value.code == "invalid_times"


async def test_перенос_без_часового_пояса_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.reschedule(OWNER_ID, event_id, datetime(2026, 11, 7, 20, 0))
    assert exc.value.code == "invalid_times"


async def test_перенос_запрещён_не_админу(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.reschedule(STRANGER, event_id, NEXT_SATURDAY)


async def test_перенос_закрытой_встречи_открывает_её_заново(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    result = await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    assert (result.registration_reopened, result.registration_stays_closed) == (True, False)
    assert (await load(app, event_id)).status == EventStatus.OPEN.value


async def test_перенос_открытой_встречи_ничего_не_переоткрывает(app: App) -> None:
    event_id = await make_event(app)
    result = await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    assert (result.registration_reopened, result.registration_stays_closed) == (False, False)


async def test_перенос_закрытой_встречи_на_близкое_время_оставляет_её_закрытой(app: App) -> None:
    # Механизм «момент закрытия записи уже прошёл» возможен только при закрытии до начала
    await app.services.settings.set("registration_close_hours_before", 3, OWNER_ID)
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    app.clock.advance(timedelta(hours=30))  # суббота 18:00 UTC, закрытие записи уже прошло
    soon = app.clock.now() + timedelta(hours=1)
    result = await app.services.events.reschedule(OWNER_ID, event_id, soon)
    assert (result.registration_reopened, result.registration_stays_closed) == (False, True)
    assert (await load(app, event_id)).status == EventStatus.CLOSED.value


async def test_перенос_раздаёт_очередь_при_автооткрытии_и_пишет_продвинутым(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    await allow_dm(app, 1001)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    first = await load_registration(app, event_id, 1000)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, first.id)
    await app.services.events.reschedule(OWNER_ID, event_id, NEXT_SATURDAY)
    assert (await statuses(app, event_id))[1001] == "active"
    assert len(app.notifier.dms_to(1001)) == 1  # одно сообщение — «место освободилось»


async def load_registration(app: App, event_id: int, user_id: int) -> Registration:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration).where(
                Registration.event_id == event_id, Registration.user_id == user_id
            )
        )
        assert found is not None
        return found


# ---- открытие и закрытие записи ----


async def test_закрытие_записи_ставит_статус_и_время(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    event = await load(app, event_id)
    assert event.status == EventStatus.CLOSED.value
    assert event.closed_at == app.clock.now()


async def test_повторное_открытие_до_начала_работает(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    await app.services.events.set_registration_open(OWNER_ID, event_id, True)
    event = await load(app, event_id)
    assert (event.status, event.closed_at) == (EventStatus.OPEN.value, None)
    assert (await app.services.registration.register(event_id, person(1))).status is (
        RegisterStatus.OK
    )


async def test_открытие_после_начала_встречи_запрещено(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    app.clock.advance(timedelta(hours=33))
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.set_registration_open(OWNER_ID, event_id, True)
    assert exc.value.code == "already_started"


async def test_открытие_после_времени_закрытия_подтягивает_закрытие_к_началу(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    app.clock.advance(timedelta(hours=30))
    await app.services.events.set_registration_open(OWNER_ID, event_id, True)
    event = await load(app, event_id)
    assert event.registration_closes_at == event.starts_at


async def test_повторная_смена_на_то_же_состояние_ничего_не_делает(app: App) -> None:
    event_id = await make_event(app)
    app.refresher.scheduled.clear()
    await app.services.events.set_registration_open(OWNER_ID, event_id, True)
    assert app.refresher.scheduled == []
    assert (await load(app, event_id)).status == EventStatus.OPEN.value


async def test_открытие_и_закрытие_запрещены_не_админу(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.set_registration_open(STRANGER, event_id, False)


async def test_открытие_несуществующей_встречи(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.events.set_registration_open(OWNER_ID, 404, False)


async def test_открытие_отменённой_встречи_запрещено(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.set_registration_open(OWNER_ID, event_id, True)
    assert exc.value.code == "bad_status"


async def test_смена_состояния_записи_пересобирает_посты(app: App) -> None:
    event_id = await make_event(app)
    app.refresher.scheduled.clear()
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    assert app.refresher.scheduled == [event_id]


# ---- отмена встречи ----


async def test_отмена_встречи_ставит_статус_причину_и_время(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id, "  Дождь  ")
    event = await load(app, event_id)
    assert event.status == EventStatus.CANCELLED.value
    assert event.cancel_reason == "Дождь"
    assert event.cancelled_at == app.clock.now()


async def test_пустая_причина_отмены_хранится_как_null(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id, "   ")
    assert (await load(app, event_id)).cancel_reason is None


async def test_отмена_встречи_уведомляет_активных_а_не_очередь(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    await allow_dm(app, 1000, 1001)
    await app.services.events.cancel_event(OWNER_ID, event_id, "Дождь")
    assert len(app.notifier.dms_to(1000)) == 1
    assert "Дождь" in app.notifier.dms_to(1000)[0]
    assert app.notifier.dms_to(1001) == []


async def test_отмена_встречи_экранирует_причину_в_сообщении(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(0))
    await allow_dm(app, 1000)
    await app.services.events.cancel_event(OWNER_ID, event_id, "<b>X</b>")
    text = app.notifier.dms_to(1000)[0]
    assert "<b>X</b>" not in text
    assert "&lt;b&gt;" in text


async def test_отмена_встречи_не_пишет_без_доступных_лс(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(0))
    await app.services.events.cancel_event(OWNER_ID, event_id)
    assert app.notifier.dms == []


async def test_повторная_отмена_встречи_запрещена(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.cancel_event(OWNER_ID, event_id)
    assert exc.value.code == "bad_status"


async def test_отмена_встречи_запрещена_не_админу(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.cancel_event(STRANGER, event_id)


async def test_неявки_отменённой_встречи_не_считаются(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(0))
    async with app.sessions() as session, session.begin():
        registration = await session.scalar(
            select(Registration).where(Registration.event_id == event_id)
        )
        assert registration is not None
        registration.attendance = Attendance.NO_SHOW.value
        event = await events.get(session, event_id)
        assert event is not None
        event.attendance_marked_at = app.clock.now()
        assert await bans_repo.count_no_shows(session, 1000) == 1
    await app.services.events.cancel_event(OWNER_ID, event_id)
    async with app.sessions() as session:
        assert await bans_repo.count_no_shows(session, 1000) == 0


async def test_отмена_встречи_пересобирает_посты(app: App) -> None:
    event_id = await make_event(app)
    app.refresher.scheduled.clear()
    await app.services.events.cancel_event(OWNER_ID, event_id)
    assert app.refresher.scheduled == [event_id]


# ---- ручная правка моментов ----


async def test_правка_моментов_меняет_только_переданные(app: App) -> None:
    event_id = await make_event(app)
    before = await load(app, event_id)
    new_reminder = before.reminder_at - timedelta(hours=1)
    await app.services.events.edit_moments(OWNER_ID, event_id, reminder_at=new_reminder)
    after = await load(app, event_id)
    assert after.reminder_at == new_reminder
    assert after.cancel_deadline_at == before.cancel_deadline_at
    assert after.registration_closes_at == before.registration_closes_at


async def test_смена_напоминания_сбрасывает_отметку_отправки(app: App) -> None:
    event_id = await make_event(app)
    async with app.sessions() as session, session.begin():
        event = await events.get(session, event_id)
        assert event is not None
        event.reminder_sent_at = app.clock.now()
    before = await load(app, event_id)
    await app.services.events.edit_moments(
        OWNER_ID, event_id, reminder_at=before.reminder_at - timedelta(hours=1)
    )
    assert (await load(app, event_id)).reminder_sent_at is None


async def test_смена_опроса_явки_сбрасывает_обе_отметки(app: App) -> None:
    event_id = await make_event(app)
    async with app.sessions() as session, session.begin():
        event = await events.get(session, event_id)
        assert event is not None
        event.attendance_prompt_sent_at = app.clock.now()
        event.attendance_reminder_sent_at = app.clock.now()
    before = await load(app, event_id)
    await app.services.events.edit_moments(
        OWNER_ID, event_id, attendance_prompt_at=before.attendance_prompt_at + timedelta(hours=1)
    )
    after = await load(app, event_id)
    assert after.attendance_prompt_sent_at is None
    assert after.attendance_reminder_sent_at is None


async def test_правка_без_изменений_не_сбрасывает_отметки(app: App) -> None:
    event_id = await make_event(app)
    async with app.sessions() as session, session.begin():
        event = await events.get(session, event_id)
        assert event is not None
        event.reminder_sent_at = app.clock.now()
    before = await load(app, event_id)
    await app.services.events.edit_moments(OWNER_ID, event_id, reminder_at=before.reminder_at)
    assert (await load(app, event_id)).reminder_sent_at == app.clock.now()


async def test_дедлайн_отмены_позже_закрытия_записи_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    event = await load(app, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.edit_moments(
            OWNER_ID, event_id, cancel_deadline_at=event.registration_closes_at + timedelta(hours=1)
        )
    assert exc.value.code == "invalid_times"


async def test_закрытие_записи_после_начала_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    event = await load(app, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.edit_moments(
            OWNER_ID, event_id, registration_closes_at=event.starts_at + timedelta(minutes=1)
        )
    assert exc.value.code == "invalid_times"


async def test_опрос_явки_раньше_начала_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    event = await load(app, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.edit_moments(
            OWNER_ID, event_id, attendance_prompt_at=event.starts_at
        )
    assert exc.value.code == "invalid_times"


async def test_момент_без_часового_пояса_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.edit_moments(
            OWNER_ID, event_id, reminder_at=datetime(2026, 10, 24, 12, 0)
        )
    assert exc.value.code == "invalid_times"


async def test_неудачная_правка_моментов_не_меняет_встречу(app: App) -> None:
    event_id = await make_event(app)
    before = await load(app, event_id)
    with pytest.raises(InvalidOperation):
        await app.services.events.edit_moments(
            OWNER_ID,
            event_id,
            reminder_at=before.reminder_at - timedelta(hours=1),
            attendance_prompt_at=before.starts_at,
        )
    assert (await load(app, event_id)).reminder_at == before.reminder_at


async def test_правка_моментов_запрещена_не_админу(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.edit_moments(STRANGER, event_id, reminder_at=NEXT_SATURDAY)


async def test_правка_моментов_пересобирает_посты(app: App) -> None:
    event_id = await make_event(app)
    before = await load(app, event_id)
    app.refresher.scheduled.clear()
    await app.services.events.edit_moments(
        OWNER_ID, event_id, reminder_at=before.reminder_at - timedelta(hours=1)
    )
    assert app.refresher.scheduled == [event_id]


# ---- список ----


async def test_список_показывает_открытые_и_закрытые_с_занятыми_местами(app: App) -> None:
    first = await make_event(app, 5)
    second = await app.services.events.create_event_as_admin(
        OWNER_ID, starts_at=NEXT_SATURDAY, capacity=8
    )
    await app.services.registration.register(first, person(0))
    await app.services.events.set_registration_open(OWNER_ID, second.event_id, False)
    items = await app.services.events.list_events(ADMIN_ID)
    by_id = {item.id: item for item in items}
    assert set(by_id) == {first, second.event_id}
    assert (by_id[first].seats_taken, by_id[first].capacity) == (1, 5)
    assert by_id[second.event_id].status == EventStatus.CLOSED.value
    assert [item.id for item in items] == [second.event_id, first]  # новые сверху


async def test_список_скрывает_отменённые_без_include_recent(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    assert await app.services.events.list_events(OWNER_ID) == []


async def test_список_с_include_recent_показывает_недавние_отменённые(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    items = await app.services.events.list_events(OWNER_ID, include_recent=True)
    assert [item.id for item in items] == [event_id]


async def test_список_с_include_recent_не_показывает_давно_прошедшие(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    app.clock.advance(timedelta(days=60))
    assert await app.services.events.list_events(OWNER_ID, include_recent=True) == []


async def test_список_запрещён_не_админу(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.events.list_events(STRANGER)


# ---- set_capacity: права и статусы ----


async def test_смена_лимита_запрещена_не_админу(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.set_capacity(STRANGER, event_id, 5)


async def test_смена_лимита_несуществующей_встречи(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.events.set_capacity(OWNER_ID, 404, 5)


async def test_смена_лимита_отменённой_встречи_запрещена(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.set_capacity(OWNER_ID, event_id, 5)
    assert exc.value.code == "bad_status"


async def test_увеличение_лимита_пишет_лс_продвинутым_и_пересобирает_пост(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    await allow_dm(app, 1001)
    app.refresher.scheduled.clear()
    await app.services.events.set_capacity(OWNER_ID, event_id, 2)
    assert len(app.notifier.dms_to(1001)) == 1
    assert app.refresher.scheduled == [event_id]


async def test_продвинутый_без_доступного_лс_не_получает_сообщение(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    await app.services.events.set_capacity(OWNER_ID, event_id, 2)
    assert app.notifier.dms == []
    assert (await statuses(app, event_id))[1001] == "active"


# ---- шаблон новой встречи по прошлой (R5) ----


async def test_шаблон_без_встреч_это_None(app: App) -> None:
    assert await app.services.events.last_event_template(OWNER_ID) is None


async def test_шаблон_берёт_последнюю_по_времени_начала(app: App) -> None:
    await app.services.events.create_event_as_admin(
        OWNER_ID, starts_at=NEXT_SATURDAY + timedelta(days=7), capacity=5, place="Поздняя"
    )
    await app.services.events.create_event_as_admin(
        OWNER_ID, starts_at=NEXT_SATURDAY, capacity=8, place="Ранняя"
    )
    template = await app.services.events.last_event_template(ADMIN_ID)
    assert template is not None
    assert (template.place, template.capacity) == ("Поздняя", 5)


async def test_шаблон_пропускает_отменённую_встречу(app: App) -> None:
    await app.services.events.create_event_as_admin(
        OWNER_ID,
        starts_at=NEXT_SATURDAY,
        place="Живая",
        description_html="<b>x</b>",
        place_url="https://example.com/m",
        photo_file_id="ph",
    )
    late = await app.services.events.create_event_as_admin(
        OWNER_ID, starts_at=NEXT_SATURDAY + timedelta(days=7), place="Отменённая"
    )
    await app.services.events.cancel_event(OWNER_ID, late.event_id)
    template = await app.services.events.last_event_template(OWNER_ID)
    assert template is not None
    assert template.place == "Живая"
    assert template.description_html == "<b>x</b>"
    assert template.place_url == "https://example.com/m"
    assert template.photo_file_id == "ph"


async def test_шаблон_только_из_отменённых_это_None(app: App) -> None:
    created = await app.services.events.create_event_as_admin(OWNER_ID, starts_at=NEXT_SATURDAY)
    await app.services.events.cancel_event(OWNER_ID, created.event_id)
    assert await app.services.events.last_event_template(OWNER_ID) is None


async def test_шаблон_время_начала_в_поясе_настроек(app: App) -> None:
    from datetime import time

    await app.services.events.create_event_as_admin(OWNER_ID, starts_at=NEXT_SATURDAY)
    template = await app.services.events.last_event_template(OWNER_ID)
    assert template is not None
    assert template.start_time == time(17, 0)  # а не 20:00 UTC


async def test_шаблон_недоступен_не_админу(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.events.last_event_template(STRANGER)
