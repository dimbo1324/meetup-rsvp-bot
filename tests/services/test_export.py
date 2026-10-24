"""ExportService: CSV-выгрузка (Приложение В), права, аудит и защита от формульных инъекций."""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import AuditLog, Registration
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import NotFound, PermissionDenied
from meetup_bot.services.export import HEADER, ExportService
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

TZ = "America/Montevideo"
BOM = b"\xef\xbb\xbf"


@pytest.fixture
async def event_id(app: App) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=10)).event_id


def parse(data: bytes) -> list[list[str]]:
    assert data.startswith(BOM)
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


async def export(app: App, event_id: int, actor: int = ADMIN_ID, tz: str = TZ) -> list[list[str]]:
    return parse(await app.services.export.build_csv(actor, event_id, tz))


async def reg_id(app: App, event_id: int, user_id: int) -> int:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration.id).where(
                Registration.event_id == event_id, Registration.user_id == user_id
            )
        )
    assert found is not None
    return found


# ---- формат ---------------------------------------------------------------------------


async def test_csv_начинается_с_BOM_и_в_UTF8(app: App, event_id: int) -> None:
    await app.services.registration.register(event_id, UserInfo(1001, "Света"))
    data = await app.services.export.build_csv(ADMIN_ID, event_id, TZ)
    assert data[:3] == BOM
    assert "Света" in data.decode("utf-8-sig")


async def test_csv_пустой_список_это_только_заголовок(app: App, event_id: int) -> None:
    assert await export(app, event_id) == [HEADER]


def test_заголовок_соответствует_приложению_в() -> None:
    assert HEADER == [
        "event_date", "position", "name", "username", "user_id",
        "profile_link", "status", "guests", "registered_at", "attendance",
    ]  # fmt: skip


async def test_csv_строка_участника_по_образцу_приложения_в(app: App, event_id: int) -> None:
    await app.services.registration.register(event_id, UserInfo(111111, "Света", None, "sveta_mvd"))
    rows = await export(app, event_id)
    assert rows[1] == [
        "2026-10-24", "1", "Света", "sveta_mvd", "111111", "tg://user?id=111111",
        "active", "0", "2026-10-23 09:00", "unknown",
    ]  # fmt: skip


async def test_csv_время_в_часовом_поясе_из_аргумента(app: App, event_id: int) -> None:
    await app.services.registration.register(event_id, person(1))
    local = (await export(app, event_id, tz=TZ))[1]
    utc = (await export(app, event_id, tz="UTC"))[1]
    assert (local[0], local[8]) == ("2026-10-24", "2026-10-23 09:00")
    assert (utc[0], utc[8]) == ("2026-10-24", "2026-10-23 12:00")


async def test_csv_дата_встречи_локальная_а_не_UTC(app: App) -> None:
    # Начало в 23:00 по Монтевидео = 02:00 UTC следующего дня
    late = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
        )
    ).event_id
    await app.services.registration.register(late, person(1))
    assert (await export(app, late))[1][0] == "2026-10-24"


async def test_csv_вручную_добавленный_помечен_и_без_пользовательских_полей(
    app: App, event_id: int
) -> None:
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="Иван Петров")
    row = (await export(app, event_id))[1]
    assert row[2] == "Иван Петров (вручную)"
    assert (row[3], row[4], row[5]) == ("", "", "")
    assert (row[6], row[7]) == ("active", "0")


async def test_csv_гость_отражается_в_колонке_guests(app: App, event_id: int) -> None:
    await app.services.settings.set("plus_one_enabled", True, ADMIN_ID)
    await app.services.registration.register(event_id, person(1), guests=1)
    assert (await export(app, event_id))[1][7] == "1"


async def test_csv_имя_и_фамилия_склеиваются(app: App, event_id: int) -> None:
    await app.services.registration.register(event_id, UserInfo(1001, "Иван", "Петров", "ip"))
    assert (await export(app, event_id))[1][2] == "Иван Петров"


async def test_csv_запятая_и_кавычки_в_имени_экранируются_форматом(app: App, event_id: int) -> None:
    await app.services.registration.register(event_id, UserInfo(1001, 'Петров, "Пётр"'))
    assert (await export(app, event_id))[1][2] == 'Петров, "Пётр"'


# ---- порядок и позиции ----------------------------------------------------------------


async def test_csv_порядок_активные_очередь_отменённые_удалённые(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, ADMIN_ID)
    small = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=2)).event_id
    registration = app.services.registration
    # Порядок записи нарочно перемешан относительно итогового порядка в выгрузке
    await registration.register(small, person(1))  # будет отменён
    app.clock.advance(timedelta(minutes=1))
    await registration.register(small, person(2))  # активный №1
    app.clock.advance(timedelta(minutes=1))
    await registration.register(small, person(3))  # очередь №1, затем поднимется
    app.clock.advance(timedelta(minutes=1))
    await registration.register(small, person(4))  # очередь №2, затем №1
    app.clock.advance(timedelta(minutes=1))
    await app.services.participants.admin_add(OWNER_ID, small, manual_name="Удалённый", force=True)
    removed = await reg_id_manual(app, small)
    await app.services.participants.admin_remove(OWNER_ID, small, removed)
    await registration.cancel(small, person(1).id)

    rows = (await export(app, small))[1:]
    # Отмена person(1) освободила место: person(3) поднялся из очереди, person(4) остался в ней
    assert [(r[1], r[2], r[6]) for r in rows] == [
        ("1", "Участник2", "active"),
        ("2", "Участник3", "active"),
        ("1", "Участник4", "waitlist"),
        ("", "Участник1", "cancelled"),
        ("", "Удалённый (вручную)", "removed"),
    ]


async def reg_id_manual(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration.id).where(
                Registration.event_id == event_id, Registration.user_id.is_(None)
            )
        )
    assert found is not None
    return found


async def test_csv_очередь_нумеруется_отдельно_от_активных(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, ADMIN_ID)
    event = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=1)).event_id
    for n in range(1, 4):
        await app.services.registration.register(event, person(n))
        app.clock.advance(timedelta(minutes=1))
    rows = (await export(app, event))[1:]
    assert [(r[1], r[6]) for r in rows] == [("1", "active"), ("1", "waitlist"), ("2", "waitlist")]


async def test_csv_отметка_явки_попадает_в_колонку_attendance(app: App, event_id: int) -> None:
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    no_show = await reg_id(app, event_id, person(2).id)
    app.clock.advance(timedelta(days=1, hours=9))  # после начала встречи
    await app.services.attendance.mark(ADMIN_ID, event_id, {no_show})
    rows = (await export(app, event_id))[1:]
    assert [(r[2], r[9]) for r in rows] == [("Участник1", "attended"), ("Участник2", "no_show")]


async def test_csv_выгружает_только_запрошенную_встречу(app: App, event_id: int) -> None:
    other = (await app.services.events.create_event_as_admin(OWNER_ID)).event_id
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(other, person(2))
    assert [r[2] for r in (await export(app, event_id))[1:]] == ["Участник1"]


# ---- защита от формульной инъекции ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "=SUM(1+1)",
        "+7 999",
        "-2+3",
        "@SUM(A1)",
        "\t=1",
        "\r=1",
        "\n=1",
        "  =1+1",
        " \t+1",
        "   @evil",
    ],
)
async def test_csv_имя_с_формульным_началом_экранируется_апострофом(
    app: App, event_id: int, name: str
) -> None:
    await app.services.registration.register(event_id, UserInfo(1001, name))
    cell = (await export(app, event_id))[1][2]
    assert cell == "'" + name


@pytest.mark.parametrize("username", ["=cmd", "+x", "-x", "@x", "\tx", "  =x"])
async def test_csv_username_с_формульным_началом_экранируется(
    app: App, event_id: int, username: str
) -> None:
    await app.services.registration.register(event_id, UserInfo(1001, "Аня", None, username))
    assert (await export(app, event_id))[1][3] == "'" + username


async def test_csv_ручное_имя_с_формулой_экранируется_вместе_с_пометкой(
    app: App, event_id: int
) -> None:
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="=HYPERLINK(1)")
    # Пометка добавляется после имени, поэтому ячейка всё равно начинается с «=»
    assert (await export(app, event_id))[1][2] == "'=HYPERLINK(1) (вручную)"


@pytest.mark.parametrize("name", ["Иван", "Anna-Maria", "a=b", "x+y", "O'Neil", "1+1", "Аня @home"])
async def test_csv_безопасное_имя_не_меняется(app: App, event_id: int, name: str) -> None:
    await app.services.registration.register(event_id, UserInfo(1001, name))
    assert (await export(app, event_id))[1][2] == name


async def test_csv_служебные_колонки_не_экранируются(app: App, event_id: int) -> None:
    await app.services.registration.register(event_id, person(1))
    row = (await export(app, event_id))[1]
    assert row[4].isdigit()
    assert row[5].startswith("tg://")
    assert not row[8].startswith("'")


# ---- права и аудит --------------------------------------------------------------------


async def test_csv_не_админ_получает_PermissionDenied(app: App, event_id: int) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.export.build_csv(555, event_id, TZ)


async def test_csv_отказ_в_правах_не_пишет_аудит(app: App, event_id: int) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.export.build_csv(555, event_id, TZ)
    async with app.sessions() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.action == "event.export"))
        ).all()
    assert rows == []


async def test_csv_владелец_тоже_может_выгружать(app: App, event_id: int) -> None:
    assert (await export(app, event_id, actor=OWNER_ID)) == [HEADER]


async def test_csv_неизвестная_встреча_это_NotFound(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.export.build_csv(ADMIN_ID, 424242, TZ)


async def test_csv_пишет_аудит_выгрузки(app: App, event_id: int) -> None:
    await export(app, event_id)
    await export(app, event_id, actor=OWNER_ID)
    async with app.sessions() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.action == "event.export").order_by(AuditLog.id)
            )
        ).all()
    assert [(r.actor_id, r.event_id, r.created_at) for r in rows] == [
        (ADMIN_ID, event_id, app.clock.now()),
        (OWNER_ID, event_id, app.clock.now()),
    ]


async def test_csv_сервис_создаётся_отдельно_от_контейнера(app: App, event_id: int) -> None:
    service = ExportService(app.sessions, app.clock)
    assert parse(await service.build_csv(ADMIN_ID, event_id, TZ)) == [HEADER]
