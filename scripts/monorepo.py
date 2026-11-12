"""Раскладка монорепозитория: обнаружение ботов, контракт каталога бота, границы между ботами.

Только стандартная библиотека: модуль импортирует `dev.py`, который работает до `uv sync`.
Контракт описан в docs/monorepo.md; проверка запускается в шлюзе качества.
"""

from __future__ import annotations

import ast
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOTS_DIR = "bots"
PACKAGES_DIR = "packages"

# Файлы, без которых каталог бота не считается самостоятельным проектом.
REQUIRED_FILES = (
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    ".env.example",
    "CLAUDE.md",
    "docs/ROADMAP.md",
    "docs/architecture/overview.md",
    "docs/open-questions.md",
)
REQUIRED_DIRS = ("tests",)
PLATFORMS = ("linux-docker", "windows-desktop")


class LayoutError(Exception):
    """Каталог бота не соответствует контракту."""


@dataclass(frozen=True)
class Bot:
    name: str
    path: Path
    dist: str
    package: str
    platform: str
    env: dict[str, str] = field(default_factory=dict)
    setup: tuple[tuple[str, ...], ...] = ()
    coverage: tuple[str, ...] = ()
    coverage_min: float = 0.0

    @property
    def has_migrations(self) -> bool:
        return (self.path / "alembic.ini").is_file()

    def resolved_env(self) -> dict[str, str]:
        """Значения по умолчанию из `[tool.monorepo].env`; относительные пути — от каталога бота.

        Переменная, уже заданная в окружении вызова, не перекрывается: так worktree агента
        или CI могут указать общий каталог браузера (PLAYWRIGHT_BROWSERS_PATH).
        """
        return {
            key: str((self.path / value).resolve()) if value.startswith(".") else value
            for key, value in self.env.items()
            if key not in os.environ
        }

    def coverage_args(self) -> list[str]:
        """Ключи pytest-cov для полного прогона: порог покрытия выбранных пакетов."""
        if not self.coverage or self.coverage_min <= 0:
            return []
        return [
            *(f"--cov={module}" for module in self.coverage),
            "--cov-report=term-missing:skip-covered",
            f"--cov-fail-under={self.coverage_min:g}",
        ]


def _load_bot(path: Path) -> Bot:
    pyproject = path / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    rel = path.relative_to(path.parent.parent).as_posix()
    try:
        dist = str(data["project"]["name"])
        meta = data["tool"]["monorepo"]
        package = str(meta["package"])
        platform = str(meta["platform"])
    except KeyError as error:
        raise LayoutError(
            f"{rel}/pyproject.toml: нет ключа {error} ([project].name, [tool.monorepo])"
        ) from error
    if platform not in PLATFORMS:
        raise LayoutError(f"{rel}: platform={platform!r}, допустимо: {', '.join(PLATFORMS)}")
    env = {str(k): str(v) for k, v in meta.get("env", {}).items()}
    setup = tuple(tuple(str(part) for part in step) for step in meta.get("setup", []))
    coverage = tuple(str(module) for module in meta.get("coverage", []))
    coverage_min = float(meta.get("coverage_min", 0))
    return Bot(path.name, path, dist, package, platform, env, setup, coverage, coverage_min)


def discover_bots(root: Path = ROOT) -> list[Bot]:
    directory = root / BOTS_DIR
    if not directory.is_dir():
        return []
    return [
        _load_bot(path)
        for path in sorted(directory.iterdir())
        if path.is_dir() and (path / "pyproject.toml").is_file()
    ]


def find_bot(name: str, root: Path = ROOT) -> Bot:
    bots = discover_bots(root)
    for bot in bots:
        if name in (bot.name, bot.dist, bot.package):
            return bot
    known = ", ".join(bot.name for bot in bots) or "нет ни одного"
    raise LayoutError(f"бот {name!r} не найден; есть: {known}")


def _imported_roots(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def check_layout(root: Path = ROOT) -> list[str]:
    """Нарушения контракта и границ: пустой список — всё в порядке."""
    try:
        bots = discover_bots(root)
    except LayoutError as error:
        return [str(error)]
    problems: list[str] = []
    for bot in bots:
        rel = f"{BOTS_DIR}/{bot.name}"
        problems += [
            f"{rel}: нет файла {name}" for name in REQUIRED_FILES if not (bot.path / name).is_file()
        ]
        problems += [
            f"{rel}: нет каталога {name}/"
            for name in REQUIRED_DIRS
            if not (bot.path / name).is_dir()
        ]
        if not (bot.path / "src" / bot.package / "__init__.py").is_file():
            problems.append(f"{rel}: нет пакета src/{bot.package}/__init__.py")
        if (bot.path / ".ai").is_dir() and not (bot.path / "AGENTS.md").is_file():
            problems.append(f"{rel}: есть .ai/, но нет сгенерированного AGENTS.md")

    packages = {bot.package: bot for bot in bots}
    if len(packages) != len(bots):
        problems.append("у двух ботов одинаковое имя пакета в [tool.monorepo].package")
    # Боты независимы: общий код выносится в packages/, а не импортируется из соседа.
    for bot in bots:
        foreign = set(packages) - {bot.package}
        for directory in ("src", "tests"):
            for source in sorted((bot.path / directory).rglob("*.py")):
                for name in sorted(_imported_roots(source) & foreign):
                    rel = source.relative_to(root).as_posix()
                    problems.append(f"{rel}: импорт пакета другого бота `{name}`")
    return problems
