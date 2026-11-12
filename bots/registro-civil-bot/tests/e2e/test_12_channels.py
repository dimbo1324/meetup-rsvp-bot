"""Каналы уведомлений на всём графе: настоящие ntfy и CallMeBot против локального HTTPS-сервера,
изоляция сбоев, приватность (SPEC 2.4, 2.6, инвариант 6)."""

from __future__ import annotations

import asyncio
import re

import pytest

from rcbot.application import alerting
from rcbot.application.messages import CALL_TEXT_EN, CALL_TEXT_RU
from rcbot.domain.models import AlertKind, Channel
from tests.e2e import site as fake
from tests.e2e.helpers import D23
from tests.e2e.site import ChannelsSite, FakeSite
from tests.e2e.stand import CALL_USER, NTFY_TOPIC, TRAMITE_ID
from tests.fakes import AGENDA_URL, FakeNotifier


async def appear(stand, site: FakeSite) -> None:
    site.slots = list(D23)
    await stand.next_check()


async def test_ntfy_получает_json_по_spec_только_с_датами_без_номера_заявки(
    make_stand, site: FakeSite, channels_site: ChannelsSite
) -> None:
    stand = await make_stand(site=site, channels=channels_site)
    await appear(stand, site)

    (request,) = channels_site.of("ntfy")
    body = request.body
    assert body["topic"] == NTFY_TOPIC and body["priority"] == 5
    assert body["tags"] == ["rotating_light"]
    assert "click" not in body  # решение R10: ссылка с номером этапа только в Telegram
    visible = f"{body['title']} {body['message']}"
    assert re.search(r"23\.02", visible) and "Срочно" in visible
    assert TRAMITE_ID not in visible and "Заявка" not in visible and "http" not in visible
    assert request.headers["Content-Type"].startswith("application/json")


async def test_звонок_callmebot_читает_общую_фразу_без_личных_данных(
    make_stand, site: FakeSite, channels_site: ChannelsSite
) -> None:
    stand = await make_stand(site=site, channels=channels_site)
    await appear(stand, site)

    (request,) = channels_site.of("callmebot")
    assert request.query == {
        "user": CALL_USER,
        "text": CALL_TEXT_RU,
        "lang": "ru-RU-Standard-A",
        "rpt": "2",
    }
    assert len(request.query["text"]) <= 256
    assert TRAMITE_ID not in str(request.query) and AGENDA_URL not in str(request.query)


async def test_служебные_сообщения_не_доходят_до_ntfy_и_звонка(
    make_stand, site: FakeSite, channels_site: ChannelsSite
) -> None:
    stand = await make_stand(site=site, channels=channels_site)
    site.push(fake.status(429))
    await stand.next_check()  # BACKOFF
    site.push(fake.html(fake.STEP1_HTML))
    await stand.advance(900)  # шаг 1
    site.push(fake.html("<html>х</html>"), times=3)
    await stand.checks(3)

    assert stand.texts(Channel.TELEGRAM), "владелец о проблемах знает"
    assert channels_site.requests == [], "ни одного запроса к ntfy и CallMeBot"


async def test_ntfy_отвечает_500_доставка_не_удалась_остальные_каналы_идут(
    make_stand, site: FakeSite, channels_site: ChannelsSite
) -> None:
    channels_site.ntfy_status = 500
    stand = await make_stand(site=site, channels=channels_site)
    await appear(stand, site)

    assert stand.sent(Channel.NTFY)[0].result.ok is False
    assert stand.sent(Channel.NTFY)[0].result.error == "HTTP 500"
    assert stand.sent(Channel.TELEGRAM)[0].result.ok and stand.sent(Channel.CALL)[0].result.ok
    deliveries = {d.channel: d for d in await stand.storage.incidents.deliveries_for(1)}
    assert deliveries[Channel.NTFY].ok is False and deliveries[Channel.NTFY].error == "HTTP 500"
    assert deliveries[Channel.CALL].ok and deliveries[Channel.TELEGRAM].ok
    # сбой канала не отменяет его повторов: ntfy пробует снова через 2 минуты
    await stand.advance(120)
    assert stand.notifiers[Channel.NTFY].attempts == [1, 2]
    assert len(channels_site.of("ntfy")) == 2


async def test_callmebot_ошибка_русского_голоса_один_запасной_английский_звонок(
    make_stand, site: FakeSite, channels_site: ChannelsSite
) -> None:
    channels_site.call_replies.append((200, "Error: you need to authorize"))
    stand = await make_stand(site=site, channels=channels_site)
    await appear(stand, site)

    first, second = channels_site.of("callmebot")
    assert first.query["lang"] == "ru-RU-Standard-A"
    assert second.query["lang"] == "en-US-Standard-B" and second.query["text"] == CALL_TEXT_EN
    assert stand.sent(Channel.CALL)[0].result.ok, "английский звонок прошёл — доставлено"


async def test_callmebot_недоступен_обе_попытки_записаны_как_отказ(
    make_stand, site: FakeSite, channels_site: ChannelsSite
) -> None:
    channels_site.call_replies.extend([(503, "down"), (503, "down")])
    stand = await make_stand(site=site, channels=channels_site)
    await appear(stand, site)

    assert len(channels_site.of("callmebot")) == 2
    result = stand.sent(Channel.CALL)[0].result
    assert not result.ok and result.error == "HTTP 503"
    assert stand.sent(Channel.NTFY)[0].result.ok


# --- изоляция сбоев канала ---


async def test_канал_упал_другой_завис_третий_не_задержан(
    make_stand, site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alerting, "SEND_TIMEOUT_SEC", 0.05)  # настоящее время: asyncio.timeout
    never = asyncio.Event()

    async def hang() -> None:
        await never.wait()

    notifiers = {
        Channel.NTFY: FakeNotifier(Channel.NTFY, raises=RuntimeError("канал упал")),
        Channel.CALL: FakeNotifier(Channel.CALL, delay=hang),
    }
    stand = await make_stand(site=site, notifiers=notifiers)
    await appear(stand, site)

    telegram = stand.sent(Channel.TELEGRAM)[0]
    assert telegram.result.ok
    assert telegram.perf_end - telegram.perf_start < 0.05, "Telegram не ждёт соседей"
    deliveries = {d.channel: d for d in await stand.storage.incidents.deliveries_for(1)}
    assert deliveries[Channel.NTFY].error == "RuntimeError" and not deliveries[Channel.NTFY].ok
    assert deliveries[Channel.CALL].error == "таймаут" and not deliveries[Channel.CALL].ok
    assert deliveries[Channel.TELEGRAM].ok
    codes = [e for e in stand.journal.read_events() if e["code"] == "delivery"]
    assert [e["level"] for e in codes if e["data"]["channel"] == "ntfy"] == ["WARNING"]
    assert stand.alerts.repeating(), "повторы продолжаются несмотря на сбои"
    never.set()


async def test_тревога_сразу_во_все_каналы_когда_telegram_отказывает(
    make_stand, site: FakeSite, channels_site: ChannelsSite
) -> None:
    notifiers = {Channel.TELEGRAM: FakeNotifier(Channel.TELEGRAM, ok=False, error="нет связи")}
    stand = await make_stand(site=site, channels=channels_site, notifiers=notifiers)
    await appear(stand, site)
    assert len(channels_site.of("ntfy")) == 1 and len(channels_site.of("callmebot")) == 1
    assert stand.sent(Channel.TELEGRAM)[0].alert.kind is AlertKind.APPEARED


# --- приватность на всём пути ---


async def run_everything(stand, site: FakeSite) -> None:
    """Вход, тревога, мусорная страница с эхом cookies, 429, шаг 1, истечение сессии, остановка."""
    stand.journal.attach_log_handler()
    await appear(stand, site)
    site.push(fake.echo_cookies_html(), fake.status(429), fake.html(fake.STEP1_HTML))
    await stand.checks(3)
    site.expire_sessions()
    await stand.advance(900)
    await stand.controller.ack()
    await stand.controller.cleanup()
    await stand.stop()  # БД закрыта, журналы сброшены


def scan(root, needles: list[str], *, skip_db: bool) -> list[str]:
    found = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix == ".ini" or (skip_db and "sqlite" in path.name):
            continue
        raw = path.read_bytes()
        found += [f"{n!r} в {path.name}" for n in needles if n.encode() in raw]
    return found


SECRETS = [
    "SESSION-1-0",
    "SESSION-2-1",
    TRAMITE_ID,
    NTFY_TOPIC,
    "hunter22-test",
    "deadbeefcafe0123",
]


async def test_ни_cookies_ни_номера_заявки_ни_темы_ntfy_нет_на_диске(
    make_stand, site: FakeSite, channels_site: ChannelsSite, tmp_path
) -> None:
    stand = await make_stand(site=site, channels=channels_site)
    await run_everything(stand, site)

    assert scan(tmp_path, SECRETS, skip_db=False) == []
    # номер этапа (часть адреса заявки) не должен быть ни в журнале, ни в логе, ни в карантине
    assert scan(tmp_path, ["TEST-ETAPA"], skip_db=True) == []
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(files) >= 6, "просмотрены журналы, лог, карантин и база"


async def test_номер_этапа_не_попадает_в_базу(make_stand, site: FakeSite, tmp_path) -> None:
    stand = await make_stand(site=site)
    await run_everything(stand, site)
    assert scan(tmp_path, ["TEST-ETAPA"], skip_db=False) == []
