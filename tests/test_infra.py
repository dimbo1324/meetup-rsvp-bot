"""Проверка самой тестовой инфраструктуры: фикстуры `app` и `tg` собираются и работают."""

from datetime import UTC, datetime
from pathlib import Path

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


ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_entrypoint_задаёт_umask_до_миграций() -> None:
    script = _read("docker/entrypoint.sh")
    assert "umask 077" in script
    assert script.index("umask 077") < script.index("alembic upgrade head")


def test_compose_ограничивает_привилегии_основного_сервиса() -> None:
    compose = _read("compose.yaml")
    main = compose.split("# Шаблон второй локации")[0]
    for line in ("no-new-privileges:true", "cap_drop:", "- ALL", "read_only: true", "- /tmp"):
        assert line in main


def test_compose_ограничивает_привилегии_шаблона_второй_локации() -> None:
    template = _read("compose.yaml").split("# Шаблон второй локации")[1]
    for line in ("no-new-privileges:true", "cap_drop:", "- ALL", "read_only: true", "- /tmp"):
        assert line in template


def test_dockerfile_не_пишет_байт_код_в_только_читаемую_фс() -> None:
    assert "PYTHONDONTWRITEBYTECODE=1" in _read("Dockerfile")


def test_env_example_по_умолчанию_боевой_режим() -> None:
    lines = [line for line in _read(".env.example").splitlines() if line.startswith("ENV=")]
    assert lines == ["ENV=prod"]


def test_неиспользуемая_зависимость_structlog_удалена() -> None:
    assert "structlog" not in _read("pyproject.toml")
    sources = (ROOT / "src").rglob("*.py")
    assert not [p for p in sources if "structlog" in p.read_text(encoding="utf-8")]
