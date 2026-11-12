"""Доменные модели (SPEC 3.1): слот, отпечаток, снимок, статистика часа, значения по умолчанию."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.domain.models import (
    Alert,
    AlertAction,
    AlertKind,
    AppEvent,
    AvailabilitySnapshot,
    Channel,
    DeliveryResult,
    Incident,
    IncidentStatus,
    PollState,
    PollStatsHour,
    Slot,
    SourceKind,
    WatchTarget,
    fingerprint_of,
)
from tests.fakes import T0, slots, slots_at

DAY = date(2027, 2, 23)

slot_strategy = st.builds(
    Slot,
    st.dates(min_value=date(2026, 1, 1), max_value=date(2028, 12, 31)),
    st.none() | st.builds(time, st.integers(0, 23), st.integers(0, 59)),
)


# --- Slot ---


def test_слот_без_времени_печатается_датой_iso() -> None:
    assert Slot(DAY).label() == "2027-02-23"


def test_слот_со_временем_печатается_с_нулями_до_минут() -> None:
    assert Slot(DAY, time(9, 5)).label() == "2027-02-23 09:05"
    assert Slot(DAY, time(0, 0)).label() == "2027-02-23 00:00"


def test_слоты_упорядочены_по_дню_потом_по_времени() -> None:
    later_day = Slot(date(2027, 3, 1), time(8, 0))
    ordered = [Slot(DAY, time(9, 0)), Slot(DAY, time(10, 30)), later_day]
    assert sorted(reversed(ordered)) == ordered


def test_слот_без_времени_и_слот_со_временем_того_же_дня_сортируются_без_ошибки() -> None:
    # Источник «только день» и источник «день+время» могут пересечься в одном снимке (DOM + SAE);
    # сортировка слотов лежит в основе отпечатка и текста тревоги — падать она не вправе.
    mixed = [Slot(DAY, time(9, 0)), Slot(DAY), Slot(date(2027, 2, 22))]
    assert sorted(mixed) == [Slot(date(2027, 2, 22)), Slot(DAY), Slot(DAY, time(9, 0))]


def test_слот_неизменяем_и_хешируем() -> None:
    slot = Slot(DAY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        slot.day = date(2027, 3, 1)  # type: ignore[misc]
    assert {slot, Slot(DAY)} == {slot}
    assert Slot(DAY) != Slot(DAY, time(0, 0))


# --- fingerprint_of ---


def test_отпечаток_стабилен_и_короткий_hex() -> None:
    first = fingerprint_of(slots(23, 24))
    assert first == fingerprint_of(slots(24, 23))
    assert len(first) == 16
    int(first, 16)  # только hex-символы


def test_отпечаток_не_зависит_от_порядка_построения_множества() -> None:
    items = [Slot(DAY, time(10, 0)), Slot(DAY, time(11, 0)), Slot(date(2027, 3, 2))]
    forward = frozenset(items)
    backward = frozenset(reversed(items))
    assert fingerprint_of(forward) == fingerprint_of(backward)


def test_отпечаток_различает_день_и_день_со_временем() -> None:
    assert fingerprint_of(frozenset({Slot(DAY)})) != fingerprint_of(
        frozenset({Slot(DAY, time(0, 0))})
    )


def test_отпечаток_различает_разные_наборы() -> None:
    assert fingerprint_of(slots(23)) != fingerprint_of(slots(23, 24))
    assert fingerprint_of(slots(23)) != fingerprint_of(slots(24))
    assert fingerprint_of(slots_at((23, 10, 30))) != fingerprint_of(slots_at((23, 10, 45)))
    assert fingerprint_of(frozenset()) != fingerprint_of(slots(23))


def test_отпечаток_слитно_записанных_слотов_не_склеивается_с_другим_набором() -> None:
    # «|» разделяет слоты: {10:30, 10:45} и {10:30} + отдельный слот не дают один хеш
    assert fingerprint_of(slots_at((23, 10, 30), (23, 10, 45))) != fingerprint_of(
        slots_at((23, 10, 30)) | slots(24)
    )


def test_отпечаток_смешанного_набора_считается_и_стабилен() -> None:
    mixed = frozenset({Slot(DAY), Slot(DAY, time(9, 0))})
    assert fingerprint_of(mixed) == fingerprint_of(frozenset(reversed(sorted(mixed))))
    assert fingerprint_of(mixed) != fingerprint_of(frozenset({Slot(DAY)}))


@given(st.frozensets(slot_strategy, max_size=8), st.frozensets(slot_strategy, max_size=8))
def test_свойство_отпечаток_равен_тогда_и_только_тогда_когда_равны_наборы(
    left: frozenset[Slot], right: frozenset[Slot]
) -> None:
    assert (fingerprint_of(left) == fingerprint_of(right)) == (left == right)


# --- AvailabilitySnapshot ---


def test_снимок_отвергает_naive_datetime() -> None:
    with pytest.raises(ValueError, match="aware"):
        AvailabilitySnapshot(1, datetime(2026, 11, 12, 15, 0), slots(23), SourceKind.DOM, 5.0)


def test_снимок_принимает_aware_в_любой_зоне() -> None:
    moment = datetime(2026, 11, 12, 12, 0, tzinfo=timezone(timedelta(hours=-3)))
    snapshot = AvailabilitySnapshot(1, moment, slots(23), SourceKind.API, 5.0)
    assert snapshot.observed_at == T0


def test_снимок_сам_считает_отпечаток() -> None:
    found = slots(23, 24)
    snapshot = AvailabilitySnapshot(1, T0, found, SourceKind.DOM, 5.0)
    assert snapshot.fingerprint == fingerprint_of(found)


def test_снимок_пустого_календаря_имеет_непустой_отпечаток() -> None:
    snapshot = AvailabilitySnapshot(1, T0, frozenset(), SourceKind.DOM, 5.0)
    assert snapshot.fingerprint
    assert snapshot.fingerprint == fingerprint_of(frozenset())


def test_снимок_сохраняет_переданный_отпечаток() -> None:
    snapshot = AvailabilitySnapshot(1, T0, slots(23), SourceKind.DOM, 5.0, fingerprint="abc")
    assert snapshot.fingerprint == "abc"


def test_has_slots_отражает_непустоту_множества() -> None:
    assert not AvailabilitySnapshot(1, T0, frozenset(), SourceKind.DOM, 1.0).has_slots
    assert AvailabilitySnapshot(1, T0, slots(23), SourceKind.DOM, 1.0).has_slots


def test_снимок_неизменяем() -> None:
    snapshot = AvailabilitySnapshot(1, T0, slots(23), SourceKind.DOM, 1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.latency_ms = 9.0  # type: ignore[misc]


# --- PollStatsHour.percentile ---


def test_перцентиль_пустого_часа_равен_нулю() -> None:
    assert PollStatsHour(T0).percentile(0.5) == 0.0
    assert PollStatsHour(T0).percentile(0.95) == 0.0


def test_перцентиль_одного_значения_это_оно_же_при_любом_q() -> None:
    stats = PollStatsHour(T0, latencies_ms=[42.0])
    assert [stats.percentile(q) for q in (0.0, 0.5, 0.95, 1.0)] == [42.0] * 4


def test_перцентиль_границы_это_минимум_и_максимум() -> None:
    stats = PollStatsHour(T0, latencies_ms=[30.0, 10.0, 50.0, 20.0, 40.0])
    assert stats.percentile(0.0) == 10.0
    assert stats.percentile(1.0) == 50.0
    assert stats.percentile(0.5) == 30.0


def test_перцентиль_не_требует_отсортированного_ввода_и_не_меняет_его() -> None:
    raw = [5.0, 1.0, 3.0, 2.0, 4.0]
    stats = PollStatsHour(T0, latencies_ms=list(raw))
    assert stats.percentile(0.5) == 3.0
    assert stats.latencies_ms == raw


def test_перцентиль_95_на_ста_значениях_близок_к_верхнему_краю() -> None:
    stats = PollStatsHour(T0, latencies_ms=[float(i) for i in range(1, 101)])
    assert stats.percentile(0.95) in (95.0, 96.0)


def test_перцентиль_вне_диапазона_q_упирается_в_края() -> None:
    stats = PollStatsHour(T0, latencies_ms=[1.0, 2.0, 3.0])
    assert stats.percentile(-1.0) == 1.0
    assert stats.percentile(2.0) == 3.0


@given(
    st.lists(st.floats(0, 1e6, allow_nan=False), min_size=1, max_size=50),
    st.floats(0, 1),
    st.floats(0, 1),
)
def test_свойство_перцентиль_внутри_диапазона_и_монотонен_по_q(
    values: list[float], q1: float, q2: float
) -> None:
    stats = PollStatsHour(T0, latencies_ms=values)
    low, high = sorted((q1, q2))
    assert min(values) <= stats.percentile(low) <= stats.percentile(high) <= max(values)
    assert stats.percentile(low) in values


def test_счётчики_часа_по_умолчанию_нулевые_и_списки_не_общие() -> None:
    first, second = PollStatsHour(T0), PollStatsHour(T0)
    first.latencies_ms.append(1.0)
    assert (first.checks, first.errors, first.relogins) == (0, 0, 0)
    assert second.latencies_ms == []


# --- Alert, результаты, события ---


def test_тревога_по_умолчанию_идёт_во_все_три_канала() -> None:
    alert = Alert(AlertKind.APPEARED, "текст")
    assert alert.channels == frozenset({Channel.TELEGRAM, Channel.NTFY, Channel.CALL})
    assert alert.action is AlertAction.NONE
    assert alert.incident_id is None
    assert (alert.call_text, alert.push_title, alert.push_text, alert.click_url) == ("",) * 4


def test_тревога_и_результат_доставки_и_событие_неизменяемы() -> None:
    alert = Alert(AlertKind.SERVICE, "x")
    result = DeliveryResult(Channel.NTFY, True, 1.0, 1)
    event = AppEvent(T0, "INFO", "code", "msg")
    for item, attr in ((alert, "text"), (result, "ok"), (event, "code")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(item, attr, "другое")


def test_результат_доставки_по_умолчанию_без_ошибки_и_инцидента() -> None:
    result = DeliveryResult(Channel.CALL, False, 12.5, 2)
    assert result.error == ""
    assert result.incident_id is None


def test_цель_наблюдения_неизменяема() -> None:
    target = WatchTarget(1, "n", "https://x", "loc", 6)
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.months_ahead = 1  # type: ignore[misc]


def test_инцидент_открыт_и_не_подтверждён_по_умолчанию() -> None:
    incident = Incident(1, 1, T0, slots(23))
    assert incident.status is IncidentStatus.OPEN
    assert incident.closed_at is None
    assert not incident.acked
    incident.acked_at = T0 + timedelta(seconds=3)
    assert incident.acked


def test_значения_перечислений_зафиксированы_они_лежат_в_бд_и_настройках() -> None:
    assert [c.value for c in Channel] == ["telegram", "ntfy", "call"]
    assert {s.value for s in SourceKind} == {"api", "dom", "sae"}
    assert {s.value for s in PollState} == {
        "running",
        "reauth",
        "backoff",
        "needs_manual",
        "paused",
    }
    assert {k.value for k in AlertKind} == {"appeared", "changed", "service", "unconfirmed", "test"}
    assert {s.value for s in IncidentStatus} == {"open", "closed"}
    assert {a.value for a in AlertAction} == {"none", "ack", "manual_done"}


def test_источник_и_канал_создаются_из_строки() -> None:
    assert Channel("call") is Channel.CALL
    assert SourceKind("sae") is SourceKind.SAE
    with pytest.raises(ValueError):
        Channel("sms")


def test_utc_константа_фейковых_часов_совместима_со_снимком() -> None:
    assert T0.tzinfo is UTC
