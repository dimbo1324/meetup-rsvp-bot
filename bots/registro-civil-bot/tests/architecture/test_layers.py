"""Границы слоёв (SPEC 2.3, 3.4; правила бота): каждая граница — тест на импорты.

Проверяются и прямые импорты (`ast`), и фактический состав загруженных модулей (отдельный
процесс Python): транзитивная зависимость от aiohttp в `domain` была бы видна только там.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.architecture.srcscan import SRC, Module, imports, modules

ALL = modules()
TOP_LEVEL = {m.package for m in ALL}
STDLIB = sys.stdlib_module_names

# Что каждый слой может импортировать из rcbot (первый уровень имени)
ALLOWED = {
    "domain": {"domain"},
    # из домена — только жёсткие пределы (domain/limits.py): см. отдельный тест ниже
    "config": {"config", "domain"},
    "application": {"application", "domain", "config", "__init__"},
    "infrastructure": {"infrastructure", "application", "domain", "config"},
    "bot": {"bot", "application", "domain", "config"},
    # корни сборки видят всё, но не друг друга «вверх»: bootstrap не знает про runner и cli
    "bootstrap": {"bootstrap", "__init__", "application", "domain", "config", "infrastructure"},
    "runner": {"runner", "bootstrap", "bot", "application", "domain", "config", "infrastructure"},
    "cli": {
        "cli",
        "__init__",
        "bootstrap",
        "runner",
        "bot",
        "application",
        "domain",
        "config",
        "infrastructure",
    },
}
# Известные отступления (находки): имя модуля → импорт. Сейчас их нет и список не должен расти.
KNOWN_DEBT: set[tuple[str, str]] = set()


def _rcbot_layer(name: str) -> str | None:
    parts = name.split(".")
    if parts[0] != "rcbot":
        return None
    # `from rcbot import __version__` — это корневой пакет, а не слой
    return parts[1] if len(parts) > 1 and parts[1] in TOP_LEVEL else "__init__"


def _violations(layer: str) -> list[tuple[str, int, str]]:
    found = []
    for module in ALL:
        if module.package != layer:
            continue
        for name, line in imports(module):
            if name.startswith("."):
                found.append((module.rel, line, f"относительный импорт {name}"))
                continue
            target = _rcbot_layer(name)
            if target is not None and target not in ALLOWED[layer]:
                found.append((module.rel, line, name))
    return found


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_слой_импортирует_только_разрешённое(layer: str) -> None:
    bad = [
        f"{rel}:{line}: {name}"
        for rel, line, name in _violations(layer)
        if (rel, name.split(".")[1] if name.startswith("rcbot.") else "") not in KNOWN_DEBT
    ]
    assert bad == []


def test_config_берёт_из_домена_только_пределы() -> None:
    found = {
        name
        for module in ALL
        if module.package == "config"
        for name, _ in imports(module)
        if name.startswith("rcbot.domain")
    }
    assert found
    assert {name.removeprefix("rcbot.domain.").split(".")[0] for name in found} == {"limits"}


def test_пределы_опроса_одни_и_те_же_в_домене_и_настройках() -> None:
    from rcbot.config import settings
    from rcbot.domain import limits

    for name in (
        "MIN_POLL_INTERVAL_SEC",
        "MIN_DOM_POLL_INTERVAL_SEC",
        "MAX_POLL_INTERVAL_SEC",
        "MIN_MONTHS_AHEAD",
        "MAX_MONTHS_AHEAD",
    ):
        assert getattr(settings, name) is getattr(limits, name)


def test_bot_не_импортирует_infrastructure() -> None:
    assert [v for v in _violations("bot") if "infrastructure" in v[2]] == []


def test_известные_отступления_не_растут() -> None:
    debt = {
        (rel, name.split(".")[1])
        for layer in ALLOWED
        for rel, _, name in _violations(layer)
        if name.startswith("rcbot.")
    }
    assert debt == KNOWN_DEBT


def test_все_модули_верхнего_уровня_известны_проверке() -> None:
    """Новый пакет в src/rcbot должен получить правило слоя, а не остаться без проверки."""
    assert {m.package for m in ALL} - {"__init__", "__main__"} <= set(ALLOWED)


# --- domain и application: состав модулей ---

DOMAIN_STDLIB = {"__future__", "dataclasses", "datetime", "enum", "functools", "hashlib"}
FORBIDDEN_THIRD_PARTY = {
    "aiohttp",
    "aiogram",
    "aiogram_dialog",
    "sqlalchemy",
    "playwright",
    "alembic",
    "keyring",
    "sentry_sdk",
    "yarl",
    "aiosqlite",
}


def test_domain_импортирует_только_стандартную_библиотеку_из_короткого_списка() -> None:
    found: set[str] = set()
    for module in ALL:
        if module.package == "domain":
            for name, _ in imports(module):
                top = name.split(".")[0]
                if top != "rcbot":
                    found.add(top)
                else:
                    assert name.startswith("rcbot.domain"), (module.rel, name)
    assert found <= DOMAIN_STDLIB, found - DOMAIN_STDLIB
    assert found <= set(STDLIB)


def _loaded_modules(import_lines: str) -> set[str]:
    code = (
        "import sys, json\n"
        f"{import_lines}\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    return set(json.loads(out.stdout))


def _import_all(package: str) -> str:
    names = [
        "rcbot." + m.rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for m in ALL
        if m.package == package
    ]
    return "\n".join(f"import {n}" for n in names)


def test_domain_в_отдельном_процессе_не_тянет_ничего_кроме_стандартной_библиотеки() -> None:
    loaded = _loaded_modules(_import_all("domain"))
    third_party = {
        m
        for m in loaded
        if m not in STDLIB and m not in ("rcbot", "_distutils_hack", "_virtualenv", "__main__")
    }
    assert third_party == set()


def test_application_в_отдельном_процессе_не_тянет_сеть_базу_браузер_и_telegram() -> None:
    loaded = _loaded_modules(_import_all("application"))
    assert loaded & FORBIDDEN_THIRD_PARTY == set()


def test_application_не_загружает_слои_выше_себя() -> None:
    code = (
        "import sys, json\n"
        f"{_import_all('application')}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('rcbot'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    loaded = json.loads(out.stdout)
    bad = [m for m in loaded if m.split(".")[1:2] and m.split(".")[1] not in ALLOWED["application"]]
    assert bad == []


def test_контроль_сканер_импортов_видит_запрещённое() -> None:
    import ast

    fake = Module(
        SRC / "domain" / "fake.py", ast.parse("import aiohttp\nfrom rcbot.bot import app"), ""
    )
    assert {n for n, _ in imports(fake)} >= {"aiohttp", "rcbot.bot"}


# --- циклы ---


def _graph() -> dict[str, set[str]]:
    names = {
        "rcbot." + m.rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__"): m
        for m in ALL
    }
    graph: dict[str, set[str]] = {n: set() for n in names}
    for name, module in names.items():
        for target, _ in imports(module):
            if target in names and target != name:
                graph[name].add(target)
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in graph[node]:
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = []
            while True:
                top = stack.pop()
                on_stack.discard(top)
                component.append(top)
                if top == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for node in graph:
        if node not in index:
            visit(node)
    return result


def test_циклических_зависимостей_между_модулями_нет() -> None:
    assert _cycles(_graph()) == []


def test_контроль_поиск_циклов_находит_цикл() -> None:
    assert _cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": set()}) == [["a", "b", "c"]]
    assert _cycles({"a": {"b"}, "b": set()}) == []
