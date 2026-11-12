"""Расписание цикла опроса (SPEC 2.2 п. 6, 0.3 правило 3, D4): точные моменты проверок.

Время идёт только по `clock.advance`; моменты считаются от первой проверки (0 с).
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest
from tests.fakes import T0, slots
from tests.unit.loop.harness import make_rig, spans

from rcbot.config.settings import MAX_POLL_INTERVAL_SEC, MIN_DOM_POLL_INTERVAL_SEC
from rcbot.domain.models import PollState, SourceKind

EMPTY: frozenset = frozenset()  # type: ignore[type-arg]


class _LowestRandom(random.Random):
    """Генератор, который всегда выбирает нижнюю границу разброса: худший случай для минимума."""

    def uniform(self, a: float, b: float) -> float:
        return a


# --- лестница D4 ---


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), 60.0),
        (timedelta(hours=5, minutes=59, seconds=59), 60.0),
        (timedelta(hours=6), 30.0),
        (timedelta(hours=17, minutes=59, seconds=59), 30.0),
        (timedelta(hours=18), 20.0),
        (timedelta(days=30), 20.0),
    ],
)
def test_лестница_D4_60_потом_30_потом_базовый(elapsed: timedelta, expected: float) -> None:
    rig = make_rig(kind=SourceKind.API, base_interval=20.0, ladder_elapsed=elapsed)
    assert rig.loop.current_interval(T0) == expected


def test_лестница_не_быстрее_базового_интервала() -> None:
    # Владелец поставил 45 с: ступень «30 с» не может опрашивать чаще, чем он разрешил
    rig = make_rig(base_interval=45.0, ladder_elapsed=timedelta(hours=7))
    assert rig.loop.current_interval(T0) == 45.0


async def test_ступень_лестницы_меняется_на_ходу() -> None:
    # За 30 с до шести часов: проверки через 60 с, потом — через 30 с
    rig = make_rig([EMPTY], ladder_elapsed=timedelta(hours=5, minutes=59, seconds=30))
    await rig.start()
    await rig.advance(130)
    assert rig.rel(rig.source.starts) == [0, 60, 90, 120]
    await rig.kill()


# --- интервал и минимум ---


async def test_проверки_идут_с_заданным_интервалом_без_разброса() -> None:
    rig = make_rig([EMPTY], base_interval=20.0)
    await rig.start()
    assert rig.rel(rig.source.starts) == [0]
    await rig.advance(19.999)
    assert len(rig.source.starts) == 1
    await rig.advance(0.001)
    await rig.advance(60)
    assert rig.rel(rig.source.starts) == [0, 20, 40, 60, 80]
    await rig.kill()


async def test_DomSource_опрашивается_не_чаще_раза_в_минуту() -> None:
    rig = make_rig([EMPTY], kind=SourceKind.DOM, base_interval=10.0)
    await rig.start()
    await rig.advance(180)
    assert rig.rel(rig.source.starts) == [0, 60, 120, 180]
    await rig.kill()


async def test_DomSource_минимум_60_с_держится_и_с_самым_коротким_разбросом() -> None:
    # Разброс −10 % не должен опускать DOM-интервал ниже 60 с (инвариант 3)
    rig = make_rig(
        [EMPTY], kind=SourceKind.DOM, base_interval=10.0, jitter=0.1, rng=_LowestRandom()
    )
    await rig.start()
    await rig.advance(600)
    gaps = spans(rig.source.starts)
    assert len(gaps) >= 9
    assert min(gaps) >= MIN_DOM_POLL_INTERVAL_SEC
    await rig.kill()


async def test_жёсткий_минимум_10_с_при_любом_разбросе() -> None:
    rig = make_rig([EMPTY], base_interval=10.0, jitter=0.5, rng=_LowestRandom())
    await rig.start()
    await rig.advance(100)
    gaps = spans(rig.source.starts)
    assert gaps and min(gaps) >= 10.0
    await rig.kill()


async def test_set_interval_ниже_10_с_поднимается_до_минимума() -> None:
    rig = make_rig([EMPTY], base_interval=20.0)
    assert await rig.loop.set_interval(1) == 10.0
    assert await rig.loop.set_interval(-5) == 10.0
    assert rig.runtime.base_interval_sec == 10.0
    assert rig.kv.data["poll_interval_sec"] == "10"


async def test_set_interval_выше_предела_и_сохранение() -> None:
    rig = make_rig([EMPTY])
    assert await rig.loop.set_interval(10**6) == MAX_POLL_INTERVAL_SEC
    assert await rig.loop.set_interval(42) == 42.0
    assert rig.kv.data["poll_interval_sec"] == "42"


async def test_set_interval_через_цикл_никогда_не_даёт_проверок_чаще_10_с() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.loop.set_interval(0.5)  # клампится до 10 и будит цикл
    await rig.advance(100)
    gaps = spans(rig.source.starts)
    assert min(gaps) >= 10.0
    await rig.kill()


# --- разброс ---


async def test_разброс_в_границах_и_не_постоянный() -> None:
    rig = make_rig([EMPTY], base_interval=20.0, jitter=0.1, seed=7)
    await rig.start()
    await rig.advance(2000)
    gaps = spans(rig.source.starts)
    assert len(gaps) > 80
    assert all(18.0 - 1e-6 <= g <= 22.0 + 1e-6 for g in gaps)
    assert len({round(g, 3) for g in gaps}) > 20  # не метроном
    await rig.kill()


async def test_разброс_несмещён_средний_интервал_около_базового() -> None:
    rig = make_rig([EMPTY], base_interval=20.0, jitter=0.1, seed=11)
    await rig.start()
    await rig.advance(4000)
    starts = rig.source.starts
    mean = (starts[-1] - starts[0]) / (len(starts) - 1)
    assert 19.5 <= mean <= 20.5
    await rig.kill()


async def test_нулевой_разброс_даёт_точный_интервал() -> None:
    rig = make_rig([EMPTY], base_interval=30.0, jitter=0.0)
    await rig.start()
    await rig.advance(300)
    gaps = spans(rig.source.starts)
    assert set(gaps) == {30.0}
    await rig.kill()


# --- без дрейфа ---


async def test_долгая_проверка_не_сдвигает_расписание() -> None:
    # Проверка занимает 5 с при интервале 20 с: следующая — через 20 с от СТАРТА, а не 25 от конца
    rig = make_rig(base_interval=20.0)

    async def slow() -> frozenset:  # type: ignore[type-arg]
        await rig.clock.sleep(5)
        return EMPTY

    rig.source.push(slow)
    await rig.start()
    await rig.advance(100)
    assert rig.rel(rig.source.starts) == [0, 20, 40, 60, 80, 100]
    await rig.kill()


async def test_проверка_дольше_интервала_не_порождает_серию_догоняющих_запросов() -> None:
    rig = make_rig(base_interval=20.0)

    async def very_slow() -> frozenset:  # type: ignore[type-arg]
        await rig.clock.sleep(35)
        return EMPTY

    rig.source.push(very_slow, EMPTY)
    await rig.start()
    await rig.advance(35)  # проверка закончилась в 35 с
    await rig.advance(100)
    # 35 с проверка + 10 с минимум (а не мгновенный повтор), далее обычные 20 с
    assert rig.rel(rig.source.starts) == [0, 45, 65, 85, 105, 125]
    await rig.kill()


async def test_расписание_не_уплывает_за_тысячу_проверок() -> None:
    rig = make_rig([EMPTY], base_interval=20.0)
    await rig.start()
    await rig.advance(20 * 1000)
    assert len(rig.source.starts) == 1001
    assert rig.rel(rig.source.starts)[-1] == 20000
    await rig.kill()


# --- wake / pause / resume / stop ---


async def test_wake_во_время_проверки_не_удваивает_следующий_интервал() -> None:
    # /interval пришла посреди проверки: лишняя проверка не должна отодвигать следующую на 2×
    rig = make_rig(base_interval=20.0)

    async def slow() -> frozenset:  # type: ignore[type-arg]
        await rig.clock.sleep(3)
        return EMPTY

    rig.source.push(slow, EMPTY)
    await rig.start()
    await rig.advance(1)
    rig.loop.wake()
    await rig.advance(100)
    starts = rig.rel(rig.source.starts)
    assert starts[0] == 0
    assert starts[1] == 10  # внеочередная проверка: не раньше 10 с от прошлой
    gaps = spans(starts[1:])
    assert gaps and set(gaps) == {20}
    await rig.kill()


async def test_wake_во_сне_запускает_проверку_сразу_если_прошло_10_с() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(15)
    rig.loop.wake()
    await rig.advance(0)
    assert rig.rel(rig.source.starts) == [0, 15]
    # и расписание считается от внеочередной проверки
    await rig.advance(60)
    assert rig.rel(rig.source.starts) == [0, 15, 75]
    await rig.kill()


async def test_wake_раньше_10_с_ждёт_минимума() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(3)
    rig.loop.wake()
    await rig.advance(6.9)
    assert len(rig.source.starts) == 1
    await rig.advance(0.1)
    assert rig.rel(rig.source.starts) == [0, 10]
    await rig.kill()


async def test_спам_wake_не_нарушает_минимум_между_проверками() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    for _ in range(40):
        await rig.advance(1)
        rig.loop.wake()
    gaps = spans(rig.source.starts)
    assert len(gaps) >= 3
    assert min(gaps) >= 10.0
    await rig.kill()


async def test_stop_во_сне_завершает_run_и_ничего_не_оставляет() -> None:
    rig = make_rig([EMPTY])
    await rig.start()
    assert rig.clock.sleeping == 1
    await rig.stop()
    assert rig.task is not None and rig.task.done()
    assert rig.clock.sleeping == 0
    await rig.advance(1000)
    assert len(rig.source.starts) == 1


async def test_stop_до_запуска_run_завершается_без_проверок() -> None:
    rig = make_rig([EMPTY])
    rig.loop.stop()
    await rig.start()
    assert rig.task is not None and rig.task.done()
    assert rig.source.starts == []


async def test_пауза_до_момента_проверки_не_идут_и_возобновляются() -> None:
    rig = make_rig([EMPTY], base_interval=20.0)
    await rig.start()
    await rig.advance(5)
    await rig.loop.pause(rig.clock.now() + timedelta(seconds=100))  # до 105 с
    await rig.advance(99)
    assert rig.rel(rig.source.starts) == [0]
    assert rig.loop.status.state is PollState.PAUSED
    assert rig.runtime.paused_until is not None
    assert rig.kv.data["paused_until"] != ""
    await rig.advance(2)
    assert rig.rel(rig.source.starts) == [0, 105]
    assert rig.loop.status.state is PollState.RUNNING
    await rig.kill()


async def test_бессрочная_пауза_держится_сутки_и_снимается_resume() -> None:
    rig = make_rig([EMPTY])
    await rig.start()
    await rig.loop.pause(rig.clock.now() + timedelta(days=3650))
    await rig.advance(86400)
    assert len(rig.source.starts) == 1
    assert rig.loop.status.state is PollState.PAUSED
    await rig.loop.resume()
    await rig.advance(0)
    assert len(rig.source.starts) == 2
    assert rig.loop.status.state is PollState.RUNNING
    assert rig.kv.data["paused_until"] == ""
    await rig.kill()


async def test_пауза_при_старте_из_сохранённых_настроек() -> None:
    rig = make_rig([EMPTY], paused_until=T0 + timedelta(seconds=300))
    await rig.start()
    assert rig.source.starts == []
    assert rig.loop.status.state is PollState.PAUSED
    await rig.advance(299)
    assert rig.source.starts == []
    await rig.advance(1)
    assert rig.rel(rig.source.starts) == [300]
    await rig.kill()


async def test_пауза_с_датой_в_прошлом_не_приостанавливает() -> None:
    rig = make_rig([EMPTY], paused_until=T0 - timedelta(seconds=1))
    await rig.start()
    assert len(rig.source.starts) == 1
    assert rig.loop.status.state is PollState.RUNNING
    await rig.kill()


async def test_stop_во_время_паузы_завершает_run() -> None:
    rig = make_rig([EMPTY], paused_until=T0 + timedelta(days=1))
    await rig.start()
    assert rig.clock.sleeping == 1
    await rig.stop()
    assert rig.clock.sleeping == 0


async def test_повторный_wake_во_время_паузы_не_запускает_проверок() -> None:
    rig = make_rig([EMPTY], paused_until=T0 + timedelta(hours=1))
    await rig.start()
    rig.loop.wake()
    rig.loop.manual_done()
    await rig.advance(600)
    assert rig.source.starts == []
    assert rig.clock.sleeping == 1
    await rig.kill()


async def test_resume_без_паузы_просто_будит_цикл() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(30)
    await rig.loop.resume()
    await rig.advance(0)
    assert rig.rel(rig.source.starts) == [0, 30]
    await rig.kill()


# --- месяцы, «Готово», /relogin ---


async def test_set_months_зажимает_и_сохраняет_и_доходит_до_источника() -> None:
    rig = make_rig([EMPTY], base_interval=20.0, months=6)
    assert await rig.loop.set_months(0) == 1
    assert await rig.loop.set_months(99) == 12
    assert await rig.loop.set_months(3) == 3
    assert rig.kv.data["months_ahead"] == "3"
    await rig.start()
    assert rig.source.targets[0].months_ahead == 3
    await rig.loop.set_months(9)
    await rig.advance(20)
    assert rig.source.targets[-1].months_ahead == 9
    await rig.kill()


async def test_manual_done_будит_цикл_из_ожидания_шага_1() -> None:
    from rcbot.domain.errors import ManualStepRequired

    rig = make_rig([ManualStepRequired("шаг 1"), EMPTY], base_interval=20.0)
    await rig.start()
    assert rig.loop.status.state is PollState.NEEDS_MANUAL
    await rig.advance(30)  # ждём 900 с, ничего не происходит
    assert len(rig.source.starts) == 1
    rig.loop.manual_done()
    await rig.advance(0)
    assert rig.rel(rig.source.starts) == [0, 30]
    assert rig.loop.status.state is PollState.RUNNING
    await rig.kill()


async def test_relogin_по_команде_входит_и_будит_цикл() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(20)
    await rig.loop.relogin()
    await rig.advance(0)
    assert rig.auth.logins == 1
    assert rig.session.has_session
    assert rig.rel(rig.source.starts) == [0, 20]
    assert rig.source.cookie_history and rig.source.cookie_history[-1][0].name == (
        "simple_bpm_session"
    )
    await rig.kill()


async def test_слоты_находятся_и_событие_публикуется_в_расписании() -> None:
    from rcbot.domain.events import SlotsAppeared

    rig = make_rig([EMPTY, EMPTY, slots(23)], base_interval=20.0)
    await rig.start()
    await rig.advance(40)
    assert len(rig.of_type(SlotsAppeared)) == 1
    assert rig.of_type(SlotsAppeared)[0].at == T0 + timedelta(seconds=40)
    await rig.kill()
