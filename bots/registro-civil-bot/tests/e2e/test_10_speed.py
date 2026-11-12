"""Сценарий 10: скорость (SPEC 7, «Скорость»): от снимка с местами до `send` — меньше 100 мс.

Меряется настоящее время (`perf_counter`), а не часы стенда. Пороги из задания: p95 < 100 мс,
максимум < 250 мс (запас на медленный CI). Цифры печатаются (`pytest -s`) и идут в отчёт.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from rcbot.domain.models import AlertKind, Channel, SourceKind
from tests.e2e.helpers import NoLogin, TimedSource, percentile
from tests.e2e.site import ChannelsSite
from tests.e2e.stand import Stand, fake_source
from tests.fakes import slots

REPS = 50
P95_LIMIT_MS = 100.0
MAX_LIMIT_MS = 250.0


def report(title: str, values_ms: list[float]) -> str:
    line = (
        f"{title}: n={len(values_ms)} p50={percentile(values_ms, 0.5):.2f} мс "
        f"p95={percentile(values_ms, 0.95):.2f} мс max={max(values_ms):.2f} мс"
    )
    print(line)
    return line


def cycle(count: int) -> list:
    """Место появилось → три пустые проверки (инцидент закрылся) — и так `count` раз."""
    return [item for _ in range(count) for item in (slots(23), slots(), slots(), slots())]


async def run_cycles(stand: Stand, count: int) -> None:
    for _ in range(count):
        await stand.checks(4)


def first_wave(stand: Stand, channel: Channel) -> list:
    return [s for s in stand.sent(channel) if s.attempt == 1 and s.alert.kind is AlertKind.APPEARED]


async def build_timed(make_stand, stand_clock, reps: int, **kwargs) -> tuple[Stand, TimedSource]:
    source = TimedSource(fake_source(stand_clock, *cycle(reps), kind=SourceKind.API))
    stand = await make_stand(primary=source, auth=NoLogin(), **kwargs)
    return stand, source


def latencies_ms(stand: Stand, source: TimedSource) -> list[float]:
    """По каждому появлению: самый поздний из трёх `send` минус момент получения снимка."""
    waves = [first_wave(stand, c) for c in Channel]
    assert all(len(w) == len(source.got) for w in waves), "на каждое появление — по отправке"
    return [
        (max(w[i].perf_start for w in waves) - source.got[i]) * 1000 for i in range(len(source.got))
    ]


@pytest.mark.slow
async def test_первая_волна_меньше_100_мс_на_настоящем_времени(make_stand, stand_clock) -> None:
    stand, source = await build_timed(make_stand, stand_clock, REPS)
    # Первая проверка идёт при старте и уже видит место: это первое из пятидесяти появлений
    await run_cycles(stand, REPS - 1)

    values = latencies_ms(stand, source)
    assert len(values) == REPS
    report("fake-каналы, SQLite и журнал на диске", values)
    assert percentile(values, 0.95) < P95_LIMIT_MS
    assert max(values) < MAX_LIMIT_MS
    # весь путь — в базе: пятьдесят инцидентов закрыты, у каждого три доставки первой волны
    incidents = await stand.incidents()
    assert len(incidents) == REPS


async def test_запись_в_базу_не_задерживает_рассылку_open_медленнее_500_мс(
    make_stand, stand_clock
) -> None:
    async def slow_open() -> None:
        await asyncio.sleep(0.5)

    source = TimedSource(fake_source(stand_clock, slots(23), kind=SourceKind.API))
    stand = await make_stand(
        primary=source,
        auth=NoLogin(),
        incidents_open_hook=slow_open,
        ignore={"bus", "background"},
        start=False,
    )
    stand.start(liveness=False)
    await stand.until(lambda: all(first_wave(stand, c) for c in Channel))
    await stand.quiesce()

    (value,) = latencies_ms(stand, source)
    print(f"первая волна при open() = 500 мс: {value:.2f} мс")
    assert value < P95_LIMIT_MS
    assert await stand.incidents() == [], "к этому моменту база ещё ничего не записала"
    # когда база всё же отвечает, инцидент и доставки появляются и связаны номером
    await stand.until(lambda: not stand.bus._tasks and not stand.alerts._background)
    (incident,) = await stand.incidents()
    deliveries = await stand.storage.incidents.deliveries_for(incident.id)
    assert len(deliveries) == 3 and {d.incident_id for d in deliveries} == {incident.id}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG-T9: poll_loop.py:_tick (journal.check после publish, до первой уступки циклу) и "
        "alerting.py:_wave (journal.event до TaskGroup): синхронная запись журнала на диск "
        "стоит перед отправкой; зависший диск или блокировка файла синхронизацией задерживают "
        "тревогу на всё время записи (SPEC 2.2 п. 4)"
    ),
)
async def test_запись_журнала_не_задерживает_рассылку_диск_завис_на_200_мс(
    make_stand, stand_clock
) -> None:
    source = TimedSource(fake_source(stand_clock, slots(), slots(23), kind=SourceKind.API))
    stand = await make_stand(primary=source, auth=NoLogin())
    stand.journal.stall_sec = 0.2
    await stand.next_check()

    (value,) = latencies_ms(stand, source)
    print(f"первая волна при записи журнала 200 мс: {value:.2f} мс")
    assert value < P95_LIMIT_MS


async def test_настоящие_ntfy_и_callmebot_по_https_от_события_до_запроса_на_сервере(
    make_stand, stand_clock, channels_site: ChannelsSite
) -> None:
    reps = 20
    stand, source = await build_timed(make_stand, stand_clock, reps, channels=channels_site)
    await run_cycles(stand, reps - 1)

    # на сервер приходят и повторы тревог; первая волна — первый запрос после каждого снимка
    def first_after(requests: list, moment: float):
        return next(r for r in requests if r.perf >= moment)

    ntfy = [first_after(channels_site.of("ntfy"), t) for t in source.got]
    calls = [first_after(channels_site.of("callmebot"), t) for t in source.got]
    assert len(source.got) == reps
    totals = [(max(ntfy[i].perf, calls[i].perf) - source.got[i]) * 1000 for i in range(reps)]
    cold, warm = totals[0], totals[1:]
    print(f"HTTPS-каналы, первый (холодный) запрос: {cold:.2f} мс")
    report("HTTPS-каналы, тёплые соединения", warm)
    assert percentile(warm, 0.95) < P95_LIMIT_MS and max(warm) < MAX_LIMIT_MS
    assert cold < MAX_LIMIT_MS
    # соединения переиспользуются (keep-alive), а не открываются на каждую тревогу
    assert len({r.peer_port for r in ntfy[1:]}) == 1
    assert len({r.peer_port for r in calls[1:]}) == 1


async def test_настоящий_ntfy_прогретое_соединение_используется_первой_тревогой(
    make_stand, stand_clock, channels_site: ChannelsSite
) -> None:
    source = TimedSource(fake_source(stand_clock, slots(), slots(23), kind=SourceKind.API))
    stand = await make_stand(primary=source, auth=NoLogin(), channels=channels_site)
    await stand.notifiers[Channel.NTFY].warmup()  # то, что делает keep_warm раз в 4 минуты
    await stand.next_check()

    health, ntfy = channels_site.of("health"), channels_site.of("ntfy")
    assert len(health) == 1 and len(ntfy) == 1
    assert health[0].peer_port == ntfy[0].peer_port, "тревога пошла по прогретому соединению"
    print(f"ntfy по прогретому соединению: {(ntfy[0].perf - source.got[0]) * 1000:.2f} мс")
    assert time.perf_counter() - source.got[0] < 5
