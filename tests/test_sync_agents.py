from pathlib import Path

from scripts import sync_agents

ROOT = Path(__file__).resolve().parent.parent


def test_все_agents_md_синхронизированы_с_модулями() -> None:
    targets = sync_agents.discover_targets(ROOT)
    assert len(targets) > 1, "у ботов с каталогом .ai/ должен быть свой AGENTS.md"
    for target in targets:
        content = sync_agents.render_target(ROOT, target)
        current = sync_agents.normalize(target.output.read_text(encoding="utf-8"))
        assert current == content, target.output


def test_размер_agents_md_в_бюджете() -> None:
    targets = sync_agents.discover_targets(ROOT)
    rendered = {t.output: sync_agents.render_target(ROOT, t) for t in targets}
    assert sync_agents.check_budget(rendered, ROOT) == []


def test_бюджет_считается_вместе_с_корневым(tmp_path: Path) -> None:
    root_md = tmp_path / sync_agents.TARGET
    bot_md = tmp_path / "bots" / "x" / sync_agents.TARGET
    half = "я" * int(sync_agents.SIZE_LIMIT_KIB * 1024 / 2 / 2 + 64)  # «я» — два байта UTF-8
    problems = sync_agents.check_budget({root_md: half, bot_md: half}, tmp_path)
    assert len(problems) == 1
    assert "bots/x/AGENTS.md" in problems[0]
