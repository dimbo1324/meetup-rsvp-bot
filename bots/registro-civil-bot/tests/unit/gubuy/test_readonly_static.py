"""Инвариант SPEC 0.3 в исходниках: адаптер страницы SAE только читает (решения R12, R13).

Разбор через `ast`, а не поиск по тексту: слова из докстрингов не мешают.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import rcbot.infrastructure.gubuy as gubuy_package

PACKAGE = Path(gubuy_package.__file__).parent
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


def _modules() -> list[Path]:
    return sorted(PACKAGE.glob("*.py"))


def test_в_пакете_только_адаптер_страницы_sae() -> None:
    assert [path.name for path in _modules()] == ["__init__.py", "sae_source.py"]


def test_в_строках_адаптера_нет_запрещённых_кнопок() -> None:
    for path in _modules():
        for literal in _string_literals(path.read_text(encoding="utf-8")):
            lowered = literal.lower()
            assert not any(word in lowered for word in FORBIDDEN_WORDS), f"{path.name}: {literal!r}"


def test_адаптер_делает_только_get_и_не_отправляет_форм() -> None:
    text = (PACKAGE / "sae_source.py").read_text(encoding="utf-8")
    assert '"GET"' in text
    assert not re.search(r'"(POST|PUT|PATCH|DELETE)"', text)
    assert ".submit(" not in text
