"""Вид разосланного поста: с фото или текстовый

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Для уже разосланных постов вид неизвестен точно: берём текущее фото встречи как лучшее
# приближение (ошибку исправит повторная попытка правки с другим методом)
_CURRENT_PHOTO = (
    "(SELECT NULLIF(events.photo_file_id, '') FROM events WHERE events.id = event_posts.event_id)"
)


def upgrade() -> None:
    with op.batch_alter_table("event_posts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("has_photo", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("last_photo_file_id", sa.Text(), nullable=True))
    op.execute(
        f"UPDATE event_posts SET last_photo_file_id = {_CURRENT_PHOTO}, "
        f"has_photo = ({_CURRENT_PHOTO} IS NOT NULL)"
    )
    with op.batch_alter_table("event_posts", schema=None) as batch_op:
        batch_op.alter_column("has_photo", existing_type=sa.Boolean(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("event_posts", schema=None) as batch_op:
        batch_op.drop_column("last_photo_file_id")
        batch_op.drop_column("has_photo")
