"""events.bans_evaluated_at: пересчёт банов по отмеченной явке завершён

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import meetup_bot.db.types

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("bans_evaluated_at", meetup_bot.db.types.UtcDateTime(), nullable=True)
        )
    # Уже отмеченные встречи считаем обработанными: баны по ним выдавались старым кодом
    op.execute(
        "UPDATE events SET bans_evaluated_at = attendance_marked_at "
        "WHERE attendance_marked_at IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("bans_evaluated_at")
