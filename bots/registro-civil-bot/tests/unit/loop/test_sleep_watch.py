"""Сон ноутбука: интервал сна, отличие от зависания, отложенное сообщение владельцу."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from tests.fakes import T0, FakeClock, RecordingJournal

from rcbot.application.liveness import HUNG_CHECK_SEC, HUNG_LATE_SEC, LivenessMonitor
from rcbot.application.messages import MessageComposer, duration_text
from rcbot.application.sleep_watch import (
    SLEEP_MIN_SEC,
    SLEEP_NOTICE_MAX_TRIES,
    SLEEP_NOTICE_MIN_SEC,
    SleepNotice,
    SleepSpan,
    SleepWatch,
)
from rcbot.infrastructure.win.sleep import awake_seconds

TZ = ZoneInfo("America/Montevideo")
HOUR = 3600.0


class Awake:
    """Время бодрствования, которым управляет тест: идёт вместе с часами, а во сне стоит."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def pass_time(self, seconds: float) -> None:
        """Обычное время: часы и бодрствование идут одинаково."""
        self._clock.advance(seconds)
        self.value += seconds

    def sleep_for(self, seconds: float) -> None:
        """Сон: часы идут, бодрствование стоит."""
        self._clock.advance(seconds)


# --- SleepWatch ---


def make_watch() -> tuple[SleepWatch, FakeClock, Awake]:
    clock = FakeClock()
    awake = Awake(clock)
    return SleepWatch(clock, awake), clock, awake


def test_без_сна_ничего_не_замечается() -> None:
    watch, _, awake = make_watch()
    awake.pass_time(30)
    assert watch.poll() is None
    awake.pass_time(30)
    assert watch.poll() is None


def test_сон_даёт_интервал_с_длительностью_и_границами() -> None:
    watch, clock, awake = make_watch()
    awake.pass_time(30)
    assert watch.poll() is None
    awake.sleep_for(4 * HOUR)
    span = watch.poll()
    assert span is not None
    assert span.slept_sec == pytest.approx(4 * HOUR)
    assert span.ended == clock.now()
    assert span.started == clock.now() - timedelta(hours=4)


def test_сон_с_бодрствованием_внутри_промежутка_вычитает_бодрствование() -> None:
    watch, _clock, awake = make_watch()
    awake.sleep_for(600)
    awake.pass_time(30)  # проснулись и 30 секунд работали до тика
    span = watch.poll()
    assert span is not None and span.slept_sec == pytest.approx(600)


def test_короткий_сон_до_порога_не_считается() -> None:
    watch, _, awake = make_watch()
    awake.sleep_for(SLEEP_MIN_SEC - 1)
    assert watch.poll() is None
    awake.sleep_for(SLEEP_MIN_SEC)
    assert watch.poll() is not None


def test_после_сна_отсчёт_идёт_заново_и_сон_не_повторяется() -> None:
    watch, _, awake = make_watch()
    awake.sleep_for(HOUR)
    assert watch.poll() is not None
    awake.pass_time(30)
    assert watch.poll() is None


def test_перевод_часов_назад_сном_не_считается() -> None:
    watch, clock, _awake = make_watch()
    clock.advance(-HOUR)
    assert watch.poll() is None


def test_пороги_согласованы() -> None:
    assert 0 < SLEEP_MIN_SEC < SLEEP_NOTICE_MIN_SEC


def test_объединение_снов_берёт_крайние_моменты_и_суммирует_длительность() -> None:
    first = SleepSpan(T0, T0 + timedelta(hours=1), HOUR)
    second = SleepSpan(T0 + timedelta(hours=2), T0 + timedelta(hours=5), 3 * HOUR)
    notice = SleepNotice(first.started, first.ended, first.slept_sec)
    notice.merge(second)
    assert (notice.started, notice.ended) == (T0, T0 + timedelta(hours=5))
    assert notice.slept_sec == 4 * HOUR and notice.spans == 2


# --- реальное время бодрствования ---


def test_реальное_время_бодрствования_идёт_вперёд_как_обычные_часы() -> None:
    first_awake, first_mono = awake_seconds(), time.monotonic()
    time.sleep(0.05)
    second_awake, second_mono = awake_seconds(), time.monotonic()
    assert second_awake > first_awake
    # Без сна оба счётчика идут вместе: если бы они расходились, ложный «сон» шёл бы постоянно
    assert abs((second_awake - first_awake) - (second_mono - first_mono)) < 0.5


# --- тексты ---


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (20, "меньше минуты"),
        (60, "1 мин"),
        (47 * 60 + 5, "47 мин"),
        (13 * HOUR + 34 * 60, "13 ч 34 мин"),
    ],
)
def test_длительность_читается_по_человечески(seconds: float, expected: str) -> None:
    assert duration_text(seconds) == expected


def test_сообщение_о_сне_в_один_день_без_дат_и_в_поясе_настроек() -> None:
    started = datetime(2026, 11, 16, 13, 13, tzinfo=UTC)  # 10:13 в Монтевидео
    text = MessageComposer.slept(
        started, started + timedelta(hours=3, minutes=40), 3 * HOUR + 2400, TZ
    )
    assert "с 10:13 до 13:53" in text and "3 ч 40 мин" in text
    assert "16.11" not in text
    assert "могли пройти мимо" in text


def test_сообщение_о_сне_через_полночь_с_датами() -> None:
    started = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)  # 15.11 23:30 в Монтевидео
    text = MessageComposer.slept(started, started + timedelta(hours=3), 3 * HOUR, TZ)
    assert "с 15.11 23:30 до 16.11 02:30" in text  # даты — в поясе настроек, а не в UTC


# --- LivenessMonitor и сон ---


class StubLoop:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.tick_started_at: float | None = None
        # 0 — «расписание не задано»: опоздание цикла монитор не проверяет (тесты ставят срок сами)
        self.next_due = 0.0
        self.status = SimpleNamespace(last_check_at=None)

    def snapshot(self) -> dict[str, object]:
        return {"state": "running", "checks_total": 7}

    def checked(self) -> None:
        """Цикл опроса успешно проверил страницу «сейчас»."""
        self.status.last_check_at = self._clock.now()


class Rig:
    def __init__(self, *, delivered: list[bool] | None = None) -> None:
        self.clock = FakeClock()
        self.awake = Awake(self.clock)
        self.loop = StubLoop(self.clock)
        self.journal = RecordingJournal()
        self.sent: list[str] = []
        self.restarts = 0
        self._delivered = delivered or []
        self.monitor = LivenessMonitor(
            self.loop,  # type: ignore[arg-type]
            self.journal,
            self.clock,
            self._notify,
            self._restart,
            sleep=SleepWatch(self.clock, self.awake),
            tz=TZ,
        )

    async def _notify(self, text: str) -> bool:
        self.sent.append(text)
        return self._delivered.pop(0) if self._delivered else True

    async def _restart(self) -> None:
        self.restarts += 1

    async def tick(self, awake_seconds: float = 30.0) -> None:
        self.awake.pass_time(awake_seconds)
        await self.monitor.tick()

    def sleep_then_tick(self, seconds: float):  # type: ignore[no-untyped-def]
        self.awake.sleep_for(seconds)
        return self.monitor.tick()


async def test_после_сна_цикл_не_объявляется_зависшим_и_не_перезапускается() -> None:
    rig = Rig()
    rig.loop.next_due = rig.clock.monotonic() + 20
    await rig.tick()
    # Срок следующей проверки остался в прошлом на часы, как сразу после настоящего пробуждения
    await rig.sleep_then_tick(4 * HOUR)
    assert rig.restarts == 0 and rig.monitor.recoveries == 0
    assert rig.journal.statuses[-1]["hung"] is False
    assert not any("завис" in text for text in rig.sent)
    assert "liveness.hung" not in rig.journal.codes()


async def test_сон_попадает_в_журнал_и_в_status_json() -> None:
    rig = Rig()
    await rig.tick()
    await rig.sleep_then_tick(2 * HOUR)
    [(_code, message, level, data)] = [e for e in rig.journal.events if e[0] == "system.sleep"]
    assert level == "WARNING" and "7200" in message
    assert data["slept_sec"] == 7200
    status = rig.journal.statuses[-1]
    assert status["sleeps_total_sec"] == 7200 and status["last_sleep_sec"] == 7200
    assert status["last_sleep_started"] is not None


async def test_владельцу_пишут_только_после_первой_успешной_проверки() -> None:
    rig = Rig()
    await rig.tick()
    await rig.sleep_then_tick(4 * HOUR)
    assert rig.sent == []  # сети после пробуждения может ещё не быть
    rig.loop.checked()
    await rig.tick()
    [text] = rig.sent
    assert "Ноутбук спал" in text and "4 ч 0 мин" in text
    await rig.tick()
    assert len(rig.sent) == 1  # второй раз не повторяется


async def test_проверка_до_пробуждения_сообщение_не_запускает() -> None:
    rig = Rig()
    rig.loop.checked()  # последняя успешная проверка была ещё до сна
    await rig.tick()
    await rig.sleep_then_tick(HOUR)
    await rig.tick()
    assert rig.sent == []


async def test_короткий_сон_пишется_в_журнал_но_владельцу_не_сообщается() -> None:
    rig = Rig()
    await rig.tick()
    await rig.sleep_then_tick(SLEEP_NOTICE_MIN_SEC - 60)
    rig.loop.checked()
    await rig.tick()
    assert "system.sleep" in rig.journal.codes()
    assert rig.sent == []


async def test_несколько_снов_до_проверки_объединяются_в_одно_сообщение() -> None:
    rig = Rig()
    await rig.tick()
    await rig.sleep_then_tick(HOUR)
    await rig.tick()
    await rig.sleep_then_tick(2 * HOUR)
    rig.loop.checked()
    await rig.tick()
    [text] = rig.sent
    assert "3 ч 0 мин" in text


async def test_недоставленное_сообщение_повторяется_пока_не_дойдёт() -> None:
    rig = Rig(delivered=[False, False, True])
    await rig.tick()
    await rig.sleep_then_tick(HOUR)
    rig.loop.checked()
    for _ in range(5):
        await rig.tick()
    assert len(rig.sent) == 3  # две неудачи и успех, дальше не шлём


async def test_если_сообщение_так_и_не_дошло_попытки_кончаются_и_это_в_журнале() -> None:
    rig = Rig(delivered=[False] * 20)
    await rig.tick()
    await rig.sleep_then_tick(HOUR)
    rig.loop.checked()
    for _ in range(SLEEP_NOTICE_MAX_TRIES + 3):
        await rig.tick()
    assert len(rig.sent) == SLEEP_NOTICE_MAX_TRIES
    assert "system.sleep_unreported" in rig.journal.codes()


async def test_сбой_отправки_сообщения_о_сне_не_роняет_монитор() -> None:
    rig = Rig()

    async def boom(text: str) -> bool:
        raise RuntimeError("сеть")

    rig.monitor._notify = boom  # type: ignore[assignment]
    await rig.tick()
    await rig.sleep_then_tick(HOUR)
    rig.loop.checked()
    await rig.tick()  # не падает
    assert rig.journal.statuses  # статус пишется и дальше


async def test_настоящее_зависание_без_сна_по_прежнему_ловится() -> None:
    rig = Rig()
    rig.loop.tick_started_at = rig.clock.monotonic() - (HUNG_CHECK_SEC + 1)
    await rig.tick(awake_seconds=0)
    assert rig.monitor.recoveries == 1 and rig.restarts == 1
    assert "system.sleep" not in rig.journal.codes()


async def test_опоздание_цикла_без_сна_по_прежнему_зависание() -> None:
    rig = Rig()
    rig.loop.next_due = rig.clock.monotonic() - (HUNG_LATE_SEC + 10)
    await rig.tick(awake_seconds=300)  # время шло и «бодрствовало»: это не сон
    assert rig.monitor.recoveries == 1


async def test_без_наблюдателя_сна_монитор_работает_как_раньше() -> None:
    clock = FakeClock()
    loop = StubLoop(clock)
    journal = RecordingJournal()

    async def notify(text: str) -> None:
        return None

    async def restart() -> None:
        return None

    monitor = LivenessMonitor(loop, journal, clock, notify, restart)  # type: ignore[arg-type]
    clock.advance(4 * HOUR)
    await monitor.tick()
    assert "system.sleep" not in journal.codes()
    assert journal.statuses[-1]["sleeps_total_sec"] == 0
