"""Детектор (SPEC 2.3, «Модель инцидента»): появление, изменение, исчезновение, дребезг."""

from __future__ import annotations

from datetime import timedelta

import pytest

from rcbot.domain.detector import EMPTY_CHECKS_TO_CLOSE, Detector
from rcbot.domain.events import SlotsAppeared, SlotsChanged, SlotsGone
from rcbot.domain.models import AvailabilitySnapshot, Slot
from tests.fakes import T0, slots, slots_at, snapshot


def _at(step: int, found: frozenset[Slot]) -> AvailabilitySnapshot:
    return snapshot(found, at=T0 + timedelta(seconds=20 * step))


def _feed(detector: Detector, *sets: frozenset[Slot]) -> list[object]:
    return [detector.compare(_at(i, found)) for i, found in enumerate(sets)]


EMPTY: frozenset[Slot] = frozenset()


def test_порог_закрытия_это_три_проверки_подряд() -> None:
    assert EMPTY_CHECKS_TO_CLOSE == 3
    assert Detector().empty_checks_to_close == 3


def test_пустой_календарь_при_закрытом_инциденте_ничего_не_даёт() -> None:
    detector = Detector()
    assert _feed(detector, EMPTY, EMPTY, EMPTY, EMPTY) == [None] * 4
    assert not detector.incident_open
    assert detector.empty_streak == 0


def test_первое_непустое_множество_открывает_инцидент_событием_appeared() -> None:
    detector = Detector()
    snap = _at(0, slots(23, 24))
    event = detector.compare(snap)
    assert isinstance(event, SlotsAppeared)
    assert event.snapshot is snap
    assert event.at == snap.observed_at
    assert detector.incident_open
    assert detector.slots == slots(23, 24)


def test_тот_же_отпечаток_ничего_не_порождает() -> None:
    detector = Detector()
    events = _feed(detector, slots(23), slots(23), slots(23))
    assert isinstance(events[0], SlotsAppeared)
    assert events[1:] == [None, None]


def test_слоты_пришли_в_другом_порядке_это_не_изменение() -> None:
    detector = Detector()
    first = slots_at((23, 10, 30), (23, 10, 45))
    second = frozenset(reversed(sorted(first)))
    assert _feed(detector, first, second)[1] is None


def test_новые_слоты_в_открытом_инциденте_дают_changed_с_добавленными() -> None:
    detector = Detector()
    _feed(detector, slots(23))
    snap = _at(1, slots(23, 24, 25))
    event = detector.compare(snap)
    assert isinstance(event, SlotsChanged)
    assert event.added == slots(24, 25)
    assert event.snapshot is snap
    assert detector.slots == slots(23, 24, 25)
    assert detector.incident_open


def test_смена_только_времени_в_тот_же_день_это_изменение() -> None:
    detector = Detector()
    detector.compare(_at(0, slots_at((23, 10, 30))))
    event = detector.compare(_at(1, slots_at((23, 10, 30), (23, 11, 0))))
    assert isinstance(event, SlotsChanged)
    assert event.added == slots_at((23, 11, 0))


def test_день_без_времени_и_тот_же_день_со_временем_это_разные_слоты() -> None:
    detector = Detector()
    detector.compare(_at(0, slots(23)))
    event = detector.compare(_at(1, slots(23) | slots_at((23, 10, 30))))
    assert isinstance(event, SlotsChanged)
    assert event.added == slots_at((23, 10, 30))


def test_пропажа_части_слотов_тихая_но_множество_обновляется() -> None:
    detector = Detector()
    _feed(detector, slots(23, 24, 25))
    assert detector.compare(_at(1, slots(23))) is None
    assert detector.slots == slots(23)
    assert detector.incident_open
    assert detector.empty_streak == 0


def test_вернувшийся_после_пропажи_слот_снова_тревога_changed() -> None:
    # Слот могли занять и освободить (отмена записи): для владельца это новое окно. Решение по
    # SPEC 2.3 «добавились слоты относительно последнего увиденного множества».
    detector = Detector()
    _feed(detector, slots(23, 24))
    assert detector.compare(_at(1, slots(23))) is None
    event = detector.compare(_at(2, slots(23, 24)))
    assert isinstance(event, SlotsChanged)
    assert event.added == slots(24)


def test_полная_замена_множества_считается_изменением_с_новыми_слотами() -> None:
    detector = Detector()
    _feed(detector, slots(23))
    event = detector.compare(_at(1, slots(24)))
    assert isinstance(event, SlotsChanged)
    assert event.added == slots(24)
    assert detector.slots == slots(24)


@pytest.mark.parametrize("empties", [1, 2])
def test_меньше_трёх_пустых_проверок_инцидент_не_закрывается(empties: int) -> None:
    detector = Detector()
    events = _feed(detector, slots(23), *[EMPTY] * empties)
    assert events[1:] == [None] * empties
    assert detector.incident_open
    assert detector.empty_streak == empties
    assert detector.slots == slots(23)


def test_ровно_третья_пустая_проверка_закрывает_инцидент_событием_gone() -> None:
    detector = Detector()
    events = _feed(detector, slots(23), EMPTY, EMPTY, EMPTY)
    assert events[1:3] == [None, None]
    gone = events[3]
    assert isinstance(gone, SlotsGone)
    assert gone.at == T0 + timedelta(seconds=60)
    assert not detector.incident_open
    assert detector.slots == frozenset()
    assert detector.empty_streak == 0
    assert detector.last_fingerprint == ""


def test_четвёртая_пустая_проверка_после_закрытия_тихая() -> None:
    detector = Detector()
    events = _feed(detector, slots(23), EMPTY, EMPTY, EMPTY, EMPTY, EMPTY)
    assert [type(e) for e in events if e is not None] == [SlotsAppeared, SlotsGone]
    assert events[4:] == [None, None]


def test_непустая_проверка_между_пустыми_сбрасывает_счётчик() -> None:
    detector = Detector()
    events = _feed(detector, slots(23), EMPTY, EMPTY, slots(23), EMPTY, EMPTY)
    assert events[1:] == [None] * 5
    assert detector.incident_open
    assert detector.empty_streak == 2


def test_дребезг_с_теми_же_слотами_не_даёт_повторной_тревоги() -> None:
    detector = Detector()
    events = _feed(detector, slots(23, 24), EMPTY, slots(23, 24), EMPTY, EMPTY, slots(23, 24))
    assert isinstance(events[0], SlotsAppeared)
    assert events[1:] == [None] * 5


def test_после_пропажи_и_возврата_с_дребезгом_новые_слоты_дают_changed() -> None:
    detector = Detector()
    events = _feed(detector, slots(23), EMPTY, slots(23, 24))
    assert isinstance(events[2], SlotsChanged)
    assert events[2].added == slots(24)  # type: ignore[attr-defined]


def test_после_закрытия_новое_появление_это_снова_appeared() -> None:
    detector = Detector()
    events = _feed(detector, slots(23), EMPTY, EMPTY, EMPTY, slots(23))
    assert isinstance(events[3], SlotsGone)
    assert isinstance(events[4], SlotsAppeared)
    assert detector.slots == slots(23)
    assert detector.incident_open


def test_после_закрытия_те_же_слоты_не_считаются_старым_отпечатком() -> None:
    # Отпечаток сбрасывается на пустой проверке: тот же набор после закрытия — новый инцидент.
    detector = Detector()
    events = _feed(detector, slots(23), EMPTY, EMPTY, EMPTY, slots(23), slots(23))
    assert isinstance(events[4], SlotsAppeared)
    assert events[5] is None


def test_порог_закрытия_настраиваемый() -> None:
    detector = Detector(empty_checks_to_close=1)
    events = _feed(detector, slots(23), EMPTY)
    assert isinstance(events[1], SlotsGone)
    detector = Detector(empty_checks_to_close=5)
    events = _feed(detector, slots(23), *[EMPTY] * 5)
    assert events[1:5] == [None] * 4
    assert isinstance(events[5], SlotsGone)


# --- restore ---


def test_restore_с_слотами_продолжает_инцидент_без_повторной_тревоги() -> None:
    detector = Detector()
    detector.restore(slots(23, 24))
    assert detector.incident_open
    assert detector.slots == slots(23, 24)
    assert detector.compare(_at(0, slots(23, 24))) is None


def test_restore_затем_новые_слоты_дают_changed_только_с_новыми() -> None:
    detector = Detector()
    detector.restore(slots(23))
    event = detector.compare(_at(0, slots(23, 24)))
    assert isinstance(event, SlotsChanged)
    assert event.added == slots(24)


def test_restore_затем_три_пустые_проверки_закрывают_инцидент() -> None:
    detector = Detector()
    detector.restore(slots(23))
    events = _feed(detector, EMPTY, EMPTY, EMPTY)
    assert events[:2] == [None, None]
    assert isinstance(events[2], SlotsGone)


def test_restore_с_пустым_множеством_оставляет_инцидент_закрытым() -> None:
    detector = Detector()
    detector.restore(frozenset())
    assert not detector.incident_open
    assert isinstance(detector.compare(_at(0, slots(23))), SlotsAppeared)


def test_restore_сбрасывает_накопленное_состояние() -> None:
    detector = Detector()
    _feed(detector, slots(23), EMPTY, EMPTY)
    assert detector.empty_streak == 2
    detector.restore(slots(26))
    assert detector.empty_streak == 0
    assert detector.slots == slots(26)
    assert detector.last_fingerprint == ""


def test_restore_пустым_закрывает_ранее_открытый_инцидент() -> None:
    detector = Detector()
    _feed(detector, slots(23))
    detector.restore(frozenset())
    assert not detector.incident_open
    assert detector.slots == frozenset()
