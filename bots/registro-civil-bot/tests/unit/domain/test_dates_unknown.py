"""Места есть, даты не разобраны (R12): снимок, детектор, текст тревоги, сборка источника SAE."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from rcbot import bootstrap
from rcbot.application.messages import DATES_UNKNOWN_LINE, DATES_UNKNOWN_PUSH, MessageComposer
from rcbot.bootstrap import build_core
from rcbot.config.paths import Paths
from rcbot.domain.detector import Detector
from rcbot.domain.events import SlotsAppeared, SlotsChanged, SlotsGone
from rcbot.domain.models import AlertKind, AvailabilitySnapshot, SourceKind
from rcbot.infrastructure.gubuy.sae_source import SaeSource
from tests.fakes import AGENDA_URL, T0, slots, snapshot

TZ = ZoneInfo("America/Montevideo")


def open_without_dates(seconds: int = 0) -> AvailabilitySnapshot:
    return AvailabilitySnapshot(
        1, T0 + timedelta(seconds=seconds), frozenset(), SourceKind.SAE, 50.0, dates_unknown=True
    )


def empty(seconds: int = 0) -> AvailabilitySnapshot:
    return AvailabilitySnapshot(
        1, T0 + timedelta(seconds=seconds), frozenset(), SourceKind.SAE, 5.0
    )


def test_снимок_без_дат_открыт_но_слотов_нет() -> None:
    shot = open_without_dates()
    assert shot.is_open and not shot.has_slots
    assert empty().is_open is False


def test_признак_без_дат_при_известных_слотах_не_меняет_отпечаток() -> None:
    known = slots(23)
    with_flag = AvailabilitySnapshot(1, T0, known, SourceKind.SAE, 1.0, dates_unknown=True)
    assert (
        with_flag.fingerprint == AvailabilitySnapshot(1, T0, known, SourceKind.SAE, 1.0).fingerprint
    )


def test_детектор_открывает_инцидент_по_снимку_без_дат() -> None:
    detector = Detector()
    assert isinstance(detector.compare(open_without_dates()), SlotsAppeared)
    assert detector.compare(open_without_dates(20)) is None  # повтор того же состояния молчит


def test_инцидент_без_дат_закрывается_тремя_пустыми_проверками() -> None:
    detector = Detector()
    detector.compare(open_without_dates())
    events = [detector.compare(empty(20 * i)) for i in range(1, 4)]
    assert events[:2] == [None, None] and isinstance(events[2], SlotsGone)


def test_когда_даты_станут_известны_это_новые_места() -> None:
    detector = Detector()
    detector.compare(open_without_dates())
    later = snapshot(slots(23), at=T0 + timedelta(seconds=20))
    assert isinstance(detector.compare(later), SlotsChanged)


def test_текст_тревоги_без_дат_просит_проверить_запись() -> None:
    alert = MessageComposer(TZ, AGENDA_URL).slots_alert(open_without_dates(), T0)
    assert alert.kind is AlertKind.APPEARED
    assert DATES_UNKNOWN_LINE in alert.text and AGENDA_URL in alert.text
    assert alert.push_text == DATES_UNKNOWN_PUSH
    assert alert.call_text  # звонок тоже идёт: это настоящая тревога


def test_текст_тревоги_с_датами_не_упоминает_неразобранные_даты() -> None:
    alert = MessageComposer(TZ, AGENDA_URL).slots_alert(snapshot(slots(23)), T0)
    assert DATES_UNKNOWN_LINE not in alert.text and "Свободные даты" in alert.push_text


async def test_источник_один_страница_sae(paths: Paths) -> None:
    source = bootstrap.build_source(build_core(console=False))
    try:
        assert isinstance(source, SaeSource) and source.kind is SourceKind.SAE
    finally:
        await source.close()
