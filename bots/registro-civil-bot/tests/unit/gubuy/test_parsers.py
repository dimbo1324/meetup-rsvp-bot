"""Разбор календаря: заголовок месяца, сырые данные страницы, слоты (SPEC 1.3).

Сам JavaScript чтения календаря проверяется в настоящем Chromium — см. integration/gubuy.
"""

from __future__ import annotations

import unicodedata
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.domain.errors import UnexpectedResponse
from rcbot.domain.models import Slot
from rcbot.infrastructure.gubuy.parsers import (
    MONTHS_ES,
    MonthView,
    month_view,
    parse_month_title,
    slots_of,
)

MONTH_NAMES = [
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
]


@pytest.mark.parametrize(("number", "name"), list(enumerate(MONTH_NAMES, start=1)))
def test_все_двенадцать_месяцев_по_испански(number: int, name: str) -> None:
    assert parse_month_title(f"{name} 2027") == (2027, number)


def test_setiembre_это_сентябрь() -> None:
    # Так сентябрь пишут в Уругвае
    assert parse_month_title("Setiembre 2026") == (2026, 9)


@pytest.mark.parametrize(
    "title",
    [
        "noviembre 2026",
        "NOVIEMBRE 2026",
        "  Noviembre   2026  ",
        "Noviembre 2026",  # неразрывный пробел: так выглядит textContent jQuery UI
        "2026 Noviembre",
        "Noviembre de 2026",
        "Noviémbre 2026",  # акценты не мешают
        "Noviembre, 2026",
    ],
)
def test_заголовок_допускает_регистр_пробелы_и_порядок(title: str) -> None:
    assert parse_month_title(title) == (2026, 11)


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "2026",
        "Noviembre",
        "Novembre 2026",  # не испанское название
        "November 2026",
        "Ноябрь 2026",
        "Noviembre 26",  # двузначный год
        "13/2026",
        "No hay cupos disponibles",
        "Nov 2026",
    ],
)
def test_мусор_в_заголовке_это_unexpected_response(title: str) -> None:
    with pytest.raises(UnexpectedResponse, match="заголовок месяца"):
        parse_month_title(title)


@given(
    month=st.integers(1, 12),
    year=st.integers(1000, 9999),
    case=st.sampled_from([str.lower, str.upper, str.title, str.swapcase]),
    spacer=st.sampled_from([" ", "  ", " ", " de "]),
    accented=st.booleans(),
)
def test_свойство_заголовок_разбирается_при_любом_оформлении(
    month: int, year: int, case: object, spacer: str, accented: bool
) -> None:
    name = next(n for n, number in MONTHS_ES.items() if number == month)
    if accented:
        name = unicodedata.normalize("NFD", name[:-1]) + "́" + name[-1]
    title = f"{case(name)}{spacer}{year}"  # type: ignore[operator]
    assert parse_month_title(title) == (year, month)


@given(st.text(alphabet="0123456789 .,/-:", max_size=30))
def test_свойство_без_названия_месяца_всегда_отказ(title: str) -> None:
    with pytest.raises(UnexpectedResponse):
        parse_month_title(title)


# --- month_view: сырые данные из страницы ---


@pytest.mark.parametrize("raw", [None, [], "calendar", 5, 1.5, True, (), b"x"])
def test_месяц_не_словарь_это_календарь_не_найден(raw: object) -> None:
    with pytest.raises(UnexpectedResponse, match="календарь на странице не найден"):
        month_view(raw)


@pytest.mark.parametrize(
    "days",
    [
        None,
        "1,2,3",
        (1, 2),
        {"1": 2},
        [1, "2"],
        [1.5],
        [2.0],
        [None],
        [[1]],
        [True],
        [False],
    ],
)
def test_дни_не_целые_числа_это_unexpected_response(days: object) -> None:
    with pytest.raises(UnexpectedResponse, match="список дней"):
        month_view({"title": "Noviembre 2026", "days": days})


def test_месяц_без_полей_получает_безопасные_значения() -> None:
    view = month_view({})
    assert view == MonthView(title="", days=(), has_next=False, no_slots_banner=False)


def test_месяц_переносит_все_поля() -> None:
    view = month_view(
        {"title": "Febrero 2027", "days": [3, 4], "hasNext": True, "noSlotsBanner": True, "x": 1}
    )
    assert view == MonthView("Febrero 2027", (3, 4), True, True)


def test_лишние_ключи_не_мешают_а_пустые_флаги_ложны() -> None:
    view = month_view({"title": "Febrero 2027", "days": [], "hasNext": None, "noSlotsBanner": 0})
    assert view.has_next is False
    assert view.no_slots_banner is False


# --- slots_of ---


def test_слоты_месяца_это_только_даты_без_времени() -> None:
    view = MonthView("Febrero 2027", (23, 24, 1), False, False)
    assert slots_of(view) == frozenset(
        {Slot(date(2027, 2, 23)), Slot(date(2027, 2, 24)), Slot(date(2027, 2, 1))}
    )
    assert all(slot.at is None for slot in slots_of(view))


def test_повторы_дней_схлопываются() -> None:
    assert len(slots_of(MonthView("Marzo 2027", (5, 5, 5), False, False))) == 1


def test_пустой_месяц_даёт_пустое_множество() -> None:
    assert slots_of(MonthView("Marzo 2027", (), False, True)) == frozenset()


@pytest.mark.parametrize(
    ("title", "day"),
    [
        ("Febrero 2027", 29),  # 2027 не високосный
        ("Febrero 2027", 31),
        ("Abril 2027", 31),
        ("Marzo 2027", 0),
        ("Marzo 2027", -1),
        ("Marzo 2027", 32),
        ("Marzo 2027", 99),
    ],
)
def test_несуществующий_день_это_unexpected_response(title: str, day: int) -> None:
    with pytest.raises(UnexpectedResponse, match="несуществующий день"):
        slots_of(MonthView(title, (1, day), False, False))


def test_високосный_февраль_допускает_29() -> None:
    assert slots_of(MonthView("Febrero 2028", (29,), False, False)) == {Slot(date(2028, 2, 29))}


def test_слоты_с_мусорным_заголовком_это_unexpected_response() -> None:
    with pytest.raises(UnexpectedResponse, match="заголовок месяца"):
        slots_of(MonthView("", (1,), False, False))


@given(
    year=st.integers(1900, 2200),
    month=st.integers(1, 12),
    days=st.sets(st.integers(1, 28)),
)
def test_свойство_слоты_совпадают_с_днями_месяца(year: int, month: int, days: set[int]) -> None:
    name = MONTH_NAMES[month - 1]
    result = slots_of(MonthView(f"{name} {year}", tuple(days), False, False))
    assert result == frozenset(Slot(date(year, month, d)) for d in days)


@given(st.lists(st.one_of(st.integers(), st.text(), st.none(), st.floats()), max_size=5))
def test_свойство_любой_мусор_вместо_дней_даёт_unexpected_response_или_слоты(
    days: list[object],
) -> None:
    # Единственное допустимое исключение — UnexpectedResponse: сырой ValueError/TypeError
    # из разбора означал бы падение опроса вместо карантина
    try:
        view = month_view({"title": "Marzo 2027", "days": days})
        slots_of(view)
    except UnexpectedResponse:
        pass
