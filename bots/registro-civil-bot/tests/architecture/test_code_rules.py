"""Правила кода (SPEC 3.4, `.ai/10-bot-rules.md`): время через Clock, ошибки, вывод, размер.

Известные отступления оформлены двумя тестами: «не растёт» (зелёный) и «нет вовсе» (xfail
strict, BUG-T8): когда отступление исправят, второй тест сам потребует снять метку.
"""

from __future__ import annotations

import ast
import re

import pytest

from tests.architecture.srcscan import Module, calls, modules

ALL = modules()
CORE = [m for m in ALL if m.package in ("domain", "application")]

# Вызовы времени, которых нет в domain и application: только порт Clock (SPEC 3.4)
TIME_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
    ("time", "time"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("time", "sleep"),
    ("asyncio", "sleep"),
}
# Явно разрешено в domain/application: пусто. `asyncio.timeout` — предел ожидания, а не чтение
# времени (poll_loop, alerting), и допустим.
TIME_ALLOWED: set[tuple[str, str]] = set()


def _dotted(call: ast.Call) -> tuple[str, str]:
    func = call.func
    if isinstance(func, ast.Attribute):
        base = func.value
        owner = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        return owner, func.attr
    return "", getattr(func, "id", "")


def test_время_в_domain_и_application_только_через_clock() -> None:
    found = [
        f"{m.rel}:{call.lineno}: {'.'.join(_dotted(call))}"
        for m in CORE
        for call, _ in calls(m)
        if _dotted(call) in TIME_CALLS - TIME_ALLOWED
    ]
    assert found == []


def test_контроль_сканер_времени_видит_запрещённое() -> None:
    tree = ast.parse("import time, asyncio\ntime.monotonic()\nasyncio.sleep(1)\ndatetime.now()")
    names = {_dotted(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert names <= TIME_CALLS and len(names) == 3


def test_datetime_всегда_с_часовым_поясом_во_всём_коде() -> None:
    """DTZ: naive-datetime запрещены; время для людей — только через явную зону."""
    found = []
    for m in ALL:
        for call, _ in calls(m):
            owner, name = _dotted(call)
            has_tz = (
                (name == "now" and len(call.args) >= 1)
                or (name == "fromtimestamp" and len(call.args) >= 2)
                or any(k.arg in ("tz", "tzinfo") for k in call.keywords)
            )
            if owner == "datetime" and name in {"utcnow", "today", "utcfromtimestamp"}:
                found.append(f"{m.rel}:{call.lineno}: {name}")
            if owner == "datetime" and name in {"now", "fromtimestamp"} and not has_tz:
                found.append(f"{m.rel}:{call.lineno}: {name} без зоны")
    assert found == []


def test_datetime_now_только_в_адаптере_часов_и_диагностике_cli() -> None:
    owners = {m.rel for m in ALL for call, _ in calls(m) if _dotted(call) == ("datetime", "now")}
    assert owners <= {"infrastructure/clock.py", "cli/setup.py"}
    assert "infrastructure/clock.py" in owners


def test_нет_голых_except() -> None:
    found = [
        f"{m.rel}:{node.lineno}"
        for m in ALL
        for node in ast.walk(m.tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]
    assert found == []


def test_except_exception_не_глотает_ошибку_молча() -> None:
    """Перехват Exception допустим, только если ошибка залогирована, возвращена или поднята."""
    found = []
    for m in ALL:
        for node in ast.walk(m.tree):
            broad = isinstance(node, ast.ExceptHandler) and ast.unparse(
                node.type or ast.Name("")
            ) in ("Exception", "BaseException")
            if isinstance(node, ast.ExceptHandler) and broad:
                body = node.body
                if all(isinstance(b, ast.Pass | ast.Continue) for b in body):
                    found.append(f"{m.rel}:{node.lineno}")
    assert found == []


def _prints(m: Module) -> list[int]:
    return [
        call.lineno
        for call, _ in calls(m)
        if isinstance(call.func, ast.Name) and call.func.id == "print"
    ]


PRINT_DEBT = {"runner.py", "infrastructure/gubuy/discovery.py"}


def test_print_только_в_cli_и_известных_отступлениях() -> None:
    owners = {m.rel for m in ALL if _prints(m) and not m.rel.startswith("cli/")}
    assert owners <= PRINT_DEBT


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T8: print() вне cli/: runner._simulate (runner.py) и DiscoveryRecorder.run "
    "(infrastructure/gubuy/discovery.py) печатают в консоль минуя логи и фильтр секретов. "
    "Ожидается вынос вывода в cli/ или обратный вызов",
)
def test_print_нет_вне_cli() -> None:
    assert {m.rel for m in ALL if _prints(m) and not m.rel.startswith("cli/")} == set()


def test_размер_модулей_не_больше_700_строк() -> None:
    big = {m.rel: len(m.text.splitlines()) for m in ALL if len(m.text.splitlines()) > 700}
    assert big == {}


def test_точки_входа_короткие() -> None:
    main = next(m for m in ALL if m.rel == "__main__.py")
    assert len(main.text.splitlines()) <= 100


def test_type_ignore_всегда_с_кодом_ошибки() -> None:
    found = [
        f"{m.rel}:{i}"
        for m in ALL
        for i, line in enumerate(m.text.splitlines(), start=1)
        if re.search(r"#\s*type:\s*ignore(?!\[)", line)
    ]
    assert found == []


def test_нет_assert_для_проверки_входных_данных_в_domain() -> None:
    """assert отключается флагом -O: инвариант домена обязан быть исключением."""
    found = [
        f"{m.rel}:{n.lineno}"
        for m in ALL
        if m.package == "domain"
        for n in ast.walk(m.tree)
        if isinstance(n, ast.Assert)
    ]
    assert found == []


def test_единственный_синглтон_это_redactor() -> None:
    redactors = [
        m.rel
        for m in ALL
        for node in m.tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "Redactor"
    ]
    assert redactors == ["infrastructure/logging/redaction.py"]
