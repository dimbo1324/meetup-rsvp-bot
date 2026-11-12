"""Тексты Telegram-интерфейса (SPEC 2.5): чистые функции данных в строки."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from rcbot.application.control import IncidentView, StatusView
from rcbot.application.status import PollStatus
from rcbot.bot import texts
from rcbot.domain.models import (
    AppEvent,
    Channel,
    DeliveryResult,
    Incident,
    IncidentStatus,
    PollState,
    SourceKind,
)
from tests.fakes import T0, slots, slots_at
from tests.unit.bot.harness import TZ

MB = 1024 * 1024


def view(**changes: object) -> StatusView:
    status = PollStatus(started_at=T0 - timedelta(hours=26, minutes=3))
    base = StatusView(
        status=status,
        source=SourceKind.DOM,
        interval_sec=60.0,
        base_interval_sec=20.0,
        months_ahead=6,
        paused_until=None,
        incident=None,
        repeating=False,
        logins_last_hour=2,
        quiet_remaining_sec=0.0,
        channels_off=frozenset(),
        folder_bytes=12 * MB,
        journal_bytes=3 * 1024,
        hung_reason="",
        now=T0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def with_status(**changes: object) -> StatusView:
    return view(status=replace(view().status, **changes))  # type: ignore[arg-type]


# --- human_size ---


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, "—"),
        (0, "0 Б"),
        (1, "1 Б"),
        (1023, "1023 Б"),
        (1024, "1.0 КБ"),
        (1536, "1.5 КБ"),
        (MB, "1.0 МБ"),
        (300 * MB, "300.0 МБ"),
        (1024 * MB, "1.0 ГБ"),
        (5 * 1024 * MB, "5.0 ГБ"),
        (5000 * 1024 * MB, "5000.0 ГБ"),  # дальше ГБ единиц нет
    ],
)
def test_human_size(size: int | None, expected: str) -> None:
    assert texts.human_size(size) == expected


# --- status_text ---


@pytest.mark.parametrize("state", list(PollState))
def test_для_каждого_состояния_опроса_есть_название(state: PollState) -> None:
    text = texts.status_text(with_status(state=state), TZ)
    assert text.startswith(f"Состояние: {texts.STATE_NAMES[state]}")


def test_названия_состояний_понятны_и_различны() -> None:
    assert set(texts.STATE_NAMES) == set(PollState)
    assert len(set(texts.STATE_NAMES.values())) == len(PollState)


def test_обычный_статус_содержит_все_строки_spec() -> None:
    text = texts.status_text(view(), TZ)
    assert text.splitlines() == [
        "Состояние: работает",
        "Источник: DOM",
        "Интервал: ≈ 60 с (базовый 20 с)",
        "Месяцев вперёд: 6",
        "Последняя проверка: ещё не было",
        "Время ответа: последнее 0 мс, медиана 0 мс",
        "Проверок: 0, ошибок: 0, входов за час: 2",
        "Работает: 1 д 2 ч 3 мин",
        "Размер папки: 12.0 МБ",
        "Журнал на рабочем столе: 3.0 КБ",
    ]


def test_причина_показывается_только_не_в_рабочем_состоянии() -> None:
    backoff = texts.status_text(
        with_status(state=PollState.BACKOFF, state_reason="сайт ответил 429"), TZ
    )
    assert "Причина: сайт ответил 429" in backoff
    running = texts.status_text(with_status(state_reason="старая причина"), TZ)
    assert "Причина" not in running


def test_состояние_ждёт_шага_1_называется_прямо() -> None:
    text = texts.status_text(with_status(state=PollState.NEEDS_MANUAL), TZ)
    assert "шаг 1" in text.splitlines()[0]


def test_пауза_показывается_в_часовом_поясе_владельца() -> None:
    until = datetime(2026, 11, 13, 2, 30, tzinfo=T0.tzinfo)  # 02:30 UTC = 23:30 накануне в UTC−3
    text = texts.status_text(view(paused_until=until, status=PollStatus(T0, PollState.PAUSED)), TZ)
    assert "Пауза до 12.11 23:30" in text
    assert "Состояние: на паузе" in text


def test_без_паузы_строки_о_паузе_нет() -> None:
    assert "Пауза до" not in texts.status_text(view(), TZ)


def test_завис_цикл_это_первая_строка() -> None:
    text = texts.status_text(view(hung_reason="проверка идёт 200 с"), TZ)
    first = text.splitlines()[0]
    assert first == "⚠️ Цикл опроса завис: проверка идёт 200 с"
    assert text.splitlines()[1].startswith("Состояние:")


def test_без_зависания_предупреждения_нет() -> None:
    assert "завис" not in texts.status_text(view(hung_reason=""), TZ)


def test_выключенные_каналы_перечислены_по_именам() -> None:
    text = texts.status_text(view(channels_off=frozenset({Channel.NTFY, Channel.CALL})), TZ)
    line = next(x for x in text.splitlines() if x.startswith("Выключены"))
    assert "ntfy" in line and "звонок" in line and "Telegram" not in line


def test_включены_все_каналы_строки_нет() -> None:
    assert "Выключены" not in texts.status_text(view(), TZ)


def test_открытый_инцидент_с_числом_мест_и_подтверждением() -> None:
    incident = Incident(3, 1, T0, slots(23, 24))
    text = texts.status_text(view(incident=incident), TZ)
    assert "Открыт инцидент №3: 2 мест, тревога не подтверждена" in text
    incident.acked_at = T0
    assert "тревога подтверждена" in texts.status_text(view(incident=incident), TZ)


def test_отложенный_вход_показывается_в_минутах() -> None:
    assert "Повторный вход отложен ещё на 5 мин (D5)" in texts.status_text(
        view(quiet_remaining_sec=300), TZ
    )
    assert "отложен" not in texts.status_text(view(quiet_remaining_sec=0), TZ)


def test_последняя_ошибка_со_сроком() -> None:
    text = texts.status_text(
        with_status(last_error="сайт недоступен", last_error_at=T0 - timedelta(seconds=45)), TZ
    )
    assert "Последняя ошибка: сайт недоступен (45 с назад)" in text


def test_ошибки_нет_строки_нет() -> None:
    assert "Последняя ошибка" not in texts.status_text(view(), TZ)


def test_последняя_проверка_секунды_и_минуты() -> None:
    near = with_status(last_check_at=T0 - timedelta(seconds=119))
    far = with_status(last_check_at=T0 - timedelta(seconds=120))
    assert "Последняя проверка: 119 с назад" in texts.status_text(near, TZ)
    assert "Последняя проверка: 2 мин назад" in texts.status_text(far, TZ)


def test_задержки_последняя_и_медиана() -> None:
    text = texts.status_text(
        with_status(last_latency_ms=420.4, recent_latencies=[900.0, 100.0, 300.0]), TZ
    )
    assert "Время ответа: последнее 420 мс, медиана 300 мс" in text


def test_счётчики_проверок_и_ошибок() -> None:
    text = texts.status_text(with_status(checks_total=4300, errors_total=12), TZ)
    assert "Проверок: 4300, ошибок: 12, входов за час: 2" in text


@pytest.mark.parametrize(
    ("folder", "warned"),
    [(None, False), (0, False), (300 * MB, False), (300 * MB + 1, True), (900 * MB, True)],
)
def test_предупреждение_о_300_мегабайтах_только_выше_порога(
    folder: int | None, warned: bool
) -> None:
    text = texts.status_text(view(folder_bytes=folder), TZ)
    assert ("⚠️ Папка бота больше 300 МБ: /cleanup" in text) is warned


def test_размеры_неизвестны_показываются_прочерком() -> None:
    text = texts.status_text(view(folder_bytes=None, journal_bytes=None), TZ)
    assert "Размер папки: —" in text and "Журнал на рабочем столе: —" in text


def test_время_работы() -> None:
    now = T0
    status = PollStatus(started_at=now - timedelta(minutes=5))
    assert "Работает: 0 д 0 ч 5 мин" in texts.status_text(view(status=status), TZ)


def test_источник_печатается_заглавными() -> None:
    for kind in SourceKind:
        assert f"Источник: {kind.value.upper()}" in texts.status_text(view(source=kind), TZ)


def test_в_статусе_нет_ни_секретов_ни_номера_заявки() -> None:
    text = texts.status_text(
        view(incident=Incident(1, 1, T0, slots(23)), paused_until=T0 + timedelta(hours=1)), TZ
    )
    assert "TEST-" not in text and "http" not in text


# --- deliveries_text ---


def test_доставки_пусто() -> None:
    assert texts.deliveries_text([]) == "Нет включённых каналов."


def test_доставки_успех_и_ошибка() -> None:
    results = [
        DeliveryResult(Channel.TELEGRAM, True, 123.6, 1),
        DeliveryResult(Channel.NTFY, False, 5000.0, 1, "TimeoutError"),
        DeliveryResult(Channel.CALL, False, 0.0, 1, "HTTP 500"),
    ]
    assert texts.deliveries_text(results).splitlines() == [
        "✅ Telegram: 124 мс",
        "❌ ntfy: 5000 мс — TimeoutError",
        "❌ звонок: 0 мс — HTTP 500",
    ]


def test_название_есть_у_каждого_канала() -> None:
    assert set(texts.CHANNEL_NAMES) == set(Channel)


# --- last_text ---


def incident_view(
    incident: Incident, deliveries: list[DeliveryResult] | None = None
) -> IncidentView:
    return IncidentView(incident, deliveries or [])


def test_last_пусто() -> None:
    assert texts.last_text([], TZ) == "Инцидентов ещё не было."


def test_last_открытый_инцидент() -> None:
    incident = Incident(4, 1, T0, slots_at((23, 10, 30), (23, 10, 45), (24, 9, 0)))
    ok = DeliveryResult(Channel.TELEGRAM, True, 1.0, 1)
    bad = DeliveryResult(Channel.CALL, False, 1.0, 1, "x")
    text = texts.last_text([incident_view(incident, [ok, bad, ok])], TZ)
    assert text.splitlines() == [
        "№4: 12.11 12:00:00, открыт",
        "  даты: 23.02, 24.02",
        "  доставок: 2 из 3",
    ]


def test_last_закрытый_и_подтверждённый_инцидент_во_времени_владельца() -> None:
    incident = Incident(
        5,
        1,
        T0,
        slots(23),
        status=IncidentStatus.CLOSED,
        closed_at=T0 + timedelta(minutes=42),
        acked_at=T0 + timedelta(seconds=30),
    )
    text = texts.last_text([incident_view(incident)], TZ)
    assert text.startswith("№5: 12.11 12:00:00, закрыт 12.11 12:42")
    assert text.endswith("доставок: 0 из 0, подтверждён")


def test_last_без_слотов_прочерк_и_блоки_через_пустую_строку() -> None:
    first = Incident(2, 1, T0, frozenset())
    second = Incident(1, 1, T0, slots(23))
    text = texts.last_text([incident_view(first), incident_view(second)], TZ)
    blocks = text.split("\n\n")
    assert len(blocks) == 2 and "даты: —" in blocks[0] and blocks[1].startswith("№1")


# --- errors_text, cleanup_text ---


def test_ошибок_нет() -> None:
    assert texts.errors_text([], TZ) == "Ошибок нет."


def test_ошибки_в_часовом_поясе_владельца() -> None:
    events = [
        AppEvent(T0, "WARNING", "poll", "сайт ответил 503"),
        AppEvent(T0 - timedelta(hours=14), "ERROR", "poll", "вход не удался"),
    ]
    assert texts.errors_text(events, TZ).splitlines() == [
        "12.11 12:00 WARNING: сайт ответил 503",
        "11.11 22:00 ERROR: вход не удался",
    ]


def test_очистка_пусто_и_с_результатом() -> None:
    assert texts.cleanup_text({}) == "Очищать нечего."
    assert texts.cleanup_text({"снимки": 3, "логи": 0}) == "Удалено: снимки — 3, логи — 0"


# --- константы ---


def test_справка_не_содержит_секретов_и_влезает_в_сообщение() -> None:
    assert len(texts.HELP) < 4096
    assert "interval <сек>" in texts.HELP and "не меньше 10" in texts.HELP


def test_шаблоны_подставляются_без_ошибок() -> None:
    assert "20 с" in texts.INTERVAL_SET.format(value=20.0)
    assert "01.02 03:04" in texts.PAUSED.format(until="01.02 03:04")
    assert "Источник: DOM, интервал ≈ 20 с" in texts.STARTED.format(source="DOM", interval=20)
    assert texts.RELOGIN_FAILED.format(error="причина") == "Вход не удался: причина"
