"""Единая точка входа для рутинных задач монорепозитория (только стандартная библиотека).

Запуск: `uv run python dev.py <команда> [--bot <бот>] [аргументы]`; список: `dev.py list`.
Без `--bot` проверки идут по всем ботам из bots/; команды запуска требуют `--bot`, если
ботов больше одного. Устройство репозитория — docs/monorepo.md.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from scripts.monorepo import Bot, LayoutError, check_layout, discover_bots, find_bot

ROOT = Path(__file__).resolve().parent

# Русский вывод не должен падать на консолях и пайпах с кодировкой cp1252 (Windows).
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
SUBPROCESS_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def run(*cmd: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> int:
    where = "" if cwd == ROOT else f"[{cwd.relative_to(ROOT).as_posix()}] "
    print(f"$ {where}{' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=cwd, env={**SUBPROCESS_ENV, **(env or {})})


def uv(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> int:
    # --all-packages: общий .venv всегда содержит всех ботов; без него `uv run` в каталоге
    # бота синхронизировал бы окружение только под этого бота.
    return run("uv", "run", "--all-packages", *args, cwd=cwd, env=env)


def in_bot(bot: Bot, *args: str) -> int:
    return uv(*args, cwd=bot.path, env=bot.resolved_env())


def split_bot(args: list[str]) -> tuple[str | None, list[str]]:
    if len(args) >= 2 and args[0] == "--bot":
        return args[1], args[2:]
    return None, args


def selected_bots(name: str | None) -> list[Bot]:
    return [find_bot(name)] if name else discover_bots()


def single_bot(name: str | None) -> Bot:
    if name:
        return find_bot(name)
    bots = discover_bots()
    if len(bots) == 1:
        return bots[0]
    names = ", ".join(bot.name for bot in bots)
    raise LayoutError(f"укажите бота: --bot <имя> (есть: {names})")


def each(bots: list[Bot], step: Callable[[Bot], int]) -> int:
    """Выполняет шаг для каждого бота и не останавливается на первом сбое."""
    failed = [bot.name for bot in bots if step(bot) != 0]
    if failed:
        print(f"Сбой у ботов: {', '.join(failed)}")
    return 1 if failed else 0


def check_mirrors() -> int:
    """Паритет зеркал: агенты и навыки `.claude/` и `.codex/` совпадают по именам."""
    claude_agents = {p.stem for p in (ROOT / ".claude/agents").glob("*.md")}
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


def cmd_check_layout(_: list[str]) -> int:
    problems = check_layout()
    for problem in problems:
        print(f"ОШИБКА: {problem}")
    if problems:
        return 1
    print(f"OK: раскладка и границы ботов ({len(discover_bots())}) в порядке.")
    return 0


def cmd_bots(_: list[str]) -> int:
    for bot in discover_bots():
        print(f"{bot.name:22} пакет {bot.package:10} платформа {bot.platform}")
    return 0


def cmd_doctor(_: list[str]) -> int:
    ok = True
    if sys.version_info[:2] != (3, 14):
        print(
            f"ВНИМАНИЕ: запущено на Python {sys.version_info.major}.{sys.version_info.minor}, "
            "проект требует 3.14 (используйте `uv run`)."
        )
    for tool, required in (("uv", True), ("git", True), ("docker", False)):
        found = shutil.which(tool)
        mark = "OK " if found else ("НЕТ" if required else "-- ")
        print(f"{mark} {tool}: {found or 'не найден в PATH'}")
        ok &= bool(found) or not required
    print(f"{'OK ' if (ROOT / '.venv').exists() else 'НЕТ'} .venv (создаётся командой `setup`)")
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    print(f"{'OK ' if hook.exists() else 'НЕТ'} pre-commit-хук")
    for bot in discover_bots():
        env = bot.path / ".env"
        print(f"{'OK ' if env.exists() else 'НЕТ'} {bot.name}: .env (из .env.example)")
    return 0 if ok else 1


def cmd_setup(args: list[str]) -> int:
    name, _ = split_bot(args)
    # --locked: ставится ровно uv.lock; разошёлся с pyproject — отказ, а не тихое переразрешение
    if run("uv", "sync", "--locked", "--all-packages", "--all-groups") != 0:
        return 1
    for bot in selected_bots(name):
        env, example = bot.path / ".env", bot.path / ".env.example"
        if not env.exists():
            shutil.copyfile(example, env)
            print(f"Создан {env.relative_to(ROOT)} из .env.example — заполните своими значениями.")
        for step in bot.setup:
            if in_bot(bot, *step) != 0:
                return 1
    return uv("pre-commit", "install")


def cmd_sync_agents(args: list[str]) -> int:
    return uv("python", "-m", "scripts.sync_agents", *args)


def cmd_format(_: list[str]) -> int:
    # Оба шага всегда: оставшиеся ошибки линтера не должны отменять форматирование
    fixed = uv("ruff", "check", "--fix", ".")
    return uv("ruff", "format", ".") or fixed


def cmd_lint(_: list[str]) -> int:
    return uv("ruff", "check", ".") or uv("ruff", "format", "--check", ".")


def cmd_typecheck(args: list[str]) -> int:
    name, _ = split_bot(args)
    tooling = 0 if name else uv("mypy")
    return each(selected_bots(name), lambda bot: in_bot(bot, "mypy")) or tooling


def cmd_test(args: list[str]) -> int:
    name, rest = split_bot(args)
    tooling = 0 if name else uv("pytest", *rest)
    # Полный прогон бота (без своих аргументов) проверяет ещё и порог покрытия его конфигурации
    return (
        each(
            selected_bots(name),
            lambda bot: in_bot(bot, "pytest", *(rest or bot.coverage_args())),
        )
        or tooling
    )


def cmd_migrate(args: list[str]) -> int:
    bot = single_bot(split_bot(args)[0])
    if not bot.has_migrations:
        print(f"У бота {bot.name} нет alembic.ini — миграций нет.")
        return 0
    return in_bot(bot, "alembic", "upgrade", "head")


def cmd_run(args: list[str]) -> int:
    name, rest = split_bot(args)
    bot = single_bot(name)
    return cmd_migrate(["--bot", bot.name]) or in_bot(bot, "python", "-m", bot.package, *rest)


def cmd_audit(_: list[str]) -> int:
    # Проверка установленных зависимостей по базам уязвимостей; нужен интернет, поэтому
    # в шлюз качества не входит.
    return uv("pip-audit", "--skip-editable", "--progress-spinner", "off")


def cmd_quality_gate(args: list[str]) -> int:
    name, _ = split_bot(args)
    scope = ["--bot", name] if name else []
    steps: list[tuple[str, Callable[[], int]]] = [
        ("синхронизация AGENTS.md", lambda: cmd_sync_agents(["--check"])),
        ("паритет зеркал", check_mirrors),
        ("раскладка ботов", lambda: cmd_check_layout([])),
        ("lint", lambda: cmd_lint([])),
        ("typecheck", lambda: cmd_typecheck(scope)),
        ("test", lambda: cmd_test(scope)),
    ]
    for step_name, step in steps:
        print(f"\n=== {step_name} ===", flush=True)
        if step() != 0:
            print(f"\nШЛЮЗ КАЧЕСТВА НЕ ПРОЙДЕН на шаге «{step_name}».")
            return 1
    print("\nШЛЮЗ КАЧЕСТВА ПРОЙДЕН.")
    return 0


COMMANDS: dict[str, tuple[Callable[[list[str]], int], str]] = {
    "doctor": (cmd_doctor, "проверить окружение"),
    "setup": (cmd_setup, "uv sync всех ботов, .env ботов, шаги setup ботов, pre-commit-хуки"),
    "bots": (cmd_bots, "список ботов монорепозитория"),
    "sync-agents": (cmd_sync_agents, "перегенерировать AGENTS.md (--check — только проверить)"),
    "check-layout": (cmd_check_layout, "контракт каталогов ботов и границы между ними"),
    "format": (cmd_format, "ruff format + ruff check --fix (весь репозиторий)"),
    "lint": (cmd_lint, "ruff check + ruff format --check (весь репозиторий)"),
    "typecheck": (cmd_typecheck, "mypy инструментов и ботов [--bot]"),
    "test": (cmd_test, "pytest инструментов и ботов [--bot], аргументы передаются дальше"),
    "quality-gate": (cmd_quality_gate, "полный шлюз качества перед слиянием [--bot]"),
    "migrate": (cmd_migrate, "alembic upgrade head для бота [--bot]"),
    "run": (cmd_run, "применить миграции и запустить бота [--bot]"),
    "audit": (cmd_audit, "pip-audit зависимостей (нужен интернет)"),
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
    try:
        return handler(args.rest)
    except LayoutError as error:
        print(f"ОШИБКА: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
