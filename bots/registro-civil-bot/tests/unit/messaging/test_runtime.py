"""Лестница частоты (D4) и настройки, меняемые из Telegram (SPEC 2.5, критерий приёмки 9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.application.runtime import (
    K_CHANNELS_OFF,
    K_INTERVAL,
    K_LADDER_START,
    K_MONTHS,
    K_PAUSED_UNTIL,
    LADDER,
    RuntimeSettings,
    RuntimeStore,
    clamp_interval,
)
from rcbot.domain.models import Channel
from tests.fakes import T0, MemoryKV

H = timedelta(hours=1)
S = timedelta(seconds=1)


def runtime(base: float = 20.0, started: datetime = T0, **kwargs: object) -> RuntimeSettings:
    return RuntimeSettings(base, 6, started, **kwargs)  # type: ignore[arg-type]


# --- clamp_interval ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (10, 10),
        (9.99, 10),
        (0, 10),
        (-5, 10),
        (20, 20),
        (3600, 3600),
        (3600.1, 3600),
        (1e9, 3600),
        (float("inf"), 3600),
        (float("-inf"), 10),
    ],
)
def test_clamp_держит_интервал_в_пределах_10_и_3600(raw: float, expected: float) -> None:
    assert clamp_interval(raw) == expected


def test_clamp_не_пропускает_nan_ниже_минимума() -> None:
    # `/interval nan` и порча в БД не должны давать расписание «без паузы» или NaN-сон
    with pytest.raises(ValueError, match="число"):
        clamp_interval(float("nan"))


@given(st.floats(allow_nan=False))
def test_свойство_clamp_никогда_не_ниже_минимума_и_не_выше_максимума(raw: float) -> None:
    assert 10.0 <= clamp_interval(raw) <= 3600.0


# --- лестница D4 ---


def test_лестница_соответствует_d4() -> None:
    assert LADDER == ((6 * H, 60.0), (12 * H, 30.0))


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), 60.0),
        (S, 60.0),
        (6 * H - S, 60.0),
        (6 * H, 30.0),  # граница: ступень 60 с кончается ровно в 6 ч
        (6 * H + S, 30.0),
        (18 * H - S, 30.0),
        (18 * H, 20.0),  # граница: ступень 30 с кончается ровно в 18 ч
        (18 * H + S, 20.0),
        (1000 * H, 20.0),
    ],
)
def test_ladder_interval_на_границах_6_и_18_часов(elapsed: timedelta, expected: float) -> None:
    assert runtime().ladder_interval(T0 + elapsed) == expected


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (10.0, [60.0, 30.0, 10.0]),
        (20.0, [60.0, 30.0, 20.0]),
        (30.0, [60.0, 30.0, 30.0]),
        (45.0, [60.0, 45.0, 45.0]),
        (60.0, [60.0, 60.0, 60.0]),
        (90.0, [90.0, 90.0, 90.0]),
    ],
)
def test_базовый_интервал_больше_ступени_побеждает(base: float, expected: list[float]) -> None:
    rt = runtime(base)
    got = [rt.ladder_interval(T0 + moment) for moment in (timedelta(0), 7 * H, 20 * H)]
    assert got == expected


def test_часы_ушли_назад_начало_лестницы_в_будущем_даёт_самый_медленный_шаг() -> None:
    rt = runtime(started=T0 + timedelta(days=2))
    assert rt.ladder_interval(T0) == 60.0
    rt = runtime(started=T0 + S)
    assert rt.ladder_interval(T0) == 60.0


@pytest.mark.parametrize(
    ("elapsed", "expected_after"),
    [
        (timedelta(0), 60.0),  # уже на первой ступени: лестница начинается заново
        (3 * H, 60.0),
        (6 * H - S, 60.0),
        (6 * H, 60.0),  # ровно на границе — уже вторая ступень, откат на первую
        (10 * H, 60.0),
        (18 * H - S, 60.0),
        (18 * H, 30.0),  # ровно на границе — базовая ступень, откат на вторую
        (500 * H, 30.0),
    ],
)
def test_step_back_уходит_на_ступень_медленнее(elapsed: timedelta, expected_after: float) -> None:
    now = T0 + elapsed
    rt = runtime()
    before = rt.ladder_interval(now)
    rt.step_back(now)
    after = rt.ladder_interval(now)
    assert after == expected_after
    assert after >= before


def test_step_back_после_отката_лестница_идёт_дальше_от_новой_точки() -> None:
    rt = runtime()
    now = T0 + 30 * H
    rt.step_back(now)  # 20 → 30
    assert rt.ladder_interval(now) == 30.0
    assert rt.ladder_interval(now + 12 * H) == 20.0  # через 12 ч вернулась базовая ступень
    rt.step_back(now)  # 30 → 60
    assert rt.ladder_interval(now) == 60.0
    assert rt.ladder_interval(now + 6 * H) == 30.0


def test_step_back_при_начале_лестницы_в_будущем_не_падает() -> None:
    rt = runtime(started=T0 + 2 * H)
    rt.step_back(T0)
    assert rt.ladder_interval(T0) == 60.0
    assert rt.ladder_started_at <= T0


@given(st.integers(-(10**6), 10**7), st.floats(10, 3600))
def test_свойство_step_back_не_ускоряет_и_не_роняет(elapsed_sec: int, base: float) -> None:
    now = T0 + timedelta(seconds=elapsed_sec)
    rt = runtime(base, started=T0)
    before = rt.ladder_interval(now)
    rt.step_back(now)
    assert rt.ladder_interval(now) >= before
    assert rt.ladder_started_at <= now


@given(st.integers(0, 10**7), st.integers(0, 10**7), st.floats(10, 3600))
def test_свойство_лестница_не_ускоряется_со_временем_и_не_ниже_базы(
    a: int, b: int, base: float
) -> None:
    rt = runtime(base)
    first, second = sorted((a, b))
    early = rt.ladder_interval(T0 + timedelta(seconds=first))
    late = rt.ladder_interval(T0 + timedelta(seconds=second))
    assert early >= late >= base


def test_пауза_до_момента_а_в_сам_момент_уже_нет() -> None:
    rt = runtime()
    assert not rt.paused(T0)
    rt.paused_until = T0 + timedelta(minutes=5)
    assert rt.paused(T0)
    assert rt.paused(T0 + timedelta(minutes=5) - S)
    assert not rt.paused(T0 + timedelta(minutes=5))
    assert not rt.paused(T0 + H)


def test_каналы_включены_пока_не_выключены() -> None:
    rt = runtime(channels_off={Channel.CALL})
    assert rt.channel_enabled(Channel.TELEGRAM)
    assert rt.channel_enabled(Channel.NTFY)
    assert not rt.channel_enabled(Channel.CALL)


def test_наборы_каналов_у_разных_настроек_не_общие() -> None:
    first, second = runtime(), runtime()
    first.channels_off.add(Channel.NTFY)
    assert second.channels_off == set()


# --- RuntimeStore ---


async def test_первая_загрузка_берёт_умолчания_и_сохраняет_их() -> None:
    kv = MemoryKV()
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert (rt.base_interval_sec, rt.months_ahead) == (20.0, 6)
    assert rt.ladder_started_at == T0
    assert rt.paused_until is None
    assert rt.channels_off == set()
    assert kv.data == {
        K_INTERVAL: "20",
        K_MONTHS: "6",
        K_LADDER_START: T0.isoformat(),
        K_PAUSED_UNTIL: "",
        K_CHANNELS_OFF: "",
    }


async def test_вторая_загрузка_не_сбрасывает_начало_лестницы() -> None:
    kv = MemoryKV()
    store = RuntimeStore(kv)
    await store.load(20.0, 6, T0)
    again = await store.load(20.0, 6, T0 + 5 * H)
    assert again.ladder_started_at == T0


async def test_сохранение_и_загрузка_возвращают_то_же_самое() -> None:
    store = RuntimeStore(MemoryKV())
    original = RuntimeSettings(
        base_interval_sec=25.5,
        months_ahead=4,
        ladder_started_at=T0 - 3 * H,
        paused_until=T0 + timedelta(minutes=15),
        channels_off={Channel.NTFY, Channel.CALL},
    )
    await store.save(original)
    loaded = await store.load(99.0, 1, T0)
    assert loaded == original


async def test_значения_в_бд_главнее_умолчаний_из_env() -> None:
    kv = MemoryKV({K_INTERVAL: "45", K_MONTHS: "3", K_LADDER_START: T0.isoformat()})
    rt = await RuntimeStore(kv).load(20.0, 6, T0 + H)
    assert (rt.base_interval_sec, rt.months_ahead) == (45.0, 3)


async def test_save_не_записывает_интервал_ниже_минимума() -> None:
    kv = MemoryKV()
    await RuntimeStore(kv).save(runtime(base=5.0))
    assert kv.data[K_INTERVAL] == "10"
    kv2 = MemoryKV()
    await RuntimeStore(kv2).save(runtime(base=99999.0))
    assert kv2.data[K_INTERVAL] == "3600"


async def test_load_поднимает_интервал_из_бд_до_минимума() -> None:
    kv = MemoryKV({K_INTERVAL: "3", K_LADDER_START: T0.isoformat()})
    assert (await RuntimeStore(kv).load(20.0, 6, T0)).base_interval_sec == 10.0
    kv = MemoryKV({K_INTERVAL: "-50", K_LADDER_START: T0.isoformat()})
    assert (await RuntimeStore(kv).load(20.0, 6, T0)).base_interval_sec == 10.0


async def test_каналы_сохраняются_отсортированными_и_читаются_обратно() -> None:
    kv = MemoryKV()
    await RuntimeStore(kv).save(runtime(channels_off={Channel.NTFY, Channel.CALL}))
    assert kv.data[K_CHANNELS_OFF] == "call,ntfy"


@pytest.mark.parametrize("garbage", ["abc", "", "nan", "1,5", "0x10", "None"])
async def test_мусор_вместо_интервала_не_роняет_запуск_и_даёт_умолчание(garbage: str) -> None:
    kv = MemoryKV({K_INTERVAL: garbage, K_LADDER_START: T0.isoformat()})
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt.base_interval_sec == 20.0


@pytest.mark.parametrize("garbage", ["abc", "", "1.5", "None", "1e3"])
async def test_мусор_вместо_числа_месяцев_даёт_умолчание(garbage: str) -> None:
    kv = MemoryKV({K_MONTHS: garbage, K_LADDER_START: T0.isoformat()})
    assert (await RuntimeStore(kv).load(20.0, 6, T0)).months_ahead == 6


@pytest.mark.parametrize(("stored", "expected"), [("0", 1), ("-3", 1), ("13", 12), ("999", 12)])
async def test_месяцев_вне_диапазона_приводятся_к_границам_настроек(
    stored: str, expected: int
) -> None:
    # Ноль месяцев означал бы «опрашивать пустой диапазон» — бот молча ничего бы не видел
    kv = MemoryKV({K_MONTHS: stored, K_LADDER_START: T0.isoformat()})
    assert (await RuntimeStore(kv).load(20.0, 6, T0)).months_ahead == expected


@pytest.mark.parametrize(
    "garbage",
    ["не дата", "2026-13-45T00:00:00+00:00", "yesterday", "2026-11-12T10:00:00"],
)
async def test_мусорное_или_наивное_начало_лестницы_заменяется_текущим_моментом(
    garbage: str,
) -> None:
    kv = MemoryKV({K_LADDER_START: garbage})
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt.ladder_started_at == T0
    # порча «ремонтируется»: следующий запуск читает уже корректное значение
    assert datetime.fromisoformat(kv.data[K_LADDER_START]) == T0
    assert (await RuntimeStore(kv).load(20.0, 6, T0 + H)).ladder_started_at == T0


async def test_начало_лестницы_в_будущем_после_ухода_часов_назад_не_остаётся_там() -> None:
    kv = MemoryKV({K_LADDER_START: (T0 + timedelta(days=400)).isoformat()})
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt.ladder_started_at == T0


@pytest.mark.parametrize("garbage", ["не дата", "2026-99-99", "2026-11-12T10:00:00"])
async def test_мусорная_пауза_это_отсутствие_паузы(garbage: str) -> None:
    kv = MemoryKV({K_PAUSED_UNTIL: garbage, K_LADDER_START: T0.isoformat()})
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt.paused_until is None
    assert not rt.paused(T0)


async def test_устаревшая_пауза_читается_но_не_действует() -> None:
    past = (T0 - H).isoformat()
    kv = MemoryKV({K_PAUSED_UNTIL: past, K_LADDER_START: T0.isoformat()})
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt.paused_until == T0 - H
    assert not rt.paused(T0)


async def test_пауза_в_другом_часовом_поясе_читается_как_тот_же_момент() -> None:
    kv = MemoryKV({K_PAUSED_UNTIL: "2026-11-12T12:30:00-03:00", K_LADDER_START: T0.isoformat()})
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt.paused_until == datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    assert rt.paused(T0)


async def test_неизвестный_канал_в_списке_отключённых_пропускается() -> None:
    kv = MemoryKV({K_CHANNELS_OFF: "telegram, bogus,,call ", K_LADDER_START: T0.isoformat()})
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt.channels_off == {Channel.TELEGRAM, Channel.CALL}


async def test_всё_испорчено_сразу_запуск_идёт_с_умолчаниями() -> None:
    kv = MemoryKV(
        {
            K_INTERVAL: "x",
            K_MONTHS: "y",
            K_LADDER_START: "z",
            K_PAUSED_UNTIL: "w",
            K_CHANNELS_OFF: "q",
        }
    )
    rt = await RuntimeStore(kv).load(20.0, 6, T0)
    assert rt == RuntimeSettings(20.0, 6, T0)
    assert (await RuntimeStore(kv).load(20.0, 6, T0 + H)) == rt


async def test_загруженное_значение_интервала_не_ниже_минимума_при_любых_умолчаниях() -> None:
    kv = MemoryKV({K_INTERVAL: "abc", K_LADDER_START: T0.isoformat()})
    rt = await RuntimeStore(kv).load(3.0, 6, T0)
    assert rt.base_interval_sec >= 10.0
