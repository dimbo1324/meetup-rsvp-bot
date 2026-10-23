import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_alembic_ini_только_ascii() -> None:
    # configparser на Windows читает файл в cp1252: кириллица ломает запуск alembic
    (ROOT / "alembic.ini").read_bytes().decode("ascii")


def test_alembic_применяется_к_чистой_базе(tmp_path: Path) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'check.db'}"}
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "check.db").exists()
