"""Напоминание «пора разослать пост» (M6.2, 1.5): в служебный чат, один раз на встречу."""

from datetime import UTC, datetime, timedelta

from meetup_bot.db.models import Event, EventStatus
from meetup_bot.repositories import event_posts
from meetup_bot.scheduler.tasks import PostReminderTask
from meetup_bot.services.post_reminders import PostReminderService
from meetup_bot.services.rules import RulesSettings
from tests.helpers import ADMIN_ID, OWNER_ID, App, RecordingNotifier

# Пятница 30.10 16:30 по Монтевидео (UTC−3); напоминание — среда 28.10 10:00 = 13:00 UTC
STARTS_AT = datetime(2026, 10, 30, 19, 30, tzinfo=UTC)
REMIND_AT = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
EXPECTED_TEXT = (
    "Пост встречи пятница, 30 октября, 16:30 ещё не разослан. "
    "Чтобы разослать: в нужном чате наберите @ и имя бота и выберите встречу."
)


def go_to(app: App, moment: datetime) -> datetime:
    app.clock.advance(moment - app.clock.now())
    return app.clock.now()


def service(app: App, notifier: RecordingNotifier | None = None) -> PostReminderService:
    settings = app.services.settings
    return PostReminderService(
        app.sessions, notifier or app.notifier, settings.rules_now, settings.schedule_now
    )


async def create_event(app: App, starts_at: datetime = STARTS_AT) -> int:
    """Встреча прямо в БД: задаче важны только статус, начало и отметка, а не мастер создания."""
    now = app.clock.now()
    event = Event(
        status=EventStatus.OPEN.value,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        capacity=10,
        cancel_deadline_at=starts_at - timedelta(days=1),
        registration_closes_at=starts_at + timedelta(hours=1),
        reminder_at=starts_at - timedelta(days=1),
        attendance_prompt_at=starts_at + timedelta(minutes=90),
        created_at=now,
        updated_at=now,
    )
    async with app.sessions() as session, session.begin():
        session.add(event)
        await session.flush()
        return event.id


async def add_post(app: App, event_id: int, inline_id: str = "inline-1") -> int:
    async with app.sessions() as session, session.begin():
        post, _ = await event_posts.register(
            session,
            event_id=event_id,
            inline_message_id=inline_id,
            posted_by=OWNER_ID,
            now=app.clock.now(),
        )
        return post.id


async def get_event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        return event


async def set_status(app: App, event_id: int, status: EventStatus) -> None:
    async with app.sessions() as session, session.begin():
        event = await session.get(Event, event_id)
        assert event is not None
        event.status = status.value


async def test_нет_поста_одно_сообщение_и_отметка(app: App) -> None:
    event_id = await create_event(app)
    now = go_to(app, REMIND_AT)
    assert await service(app).send_due(now) == [event_id]
    assert app.notifier.service[-1] == EXPECTED_TEXT
    assert (await get_event(app, event_id)).post_reminder_sent_at == now


async def test_есть_пост_молчит(app: App) -> None:
    event_id = await create_event(app)
    await add_post(app, event_id)
    sent_before = list(app.notifier.service)
    assert await service(app).send_due(go_to(app, REMIND_AT)) == []
    assert await service(app).send_due(go_to(app, STARTS_AT - timedelta(minutes=1))) == []
    assert app.notifier.service == sent_before
    assert (await get_event(app, event_id)).post_reminder_sent_at is None


async def test_повторный_тик_не_повторяет(app: App) -> None:
    event_id = await create_event(app)
    sent_before = len(app.notifier.service)
    now = go_to(app, REMIND_AT)
    assert await service(app).send_due(now) == [event_id]
    assert await service(app).send_due(now) == []
    assert await service(app).send_due(now + timedelta(hours=1)) == []
    assert len(app.notifier.service) == sent_before + 1


async def test_ноль_дней_выключено(app: App) -> None:
    event_id = await create_event(app)
    await app.services.settings.set("post_reminder_days_before", 0, ADMIN_ID)
    sent_before = list(app.notifier.service)
    assert await service(app).send_due(go_to(app, STARTS_AT - timedelta(minutes=1))) == []
    assert app.notifier.service == sent_before
    assert (await get_event(app, event_id)).post_reminder_sent_at is None


async def test_отменённая_встреча_не_напоминается(app: App) -> None:
    event_id = await create_event(app)
    await set_status(app, event_id, EventStatus.CANCELLED)
    sent_before = list(app.notifier.service)
    assert await service(app).send_due(go_to(app, REMIND_AT)) == []
    assert app.notifier.service == sent_before


async def test_закрытая_и_завершённая_встречи_не_напоминаются(app: App) -> None:
    closed = await create_event(app)
    finished = await create_event(app, STARTS_AT + timedelta(hours=3))
    await set_status(app, closed, EventStatus.CLOSED)
    await set_status(app, finished, EventStatus.FINISHED)
    assert await service(app).send_due(go_to(app, REMIND_AT)) == []


async def test_момент_ещё_не_наступил(app: App) -> None:
    event_id = await create_event(app)
    assert await service(app).send_due(go_to(app, REMIND_AT - timedelta(seconds=1))) == []
    assert (await get_event(app, event_id)).post_reminder_sent_at is None


async def test_момент_по_локальному_часовому_поясу(app: App) -> None:
    """10:00 по Монтевидео = 13:00 UTC: в 10:00 UTC напоминать рано."""
    event_id = await create_event(app)
    assert await service(app).send_due(go_to(app, REMIND_AT - timedelta(hours=3))) == []
    assert await service(app).send_due(go_to(app, REMIND_AT)) == [event_id]


async def test_часовой_пояс_и_время_берутся_из_настроек(app: App) -> None:
    event_id = await create_event(app)
    await app.services.settings.set("timezone", "UTC", OWNER_ID)
    await app.services.settings.set("post_reminder_time", "09:00", OWNER_ID)
    # Дата начала по UTC — тоже 30.10, значит момент — 28.10 09:00 UTC
    moment = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    assert await service(app).send_due(go_to(app, moment - timedelta(seconds=1))) == []
    assert await service(app).send_due(go_to(app, moment)) == [event_id]
    assert "пятница, 30 октября, 19:30" in app.notifier.service[-1]


async def test_бот_лежал_одно_сообщение(app: App) -> None:
    event_id = await create_event(app)
    sent_before = len(app.notifier.service)
    now = go_to(app, STARTS_AT - timedelta(hours=1))
    assert await service(app).send_due(now) == [event_id]
    assert await service(app).send_due(now + timedelta(minutes=1)) == []
    assert len(app.notifier.service) == sent_before + 1


async def test_начавшаяся_встреча_не_напоминается(app: App) -> None:
    event_id = await create_event(app)
    assert await service(app).send_due(go_to(app, STARTS_AT)) == []
    assert (await get_event(app, event_id)).post_reminder_sent_at is None


async def test_мёртвый_пост_считается_отсутствующим(app: App) -> None:
    event_id = await create_event(app)
    post_id = await add_post(app, event_id)
    async with app.sessions() as session, session.begin():
        await event_posts.mark_dead(session, post_id)
    assert await service(app).send_due(go_to(app, REMIND_AT)) == [event_id]


async def test_пост_одной_встречи_не_глушит_другую(app: App) -> None:
    posted = await create_event(app)
    unposted = await create_event(app, STARTS_AT + timedelta(hours=3))
    await add_post(app, posted)
    assert await service(app).send_due(go_to(app, REMIND_AT)) == [unposted]


class MarkCheckingNotifier(RecordingNotifier):
    """Запоминает, была ли отметка уже в БД на момент отправки."""

    def __init__(self, app: App, event_id: int) -> None:
        super().__init__()
        self._app = app
        self._event_id = event_id
        self.marked_before_send: list[bool] = []

    async def send_service(self, text: str) -> None:
        event = await get_event(self._app, self._event_id)
        self.marked_before_send.append(event.post_reminder_sent_at is not None)
        await super().send_service(text)


async def test_отметка_коммитится_до_отправки(app: App) -> None:
    event_id = await create_event(app)
    notifier = MarkCheckingNotifier(app, event_id)
    await service(app, notifier).send_due(go_to(app, REMIND_AT))
    assert notifier.marked_before_send == [True]


class FailingNotifier(RecordingNotifier):
    async def send_service(self, text: str) -> None:
        raise RuntimeError("сеть")


async def test_сбой_отправки_не_ломает_и_не_повторяется(app: App) -> None:
    event_id = await create_event(app)
    notifier = FailingNotifier()
    now = go_to(app, REMIND_AT)
    assert await service(app, notifier).send_due(now) == [event_id]
    assert await service(app, notifier).send_due(now + timedelta(minutes=1)) == []
    assert (await get_event(app, event_id)).post_reminder_sent_at == now


async def test_принимает_асинхронный_провайдер_правил(app: App) -> None:
    await create_event(app)

    async def rules() -> RulesSettings:
        return RulesSettings(post_reminder_days_before=0)

    svc = PostReminderService(app.sessions, app.notifier, rules, app.services.settings.schedule_now)
    assert await svc.send_due(go_to(app, REMIND_AT)) == []


async def test_задача_планировщика_считает_напоминания(app: App) -> None:
    await create_event(app)
    task = PostReminderTask(service(app))
    assert task.name == "post_reminder"
    now = go_to(app, REMIND_AT)
    assert await task.run(now) == 1
    assert await task.run(now) == 0


class PostingDuringSendNotifier(RecordingNotifier):
    """Пока уходит первое напоминание, админ успевает разослать пост второй встречи."""

    def __init__(self, app: App, event_id: int) -> None:
        super().__init__()
        self._app = app
        self._event_id = event_id

    async def send_service(self, text: str) -> None:
        await super().send_service(text)
        await add_post(self._app, self._event_id)


async def test_пост_разосланный_после_выборки_отменяет_напоминание(app: App) -> None:
    first = await create_event(app)
    second = await create_event(app, STARTS_AT + timedelta(hours=3))
    notifier = PostingDuringSendNotifier(app, second)
    assert await service(app, notifier).send_due(go_to(app, REMIND_AT)) == [first]
    assert len(notifier.service) == 1
    assert (await get_event(app, second)).post_reminder_sent_at is None
