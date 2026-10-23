from pathlib import Path

from scripts import sync_agents

ROOT = Path(__file__).resolve().parent.parent


def test_agents_md_синхронизирован_с_модулями() -> None:
    content = sync_agents.render(ROOT)
    current = sync_agents.normalize((ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    assert current == content


def test_размер_agents_md_в_бюджете() -> None:
    size = len(sync_agents.render(ROOT).encode("utf-8")) / 1024
    assert size <= sync_agents.SIZE_LIMIT_KIB
