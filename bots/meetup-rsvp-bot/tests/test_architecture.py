"""Архитектурные требования: слои, единый источник правды, запрещённые конструкции."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "meetup_bot"


def _sources(subdir: str = "") -> list[Path]:
    return sorted((SRC / subdir).rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _definitions(name: str) -> list[str]:
    hits = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
                hits.append(str(path.relative_to(SRC)))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        hits.append(str(path.relative_to(SRC)))
    return hits


@pytest.mark.parametrize("path", _sources("repositories"), ids=lambda p: p.name)
def test_репозитории_не_зависят_от_сервисов_и_telegram(path: Path) -> None:
    forbidden = {
        m
        for m in _imports(path)
        if m.startswith(("meetup_bot.services", "meetup_bot.telegram", "aiogram"))
    }
    assert not forbidden, f"{path.name}: {forbidden}"


@pytest.mark.parametrize(
    "path",
    _sources("services") + _sources("scheduler") + _sources("renderer"),
    ids=lambda p: p.name,
)
def test_бизнес_логика_не_знает_про_telegram(path: Path) -> None:
    forbidden = {
        m
        for m in _imports(path)
        if m.startswith(("aiogram", "meetup_bot.telegram", "meetup_bot.handlers"))
        or m.startswith("meetup_bot.dialogs")
    }
    assert not forbidden, f"{path.name}: {forbidden}"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(SRC)))
def test_время_только_через_clock(path: Path) -> None:
    if path.name == "clock.py":
        return
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"datetime\.now\(|datetime\.utcnow\(|time\.time\(\)", text), path.name


@pytest.mark.parametrize(
    "name",
    [
        "display_name",
        "DEFAULT_TIMEZONE",
        "ATTENDANCE_WINDOW",
        "PROMOTED_DM_TEXT",
        "LIVE_REGISTRATION_STATUSES",
        "UNFINISHED_EVENT_STATUSES",
        "DEFAULT_CAPACITY",
        "MAX_CAPACITY",
        "GENERIC_ERROR_TEXT",
        "ERROR_TEXTS",
        "MAX_RETRY_AFTER",
        "GUEST_SUFFIX",
        "WEEKDAYS_SHORT",
        "page_count",
        "admin_label",
        "participant_line",
    ],
)
def test_единый_источник_определения(name: str) -> None:
    assert len(_definitions(name)) <= 1, _definitions(name)


def test_тексты_уведомлений_не_зашиты_в_сервисы() -> None:
    """Пользовательские тексты живут в `texts/defaults.py` и настройках, а не в сервисах."""
    phrases = ("Освободилось место", "Напоминаем", "Встреча перенесена")
    for path in _sources("services") + _sources("scheduler"):
        text = path.read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase not in text, f"{path.name}: «{phrase}»"


def test_диалоги_и_хендлеры_не_пишут_в_бд_напрямую() -> None:
    """Запись — только через сервисы: в UI-слое нет add/delete/commit сессии."""
    pattern = re.compile(r"session\.(add|delete|commit|merge)\(|\.begin\(\)")
    for path in _sources("dialogs") + _sources("handlers"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


def test_коды_ошибок_сервисов_только_из_errorcode() -> None:
    """Код InvalidOperation — член ErrorCode: строковый литерал разошёлся бы с текстами UI."""
    pattern = re.compile(r"InvalidOperation\(\s*[\"']")
    for path in _sources():
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


# Сервисы ядра встреч ходят в БД только через репозитории: запросы в них не пишем
_REPOSITORY_ONLY_SERVICES = (
    "access",
    "admins",
    "attendance",
    "bans",
    "events",
    "export",
    "participants",
    "users",
)


@pytest.mark.parametrize("name", _REPOSITORY_ONLY_SERVICES)
def test_сервисы_ядра_не_строят_запросов_в_обход_репозиториев(name: str) -> None:
    text = (SRC / "services" / f"{name}.py").read_text(encoding="utf-8")
    assert not re.search(r"\bselect\(|session\.(scalars?|execute)\(|session\.get\(", text), name
    assert "from sqlalchemy import" not in text, name


def test_загрузка_встречи_для_админа_только_в_access() -> None:
    """Копии «найти встречу, проверить права и статус» в сервисах не плодим."""
    for name in _REPOSITORY_ONLY_SERVICES:
        if name == "access":
            continue
        text = (SRC / "services" / f"{name}.py").read_text(encoding="utf-8")
        assert "встреча {" not in text, name
    assert len(_definitions("load_event_for_admin")) == 1


def test_провайдер_настроек_разрешается_одним_хелпером() -> None:
    for name in ("events", "participants", "bans"):
        text = (SRC / "services" / f"{name}.py").read_text(encoding="utf-8")
        assert "Callable[[], RulesSettings]" not in text, name
        assert "resolve_or_default" in text, name
