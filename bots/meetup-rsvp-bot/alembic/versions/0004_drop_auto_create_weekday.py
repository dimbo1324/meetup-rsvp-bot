"""Удаление устаревшей настройки auto_create_weekday

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Автосоздание теперь считается от дня встречи (auto_create_days_after): старое
    # значение «день недели» потеряло смысл и не должно переживать обновление
    op.execute("DELETE FROM settings WHERE key = 'auto_create_weekday'")


def downgrade() -> None:
    # Удалённое значение не восстановить, да и не нужно: код до этой миграции берёт
    # умолчание, когда строки в таблице нет
    pass
