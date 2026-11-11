"""Сборка AGENTS.md из модулей правил `.ai/` и проверка его актуальности.

AGENTS.md никогда не правится руками: меняется модуль, затем запускается синхронизация.
Цели две: корневой AGENTS.md (модули `.ai/universal` и `.ai/project`) и вложенный
`bots/<бот>/AGENTS.md` для каждого бота с каталогом `.ai/` (его модули). Codex читает
AGENTS.md от корня до рабочего каталога и склеивает их, поэтому бюджет размера считается
для пары «корень + бот».
Шлюз качества вызывает проверку (`--check`): изменение модуля без перегенерации ломает сборку.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_GROUPS = (".ai/universal", ".ai/project")
TARGET = "AGENTS.md"
SIZE_LIMIT_KIB = 30.0

ROOT_BANNER = """<!--
ГЕНЕРИРУЕМЫЙ ФАЙЛ — НЕ РЕДАКТИРОВАТЬ.
Источник истины: .ai/universal/*.md и .ai/project/*.md.
Измените модуль и выполните: uv run python dev.py sync-agents
-->

# Монорепозиторий чат-ботов — рабочие заметки для AI-ассистентов

Это точка входа для Codex и любых ассистентов, читающих `AGENTS.md` (Cursor, Copilot,
Gemini и др.). Файл собран из тех же модулей правил `.ai/`, что и `CLAUDE.md` для
Claude Code, поэтому все ассистенты следуют одинаковым правилам. Более поздние разделы
главнее более ранних; явное указание владельца в текущем диалоге главнее всего.

У каждого бота в `bots/<бот>/` есть свой `AGENTS.md` с правилами этого бота: работая в
боте, прочтите и его. Начиная сессию с нуля, выполните ритуал ориентации из
`13-progress-tracking.md` ниже.
Модули уровня «extended» приведены здесь краткой сутью — полный текст по указанному пути
обязателен к прочтению, когда задача их касается."""

BOT_BANNER = """<!--
ГЕНЕРИРУЕМЫЙ ФАЙЛ — НЕ РЕДАКТИРОВАТЬ.
Источник истины: {rel}/.ai/*.md.
Измените модуль и выполните: uv run python dev.py sync-agents
-->

# {name} — правила бота для AI-ассистентов

Дополняет корневой `AGENTS.md` монорепозитория: сначала действуют общие правила, затем эти.
Правила бота главнее общих там, где они строже или конкретнее."""

TIER_RE = re.compile(r"<!--\s*tier:\s*extended\s*-->")
ESSENCE_RE = re.compile(r"^>\s*\*\*Суть\.\*\*\s*(.+)$", re.M)


class SyncError(Exception):
    """Ошибка структуры модулей правил."""


@dataclass(frozen=True)
class Target:
    """Один собираемый AGENTS.md: откуда берутся модули и куда пишется результат."""

    directory: Path
    groups: tuple[str, ...]
    banner: str

    @property
    def output(self) -> Path:
        return self.directory / TARGET


def normalize(text: str) -> str:
    """Приводит переводы строк к LF и убирает хвостовые пробелы файла."""
    return text.replace("\r\n", "\n").strip("\n") + "\n"


def discover_targets(root: Path) -> list[Target]:
    targets = [Target(root, ROOT_GROUPS, ROOT_BANNER)]
    bots = root / "bots"
    if bots.is_dir():
        for bot in sorted(path for path in bots.iterdir() if (path / ".ai").is_dir()):
            rel = bot.relative_to(root).as_posix()
            banner = BOT_BANNER.format(rel=rel, name=bot.name)
            targets.append(Target(bot, (".ai",), banner))
    return targets


def collect_modules(target: Target) -> list[Path]:
    modules: list[Path] = []
    for group in target.groups:
        directory = target.directory / group
        if not directory.is_dir():
            raise SyncError(f"нет каталога модулей: {directory}")
        found = sorted(directory.glob("*.md"))
        if not found:
            raise SyncError(f"в каталоге {directory} нет модулей")
        modules.extend(found)
    return modules


def render_module(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    text = normalize(path.read_text(encoding="utf-8"))
    head = "\n".join(text.splitlines()[:5])
    if not TIER_RE.search(head):
        return f"<!-- module: {rel} -->\n\n{text.rstrip()}"

    title = next((line for line in text.splitlines() if line.startswith("# ")), None)
    essence = ESSENCE_RE.search(text)
    if title is None or essence is None:
        raise SyncError(
            f"{rel}: у модуля уровня extended нужны заголовок `# ...` и строка `> **Суть.** ...`"
        )
    return (
        f"<!-- module: {rel} (extended) -->\n\n{title}\n\n"
        f"**Суть.** {essence.group(1).strip()}\n\n"
        f"Полный текст обязателен к прочтению, когда задача касается этой темы: `{rel}`."
    )


def render_target(root: Path, target: Target) -> str:
    parts = [target.banner]
    parts.extend(render_module(root, path) for path in collect_modules(target))
    return normalize("\n\n---\n\n".join(parts))


def render(root: Path) -> str:
    """Корневой AGENTS.md."""
    return render_target(root, discover_targets(root)[0])


def kib(text: str) -> float:
    return len(text.encode("utf-8")) / 1024


def check_budget(rendered: dict[Path, str], root: Path) -> list[str]:
    """Корень отдельно и каждая пара «корень + бот» укладываются в бюджет Codex."""
    base = kib(rendered[root / TARGET])
    problems = []
    for path, content in rendered.items():
        size = base if path == root / TARGET else base + kib(content)
        if size > SIZE_LIMIT_KIB:
            rel = path.relative_to(root).as_posix()
            problems.append(
                f"{rel}: {size:.1f} КиБ (с корневым) при лимите {SIZE_LIMIT_KIB:.0f} КиБ. "
                "Сократите модуль или переведите ситуативный в `<!-- tier: extended -->` "
                "со строкой `> **Суть.** ...`."
            )
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sync-agents",
        description="Перегенерировать AGENTS.md из .ai/ или проверить его актуальность.",
    )
    parser.add_argument("--check", action="store_true", help="только проверить, не переписывая")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    try:
        rendered = {t.output: render_target(ROOT, t) for t in discover_targets(ROOT)}
    except SyncError as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1

    problems = check_budget(rendered, ROOT)
    for problem in problems:
        print(f"ОШИБКА: {problem}", file=sys.stderr)
    if problems:
        return 1

    failed = False
    for path, content in rendered.items():
        rel = path.relative_to(ROOT).as_posix()
        current = normalize(path.read_text(encoding="utf-8")) if path.exists() else ""
        if current == content:
            print(f"OK: {rel} актуален ({kib(content):.1f} КиБ).")
        elif args.check:
            print(
                f"ОШИБКА: {rel} разошёлся с модулями .ai/. "
                "Выполните: uv run python dev.py sync-agents",
                file=sys.stderr,
            )
            failed = True
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"{rel} перегенерирован ({kib(content):.1f} КиБ).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
