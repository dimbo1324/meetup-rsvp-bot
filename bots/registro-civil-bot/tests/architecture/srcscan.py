"""Общие помощники статических проверок: обход исходников `src/rcbot` через `ast`."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import rcbot

SRC = Path(rcbot.__file__).resolve().parent


@dataclass(frozen=True)
class Module:
    path: Path
    tree: ast.Module
    text: str

    @property
    def rel(self) -> str:
        return self.path.relative_to(SRC).as_posix()

    @property
    def package(self) -> str:
        """Первый уровень: `domain`, `application`, `bot`… или имя модуля верхнего уровня."""
        first = self.rel.split("/")[0]
        return first.removesuffix(".py")


def modules() -> list[Module]:
    result = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        result.append(Module(path, ast.parse(text), text))
    return result


def imports(module: Module) -> list[tuple[str, int]]:
    """Все импортируемые модули (полные имена) и номера строк, включая вложенные и TYPE_CHECKING."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # относительный импорт помечается ведущими точками: слои проверяются по полным именам
            name = "." * node.level + (node.module or "")
            found.append((name, node.lineno))
            # `from rcbot import x` и `from rcbot.infrastructure import notify` ведут в подмодуль
            found += [(f"{name}.{alias.name}", node.lineno) for alias in node.names]
    return found


def docstring_nodes(tree: ast.AST) -> set[int]:
    """Идентификаторы узлов-докстрингов: они описывают запрещённое, а не делают его."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def string_constants(module: Module) -> Iterator[tuple[str, int]]:
    skip = docstring_nodes(module.tree)
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.value, node.lineno


def enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Для каждого узла-вызова — имя ближайшей функции, в которой он находится."""
    owner: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current
            )
            owner[id(child)] = name
            visit(child, name)

    visit(tree, "<module>")
    return owner


def calls(module: Module) -> Iterator[tuple[ast.Call, str]]:
    owner = enclosing_functions(module.tree)
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Call):
            yield node, owner.get(id(node), "<module>")


def attr_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else ""


def receiver(call: ast.Call) -> str:
    func = call.func
    return ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
