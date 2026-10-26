"""Явка — только у завершённой встречи: лист явки, окно, целевое значение исправления."""

from __future__ import annotations

from datetime import timedelta

import pytest

from meetup_bot.db.models import Attendance, EventStatus
from meetup_bot.services.attendance import ATTENDANCE_WINDOW
from meetup_bot.services.errors import InvalidOperation, NotFound, PermissionDenied
from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.services.test_attendance import (
    STRANGER_ID,
    advance_to_after_start,
    attendance_of,
    audit_actions,
    join,
    make_event,
    start_of,
)


async def test_после_начала_но_до_закрытия_записи_явку_отмечать_рано(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    app.clock.advance(await start_of(app, event_id) + timedelta(minutes=30) - app.clock.now())
    await app.services.lifecycle.finish_due(app.clock.now())
    for call in (
        app.services.attendance.get_sheet(ADMIN_ID, event_id),
        app.services.attendance.sheet_info(ADMIN_ID, event_id),
        app.services.attendance.mark(ADMIN_ID, event_id, {registration_id}),
    ):
        with pytest.raises(InvalidOperation) as error:
            await call
        assert error.value.code == "attendance_not_yet"


async def test_закрытая_вручную_запись_не_делает_встречу_завершённой(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    app.clock.advance(await start_of(app, event_id) + timedelta(minutes=10) - app.clock.now())
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert error.value.code == "attendance_not_yet"


async def test_исправление_до_завершения_встречи_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.correct(
            ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
        )
    assert error.value.code == "attendance_not_yet"


async def test_sheet_info_до_отметки_даёт_обычный_режим(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)

    info = await app.services.attendance.sheet_info(ADMIN_ID, event_id)

    assert not info.correction
    assert info.starts_at == await start_of(app, event_id)
    assert info.window_ends_at == info.starts_at + ATTENDANCE_WINDOW
    assert [row.name for row in info.rows] == [person(1).first_name]
    assert info.event_id == event_id


async def test_sheet_info_после_отметки_даёт_режим_исправления(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert (await app.services.attendance.sheet_info(ADMIN_ID, event_id)).correction


async def test_sheet_info_после_окна_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    await advance_to_after_start(app, event_id)
    app.clock.advance(await start_of(app, event_id) + ATTENDANCE_WINDOW - app.clock.now())
    await app.services.attendance.sheet_info(ADMIN_ID, event_id)  # граница ещё в окне
    app.clock.advance(timedelta(minutes=1))
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.sheet_info(ADMIN_ID, event_id)
    assert error.value.code == "attendance_window_closed"


async def test_sheet_info_отменённой_встречи_и_чужого_и_несуществующей(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.sheet_info(ADMIN_ID, event_id)
    assert error.value.code == "event_cancelled"
    with pytest.raises(PermissionDenied):
        await app.services.attendance.sheet_info(STRANGER_ID, event_id)
    with pytest.raises(NotFound):
        await app.services.attendance.sheet_info(ADMIN_ID, 9999)


async def test_исправление_принимает_целевое_значение_и_повтор_ничего_не_меняет(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())

    await app.services.attendance.correct(ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW)
    # Второй админ нажал то же самое: итог тот же, а не обратный
    outcome = await app.services.attendance.correct(
        ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
    )

    assert await attendance_of(app, registration_id) == "no_show"
    assert not outcome.banned  # бан выдан первым исправлением, второе его не дублирует
    assert (await audit_actions(app)).count("attendance.correct") == 1


async def test_expire_не_закрывает_открытую_встречу(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    later = await start_of(app, event_id) + ATTENDANCE_WINDOW + timedelta(days=1)
    assert await app.services.attendance.expire_unmarked(later) == []
    assert (await app.services.events.get_card(OWNER_ID, event_id)).status == (
        EventStatus.OPEN.value
    )
