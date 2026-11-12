"""Инвариант SPEC 0.3 в исходниках: что вообще умеет трогать код, работающий с календарём.

Дополняет браузерные тесты: те доказывают поведение на макете, этот — что в коде нет других
действий на странице. Разбор через `ast`, а не поиск по тексту: слова из докстрингов не мешают.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import rcbot.infrastructure.gubuy as gubuy_package

PACKAGE = Path(gubuy_package.__file__).parent
PAGE_ACTIONS = re.compile(
    r"\.(click|dblclick|check|uncheck|set_checked|fill|press|type|tap|hover|drag_to|"
    r"select_option|dispatch_event|press_sequentially|set_input_files)\("
)
TEXT_INPUT = re.compile(r"\.(fill|press|type|press_sequentially|select_option)\(")
FORBIDDEN_WORDS = ("siguiente", "guardar", "cerrar", "volver", "pagar", "pago", "efectuar")


def _string_literals(source: str) -> list[str]:
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _action_lines(name: str) -> list[tuple[str, str]]:
    lines = (PACKAGE / name).read_text(encoding="utf-8").splitlines()
    found = []
    for line in lines:
        match = PAGE_ACTIONS.search(line)
        if match:
            found.append((match.group(1), line.strip()))
    return found


@pytest.mark.parametrize("name", ["dom_source.py", "discovery.py"])
def test_в_коде_чтения_календаря_нет_запрещённых_кнопок_в_строках(name: str) -> None:
    for literal in _string_literals((PACKAGE / name).read_text(encoding="utf-8")):
        lowered = literal.lower()
        assert not any(word in lowered for word in FORBIDDEN_WORDS), f"{name}: {literal!r}"


@pytest.mark.parametrize("name", ["dom_source.py", "discovery.py"])
def test_календарь_трогают_только_стрелка_месяца_и_радиокнопка_офиса(name: str) -> None:
    actions = _action_lines(name)
    assert sorted(kind for kind, _ in actions) == ["check", "click"], actions
    by_kind = dict(actions)
    assert "ui-datepicker-next" in by_kind["click"] or "NEXT_MONTH" in by_kind["click"]
    assert "radio" in by_kind["check"]


@pytest.mark.parametrize("name", ["dom_source.py", "discovery.py", "probe.py", "api_source.py"])
def test_источники_не_вводят_текст_и_не_отправляют_формы(name: str) -> None:
    text = (PACKAGE / name).read_text(encoding="utf-8")
    assert not TEXT_INPUT.search(text)
    assert ".submit(" not in text
    assert not re.search(r'"(POST|PUT|PATCH|DELETE)"', text)


def test_api_source_делает_только_get() -> None:
    text = (PACKAGE / "api_source.py").read_text(encoding="utf-8")
    assert '"GET"' in text


def test_ввод_данных_есть_только_во_входе_на_странице_id_uruguay() -> None:
    # fill/click по тексту — единственное место: auth.py (страница ID Uruguay, не календарь)
    owners = {
        path.name
        for path in PACKAGE.glob("*.py")
        if TEXT_INPUT.search(path.read_text(encoding="utf-8"))
    }
    assert owners == {"auth.py"}
