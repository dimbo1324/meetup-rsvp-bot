"""READ_MONTH_JS в настоящем Chromium на макете jQuery UI Datepicker (SPEC 1.3).

Правило зелёного дня: `g > 110 && g > r + 35 && g > b + 25`. Синхронный Playwright: один браузер
на модуль, страницы подставляются через `set_content` без сети.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, sync_playwright
from tests.integration.gubuy.site import (
    GREEN,
    GREY,
    calendar_markup,
    green,
    grey,
    rows,
    standalone,
    td,
)

from rcbot.domain.errors import UnexpectedResponse
from rcbot.infrastructure.gubuy.parsers import READ_MONTH_JS, month_view, slots_of

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def page() -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def read(page: Page, html: str) -> dict[str, object]:
    page.set_content(html)
    raw = page.evaluate(READ_MONTH_JS)
    assert isinstance(raw, dict)
    return raw


def test_зелёные_дни_читаются_целыми_числами(page: Page) -> None:
    html = standalone("Febrero 2027", rows(grey(1), green(2), green(3, "td"), green(4, "span")))
    raw = read(page, html)
    assert raw["days"] == [2, 3, 4]
    assert all(type(day) is int for day in raw["days"])  # type: ignore[attr-defined]
    view = month_view(raw)
    assert [s.day.isoformat() for s in sorted(slots_of(view))] == [
        "2027-02-02",
        "2027-02-03",
        "2027-02-04",
    ]


@pytest.mark.parametrize(
    ("color", "is_green"),
    [
        ("rgb(0, 160, 0)", True),
        ("rgb(0, 128, 0)", True),  # CSS green
        ("rgb(0, 255, 0)", True),  # lime
        ("rgb(144, 238, 144)", True),  # lightgreen
        ("rgb(0, 111, 0)", True),  # g=111: минимальное значение
        ("rgb(0, 110, 0)", False),  # g=110: не больше 110
        ("rgb(75, 111, 0)", True),  # g = r + 36
        ("rgb(76, 111, 0)", False),  # g = r + 35: не больше r + 35
        ("rgb(0, 111, 85)", True),  # g = b + 26
        ("rgb(0, 111, 86)", False),  # g = b + 25: не больше b + 25
        ("rgb(0, 100, 0)", False),  # darkgreen: слишком тёмный
        ("rgb(128, 128, 0)", False),  # olive
        ("rgb(0, 128, 128)", False),  # teal
        ("rgb(238, 238, 238)", False),
        ("rgb(255, 255, 255)", False),
        ("rgb(220, 53, 69)", False),  # красный
        ("rgba(0, 160, 0, 0.5)", True),  # полупрозрачный зелёный
        ("rgba(0, 0, 0, 0)", False),  # transparent
        ("transparent", False),
    ],
)
def test_граничные_цвета_правила_зелёного(page: Page, color: str, is_green: bool) -> None:
    html = standalone("Febrero 2027", rows(td(9, a_bg=color)))
    assert read(page, html)["days"] == ([9] if is_green else [])


@pytest.mark.parametrize("where", ["td", "a", "span"])
def test_фон_на_td_a_или_span_считается(page: Page, where: str) -> None:
    assert read(page, standalone("Marzo 2027", rows(green(5, where))))["days"] == [5]


def test_цвет_берётся_вычисленный_а_не_из_атрибута_style(page: Page) -> None:
    # Календарь красится классом из таблицы стилей, как на настоящем сайте
    html = "<style>.libre { background-color: rgb(0, 150, 0); }</style>" + standalone(
        "Marzo 2027", rows('<td><a class="libre">7</a></td>', grey(8))
    )
    assert read(page, html)["days"] == [7]


def test_ячейки_соседнего_месяца_с_зелёным_фоном_не_считаются(page: Page) -> None:
    body = rows(
        td(28, td_bg=GREEN, a_bg=GREEN, cls="ui-datepicker-other-month"),
        green(1),
        green(2),
        td(1, td_bg=GREEN, a_bg=GREEN, cls=" ui-datepicker-other-month ui-state-disabled"),
    )
    assert read(page, standalone("Marzo 2027", body))["days"] == [1, 2]


def test_ячейки_без_числа_пропускаются(page: Page) -> None:
    body = rows(
        '<td style="background-color: rgb(0,160,0)">&nbsp;</td>',
        '<td style="background-color: rgb(0,160,0)"><a>x</a></td>',
        '<td style="background-color: rgb(0,160,0)"><a>123</a></td>',  # три цифры — не день
        '<td style="background-color: rgb(0,160,0)"><a> 12 </a></td>',  # пробелы вокруг
        green(3),
    )
    assert read(page, standalone("Marzo 2027", body))["days"] == [12, 3]


def test_серые_и_недоступные_дни_не_считаются(page: Page) -> None:
    body = rows(
        grey(1),
        '<td class="ui-datepicker-unselectable ui-state-disabled"><span>2</span></td>',
        green(3),
    )
    assert read(page, standalone("Marzo 2027", body))["days"] == [3]


def test_пустой_календарь_даёт_пустой_список_дней(page: Page) -> None:
    raw = read(page, standalone("Marzo 2027", ""))
    assert raw["days"] == []
    assert raw["title"] == "Marzo 2027"
    assert slots_of(month_view(raw)) == frozenset()


def test_заголовок_приходит_без_пробелов_по_краям(page: Page) -> None:
    assert read(page, standalone("  Noviembre 2026  ", ""))["title"] == "Noviembre 2026"


def test_стрелка_следующего_месяца_активна(page: Page) -> None:
    assert read(page, standalone("Marzo 2027", rows(green(1))))["hasNext"] is True


def test_отключённая_стрелка_следующего_месяца(page: Page) -> None:
    raw = read(page, standalone("Marzo 2027", rows(green(1)), next_disabled=True))
    assert raw["hasNext"] is False


def test_нет_стрелки_нет_следующего_месяца(page: Page) -> None:
    html = calendar_markup("Marzo 2027", rows(green(1))).replace("ui-datepicker-next", "otra")
    assert read(page, f"<body>{html}</body>")["hasNext"] is False


def test_плашка_нет_мест_замечается(page: Page) -> None:
    raw = read(page, standalone("Marzo 2027", rows(grey(1)), banner="No hay cupos disponibles"))
    assert raw["noSlotsBanner"] is True
    assert raw["days"] == []


def test_без_плашки_нет_флага(page: Page) -> None:
    assert read(page, standalone("Marzo 2027", rows(green(1))))["noSlotsBanner"] is False


def test_скрытая_плашка_не_считается(page: Page) -> None:
    html = standalone("Marzo 2027", rows(green(1)))
    html = html.replace(
        "</body>", '<div style="display:none">No hay cupos disponibles</div></body>'
    )
    assert read(page, html)["noSlotsBanner"] is False


def test_плашка_и_зелёные_дни_одновременно_читаются_раздельно(page: Page) -> None:
    # Плашка относится к выбранному дню, а не к месяцу: детектор решает по дням, не по плашке
    raw = read(page, standalone("Marzo 2027", rows(green(4)), banner="No hay cupos disponibles"))
    assert raw["days"] == [4]
    assert raw["noSlotsBanner"] is True


def test_страница_без_календаря_это_null_и_unexpected_response(page: Page) -> None:
    page.set_content("<html><body><p>Seleccionar día</p></body></html>")
    raw = page.evaluate(READ_MONTH_JS)
    assert raw is None
    with pytest.raises(UnexpectedResponse, match="календарь на странице не найден"):
        month_view(raw)


def test_чтение_ничего_не_меняет_на_странице(page: Page) -> None:
    html = standalone("Marzo 2027", rows(green(4), green(5)))
    page.set_content(html)
    before = page.content()
    page.evaluate("window.__n = 0; document.addEventListener('click', () => window.__n++, true)")
    page.evaluate(READ_MONTH_JS)
    page.evaluate(READ_MONTH_JS)
    assert page.content() == before
    assert page.evaluate("window.__n") == 0  # ни одного клика: скрипт только читает


def test_зелёный_цвет_константа_макета_проходит_правило() -> None:
    # Защита от тихого изменения макета: GREEN и GREY из site.py должны давать ожидаемое
    def is_green(rgb: str) -> bool:
        r, g, b = (int(x) for x in rgb.removeprefix("rgb(").removesuffix(")").split(","))
        return g > 110 and g > r + 35 and g > b + 25

    assert is_green(GREEN)
    assert not is_green(GREY)
