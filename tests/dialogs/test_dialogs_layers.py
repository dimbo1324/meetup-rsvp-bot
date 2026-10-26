"""Слои диалогов: все окна меню админа.

Диалоги не читают БД сами: права, списки людей и имена приходят из сервисов, а ошибки
ловятся как `DomainError`, чтобы непредвиденный сбой дошёл до глобального обработчика.
"""

import ast
import re
from pathlib import Path

import pytest

DIALOGS = Path(__file__).resolve().parents[2] / "src" / "meetup_bot" / "dialogs"
MODULES = (
    "bans",
    "admins",
    "picker",
    "main_menu",
    "settings",
    "settings_view",
    "events",
    "event_edit",
    "participants",
    "event_wizard",
    "event_wizard_support",
)
MAX_MODULE_LINES = 700
DIRECT_DB_USE = re.compile(r"\bselect\(|\bsession\.|\bsessions\(\)|events_repo")
# UserInfo — значение, которое принимают сервисы (имя и @username), а не доступ к БД
ALLOWED_IMPORTS = {"meetup_bot.repositories.users"}
DB_PREFIXES = ("meetup_bot.repositories", "meetup_bot.db.session", "sqlalchemy")


def _text(name: str) -> str:
    return (DIALOGS / f"{name}.py").read_text(encoding="utf-8")


def _imports(name: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(_text(name))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("name", MODULES)
def test_диалог_не_импортирует_репозитории_и_сессии(name: str) -> None:
    forbidden = {
        m
        for m in _imports(name)
        if m.startswith(DB_PREFIXES) and not any(m.startswith(ok) for ok in ALLOWED_IMPORTS)
    }
    assert not forbidden, f"{name}: {forbidden}"


@pytest.mark.parametrize("name", MODULES)
def test_диалог_не_берёт_сессию_и_не_проверяет_права_в_обход_сервисов(name: str) -> None:
    text = _text(name)
    assert "get_sessions" not in text, name
    assert "check_admin" not in text, name


@pytest.mark.parametrize("name", MODULES)
def test_диалог_не_глотает_любые_исключения(name: str) -> None:
    assert not re.search(r"except\s+(Exception|BaseException)\b|except\s*:", _text(name)), name


@pytest.mark.parametrize("name", MODULES)
def test_подписи_повторяющихся_кнопок_только_из_констант(name: str) -> None:
    from meetup_bot.dialogs import _buttons

    literals = [
        value for key, value in vars(_buttons).items() if key.isupper() and isinstance(value, str)
    ]
    text = _text(name)
    for literal in literals:
        assert f'Const("{literal}")' not in text, f"{name}: «{literal}»"


@pytest.mark.parametrize("name", MODULES)
def test_диалог_не_строит_запросов(name: str) -> None:
    assert not DIRECT_DB_USE.search(_text(name)), name


@pytest.mark.parametrize("name", [*MODULES, "_common"])
def test_модуль_диалога_не_длиннее_предела(name: str) -> None:
    lines = _text(name).count("\n")
    assert lines <= MAX_MODULE_LINES, f"{name}: {lines} строк"
