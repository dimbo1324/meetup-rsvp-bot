"""Подписи людей: в посте, в списках админа и в меню."""

import pytest

from meetup_bot.services.names import admin_label


@pytest.mark.parametrize(
    ("first", "last", "username", "expected"),
    [
        ("Анна", "Петрова", "anna", "Анна Петрова (@anna)"),
        ("Анна", None, None, "Анна"),
        ("", None, "anna", "@anna"),
        (None, None, None, "Пользователь 42"),
        ("", "", "", "Пользователь 42"),
    ],
)
def test_admin_label_самый_информативный_вариант(
    first: str | None, last: str | None, username: str | None, expected: str
) -> None:
    assert admin_label(first, last, username, 42) == expected
