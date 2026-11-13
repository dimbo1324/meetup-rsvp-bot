"""Stateful-тест детектора на hypothesis: произвольные последовательности проверок и перезапусков.

Независимая от реализации модель — SPEC 2.3: события допустимы только в своих переходах;
`SlotsGone` — не раньше трёх пустых проверок подряд; после него состояние чистое.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from rcbot.domain.detector import Detector
from rcbot.domain.events import DomainEvent, SlotsAppeared, SlotsChanged, SlotsGone
from rcbot.domain.models import Slot
from tests.fakes import T0, snapshot

# Малая вселенная слотов: частые пересечения наборов, день без времени и со временем — вперемешку
UNIVERSE = [
    Slot(date(2027, 2, 23)),
    Slot(date(2027, 2, 23), time(10, 30)),
    Slot(date(2027, 2, 23), time(10, 45)),
    Slot(date(2027, 2, 24)),
    Slot(date(2027, 2, 24), time(9, 0)),
    Slot(date(2027, 3, 1), time(12, 0)),
]
subsets = st.frozensets(st.sampled_from(UNIVERSE), max_size=len(UNIVERSE))
non_empty = subsets.filter(bool)


def expected_new(found: frozenset[Slot], previous: frozenset[Slot]) -> frozenset[Slot]:
    """Новое — это слот, для которого нет известного слота того же дня с тем же временем;
    если у одного из двух время неизвестно, сравнение идёт по дню (смена источника API/DOM)."""
    result = set()
    for slot in found:
        same_day = [p for p in previous if p.day == slot.day]
        known = any(p.at == slot.at or p.at is None or slot.at is None for p in same_day)
        if not known:
            result.add(slot)
    return frozenset(result)


class DetectorMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.detector = Detector()
        self.step = 0
        self.was_open = False
        self.known: frozenset[Slot] = frozenset()  # последнее непустое множество в инциденте
        self.empties = 0  # пустых проверок подряд с последнего непустого снимка
        self.appeared = 0
        self.gone = 0

    def _observe(self, found: frozenset[Slot]) -> DomainEvent | None:
        self.step += 1
        snap = snapshot(found, at=T0 + timedelta(seconds=20 * self.step))
        event = self.detector.compare(snap)
        if event is not None:
            assert event.at == snap.observed_at
        return event

    @rule(found=non_empty)
    def непустая_проверка(self, found: frozenset[Slot]) -> None:
        was_open, previous = self.was_open, self.known
        event = self._observe(found)
        self.empties = 0
        if not was_open:
            assert isinstance(event, SlotsAppeared), "первое непустое множество — это появление"
            assert event.snapshot.slots == found
            self.appeared += 1
        elif expected_new(found, previous):
            assert isinstance(event, SlotsChanged), "новые слоты в инциденте — это изменение"
            assert event.added == expected_new(found, previous)
        else:
            assert event is None, "нет новых слотов — тревоги нет (пропажа части тихая)"
        self.was_open, self.known = True, found

    @rule()
    def пустая_проверка(self) -> None:
        was_open = self.was_open
        event = self._observe(frozenset())
        if not was_open:
            assert event is None
            return
        self.empties += 1
        if self.empties >= 3:
            assert isinstance(event, SlotsGone)
            assert self.empties == 3, "закрытие ровно на третьей пустой, не позже"
            self.gone += 1
            self.was_open, self.known, self.empties = False, frozenset(), 0
        else:
            assert event is None, "SlotsGone не раньше трёх пустых проверок подряд"

    @rule(found=subsets)
    def перезапуск_с_восстановлением(self, found: frozenset[Slot]) -> None:
        self.detector.restore(found)
        if found and not self.was_open:
            self.appeared += 1  # инцидент продолжен из БД без события
        elif not found and self.was_open:
            self.gone += 1  # инцидент закрыт восстановлением пустого состояния
        self.was_open, self.known, self.empties = bool(found), found, 0

    @invariant()
    def состояние_совпадает_с_моделью(self) -> None:
        assert self.detector.incident_open == self.was_open
        assert self.detector.slots == (self.known if self.was_open else frozenset())

    @invariant()
    def открыт_тогда_и_только_тогда_когда_есть_слоты(self) -> None:
        assert self.detector.incident_open == bool(self.detector.slots)

    @invariant()
    def после_закрытия_состояние_чистое(self) -> None:
        if not self.detector.incident_open:
            assert self.detector.slots == frozenset()
            assert self.detector.empty_streak == 0

    @invariant()
    def счётчик_пустых_не_превышает_порога(self) -> None:
        assert 0 <= self.detector.empty_streak < self.detector.empty_checks_to_close

    @invariant()
    def открытий_ровно_столько_сколько_закрытий_плюс_текущий(self) -> None:
        assert self.appeared == self.gone + (1 if self.was_open else 0)


TestDetectorStateful = DetectorMachine.TestCase
