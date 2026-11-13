"""JournalFeed (решение R6): каждое событие шины — в журнал наблюдения, окна записи — в openings."""

from __future__ import annotations

from tests.fakes import T0, ManualClock, RecordingJournal, settle, slots, slots_at, snapshot

from rcbot.application.bus import EventBus
from rcbot.application.journal_feed import JournalFeed
from rcbot.domain import events
from rcbot.domain.events import (
    DomainEvent,
    ManualActionRequired,
    PollerStateChanged,
    SlotsAppeared,
    SlotsChanged,
    SlotsGone,
    SourceSwitched,
    UnconfirmedSlotsSeen,
)
from rcbot.domain.models import PollState, SourceKind


class Feed:
    def __init__(self) -> None:
        self.clock = ManualClock()
        self.journal = RecordingJournal()
        self.bus = EventBus()
        JournalFeed(self.journal, self.clock).subscribe(self.bus)

    async def send(self, event: DomainEvent) -> None:
        self.bus.publish(event)
        await settle()


async def test_появление_мест_событие_и_окно_записи() -> None:
    feed = Feed()
    snap = snapshot(slots(24, 23), source=SourceKind.API)
    await feed.send(SlotsAppeared(T0, snap))
    code, message, level, _ = feed.journal.events[0]
    assert (code, level) == ("slots.appeared", "WARNING")
    assert message == "появились места: 2027-02-23, 2027-02-24"  # дни по порядку
    [opening] = feed.journal.openings
    assert (opening.at, opening.kind, opening.slots, opening.source) == (
        T0,
        "появились",
        snap.slots,
        SourceKind.API,
    )
    assert opening.incident_open_sec is None


async def test_несколько_слотов_одного_дня_это_один_день_в_сообщении() -> None:
    feed = Feed()
    await feed.send(SlotsAppeared(T0, snapshot(slots_at((23, 10, 30), (23, 10, 45), (23, 11, 0)))))
    assert feed.journal.events[0][1] == "появились места: 2027-02-23"


async def test_добавились_места_пишут_только_новые() -> None:
    feed = Feed()
    snap = snapshot(slots(23, 25))
    await feed.send(SlotsChanged(T0, snap, slots(25)))
    code, message, level, _ = feed.journal.events[0]
    assert (code, level, message) == ("slots.changed", "WARNING", "добавились места: 2027-02-25")
    [opening] = feed.journal.openings
    assert opening.kind == "добавились" and opening.slots == slots(25)


async def test_пропажа_мест_и_длительность_окна() -> None:
    feed = Feed()
    await feed.send(SlotsAppeared(T0, snapshot(slots(23))))
    await feed.clock.advance(125)
    await feed.send(SlotsGone(T0, snapshot(source=SourceKind.DOM)))
    code, _, level, _ = feed.journal.events[-1]
    assert (code, level) == ("slots.gone", "INFO")
    gone = feed.journal.openings[-1]
    assert gone.kind == "пропали" and gone.slots == frozenset()
    assert gone.incident_open_sec == 125.0 and gone.source is SourceKind.DOM


async def test_изменение_состава_не_сбрасывает_отсчёт_окна() -> None:
    feed = Feed()
    await feed.send(SlotsAppeared(T0, snapshot(slots(23))))
    await feed.clock.advance(40)
    await feed.send(SlotsChanged(T0, snapshot(slots(23, 24)), slots(24)))
    await feed.clock.advance(60)
    await feed.send(SlotsGone(T0, snapshot()))
    assert feed.journal.openings[-1].incident_open_sec == 100.0


async def test_пропажа_без_записанного_появления_длительность_неизвестна() -> None:
    # Бот перезапущен при открытом инциденте: «появились» он не видел
    feed = Feed()
    await feed.send(SlotsGone(T0, snapshot()))
    assert feed.journal.openings[0].incident_open_sec is None


async def test_второе_окно_считается_от_своего_появления() -> None:
    feed = Feed()
    await feed.send(SlotsAppeared(T0, snapshot(slots(23))))
    await feed.clock.advance(30)
    await feed.send(SlotsGone(T0, snapshot()))
    await feed.clock.advance(1000)
    await feed.send(SlotsAppeared(T0, snapshot(slots(24))))
    await feed.clock.advance(7)
    await feed.send(SlotsGone(T0, snapshot()))
    assert [o.incident_open_sec for o in feed.journal.openings if o.kind == "пропали"] == [
        30.0,
        7.0,
    ]
    # повторная пропажа без нового появления снова «неизвестна», а не старое значение
    await feed.send(SlotsGone(T0, snapshot()))
    assert feed.journal.openings[-1].incident_open_sec is None


async def test_смена_состояния() -> None:
    feed = Feed()
    await feed.send(PollerStateChanged(T0, PollState.RUNNING, PollState.BACKOFF, "API ответил 503"))
    assert feed.journal.events == [
        ("state.changed", "running → backoff: API ответил 503", "INFO", {})
    ]
    assert feed.journal.openings == []


async def test_смена_источника() -> None:
    feed = Feed()
    await feed.send(SourceSwitched(T0, SourceKind.API, SourceKind.DOM, "3 неожиданных ответа"))
    code, message, level, _ = feed.journal.events[0]
    assert (code, level) == ("source.switched", "WARNING")
    assert "api → dom" in message and "3 неожиданных ответа" in message


async def test_нужен_шаг_1() -> None:
    feed = Feed()
    await feed.send(ManualActionRequired(T0, "сайт просит шаг 1"))
    code, message, level, _ = feed.journal.events[0]
    assert (code, level) == ("manual.required", "WARNING")
    assert message.endswith("сайт просит шаг 1")


async def test_неподтверждённые_места_SAE() -> None:
    feed = Feed()
    await feed.send(UnconfirmedSlotsSeen(T0, snapshot(slots(23, 24), source=SourceKind.SAE)))
    assert feed.journal.events == [("sae.seen", "SAE: 2 мест (неподтверждённые)", "INFO", {})]
    assert feed.journal.openings == []  # неподтверждённое не попадает в статистику окон


async def test_каждый_вид_доменного_события_разобран_явно() -> None:
    # Запасной ветки «неизвестное событие» нет: новое событие без своей ветки молча пропало бы
    examples: dict[type[DomainEvent], DomainEvent] = {
        SlotsAppeared: SlotsAppeared(T0, snapshot(slots(23))),
        SlotsChanged: SlotsChanged(T0, snapshot(slots(23)), slots(23)),
        SlotsGone: SlotsGone(T0, snapshot()),
        ManualActionRequired: ManualActionRequired(T0, "шаг 1"),
        SourceSwitched: SourceSwitched(T0, SourceKind.API, SourceKind.DOM, "x"),
        PollerStateChanged: PollerStateChanged(T0, PollState.RUNNING, PollState.BACKOFF, "x"),
        UnconfirmedSlotsSeen: UnconfirmedSlotsSeen(T0, snapshot(slots(23))),
    }
    defined = {
        cls
        for cls in vars(events).values()
        if isinstance(cls, type) and issubclass(cls, DomainEvent) and cls is not DomainEvent
    }
    assert defined == set(examples)
    for event in examples.values():
        feed = Feed()
        await feed.send(event)
        assert len(feed.journal.events) == 1, type(event).__name__


def test_доменного_события_SessionExpired_нет_есть_только_исключение() -> None:
    # Одноимённые событие и исключение путали импорты; событие никто не публиковал
    assert not hasattr(events, "SessionExpired")


async def test_каждое_событие_даёт_запись_и_порядок_сохраняется() -> None:
    feed = Feed()
    await feed.send(SlotsAppeared(T0, snapshot(slots(23))))
    feed.bus.publish(PollerStateChanged(T0, PollState.RUNNING, PollState.BACKOFF, "x"))
    feed.bus.publish(SlotsGone(T0, snapshot()))
    await settle()
    assert feed.journal.codes() == ["slots.appeared", "state.changed", "slots.gone"]
