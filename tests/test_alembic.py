import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_alembic_ini_только_ascii() -> None:
    # configparser на Windows читает файл в cp1252: кириллица ломает запуск alembic
    (ROOT / "alembic.ini").read_bytes().decode("ascii")


def test_alembic_применяется_к_чистой_базе(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'check.db'}",
    }
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "check.db").exists()


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
    }
    env.pop("PYTHONUTF8", None)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _setting_keys(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {row[0] for row in conn.execute("SELECT key FROM settings")}


def test_миграция_0004_убирает_устаревший_ключ_автосоздания(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    assert _alembic(db, "upgrade", "0003").returncode == 0
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executemany(
            "INSERT INTO settings (key, value, updated_by, updated_at) VALUES (?, ?, 1, ?)",
            [
                ("auto_create_weekday", "6", "2026-10-24 12:00:00"),
                ("capacity", "12", "2026-10-24 12:00:00"),
            ],
        )
    result = _alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert _setting_keys(db) == {"capacity"}


def test_миграция_0004_на_чистой_базе_и_откат(tmp_path: Path) -> None:
    db = tmp_path / "clean.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    assert _setting_keys(db) == set()
    result = _alembic(db, "downgrade", "0003")
    assert result.returncode == 0, result.stderr
    assert _alembic(db, "upgrade", "head").returncode == 0
