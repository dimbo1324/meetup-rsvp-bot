"""Общие помощники Telegram-слоя: даты, лимит всплывающего окна, проверка прав."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from meetup_bot.telegram.common import ALERT_LIMIT, check_admin, fit_alert
from meetup_bot.texts.render import date_parts, format_moment
from tests.helpers import ADMIN_ID, OWNER_ID, App

MONTEVIDEO = ZoneInfo("America/Montevideo")
# Суббота, 24 октября 2026, 20:00 UTC = 17:00 в Монтевидео (UTC-3)
MOMENT = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)


def test_date_parts_в_часовом_поясе_клуба() -> None:
    assert date_parts(MOMENT, MONTEVIDEO) == ("суббота", "24 октября", "17:00")


def test_date_parts_переходит_на_другой_день_по_поясу() -> None:
    late = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 22:30 субботы в Монтевидео
    assert date_parts(late, MONTEVIDEO) == ("суббота", "24 октября", "22:30")


def test_date_parts_учитывает_другой_пояс() -> None:
    assert date_parts(MOMENT, ZoneInfo("Asia/Tokyo")) == ("воскресенье", "25 октября", "05:00")


def test_date_parts_дополняет_время_нулём() -> None:
    moment = datetime(2026, 1, 3, 12, 5, tzinfo=UTC)  # 09:05 в Монтевидео
    assert date_parts(moment, MONTEVIDEO)[2] == "09:05"


def test_format_moment() -> None:
    assert format_moment(MOMENT, MONTEVIDEO) == "суббота, 24 октября, 17:00"


def test_format_moment_все_месяцы_названы_в_родительном_падеже() -> None:
    names = [
        format_moment(datetime(2026, month, 15, 12, 0, tzinfo=UTC), MONTEVIDEO).split(", ")[1]
        for month in range(1, 13)
    ]
    assert names[0] == "15 января"
    assert names[11] == "15 декабря"
    assert len(set(names)) == 12


def test_короткий_текст_остаётся_как_есть() -> None:
    assert fit_alert("Привет") == "Привет"


def test_пустой_текст_остаётся_пустым() -> None:
    assert fit_alert("") == ""


def test_ровно_200_символов_не_режутся() -> None:
    text = "а" * ALERT_LIMIT
    assert fit_alert(text) == text


def test_201_символ_режется_до_200_с_многоточием() -> None:
    result = fit_alert("а" * (ALERT_LIMIT + 1))
    assert len(result) == ALERT_LIMIT
    assert result == "а" * (ALERT_LIMIT - 1) + "…"


def test_пробелы_перед_многоточием_убираются() -> None:
    text = "а" * 150 + " " * 49 + "б" * 20
    result = fit_alert(text)
    assert result == "а" * 150 + "…"
    assert len(result) <= ALERT_LIMIT


def test_лимит_окна_равен_200() -> None:
    assert ALERT_LIMIT == 200


@pytest.mark.parametrize("user_id", [OWNER_ID, ADMIN_ID])
async def test_check_admin_для_владельца_и_админа(app: App, user_id: int) -> None:
    assert await check_admin(app.sessions, user_id) is True


async def test_check_admin_для_постороннего(app: App) -> None:
    assert await check_admin(app.sessions, 123456) is False


async def test_check_admin_после_снятия_прав(app: App) -> None:
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    assert await check_admin(app.sessions, ADMIN_ID) is False
