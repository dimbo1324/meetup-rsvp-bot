"""Сценарий 5: неожиданный ответ → карантин → запасной источник (SPEC 2.3, 6.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcbot.domain.models import Channel, PollState, SourceKind
from tests.e2e import site as fake
from tests.e2e.helpers import slot
from tests.e2e.site import FakeSite
from tests.e2e.stand import fake_source
from tests.fakes import slots

LEAKS = [
    "SESSION-1-0",
    "deadbeefcafe0123",
    "hunter22-test",
    "abcd1234efgh",
    "123456789012",
    "owner.test@example.org",
    "+598 99 123 456",
]


def quarantined(stand) -> list[Path]:
    return sorted(stand.paths.quarantine.glob("*.json"))


async def test_три_мусорных_ответа_карантин_один_переход_на_запасной_источник(
    make_stand, site: FakeSite, stand_clock
) -> None:
    fallback = fake_source(stand_clock, slots(), slots(23))
    stand = await make_stand(site=site, fallback=fallback, ladder_age_hours=20)
    site.push(fake.echo_cookies_html(), times=3)

    await stand.checks(3)  # три неожиданных ответа подряд
    assert stand.loop.source_kind is SourceKind.DOM
    api_calls = len(site.calls)

    # три очищенных снимка: ни cookies, ни секретов, ни заголовков
    files = quarantined(stand)
    assert len(files) == 3
    for path in files:
        raw = path.read_text("utf-8")
        record = json.loads(raw)
        assert set(record) == {"at", "source", "status", "content_type", "body"}
        assert record["status"] == 200 and record["content_type"].startswith("text/html")
        assert "***" in record["body"]
        for leak in LEAKS:
            assert leak not in raw, leak

    # служебное сообщение об источнике — одно и только в Telegram
    switched = [m for m in stand.texts(Channel.TELEGRAM) if "Источник данных" in m]
    assert len(switched) == 1 and "api → dom" in switched[0]
    assert all(not stand.sent(c) for c in (Channel.NTFY, Channel.CALL))
    assert [e["code"] for e in stand.journal.read_events()].count("source.switched") == 1

    # дальше опрос идёт через запасной источник; API больше не трогаем, его соединение закрыто
    await stand.advance(1800)
    assert len(site.calls) == api_calls
    assert len(fallback.calls) >= 2
    http = stand.primary._http
    assert http._session is None or http._session.closed
    # в режиме браузера интервал не ниже 60 с, даже если базовый — 20 с
    assert stand.loop.current_interval(stand.clock.now()) == 60
    assert fallback.cookie_history, "запасной источник получил cookies сессии"


async def test_находка_запасного_источника_доходит_до_всех_каналов(
    make_stand, site: FakeSite, stand_clock
) -> None:
    fallback = fake_source(stand_clock, slots(), slots(23, 24))
    stand = await make_stand(site=site, fallback=fallback)
    site.push(fake.html("<html>что-то новое</html>"), times=3)
    await stand.checks(5)

    for channel in Channel:  # первая волна одна; повторы — отдельно
        assert (
            sum(
                1
                for s in stand.sent(channel)
                if s.alert.kind.value == "appeared" and s.attempt == 1
            )
            == 1
        ), channel
    (incident,) = await stand.incidents()
    assert incident.slots == frozenset({slot(23), slot(24)})
    assert stand.journal.read_openings()[0]["источник"] == "dom"


@pytest.mark.parametrize(
    "reply",
    [
        fake.html("<html>Mantenimiento</html>"),
        fake.json_text("{сломанный json"),
        fake.json_text('{"unexpected": true}'),
        fake.json_text('{"slots": [1, 2]}'),
    ],
    ids=["html", "невалидный-json", "чужая-структура", "не-строки"],
)
async def test_каждый_вид_неожиданного_ответа_уходит_в_карантин(
    make_stand, site: FakeSite, reply
) -> None:
    stand = await make_stand(site=site)
    site.push(reply)
    await stand.next_check()

    assert len(quarantined(stand)) == 1
    assert stand.journal.read_checks()[-1]["error_kind"] == "UnexpectedResponse"
    assert stand.loop.status.state is PollState.RUNNING  # одиночный сбой без паники
    assert stand.loop.source_kind is SourceKind.API


async def test_успешная_проверка_обнуляет_серию_неожиданных_ответов(
    make_stand, site: FakeSite, stand_clock
) -> None:
    stand = await make_stand(
        site=site, fallback=fake_source(stand_clock, slots()), ladder_age_hours=20
    )
    site.push(fake.html("<html>a</html>"), fake.html("<html>b</html>"))
    await stand.checks(2)
    await stand.next_check()  # хороший ответ
    site.push(fake.html("<html>c</html>"), fake.html("<html>d</html>"))
    await stand.checks(2)

    assert stand.loop.source_kind is SourceKind.API, "два плюс два — не три подряд"


async def test_без_запасного_источника_опрос_продолжается_в_backoff(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    site.push(fake.html("<html>x</html>"), times=4)
    await stand.checks(5)
    assert stand.loop.source_kind is SourceKind.API
    assert stand.loop.status.state is PollState.RUNNING  # пятый запрос прошёл
