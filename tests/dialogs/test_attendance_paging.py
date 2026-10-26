"""Отметка явки для большой встречи и быстрых нажатий: страницы и черновик на стороне бота."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select

from meetup_bot.db.models import Event, Registration
from meetup_bot.telegram.attendance_ui import PAGE_SIZE, AttendanceUI, SheetStatus
from tests.helpers import OWNER_ID, App
from tests.tg_fake import Harness, callback_update

GUESTS = PAGE_SIZE * 2 + 5  # три страницы


async def _big_event(app: App, count: int = GUESTS) -> int:
    created = await app.services.events.create_event_as_admin(OWNER_ID, capacity=200)
    for n in range(count):
        await app.services.participants.admin_add(
            OWNER_ID, created.event_id, manual_name=f"Гость {n:03d}"
        )
    async with app.sessions() as session:
        event = await session.get(Event, created.event_id)
        assert event is not None
        starts_at = event.starts_at
    app.clock.advance(starts_at - app.clock.now() + timedelta(hours=1))
    await app.services.lifecycle.finish_due(app.clock.now())
    return created.event_id


async def _sheet(tg: Harness, event_id: int) -> int:
    assert await tg.attendance_ui.send_sheet(OWNER_ID, event_id) is SheetStatus.SENT
    return tg.session.last_message_id


def _labels(tg: Harness, mid: int) -> list[str]:
    return [text for text, _ in tg.session.buttons(mid)]


def _data(tg: Harness, mid: int, label: str) -> str:
    return next(data for text, data in tg.session.buttons(mid) if label in text)


async def _attendance(app: App, event_id: int) -> dict[str, str]:
    async with app.sessions() as session:
        rows = await session.execute(
            select(Registration.manual_name, Registration.attendance).where(
                Registration.event_id == event_id
            )
        )
        return {name: value for name, value in rows.all()}


async def test_большая_встреча_разбита_на_страницы_без_обрезки(app: App, tg: Harness) -> None:
    event_id = await _big_event(app)
    mid = await _sheet(tg, event_id)

    labels = _labels(tg, mid)
    assert labels[:PAGE_SIZE] == [f"✅ Гость {n:03d} (вручную)" for n in range(PAGE_SIZE)]
    assert "1/3" in labels and "▶" in labels and "Готово" in labels
    assert len(labels) <= 100
    assert "Показаны первые" not in tg.session.of("SendMessage")[-1]["text"]


async def test_неявки_на_разных_страницах_все_сохраняются(app: App, tg: Harness) -> None:
    event_id = await _big_event(app)
    mid = await _sheet(tg, event_id)

    await tg.click(OWNER_ID, mid, "Гость 001")
    await tg.click(OWNER_ID, mid, "▶")
    assert f"Гость {PAGE_SIZE:03d}" in _labels(tg, mid)[0]
    await tg.click(OWNER_ID, mid, f"Гость {PAGE_SIZE + 3:03d}")
    await tg.click(OWNER_ID, mid, "▶")
    await tg.click(OWNER_ID, mid, f"Гость {GUESTS - 1:03d}")
    await tg.click(OWNER_ID, mid, "Готово")

    marks = await _attendance(app, event_id)
    no_shows = sorted(name for name, value in marks.items() if value == "no_show")
    assert no_shows == ["Гость 001", f"Гость {PAGE_SIZE + 3:03d}", f"Гость {GUESTS - 1:03d}"]
    assert sum(value == "attended" for value in marks.values()) == GUESTS - 3


async def test_отметка_сохраняется_при_листании_туда_и_обратно(app: App, tg: Harness) -> None:
    event_id = await _big_event(app)
    mid = await _sheet(tg, event_id)
    await tg.click(OWNER_ID, mid, "Гость 002")
    await tg.click(OWNER_ID, mid, "▶")
    await tg.click(OWNER_ID, mid, "◀")
    assert "🚫 Гость 002 (вручную)" in _labels(tg, mid)


async def test_быстрые_нажатия_на_разные_имена_не_теряются(app: App, tg: Harness) -> None:
    event_id = await _big_event(app, count=5)
    mid = await _sheet(tg, event_id)
    markup = tg.session.last_markup[mid]
    first, second = _data(tg, mid, "Гость 000"), _data(tg, mid, "Гость 003")

    # Оба нажатия видят одну и ту же (старую) разметку сообщения
    await asyncio.gather(
        tg.feed(callback_update(OWNER_ID, first, message_id=mid, reply_markup=markup)),
        tg.feed(callback_update(OWNER_ID, second, message_id=mid, reply_markup=markup)),
    )
    await tg.click(OWNER_ID, mid, "Готово")

    marks = await _attendance(app, event_id)
    assert marks["Гость 000"] == "no_show"
    assert marks["Гость 003"] == "no_show"


async def test_после_перезапуска_бота_отметки_текущей_страницы_не_теряются(
    app: App, tg: Harness
) -> None:
    event_id = await _big_event(app, count=5)
    mid = await _sheet(tg, event_id)
    await tg.click(OWNER_ID, mid, "Гость 004")
    # Перезапуск: черновик в памяти пропал, осталась только разметка сообщения
    tg.dispatcher.workflow_data["attendance_ui"] = AttendanceUI(tg.bot, app.services)
    await tg.click(OWNER_ID, mid, "Готово")

    assert (await _attendance(app, event_id))["Гость 004"] == "no_show"


async def test_режим_исправления_тоже_постраничный(app: App, tg: Harness) -> None:
    event_id = await _big_event(app)
    mid = await _sheet(tg, event_id)
    await tg.click(OWNER_ID, mid, "Готово")

    mid = await _sheet(tg, event_id)
    await tg.click(OWNER_ID, mid, "▶")
    await tg.click(OWNER_ID, mid, f"Гость {PAGE_SIZE + 1:03d}")

    assert (await _attendance(app, event_id))[f"Гость {PAGE_SIZE + 1:03d}"] == "no_show"
    assert f"🚫 Гость {PAGE_SIZE + 1:03d} (вручную)" in _labels(tg, mid)
