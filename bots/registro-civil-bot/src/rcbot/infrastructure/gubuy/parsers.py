"""Разбор календаря jQuery UI Datepicker (SPEC 1.3). Чистые функции, без браузера.

Зелёный день — ячейка, у которой фон самой ячейки или вложенного `a`/`span` зелёный: условие
`g > 110 && g > r + 35 && g > b + 25` проверено на макете, на живом сайте — на этапе 3.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from rcbot.domain.errors import UnexpectedResponse
from rcbot.domain.models import Slot

MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

# Выполняется в странице: только чтение DOM, без кликов и без изменения формы
READ_MONTH_JS = """
() => {
  const root = document.querySelector('.ui-datepicker');
  if (!root) return null;
  const title = (root.querySelector('.ui-datepicker-title') || {}).textContent || '';
  const green = (el) => {
    if (!el) return false;
    const m = getComputedStyle(el).backgroundColor.match(/\\d+/g);
    if (!m) return false;
    const [r, g, b] = m.map(Number);
    return g > 110 && g > r + 35 && g > b + 25;
  };
  const days = [];
  for (const td of root.querySelectorAll('.ui-datepicker-calendar td')) {
    if (td.classList.contains('ui-datepicker-other-month')) continue;
    const text = (td.textContent || '').trim();
    if (!/^\\d{1,2}$/.test(text)) continue;
    if (green(td) || green(td.querySelector('a')) || green(td.querySelector('span'))) {
      days.push(Number(text));
    }
  }
  const next = root.querySelector('.ui-datepicker-next');
  const banner = document.body.innerText.includes('No hay cupos disponibles');
  return {
    title: title.trim(),
    days,
    hasNext: !!next && !next.classList.contains('ui-state-disabled'),
    noSlotsBanner: banner,
  };
}
"""


@dataclass(frozen=True)
class MonthView:
    title: str
    days: tuple[int, ...]
    has_next: bool
    no_slots_banner: bool


def _plain(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def parse_month_title(title: str) -> tuple[int, int]:
    """«Noviembre 2026» → (2026, 11)."""
    words = re.findall(r"[a-z]+|\d{4}", _plain(title))
    month = next((MONTHS_ES[w] for w in words if w in MONTHS_ES), None)
    year = next((int(w) for w in words if w.isdigit()), None)
    if month is None or year is None:
        raise UnexpectedResponse("заголовок месяца календаря не распознан")
    return year, month


def month_view(raw: object) -> MonthView:
    if not isinstance(raw, dict):
        raise UnexpectedResponse("календарь на странице не найден")
    days = raw.get("days", [])
    if not isinstance(days, list) or not all(type(d) is int for d in days):
        raise UnexpectedResponse("список дней календаря не распознан")
    return MonthView(
        title=str(raw.get("title", "")),
        days=tuple(days),
        has_next=bool(raw.get("hasNext")),
        no_slots_banner=bool(raw.get("noSlotsBanner")),
    )


def slots_of(view: MonthView) -> frozenset[Slot]:
    year, month = parse_month_title(view.title)
    slots = set()
    for day in view.days:
        try:
            slots.add(Slot(date(year, month, day)))
        except (ValueError, OverflowError) as error:
            raise UnexpectedResponse("в календаре несуществующий день") from error
    return frozenset(slots)
