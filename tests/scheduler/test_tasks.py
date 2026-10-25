"""Задачи планировщика (раздел 11): напоминания, запрос явки, автосоздание, закрытие."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from meetup_bot.container import make_text_renderer
from meetup_bot.db.models import AuditLog, Event, EventStatus, Registration
from meetup_bot.repositories import users
from meetup_bot.repositories.users import UserInfo
from meetup_bot.scheduler.tasks import (
    ATTENDANCE_REMINDER_AFTER,
    AttendanceExpireTask,
    AttendancePromptTask,
    AttendanceReminderTask,
    AutoCreateTask,
    CloseRegistrationTask,
    FinishEventsTask,
    ReminderTask,
)
from meetup_bot.services.attendance import ATTENDANCE_WINDOW
from meetup_bot.services.registration import RegisterStatus
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.timing import ScheduleSettings
from tests.helpers import ADMIN_ID, OWNER_ID, App, RecordingNotifier, person

# Четверг 2026-10-22 12:00 UTC; встреча по умолчанию: пятница 23.10 16:30 по Монтевидео,
# запись закрывается в 17:30, запрос явки — в 18:00
STARTS_AT = datetime(2026, 10, 23, 19, 30, tzinfo=UTC)
REMINDER_AT = datetime(2026, 10, 22, 19, 30, tzinfo=UTC)
CLOSES_AT = datetime(2026, 10, 23, 20, 30, tzinfo=UTC)
PROMPT_AT = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)


def go_to(app: App, moment: datetime) -> datetime:
    app.clock.advance(moment - app.clock.now())
    return app.clock.now()


async def create_event(app: App, *, place: str = "", capacity: int = 10) -> int:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, capacity=capacity, place=place
    )
    return created.event_id


async def join(app: App, event_id: int, n: int, *, dm: bool = True) -> None:
    result = await app.services.registration.register(event_id, person(n))
    assert result.status is RegisterStatus.OK
    if dm:
        async with app.sessions() as session, session.begin():
            await users.set_dm_available(session, person(n).id, True, app.clock.now())


async def get_event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        return event


def reminder_task(app: App) -> ReminderTask:
    settings = app.services.settings
    render = make_text_renderer(settings)
    return ReminderTask(app.sessions, app.notifier, settings.rules_now, render)


async def finish_and_wait_prompt(app: App, event_id: int) -> datetime:
    """Часы на моменте запроса явки, встреча завершена."""
    now = go_to(app, PROMPT_AT)
    await app.services.lifecycle.finish_due(now)
    assert (await get_event(app, event_id)).status == EventStatus.FINISHED.value
    return now


# ---- ReminderTask ---------------------------------------------------------------------


async def test_напоминание_до_срока_не_шлётся(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    go_to(app, REMINDER_AT - timedelta(seconds=1))
    assert await reminder_task(app).run(app.clock.now()) == 0
    assert app.notifier.dms == []
    assert (await get_event(app, event_id)).reminder_sent_at is None


async def test_напоминание_в_срок_шлётся_активным_с_dm(app: App) -> None:
    event_id = await create_event(app, place="Кафе")
    await join(app, event_id, 1)
    await join(app, event_id, 2, dm=False)
    await join(app, event_id, 3)
    now = go_to(app, REMINDER_AT)
    assert await reminder_task(app).run(now) == 1
    assert [uid for uid, _ in app.notifier.dms] == [person(1).id, person(3).id]
    assert (await get_event(app, event_id)).reminder_sent_at == now


async def test_текст_напоминания_содержит_время_место_и_дедлайн(app: App) -> None:
    event_id = await create_event(app, place="Кафе")
    await join(app, event_id, 1)
    await reminder_task(app).run(go_to(app, REMINDER_AT))
    assert app.notifier.dms_to(person(1).id) == [
        "Напоминаем: завтра встреча! пятница, 23 октября, 16:30, Кафе. "
        "Не сможете прийти? Отмените запись до 22 октября, 20:00."
    ]
    assert event_id


async def test_напоминание_без_места_без_лишней_запятой(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await reminder_task(app).run(go_to(app, REMINDER_AT))
    assert "16:30. Не сможете" in app.notifier.dms_to(person(1).id)[0]
    assert event_id


async def test_напоминание_экранирует_html_в_месте(app: App) -> None:
    event_id = await create_event(app, place="Бар <b>& Гриль</b>")
    await join(app, event_id, 1)
    await reminder_task(app).run(go_to(app, REMINDER_AT))
    assert "Бар &lt;b&gt;&amp; Гриль&lt;/b&gt;" in app.notifier.dms_to(person(1).id)[0]
    assert event_id


async def test_напоминание_использует_часовой_пояс_из_настроек(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await app.services.settings.set("timezone", "UTC", OWNER_ID)
    await reminder_task(app).run(go_to(app, REMINDER_AT))
    assert "пятница, 23 октября, 19:30" in app.notifier.dms_to(person(1).id)[0]
    assert event_id


async def test_повторный_тик_не_шлёт_напоминание_дважды(app: App) -> None:  # кейс 15
    event_id = await create_event(app)
    await join(app, event_id, 1)
    task = reminder_task(app)
    now = go_to(app, REMINDER_AT)
    assert await task.run(now) == 1
    assert await task.run(now) == 0
    assert await task.run(now + timedelta(minutes=1)) == 0
    assert len(app.notifier.dms) == 1
    assert event_id


async def test_напоминание_выключено_ничего_не_шлёт_и_не_ставит_отметку(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await app.services.settings.set("reminder_enabled", False, ADMIN_ID)
    assert await reminder_task(app).run(go_to(app, REMINDER_AT)) == 0
    assert app.notifier.dms == []
    assert (await get_event(app, event_id)).reminder_sent_at is None


async def test_напоминание_шлётся_после_включения_настройки(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await app.services.settings.set("reminder_enabled", False, ADMIN_ID)
    await reminder_task(app).run(go_to(app, REMINDER_AT))
    await app.services.settings.set("reminder_enabled", True, ADMIN_ID)
    assert await reminder_task(app).run(app.clock.now()) == 1
    assert len(app.notifier.dms) == 1
    assert event_id


async def test_напоминание_не_идёт_отменившим_и_листу_ожидания(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, ADMIN_ID)
    event_id = await create_event(app, capacity=1)
    await join(app, event_id, 1)
    await app.services.registration.register(event_id, person(2))  # очередь
    await app.services.registration.register(event_id, person(3))  # очередь, затем отмена
    await app.services.registration.cancel(event_id, person(3).id)
    await reminder_task(app).run(go_to(app, REMINDER_AT))
    assert [uid for uid, _ in app.notifier.dms] == [person(1).id]


async def test_напоминание_не_идёт_по_отменённой_встрече(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    app.notifier.dms.clear()
    assert await reminder_task(app).run(go_to(app, REMINDER_AT)) == 0
    assert app.notifier.dms == []


async def test_напоминание_идёт_по_встрече_с_закрытой_записью(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    assert await reminder_task(app).run(go_to(app, REMINDER_AT)) == 1
    assert len(app.notifier.dms) == 1


async def test_напоминание_нескольких_встреч_считает_встречи(app: App) -> None:
    first = await create_event(app)
    second = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=STARTS_AT + timedelta(hours=1)
        )
    ).event_id
    await join(app, first, 1)
    await join(app, second, 2)
    assert await reminder_task(app).run(go_to(app, REMINDER_AT + timedelta(hours=2))) == 2
    assert sorted(uid for uid, _ in app.notifier.dms) == [person(1).id, person(2).id]


class MarkCheckingNotifier(RecordingNotifier):
    """Запоминает, была ли отметка о рассылке уже в БД на момент отправки."""

    def __init__(self, app: App, event_id: int) -> None:
        super().__init__()
        self._app = app
        self._event_id = event_id
        self.marked_before_send: list[bool] = []

    async def send_dm(self, user_id: int, text: str) -> bool:
        event = await get_event(self._app, self._event_id)
        self.marked_before_send.append(event.reminder_sent_at is not None)
        return await super().send_dm(user_id, text)


async def test_отметка_напоминания_коммитится_до_отправки(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await join(app, event_id, 2)
    notifier = MarkCheckingNotifier(app, event_id)
    settings = app.services.settings
    task = ReminderTask(app.sessions, notifier, settings.rules_now, make_text_renderer(settings))
    await task.run(go_to(app, REMINDER_AT))
    assert notifier.marked_before_send == [True, True]


class FlakyNotifier(RecordingNotifier):
    async def send_dm(self, user_id: int, text: str) -> bool:
        if user_id == person(1).id:
            raise RuntimeError("сеть")
        return await super().send_dm(user_id, text)


async def test_сбой_отправки_одному_не_мешает_остальным_и_не_повторяется(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await join(app, event_id, 2)
    notifier = FlakyNotifier()
    settings = app.services.settings
    task = ReminderTask(app.sessions, notifier, settings.rules_now, make_text_renderer(settings))
    now = go_to(app, REMINDER_AT)
    await task.run(now)
    await task.run(now)
    assert [uid for uid, _ in notifier.dms] == [person(2).id]


async def test_напоминание_заблокировавшему_бота_не_падает(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await join(app, event_id, 2)
    app.notifier.blocked.add(person(1).id)
    await reminder_task(app).run(go_to(app, REMINDER_AT))
    assert [uid for uid, _ in app.notifier.dms] == [person(2).id]
    assert event_id


async def test_напоминание_после_начала_встречи_пропускается_с_отметкой(
    app: App,
) -> None:  # кейс 16
    event_id = await create_event(app)
    await join(app, event_id, 1)
    now = go_to(app, STARTS_AT + timedelta(hours=1))
    assert await reminder_task(app).run(now) == 0
    assert app.notifier.dms == []
    assert (await get_event(app, event_id)).reminder_sent_at == now
    # отметка не даёт задаче возвращаться к протухшей встрече
    assert await reminder_task(app).run(now + timedelta(minutes=1)) == 0


async def test_напоминание_ровно_в_момент_начала_ещё_шлётся(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    assert await reminder_task(app).run(go_to(app, STARTS_AT)) == 1
    assert event_id


async def test_напоминание_у_пустой_встречи_считается_но_никому_не_шлётся(app: App) -> None:
    event_id = await create_event(app)
    assert await reminder_task(app).run(go_to(app, REMINDER_AT)) == 1
    assert app.notifier.dms == []
    assert (await get_event(app, event_id)).reminder_sent_at is not None


async def test_напоминание_принимает_асинхронный_провайдер_правил(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)

    async def rules() -> RulesSettings:
        return RulesSettings(reminder_enabled=False)

    task = ReminderTask(app.sessions, app.notifier, rules)
    assert await task.run(go_to(app, REMINDER_AT)) == 0
    assert event_id


async def test_у_задач_ожидаемые_имена() -> None:
    assert ReminderTask.name == "reminder"
    assert AttendancePromptTask.name == "attendance_prompt"
    assert AttendanceReminderTask.name == "attendance_reminder"
    assert AttendanceExpireTask.name == "attendance_expire"
    assert AutoCreateTask.name == "auto_create"
    assert CloseRegistrationTask.name == "close_registration"
    assert FinishEventsTask.name == "finish_events"


# ---- AttendancePromptTask -------------------------------------------------------------


async def test_запрос_явки_уходит_всем_админам(app: App) -> None:
    event_id = await create_event(app)
    now = await finish_and_wait_prompt(app, event_id)
    task = AttendancePromptTask(app.sessions, app.notifier)
    assert await task.run(now) == 1
    assert sorted(app.notifier.prompts) == [(OWNER_ID, event_id), (ADMIN_ID, event_id)]
    assert (await get_event(app, event_id)).attendance_prompt_sent_at == now


async def test_запрос_явки_уходит_и_добавленному_позже_админу(app: App) -> None:
    await app.services.admins.add_admin(OWNER_ID, UserInfo(3, "Третий"))
    event_id = await create_event(app)
    now = await finish_and_wait_prompt(app, event_id)
    await AttendancePromptTask(app.sessions, app.notifier).run(now)
    assert {admin for admin, _ in app.notifier.prompts} == {OWNER_ID, ADMIN_ID, 3}


async def test_повторный_тик_не_шлёт_запрос_явки_дважды(app: App) -> None:  # кейс 15
    event_id = await create_event(app)
    now = await finish_and_wait_prompt(app, event_id)
    task = AttendancePromptTask(app.sessions, app.notifier)
    assert await task.run(now) == 1
    assert await task.run(now) == 0
    assert await task.run(now + timedelta(minutes=1)) == 0
    assert len(app.notifier.prompts) == 2


async def test_запрос_явки_до_срока_не_шлётся(app: App) -> None:
    event_id = await create_event(app)
    now = go_to(app, PROMPT_AT - timedelta(seconds=1))
    await app.services.lifecycle.finish_due(now)
    assert await AttendancePromptTask(app.sessions, app.notifier).run(now) == 0
    assert app.notifier.prompts == []
    assert event_id


async def test_запрос_явки_по_незавершённой_встрече_не_шлётся(app: App) -> None:
    await create_event(app)
    now = go_to(app, PROMPT_AT)
    assert await AttendancePromptTask(app.sessions, app.notifier).run(now) == 0


async def test_запрос_явки_по_отменённой_встрече_не_шлётся(app: App) -> None:
    event_id = await create_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    now = go_to(app, PROMPT_AT)
    await app.services.lifecycle.finish_due(now)
    assert await AttendancePromptTask(app.sessions, app.notifier).run(now) == 0
    assert app.notifier.prompts == []


async def test_запрос_явки_не_шлётся_если_явка_уже_отмечена(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    now = await finish_and_wait_prompt(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert await AttendancePromptTask(app.sessions, app.notifier).run(now) == 0
    assert app.notifier.prompts == []


async def test_запрос_явки_после_простоя_дольше_окна_пропускается_с_отметкой(  # кейс 16
    app: App,
) -> None:
    event_id = await create_event(app)
    now = go_to(app, STARTS_AT + ATTENDANCE_WINDOW + timedelta(hours=1))
    await app.services.lifecycle.finish_due(now)
    assert await AttendancePromptTask(app.sessions, app.notifier).run(now) == 0
    assert app.notifier.prompts == []
    assert (await get_event(app, event_id)).attendance_prompt_sent_at == now


async def test_запрос_явки_сбой_одному_админу_не_мешает_другому(app: App) -> None:
    class Flaky(RecordingNotifier):
        async def send_attendance_prompt(self, admin_id: int, event_id: int) -> bool:
            if admin_id == OWNER_ID:
                raise RuntimeError("сеть")
            return await super().send_attendance_prompt(admin_id, event_id)

    event_id = await create_event(app)
    now = await finish_and_wait_prompt(app, event_id)
    notifier = Flaky()
    assert await AttendancePromptTask(app.sessions, notifier).run(now) == 1
    assert notifier.prompts == [(ADMIN_ID, event_id)]


# ---- AttendanceReminderTask -----------------------------------------------------------


def attendance_reminder(app: App) -> AttendanceReminderTask:
    return AttendanceReminderTask(app.sessions, app.notifier, app.services.settings.schedule_now)


async def prompted(app: App, event_id: int) -> datetime:
    now = await finish_and_wait_prompt(app, event_id)
    await AttendancePromptTask(app.sessions, app.notifier).run(now)
    app.notifier.service.clear()
    return now


async def test_напоминание_про_явку_через_14_часов(app: App) -> None:
    event_id = await create_event(app)
    sent_at = await prompted(app, event_id)
    now = go_to(app, sent_at + ATTENDANCE_REMINDER_AFTER)
    assert await attendance_reminder(app).run(now) == 1
    assert app.notifier.service == [
        "Никто ещё не отметил явку на встрече (пятница, 23 октября, 16:30)."
    ]
    assert (await get_event(app, event_id)).attendance_reminder_sent_at == now


async def test_напоминание_про_явку_за_секунду_до_14_часов_не_шлётся(app: App) -> None:
    event_id = await create_event(app)
    sent_at = await prompted(app, event_id)
    now = go_to(app, sent_at + ATTENDANCE_REMINDER_AFTER - timedelta(seconds=1))
    assert await attendance_reminder(app).run(now) == 0
    assert app.notifier.service == []
    assert event_id


def test_интервал_напоминания_про_явку_14_часов() -> None:
    assert timedelta(hours=14) == ATTENDANCE_REMINDER_AFTER


async def test_напоминание_про_явку_не_дублируется(app: App) -> None:  # кейс 15
    event_id = await create_event(app)
    sent_at = await prompted(app, event_id)
    now = go_to(app, sent_at + ATTENDANCE_REMINDER_AFTER)
    task = attendance_reminder(app)
    assert await task.run(now) == 1
    assert await task.run(now) == 0
    assert await task.run(now + timedelta(hours=3)) == 0
    assert len(app.notifier.service) == 1


async def test_напоминание_про_явку_не_шлётся_если_явка_отмечена(app: App) -> None:
    event_id = await create_event(app)
    sent_at = await prompted(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert await attendance_reminder(app).run(go_to(app, sent_at + timedelta(hours=15))) == 0
    assert app.notifier.service == []


async def test_напоминание_про_явку_не_шлётся_без_запроса(app: App) -> None:
    event_id = await create_event(app)
    now = await finish_and_wait_prompt(app, event_id)
    assert await attendance_reminder(app).run(now + timedelta(hours=20)) == 0
    assert app.notifier.service == []


async def test_напоминание_про_явку_после_окна_пропускается_с_отметкой(app: App) -> None:  # кейс 16
    event_id = await create_event(app)
    sent_at = await prompted(app, event_id)
    now = go_to(app, sent_at + timedelta(days=8))
    assert await attendance_reminder(app).run(now) == 0
    assert app.notifier.service == []
    assert (await get_event(app, event_id)).attendance_reminder_sent_at == now


async def test_напоминание_про_явку_принимает_значение_настроек(app: App) -> None:
    event_id = await create_event(app)
    sent_at = await prompted(app, event_id)
    task = AttendanceReminderTask(app.sessions, app.notifier, ScheduleSettings(timezone="UTC"))
    await task.run(go_to(app, sent_at + ATTENDANCE_REMINDER_AFTER))
    assert app.notifier.service == [
        "Никто ещё не отметил явку на встрече (пятница, 23 октября, 19:30)."
    ]
    assert event_id


# ---- AttendanceExpireTask -------------------------------------------------------------


async def registration_attendance(app: App, event_id: int) -> list[str]:
    async with app.sessions() as session:
        rows = await session.scalars(
            select(Registration.attendance)
            .where(Registration.event_id == event_id)
            .order_by(Registration.id)
        )
        return list(rows.all())


async def test_окно_отметки_закрывается_через_7_суток(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, 1)
    await join(app, event_id, 2)
    now = go_to(app, STARTS_AT + ATTENDANCE_WINDOW + timedelta(seconds=1))
    assert await AttendanceExpireTask(app.services.attendance).run(now) == 1
    event = await get_event(app, event_id)
    assert event.attendance_marked_at == now
    assert event.attendance_marked_by is None
    assert await registration_attendance(app, event_id) == ["attended", "attended"]


async def test_окно_отметки_ровно_через_7_суток_ещё_открыто(app: App) -> None:
    event_id = await create_event(app)
    now = go_to(app, STARTS_AT + ATTENDANCE_WINDOW)
    assert await AttendanceExpireTask(app.services.attendance).run(now) == 0
    assert (await get_event(app, event_id)).attendance_marked_at is None


async def test_окно_отметки_закрывается_один_раз(app: App) -> None:  # кейс 15
    event_id = await create_event(app)
    now = go_to(app, STARTS_AT + timedelta(days=8))
    task = AttendanceExpireTask(app.services.attendance)
    assert await task.run(now) == 1
    assert await task.run(now) == 0
    async with app.sessions() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.action == "attendance.expire"))
        ).all()
    assert [r.event_id for r in rows] == [event_id]


async def test_окно_отметки_не_трогает_отмеченную_и_отменённую(app: App) -> None:
    marked = await create_event(app)
    await join(app, marked, 1)
    go_to(app, STARTS_AT + timedelta(hours=1))
    await app.services.attendance.mark(ADMIN_ID, marked, {await first_reg(app, marked)})
    cancelled = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=STARTS_AT + timedelta(days=1)
        )
    ).event_id
    await app.services.events.cancel_event(OWNER_ID, cancelled)
    now = go_to(app, STARTS_AT + timedelta(days=9))
    assert await AttendanceExpireTask(app.services.attendance).run(now) == 0
    assert await registration_attendance(app, marked) == ["no_show"]


async def first_reg(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration.id).where(Registration.event_id == event_id)
        )
    assert found is not None
    return found


async def test_окно_отметки_принимает_любой_сервис_с_нужным_методом() -> None:
    class Fake:
        async def expire_unmarked(self, now: datetime) -> list[int]:
            return [4, 5]

    assert await AttendanceExpireTask(Fake()).run(datetime(2026, 1, 1, tzinfo=UTC)) == 2


# ---- AutoCreateTask / CloseRegistrationTask / FinishEventsTask -------------------------


def auto_create(app: App) -> AutoCreateTask:
    return AutoCreateTask(app.services.lifecycle, app.services.settings.rules_now)


async def test_автосоздание_возвращает_1_при_создании_и_0_при_повторе(app: App) -> None:  # кейс 15
    task = auto_create(app)
    assert await task.run(app.clock.now()) == 1
    assert await task.run(app.clock.now()) == 0
    assert await task.run(app.clock.now() + timedelta(minutes=1)) == 0
    async with app.sessions() as session:
        keys = list((await session.scalars(select(Event.recurrence_key))).all())
    assert keys == ["weekly:2026-10-23"]


async def test_автосоздание_выключено_возвращает_0(app: App) -> None:
    await app.services.settings.set("auto_create_enabled", False, ADMIN_ID)
    assert await auto_create(app).run(app.clock.now()) == 0


async def test_автосоздание_принимает_асинхронный_провайдер_правил(app: App) -> None:
    async def rules() -> RulesSettings:
        return RulesSettings(auto_create_enabled=False)

    task = AutoCreateTask(app.services.lifecycle, rules)
    assert await task.run(app.clock.now()) == 0


async def test_задача_закрытия_записи_возвращает_число_закрытых(app: App) -> None:
    await create_event(app)
    task = CloseRegistrationTask(app.services.lifecycle)
    assert await task.run(app.clock.now()) == 0
    assert await task.run(go_to(app, STARTS_AT)) == 0
    assert await task.run(go_to(app, CLOSES_AT)) == 1
    assert await task.run(app.clock.now()) == 0


async def test_задача_завершения_возвращает_число_завершённых(app: App) -> None:
    await create_event(app)
    task = FinishEventsTask(app.services.lifecycle)
    assert await task.run(app.clock.now()) == 0
    # Встреча завершается после закрытия записи, а не в момент начала
    assert await task.run(go_to(app, STARTS_AT)) == 0
    assert await task.run(go_to(app, CLOSES_AT)) == 1
    assert await task.run(app.clock.now()) == 0


# ---- момент запроса явки (решение заказчика 2026-10-25) ------------------------------


async def run_lifecycle_and_prompt(app: App, moment: datetime) -> int:
    now = go_to(app, moment)
    await CloseRegistrationTask(app.services.lifecycle).run(now)
    await FinishEventsTask(app.services.lifecycle).run(now)
    return await AttendancePromptTask(app.sessions, app.notifier).run(now)


async def test_запрос_явки_встречи_16_30_уходит_в_18_00(app: App) -> None:
    event_id = await create_event(app)
    assert (await get_event(app, event_id)).attendance_prompt_at == PROMPT_AT
    assert await run_lifecycle_and_prompt(app, PROMPT_AT - timedelta(seconds=1)) == 0
    assert await run_lifecycle_and_prompt(app, PROMPT_AT) == 1
    assert app.notifier.prompts == [(OWNER_ID, event_id), (ADMIN_ID, event_id)]


async def test_при_закрытии_через_120_минут_запрос_явки_сдвигается_на_18_30(app: App) -> None:
    await app.services.settings.set("registration_close_minutes_after_start", 120, ADMIN_ID)
    event_id = await create_event(app)
    shifted = PROMPT_AT + timedelta(minutes=30)
    event = await get_event(app, event_id)
    assert (event.registration_closes_at, event.attendance_prompt_at) == (shifted, shifted)
    # В 18:00 запись ещё открыта: встреча не завершена, явку не запрашиваем
    assert await run_lifecycle_and_prompt(app, PROMPT_AT) == 0
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value
    assert await run_lifecycle_and_prompt(app, shifted - timedelta(seconds=1)) == 0
    assert await run_lifecycle_and_prompt(app, shifted) == 1
    assert {event for _, event in app.notifier.prompts} == {event_id}
