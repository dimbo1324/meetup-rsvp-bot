from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts import monorepo

ROOT = Path(__file__).resolve().parent.parent


def _make_bot(root: Path, name: str, package: str, *, code: str = "") -> Path:
    bot = root / "bots" / name
    for rel in monorepo.REQUIRED_FILES:
        (bot / rel).parent.mkdir(parents=True, exist_ok=True)
        (bot / rel).write_text("", encoding="utf-8")
    (bot / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\n\n[tool.monorepo]\npackage = "{package}"\n'
        'platform = "linux-docker"\nenv = { PROBE = "./.cache", MODE = "test" }\n',
        encoding="utf-8",
    )
    (bot / "tests").mkdir()
    (bot / "src" / package).mkdir(parents=True)
    (bot / "src" / package / "__init__.py").write_text(code, encoding="utf-8")
    return bot


def test_реальный_репозиторий_соответствует_контракту() -> None:
    assert monorepo.check_layout(ROOT) == []
    names = {bot.name for bot in monorepo.discover_bots(ROOT)}
    assert {"meetup-rsvp-bot", "registro-civil-bot"} <= names


def test_корректная_раскладка_проходит(tmp_path: Path) -> None:
    _make_bot(tmp_path, "a-bot", "abot")
    _make_bot(tmp_path, "b-bot", "bbot", code="import json\nfrom . import x\n")
    assert monorepo.check_layout(tmp_path) == []


def test_импорт_пакета_другого_бота_запрещён(tmp_path: Path) -> None:
    _make_bot(tmp_path, "a-bot", "abot")
    _make_bot(tmp_path, "b-bot", "bbot", code="from abot.services import thing\n")
    problems = monorepo.check_layout(tmp_path)
    assert problems == ["bots/b-bot/src/bbot/__init__.py: импорт пакета другого бота `abot`"]


def test_отсутствующие_файлы_контракта_перечислены(tmp_path: Path) -> None:
    bot = _make_bot(tmp_path, "a-bot", "abot")
    (bot / "docs" / "ROADMAP.md").unlink()
    shutil.rmtree(bot / "tests")
    problems = monorepo.check_layout(tmp_path)
    assert "bots/a-bot: нет файла docs/ROADMAP.md" in problems
    assert "bots/a-bot: нет каталога tests/" in problems


def test_без_таблицы_monorepo_понятная_ошибка(tmp_path: Path) -> None:
    bot = _make_bot(tmp_path, "a-bot", "abot")
    (bot / "pyproject.toml").write_text('[project]\nname = "a-bot"\n', encoding="utf-8")
    [problem] = monorepo.check_layout(tmp_path)
    assert "[tool.monorepo]" in problem


def test_окружение_бота_разрешает_относительные_пути(tmp_path: Path) -> None:
    bot_dir = _make_bot(tmp_path, "a-bot", "abot")
    bot = monorepo.find_bot("abot", tmp_path)
    env = bot.resolved_env()
    assert env["MODE"] == "test"
    assert Path(env["PROBE"]) == (bot_dir / ".cache").resolve()


def test_неизвестный_бот(tmp_path: Path) -> None:
    _make_bot(tmp_path, "a-bot", "abot")
    with pytest.raises(monorepo.LayoutError, match="a-bot"):
        monorepo.find_bot("nope", tmp_path)
