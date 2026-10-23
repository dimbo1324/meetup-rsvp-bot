"""Сборка AGENTS.md из модулей правил `.ai/` и проверка его актуальности.

AGENTS.md никогда не правится руками: меняется модуль, затем запускается синхронизация.
Шлюз качества вызывает проверку (`--check`), поэтому изменение модуля без перегенерации
`AGENTS.md` ломает сборку.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS = (".ai/universal", ".ai/project")
TARGET = "AGENTS.md"
SIZE_LIMIT_KIB = 30.0

BANNER = """<!--
ГЕНЕРИРУЕМЫЙ ФАЙЛ — НЕ РЕДАКТИРОВАТЬ.
Источник истины: .ai/universal/*.md и .ai/project/*.md.
Измените модуль и выполните: uv run python dev.py sync-agents
-->

# meetup-rsvp-bot — рабочие заметки для AI-ассистентов

Это точка входа для Codex и любых ассистентов, читающих `AGENTS.md` (Cursor, Copilot,
Gemini и др.). Файл собран из тех же модулей правил `.ai/`, что и `CLAUDE.md` для
Claude Code, поэтому все ассистенты следуют одинаковым правилам. Более поздние разделы
главнее более ранних; явное указание владельца в текущем диалоге главнее всего.

Начиная сессию с нуля, выполните ритуал ориентации из `13-progress-tracking.md` ниже.
Модули уровня «extended» приведены здесь краткой сутью — полный текст по указанному пути
обязателен к прочтению, когда задача их касается."""

TIER_RE = re.compile(r"<!--\s*tier:\s*extended\s*-->")
ESSENCE_RE = re.compile(r"^>\s*\*\*Суть\.\*\*\s*(.+)$", re.M)


class SyncError(Exception):
    """Ошибка структуры модулей правил."""


def normalize(text: str) -> str:
    """Приводит переводы строк к LF и убирает хвостовые пробелы файла."""
    return text.replace("\r\n", "\n").strip("\n") + "\n"


def collect_modules(root: Path) -> list[Path]:
    modules: list[Path] = []
    for group in GROUPS:
        directory = root / group
        if not directory.is_dir():
            raise SyncError(f"нет каталога модулей: {group}")
        found = sorted(directory.glob("*.md"))
        if not found:
            raise SyncError(f"в каталоге {group} нет модулей")
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


def render(root: Path) -> str:
    parts = [BANNER]
    parts.extend(render_module(root, path) for path in collect_modules(root))
    return normalize("\n\n---\n\n".join(parts))


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
        content = render(ROOT)
    except SyncError as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1

    size = len(content.encode("utf-8")) / 1024
    if size > SIZE_LIMIT_KIB:
        print(
            f"ОШИБКА: AGENTS.md занимает {size:.1f} КиБ при лимите {SIZE_LIMIT_KIB:.0f} КиБ. "
            "Сократите модуль или переведите ситуативный в `<!-- tier: extended -->` "
            "со строкой `> **Суть.** ...`.",
            file=sys.stderr,
        )
        return 1

    target = ROOT / TARGET
    current = normalize(target.read_text(encoding="utf-8")) if target.exists() else ""

    if args.check:
        if current == content:
            print(f"OK: AGENTS.md соответствует модулям .ai/ ({size:.1f} КиБ).")
            return 0
        print(
            "ОШИБКА: AGENTS.md разошёлся с модулями .ai/. "
            "Выполните: uv run python dev.py sync-agents",
            file=sys.stderr,
        )
        return 1

    if current == content:
        print(f"AGENTS.md уже актуален ({size:.1f} КиБ).")
        return 0
    target.write_text(content, encoding="utf-8", newline="\n")
    print(f"AGENTS.md перегенерирован ({size:.1f} из {SIZE_LIMIT_KIB:.0f} КиБ).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
