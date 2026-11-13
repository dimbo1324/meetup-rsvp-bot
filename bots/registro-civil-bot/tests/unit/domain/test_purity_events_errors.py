"""Чистота доменного слоя (SPEC 3.4), события и исключения SPEC 3.1."""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

import rcbot.domain
from rcbot.domain import errors, events
from rcbot.domain.models import PollState, SourceKind
from tests.fakes import T0, slots, snapshot

DOMAIN_DIR = Path(rcbot.domain.__file__).parent
FORBIDDEN = ("aiohttp", "aiogram", "sqlalchemy", "playwright", "keyring", "pydantic", "sentry_sdk")


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", sorted(DOMAIN_DIR.glob("*.py")), ids=lambda p: p.name)
def test_модуль_домена_не_импортирует_внешних_пакетов(path: Path) -> None:
    roots = _imported_roots(path)
    assert not roots & set(FORBIDDEN)
    # допустимы только стандартная библиотека и сам домен
    assert all(root in sys.stdlib_module_names or root in {"rcbot", "__future__"} for root in roots)


def test_импорт_домена_не_тянет_внешние_пакеты_в_процесс() -> None:
    code = (
        "import sys\n"
        "import rcbot.domain.models, rcbot.domain.detector, rcbot.domain.events\n"
        "import rcbot.domain.errors\n"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_события_неизменяемы_и_несут_момент() -> None:
    snap = snapshot(slots(23))
    appeared = events.SlotsAppeared(T0, snap)
    changed = events.SlotsChanged(T0, snap, slots(23))
    gone = events.SlotsGone(T0, snap)
    for event in (appeared, changed, gone):
        assert event.at == T0
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.at = T0  # type: ignore[misc]
    assert changed.added == slots(23)


def test_события_состояния_и_источника_хранят_причину() -> None:
    switched = events.SourceSwitched(T0, SourceKind.API, SourceKind.DOM, "3 неожиданных ответа")
    changed = events.PollerStateChanged(T0, PollState.RUNNING, PollState.BACKOFF, "429")
    assert (switched.old, switched.new, switched.reason) == (
        SourceKind.API,
        SourceKind.DOM,
        "3 неожиданных ответа",
    )
    assert (changed.old, changed.new) == (PollState.RUNNING, PollState.BACKOFF)
    assert events.ManualActionRequired(T0, "шаг 1").reason == "шаг 1"
    assert events.UnconfirmedSlotsSeen(T0, snapshot(slots(23))).snapshot.has_slots


def test_все_события_наследуют_domain_event() -> None:
    names = [
        "SlotsAppeared",
        "SlotsChanged",
        "SlotsGone",
        "ManualActionRequired",
        "SourceSwitched",
        "PollerStateChanged",
        "UnconfirmedSlotsSeen",
    ]
    for name in names:
        assert issubclass(getattr(events, name), events.DomainEvent)


def test_все_исключения_домена_наследуют_rcbot_error_и_различимы() -> None:
    classes = [
        errors.SessionExpired,
        errors.ManualStepRequired,
        errors.RateLimited,
        errors.UnexpectedResponse,
        errors.SiteUnavailable,
        errors.ReloginBudgetExceeded,
        errors.ReloginDeferred,
        errors.LoginFailed,
        errors.SourceNotConfigured,
        errors.HostNotAllowed,
    ]
    for cls in classes:
        assert issubclass(cls, errors.RcbotError)
        with pytest.raises(errors.RcbotError, match="причина"):
            raise cls("причина")
    # Исключения не вложены друг в друга: `except RateLimited` не должен ловить чужие
    for first in classes:
        for second in classes:
            if first is not second:
                assert not issubclass(first, second)
