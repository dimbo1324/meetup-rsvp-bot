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


def _insert_post(db: Path, event_id: int, inline_id: str) -> None:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO event_posts (event_id, inline_message_id, created_at, is_dead) "
            "VALUES (?, ?, '2026-10-24 12:00:00.000000', 0)",
            (event_id, inline_id),
        )


def test_миграция_0006_заполняет_вид_старых_постов_по_фото_встречи(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    assert _alembic(db, "upgrade", "0005").returncode == 0
    with closing(sqlite3.connect(db)) as conn, conn:
        for starts_at, photo in (("2026-10-24 20:00:00.000000", "photo-1"), ("2026-10-31", None)):
            conn.execute(
                "INSERT INTO events (status, starts_at, ends_at, capacity, description_html, "
                "place, place_url, photo_file_id, cancel_deadline_at, registration_closes_at, "
                "reminder_at, attendance_prompt_at, created_at, updated_at) "
                "VALUES ('open', ?1, ?1, 10, '', '', '', ?2, ?1, ?1, ?1, ?1, ?1, ?1)",
                (starts_at, photo),
            )
    _insert_post(db, 1, "with-photo")
    _insert_post(db, 2, "without-photo")
    result = _alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT inline_message_id, has_photo, last_photo_file_id FROM event_posts ORDER BY id"
        ).fetchall()
    assert rows == [("with-photo", 1, "photo-1"), ("without-photo", 0, None)]


def test_миграция_0006_откат_и_повторное_применение(tmp_path: Path) -> None:
    db = tmp_path / "clean.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    result = _alembic(db, "downgrade", "0005")
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(db)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(event_posts)")}
    assert not {"has_photo", "last_photo_file_id"} & columns
    assert _alembic(db, "upgrade", "head").returncode == 0


def _fill_children(db: Path, event_id: int) -> None:
    """Дочерние строки встречи на схеме 0004: на events ссылаются записи, посты и баны."""
    moment = "2026-10-24 12:00:00.000000"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO users (id, first_name, dm_available, created_at, updated_at) "
            "VALUES (500, 'Анна', 1, ?, ?)",
            (moment, moment),
        )
        conn.execute(
            "INSERT INTO registrations (event_id, user_id, guests, status, source, attendance, "
            "late_promoted, created_at) VALUES (?, 500, 0, 'active', 'button', 'unknown', 0, ?)",
            (event_id, moment),
        )
        conn.execute(
            "INSERT INTO bans (user_id, active, reason, event_id, banned_at) "
            "VALUES (500, 1, 'no_show', ?, ?)",
            (event_id, moment),
        )
    _insert_post(db, event_id, "inline-1")


def _foreign_key_violations(db: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("PRAGMA foreign_key_check").fetchall()


def test_миграции_на_базе_с_записями_постами_и_банами(tmp_path: Path) -> None:
    # Регрессия: пересоздание таблицы events в 0005 падало на внешних ключах реальной базы
    db = tmp_path / "bot.db"
    assert _alembic(db, "upgrade", "0004").returncode == 0
    _insert_legacy_event(db, "2026-10-30 19:30:00.000000")
    _fill_children(db, 1)
    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM registrations").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM bans").fetchone() == (1,)
        assert conn.execute("SELECT count(*), max(has_photo) FROM event_posts").fetchone() == (1, 0)
        assert conn.execute("SELECT ends_at FROM events").fetchone() == (
            "2026-10-30 21:30:00.000000",
        )
    assert _foreign_key_violations(db) == []


def test_откат_миграций_на_базе_с_записями_постами_и_банами(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    assert _alembic(db, "upgrade", "0004").returncode == 0
    _insert_legacy_event(db, "2026-10-30 19:30:00.000000")
    _fill_children(db, 1)
    assert _alembic(db, "upgrade", "head").returncode == 0
    down = _alembic(db, "downgrade", "0004")
    assert down.returncode == 0, down.stderr
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM registrations").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM event_posts").fetchone() == (1,)
    assert "ends_at" not in _event_columns(db)
    assert _foreign_key_violations(db) == []
    assert _alembic(db, "upgrade", "head").returncode == 0
