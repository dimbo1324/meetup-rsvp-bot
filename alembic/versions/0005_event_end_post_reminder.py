"""Конец встречи, напоминание о рассылке поста, переход настроек на минуты

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import meetup_bot.db.types

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Длительность по умолчанию для уже созданных встреч (раздел 7, event_duration_minutes)
LEGACY_DURATION_MINUTES = 120
# Ключи настроек в часах заменены ключами в минутах с другим смыслом: старые значения
# не переводятся, действует новое умолчание
OBSOLETE_SETTING_KEYS = ("registration_close_hours_before", "attendance_prompt_hours_after")


def upgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ends_at", meetup_bot.db.types.UtcDateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("post_reminder_sent_at", meetup_bot.db.types.UtcDateTime(), nullable=True)
        )
    # Время хранится строкой SQLAlchemy «ГГГГ-ММ-ДД ЧЧ:ММ:СС.ffffff» в UTC. strftime() SQLite
    # теряет дробную часть, поэтому она дописывается из starts_at (с 20-го символа): строки
    # одного формата нужны, чтобы сравнения моментов в SQL оставались верными
    op.execute(
        "UPDATE events SET ends_at = "
        f"strftime('%Y-%m-%d %H:%M:%S', starts_at, '+{LEGACY_DURATION_MINUTES} minutes') "
        "|| substr(starts_at, 20)"
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.alter_column(
            "ends_at", existing_type=meetup_bot.db.types.UtcDateTime(), nullable=False
        )
    keys = ", ".join(f"'{key}'" for key in OBSOLETE_SETTING_KEYS)
    op.execute(f"DELETE FROM settings WHERE key IN ({keys})")


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("post_reminder_sent_at")
        batch_op.drop_column("ends_at")
