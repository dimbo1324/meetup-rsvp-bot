"""Тексты по умолчанию и образцы подстановок под пятничную встречу (решения 2026-10-25)."""

import re

from meetup_bot.services.settings_schema import SAMPLE_VARS, WORST_CASE_VARS
from meetup_bot.texts.defaults import ALLOWED_PLACEHOLDERS, DEFAULT_TEXTS
from meetup_bot.texts.render import WEEKDAYS

# Корни названий дней недели во всех падежах: «субботу», «пятницу», «в среду»
_WEEKDAY_STEMS = re.compile(
    r"понедельник|вторник|сред[аыуе]|четверг|пятниц|суббот|воскресень", re.IGNORECASE
)
_FIXED_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")


def test_мест_нет_без_дня_недели() -> None:
    assert DEFAULT_TEXTS["text.full"] == "Извините, мест не осталось. Попробуйте в следующий раз!"


def test_тексты_не_привязаны_к_дню_недели_и_времени() -> None:
    for key, text in DEFAULT_TEXTS.items():
        assert not _WEEKDAY_STEMS.search(text), key
        assert not _FIXED_TIME.search(text), key


def test_end_time_разрешённая_подстановка() -> None:
    assert "end_time" in ALLOWED_PLACEHOLDERS


def test_образцы_покрывают_все_подстановки() -> None:
    assert set(SAMPLE_VARS) == ALLOWED_PLACEHOLDERS
    assert set(WORST_CASE_VARS) == ALLOWED_PLACEHOLDERS


def test_образец_соответствует_пятничной_встрече() -> None:
    assert SAMPLE_VARS["weekday"] == "пятница"
    assert SAMPLE_VARS["time"] == "16:30"
    assert SAMPLE_VARS["end_time"] == "18:30"
    # Дедлайн подставляется как «дата, время» (text_variables), без дня недели
    assert SAMPLE_VARS["deadline"] == "29 октября, 20:00"


def test_худший_случай_не_короче_реальных_значений() -> None:
    assert len(WORST_CASE_VARS["weekday"]) == max(len(day) for day in WEEKDAYS)
    longest_date = "30 сентября"
    assert len(WORST_CASE_VARS["date"]) >= len(longest_date)
    assert len(WORST_CASE_VARS["deadline"]) >= len(f"{longest_date}, 23:59")
    # Конец через полночь: время плюс дата в скобках
    assert len(WORST_CASE_VARS["end_time"]) >= len("00:00 (30 сентября)")
