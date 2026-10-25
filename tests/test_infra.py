"""Проверка самой тестовой инфраструктуры: фикстуры `app` и `tg` собираются и работают."""

from datetime import UTC, datetime

from sqlalchemy import select

from meetup_bot.db.models import Admin
from meetup_bot.services.registration import RegisterStatus
from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.tg_fake import Harness, command_update


async def test_app_создаёт_владельца_и_админа(app: App) -> None:
    async with app.sessions() as session:
        roles = dict((await session.execute(select(Admin.user_id, Admin.role))).all())
    assert roles == {OWNER_ID: "owner", ADMIN_ID: "admin"}


async def test_app_запись_и_уведомления(app: App) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=1)).event_id
    first = await app.services.registration.register(event_id, person(1))
    assert first.status is RegisterStatus.OK
    assert set(app.refresher.scheduled) <= {event_id}
    assert app.clock.now() == datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


async def test_tg_фейковая_сессия_отвечает_на_команду(tg: Harness) -> None:
    await tg.feed(command_update(10, "/start"))
    assert tg.session.of("SendMessage")
