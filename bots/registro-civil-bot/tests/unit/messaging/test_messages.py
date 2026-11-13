"""Тексты тревог (SPEC 2.4): формат, каналы, приватность ntfy и звонка."""

from __future__ import annotations

import string
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.application.messages import (
    CALL_MAX_CHARS,
    CALL_TEXT_EN,
    CALL_TEXT_RU,
    TELEGRAM_ONLY,
    WEEKDAYS,
    MessageComposer,
)
from rcbot.domain.models import Alert, AlertAction, AlertKind, Channel, Slot
from tests.fakes import AGENDA_URL, T0, slots, slots_at, snapshot

MONTEVIDEO = ZoneInfo("America/Montevideo")
TRAMITE = "TEST-TRAMITE-77"
ALL_CHANNELS = frozenset(Channel)


def composer(
    tramite: str = TRAMITE, url: str = AGENDA_URL, tz: str = "America/Montevideo"
) -> MessageComposer:
    return MessageComposer(ZoneInfo(tz), url, tramite)


def feb(*days: int) -> frozenset[Slot]:
    return slots(*days)


# --- формат тревоги (SPEC 2.4) ---


def test_тревога_по_образцу_spec_два_с_временем_одного_дня() -> None:
    snap = snapshot(
        slots_at((23, 10, 30), (23, 10, 45), (23, 11, 0)),
        at=T0 + timedelta(seconds=7),
        latency_ms=300,
    )
    alert = composer().slots_alert(snap, T0 + timedelta(seconds=7, milliseconds=100))
    assert alert.text.splitlines() == [
        "🟢 Registro Civil: появились места!",
        "• 23.02.2027 (вт): 10:30, 10:45, 11:00",
        f"Заявка {TRAMITE} → «Realizar» → шаг «Agenda»",
        AGENDA_URL,
        "Найдено в 12:00:07, уведомление за 0,4 с.",
    ]
    assert alert.kind is AlertKind.APPEARED
    assert alert.action is AlertAction.ACK
    assert alert.channels == ALL_CHANNELS
    assert alert.incident_id is None


def test_дни_недели_по_русски_для_всей_недели() -> None:
    assert WEEKDAYS == ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
    week = feb(22, 23, 24, 25, 26, 27, 28)  # 22.02.2027 — понедельник
    lines = composer().slots_alert(snapshot(week), T0).text.splitlines()[1:8]
    assert [line.split("(")[1].split(")")[0] for line in lines] == list(WEEKDAYS)


def test_слоты_без_времени_печатаются_днём_без_двоеточия() -> None:
    text = composer().slots_alert(snapshot(feb(23)), T0).text
    assert "• 23.02.2027 (вт)\n" in text
    assert "(вт):" not in text


def test_дни_по_возрастанию_а_времена_внутри_дня_по_возрастанию() -> None:
    found = slots_at((24, 9, 0), (23, 11, 0), (23, 9, 5), (23, 10, 0))
    lines = composer().slots_alert(snapshot(found), T0).text.splitlines()
    assert lines[1] == "• 23.02.2027 (вт): 09:05, 10:00, 11:00"
    assert lines[2] == "• 24.02.2027 (ср): 09:00"


def test_полночь_печатается_а_не_считается_отсутствием_времени() -> None:
    text = (
        composer().slots_alert(snapshot(frozenset({Slot(date(2027, 2, 23), time(0, 0))})), T0).text
    )
    assert "(вт): 00:00" in text


def test_день_и_день_со_временем_в_один_день_сливаются_в_одну_строку() -> None:
    found = frozenset({Slot(date(2027, 2, 23)), Slot(date(2027, 2, 23), time(10, 30))})
    lines = composer().slots_alert(snapshot(found), T0).text.splitlines()
    assert lines[1] == "• 23.02.2027 (вт): 10:30"
    assert lines[2].startswith("Заявка")


def test_задержка_с_запятой_это_ответ_сайта_плюс_обработка() -> None:
    snap = snapshot(feb(23), latency_ms=1000)
    text = composer().slots_alert(snap, T0 + timedelta(seconds=1)).text
    assert text.endswith("уведомление за 2,0 с.")
    assert "." not in text.splitlines()[-1].split("за ")[1].rstrip(".").replace(",", "")


def test_отрицательная_обработка_не_уменьшает_задержку_ниже_ответа_сайта() -> None:
    # Часы ушли назад между проверкой и отправкой: показываем только задержку ответа
    snap = snapshot(feb(23), at=T0 + timedelta(seconds=5), latency_ms=300)
    assert composer().slots_alert(snap, T0).text.endswith("уведомление за 0,3 с.")


def test_локальное_время_находки_по_часовому_поясу() -> None:
    snap = snapshot(feb(23), at=datetime(2026, 11, 13, 2, 30, 5, tzinfo=T0.tzinfo))
    assert "Найдено в 23:30:05" in composer().slots_alert(snap, snap.observed_at).text
    assert (
        "Найдено в 05:30:05"
        in composer(tz="Europe/Moscow").slots_alert(snap, snap.observed_at).text
    )


def test_пустой_номер_заявки_не_оставляет_мусора() -> None:
    text = composer(tramite="").slots_alert(snapshot(feb(23)), T0).text
    assert "Заявка → «Realizar» → шаг «Agenda»" in text
    assert "None" not in text and "  " not in text


def test_адрес_с_лишними_символами_передаётся_как_есть_в_тексте() -> None:
    url = "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1?a=1&b=<x>#ф"
    alert = composer(url=url).slots_alert(snapshot(feb(23)), T0)
    assert url in alert.text.splitlines()


def test_изменение_добавляет_строку_новых_и_меняет_заголовок() -> None:
    snap = snapshot(feb(23, 24, 25))
    alert = composer().slots_alert(snap, T0, added=feb(24, 25))
    lines = alert.text.splitlines()
    assert lines[0] == "🟢 Registro Civil: появились новые места!"
    assert alert.kind is AlertKind.CHANGED
    assert "Новые: 24.02, 25.02" in lines
    # строка «Новые» идёт после списка дней и до заявки
    assert lines.index("Новые: 24.02, 25.02") < lines.index(
        f"Заявка {TRAMITE} → «Realizar» → шаг «Agenda»"
    )
    assert lines.index("Новые: 24.02, 25.02") > lines.index("• 25.02.2027 (чт)")
    assert alert.channels == ALL_CHANNELS and alert.action is AlertAction.ACK


def test_изменение_с_пустым_added_без_строки_новых() -> None:
    alert = composer().slots_alert(snapshot(feb(23)), T0, added=frozenset())
    assert alert.kind is AlertKind.CHANGED
    assert "Новые:" not in alert.text


def test_появление_без_added_не_содержит_строки_новых() -> None:
    assert "Новые:" not in composer().slots_alert(snapshot(feb(23, 24)), T0).text


def _days_in_march(count: int) -> frozenset[Slot]:
    return frozenset(Slot(date(2027, 3, 1) + timedelta(days=i)) for i in range(count))


@pytest.mark.parametrize(("count", "tail"), [(1, ""), (6, ""), (7, " и ещё 1"), (9, " и ещё 3")])
def test_после_шести_дней_и_ещё_n(count: int, tail: str) -> None:
    found = _days_in_march(count)
    alert = composer().slots_alert(snapshot(found), T0, added=found)
    first_six = ", ".join(f"{d:%d.%m}" for d in sorted({s.day for s in found})[:6])
    assert f"Новые: {first_six}{tail}" in alert.text.splitlines()
    assert alert.push_text == f"Свободные даты: {first_six}{tail}. Срочно записывайтесь."


def test_список_дней_в_push_считает_дни_а_не_слоты_со_временем() -> None:
    found = slots_at(*[(23, 9, m) for m in range(0, 60, 5)])  # 12 слотов одного дня
    assert composer().slots_alert(snapshot(found), T0).push_text == (
        "Свободные даты: 23.02. Срочно записывайтесь."
    )


# --- приватность ntfy и звонка (SPEC 2.4, 2.6) ---


def test_звонок_это_общая_фраза_до_256_символов() -> None:
    alert = composer().slots_alert(snapshot(feb(23)), T0)
    assert alert.call_text == CALL_TEXT_RU
    assert len(alert.call_text) <= CALL_MAX_CHARS == 256
    assert len(CALL_TEXT_EN) <= CALL_MAX_CHARS


def test_в_push_и_звонке_нет_ссылки_номера_заявки_и_этапа() -> None:
    url = "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA-5555/1"
    alert = composer(tramite="TEST-ZAYAVKA-4242", url=url).slots_alert(
        snapshot(slots_at((23, 10, 30), (24, 11, 0))), T0, added=feb(24)
    )
    external = [alert.push_title, alert.push_text, alert.call_text]
    for text in external:
        assert "http" not in text
        assert "TEST-ZAYAVKA-4242" not in text
        assert "TEST-ETAPA-5555" not in text
        assert "bpmgob" not in text
    # а в личном Telegram-сообщении всё это есть
    assert "TEST-ZAYAVKA-4242" in alert.text and url in alert.text


@given(
    tramite=st.text(string.digits, min_size=6, max_size=12).map("ZAYAVKA-".__add__),
    etapa=st.text(string.digits, min_size=6, max_size=12).map("ETAPA-".__add__),
    days=st.sets(st.integers(1, 28), min_size=1, max_size=10),
    with_time=st.booleans(),
)
def test_свойство_внешние_тексты_не_содержат_идентификаторов_заявки(
    tramite: str, etapa: str, days: set[int], with_time: bool
) -> None:
    url = f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{etapa}/1"
    found = slots_at(*[(d, 10, 0) for d in days]) if with_time else slots(*days)
    alert = composer(tramite=tramite, url=url).slots_alert(snapshot(found), T0, added=found)
    for text in (alert.push_title, alert.push_text, alert.call_text):
        assert tramite not in text and etapa not in text and "http" not in text
    assert len(alert.call_text) <= CALL_MAX_CHARS


def test_ссылка_на_agenda_только_в_тексте_для_telegram_а_не_в_push_и_звонке() -> None:
    # Решение R10 (закрывает Q-M4): ссылка с номером этапа не уходит на сторонние сервисы
    alert = composer().slots_alert(snapshot(feb(23)), T0)
    assert AGENDA_URL in alert.text
    for other in (alert.push_title, alert.push_text, alert.call_text):
        assert AGENDA_URL not in other


# --- служебные, неподтверждённые, тестовые ---


def test_неподтверждённая_находка_только_в_telegram_без_кнопки_и_звонка() -> None:
    alert = composer().unconfirmed_alert(snapshot(feb(23)))
    assert alert.kind is AlertKind.UNCONFIRMED
    assert alert.channels == TELEGRAM_ONLY == frozenset({Channel.TELEGRAM})
    assert alert.action is AlertAction.NONE
    assert alert.call_text == "" and alert.push_text == ""
    assert alert.text.splitlines()[0].startswith("🟡 SAE")
    assert "• 23.02.2027 (вт)" in alert.text


def test_пропажа_мест_только_в_telegram_и_с_локальным_временем() -> None:
    alert = composer().gone(T0)
    assert alert.kind is AlertKind.SERVICE
    assert alert.channels == TELEGRAM_ONLY
    assert alert.action is AlertAction.NONE
    assert alert.text == "⚪ Места пропали (12:00:00): три проверки подряд без мест."


def test_ручной_шаг_с_кнопкой_готово_только_в_telegram() -> None:
    alert = composer().manual_step("страница шага 1")
    assert alert.kind is AlertKind.SERVICE
    assert alert.action is AlertAction.MANUAL_DONE
    assert alert.channels == TELEGRAM_ONLY
    assert "Причина: страница шага 1" in alert.text
    assert AGENDA_URL in alert.text
    assert alert.call_text == "" and alert.push_text == ""


def test_служебное_сообщение_только_в_telegram_без_кнопки() -> None:
    alert = composer().service("запуск")
    assert alert == Alert(AlertKind.SERVICE, "запуск", channels=TELEGRAM_ONLY)


def test_тест_каналов_идёт_во_все_три_канала_и_без_идентификаторов_наружу() -> None:
    alert = composer().test(T0)
    assert alert.kind is AlertKind.TEST
    assert alert.channels == ALL_CHANNELS
    assert alert.action is AlertAction.ACK
    assert "12:00:00" in alert.text and "Это тест" in alert.text
    assert 0 < len(alert.call_text) <= CALL_MAX_CHARS
    for text in (alert.push_title, alert.push_text, alert.call_text):
        assert "http" not in text and TRAMITE not in text
    assert "мест нет" in alert.push_text.lower() or "Мест нет" in alert.push_text


def test_тревога_о_местах_не_путается_с_тестовой_по_смыслу_звонка() -> None:
    real = composer().slots_alert(snapshot(feb(23)), T0).call_text
    test = composer().test(T0).call_text
    assert real != test
    assert "проверка" in test.lower()


@pytest.mark.parametrize("changed", [False, True])
def test_запасной_английский_текст_звонка_только_у_настоящей_тревоги(changed: bool) -> None:
    added = feb(23) if changed else None
    alert = composer().slots_alert(snapshot(feb(23)), T0, added=added)
    assert alert.call_fallback_text == CALL_TEXT_EN
    assert len(alert.call_fallback_text) <= CALL_MAX_CHARS


def test_тестовый_звонок_без_запасного_текста_о_появлении_мест() -> None:
    # Раньше запасной голос подставлял «Free appointments appeared» и в тестовый звонок
    assert composer().test(T0).call_fallback_text == ""
    for alert in (
        composer().gone(T0),
        composer().service("x"),
        composer().unconfirmed_alert(snapshot(feb(23))),
        composer().manual_step("x"),
    ):
        assert alert.call_fallback_text == ""


def test_composer_неизменяем_и_local_переводит_aware_момент() -> None:
    c = composer()
    assert c.local(T0).utcoffset() == timedelta(hours=-3)
    assert c.local(T0).tzinfo == MONTEVIDEO
