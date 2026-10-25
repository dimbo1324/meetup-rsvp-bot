import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
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


def _insert_legacy_event(db: Path, starts_at: str) -> None:
    """Встреча в формате до 0005: без ends_at, время — строкой SQLAlchemy в UTC."""
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO events (status, starts_at, capacity, description_html, place, place_url, "
            "cancel_deadline_at, registration_closes_at, reminder_at, attendance_prompt_at, "
            "created_at, updated_at) VALUES ('open', ?, 10, '', '', '', ?, ?, ?, ?, ?, ?)",
            (starts_at, starts_at, starts_at, starts_at, starts_at, starts_at, starts_at),
        )


def test_миграция_0005_заполняет_конец_старых_встреч(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    assert _alembic(db, "upgrade", "0004").returncode == 0
    _insert_legacy_event(db, "2026-10-24 20:00:00.000000")
    result = _alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(db)) as conn:
        ends_at, post_reminder = conn.execute(
            "SELECT ends_at, post_reminder_sent_at FROM events"
        ).fetchone()
    # Тот же формат, что пишет SQLAlchemy: иначе строковые сравнения в SQL разъедутся
    assert ends_at == "2026-10-24 22:00:00.000000"
    assert post_reminder is None


def test_миграция_0005_конец_читается_через_orm_как_aware_utc(tmp_path: Path) -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from meetup_bot.db.models import Event

    db = tmp_path / "old.db"
    assert _alembic(db, "upgrade", "0004").returncode == 0
    _insert_legacy_event(db, "2026-10-24 20:00:00.000000")
    assert _alembic(db, "upgrade", "head").returncode == 0
    engine = create_engine(f"sqlite:///{db}")
    try:
        with Session(engine) as session:
            event = session.scalars(select(Event)).one()
            assert event.starts_at == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
            assert event.ends_at == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
            assert event.ends_at.tzinfo is UTC
    finally:
        engine.dispose()


def test_миграция_0005_удаляет_ключи_настроек_в_часах(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    assert _alembic(db, "upgrade", "0004").returncode == 0
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executemany(
            "INSERT INTO settings (key, value, updated_by, updated_at) VALUES (?, ?, 1, ?)",
            [
                ("registration_close_hours_before", "3", "2026-10-24 12:00:00"),
                ("attendance_prompt_hours_after", "5", "2026-10-24 12:00:00"),
                ("capacity", "12", "2026-10-24 12:00:00"),
            ],
        )
    result = _alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert _setting_keys(db) == {"capacity"}


def _event_columns(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(events)")}


def test_миграция_0005_на_чистой_базе_и_откат(tmp_path: Path) -> None:
    db = tmp_path / "clean.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    assert {"ends_at", "post_reminder_sent_at"} <= _event_columns(db)
    result = _alembic(db, "downgrade", "0004")
    assert result.returncode == 0, result.stderr
    assert not {"ends_at", "post_reminder_sent_at"} & _event_columns(db)
    assert _alembic(db, "upgrade", "head").returncode == 0
    assert {"ends_at", "post_reminder_sent_at"} <= _event_columns(db)


def test_откат_0005_сохраняет_встречи(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    assert _alembic(db, "upgrade", "0004").returncode == 0
    _insert_legacy_event(db, "2026-10-24 20:00:00.000000")
    assert _alembic(db, "upgrade", "head").returncode == 0
    result = _alembic(db, "downgrade", "0004")
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT starts_at FROM events").fetchall() == [
            ("2026-10-24 20:00:00.000000",)
        ]
