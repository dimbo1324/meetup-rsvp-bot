"""Единая точка входа для рутинных задач проекта (только стандартная библиотека).

Запуск: `uv run python dev.py <команда>`; список команд: `uv run python dev.py list`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Русский вывод не должен падать на консолях и пайпах с кодировкой cp1252 (Windows).
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
SUBPROCESS_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def run(*cmd: str) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=ROOT, env=SUBPROCESS_ENV)


def uv(*args: str) -> int:
    return run("uv", "run", *args)


def check_mirrors() -> int:
    """Паритет зеркал: агенты и навыки `.claude/` и `.codex/` совпадают по именам."""
    claude_agents = {p.stem.removeprefix("meetup-") for p in (ROOT / ".claude/agents").glob("*.md")}
    codex_agents = {p.stem for p in (ROOT / ".codex/agents").glob("*.toml")}
    claude_skills = {p.parent.name for p in (ROOT / ".claude/skills").glob("*/SKILL.md")}
    codex_skills = {p.parent.name for p in (ROOT / ".codex/skills").glob("*/SKILL.md")}
    problems = [
        f"агенты: {sorted(claude_agents ^ codex_agents)}" if claude_agents != codex_agents else "",
        f"навыки: {sorted(claude_skills ^ codex_skills)}" if claude_skills != codex_skills else "",
    ]
    problems = [p for p in problems if p]
    if problems:
        print("ОШИБКА: зеркала .claude/ и .codex/ разошлись — " + "; ".join(problems))
        return 1
    print("OK: зеркала .claude/ и .codex/ совпадают.")
    return 0


def cmd_doctor(_: list[str]) -> int:
    ok = True
    if sys.version_info[:2] != (3, 14):
        print(
            f"ВНИМАНИЕ: запущено на Python {sys.version_info.major}.{sys.version_info.minor}, "
            "проект требует 3.14 (используйте `uv run`)."
        )
    for tool in ("uv", "git"):
        found = shutil.which(tool)
        print(f"{'OK ' if found else 'НЕТ'} {tool}: {found or 'не найден в PATH'}")
        ok &= bool(found)
    print(
        f"{'OK ' if (ROOT / '.env').exists() else 'НЕТ'} .env "
        "(создаётся командой `setup` из .env.example)"
    )
    print(f"{'OK ' if (ROOT / '.venv').exists() else 'НЕТ'} .venv (создаётся командой `setup`)")
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    print(f"{'OK ' if hook.exists() else 'НЕТ'} pre-commit-хук")
    return 0 if ok else 1


def cmd_setup(_: list[str]) -> int:
    if run("uv", "sync") != 0:
        return 1
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists():
        shutil.copyfile(example, env)
        print("Создан .env из .env.example — впишите СВОЙ тестовый токен.")
    return uv("pre-commit", "install")


def cmd_sync_agents(args: list[str]) -> int:
    return uv("python", "-m", "scripts.sync_agents", *args)


def cmd_format(_: list[str]) -> int:
    return uv("ruff", "check", "--fix", ".") or uv("ruff", "format", ".")


def cmd_lint(_: list[str]) -> int:
    return uv("ruff", "check", ".") or uv("ruff", "format", "--check", ".")


def cmd_typecheck(_: list[str]) -> int:
    return uv("mypy")


def cmd_test(args: list[str]) -> int:
    return uv("pytest", *args)


def cmd_migrate(_: list[str]) -> int:
    return uv("alembic", "upgrade", "head")


def cmd_run(_: list[str]) -> int:
    return cmd_migrate([]) or uv("python", "-m", "meetup_bot")


def cmd_quality_gate(_: list[str]) -> int:
    steps: list[tuple[str, Callable[[], int]]] = [
        ("синхронизация AGENTS.md", lambda: cmd_sync_agents(["--check"])),
        ("паритет зеркал", check_mirrors),
        ("lint", lambda: cmd_lint([])),
        ("typecheck", lambda: cmd_typecheck([])),
        ("test", lambda: cmd_test([])),
    ]
    for name, step in steps:
        print(f"\n=== {name} ===", flush=True)
        if step() != 0:
            print(f"\nШЛЮЗ КАЧЕСТВА НЕ ПРОЙДЕН на шаге «{name}».")
            return 1
    print("\nШЛЮЗ КАЧЕСТВА ПРОЙДЕН.")
    return 0


COMMANDS: dict[str, tuple[Callable[[list[str]], int], str]] = {
    "doctor": (cmd_doctor, "проверить окружение"),
    "setup": (cmd_setup, "uv sync, .env, pre-commit-хуки"),
    "sync-agents": (cmd_sync_agents, "перегенерировать AGENTS.md (--check — только проверить)"),
    "format": (cmd_format, "ruff format + ruff check --fix"),
    "lint": (cmd_lint, "ruff check + ruff format --check"),
    "typecheck": (cmd_typecheck, "mypy"),
    "test": (cmd_test, "pytest (аргументы передаются дальше)"),
    "quality-gate": (cmd_quality_gate, "полный шлюз качества перед слиянием"),
    "migrate": (cmd_migrate, "alembic upgrade head"),
    "run": (cmd_run, "применить миграции и запустить бота"),
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev.py", description=__doc__)
    parser.add_argument("command", choices=[*COMMANDS, "list"], help="команда или `list`")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="аргументы команды")
    args = parser.parse_args(argv)
    if args.command == "list":
        for name, (_, description) in COMMANDS.items():
            print(f"{name:14} {description}")
        return 0
    handler, _ = COMMANDS[args.command]
    return handler(args.rest)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
