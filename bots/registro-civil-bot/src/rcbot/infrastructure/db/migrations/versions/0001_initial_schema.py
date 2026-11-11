"""Начальная схема: цели наблюдения, инциденты и их слоты, доставки, почасовая статистика,
журнал событий, настройки из Telegram (SPEC 3.2). Время — ISO-строки в UTC.

Revision ID: 0001
Revises:
Create Date: 2026-11-11 18:47:01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("app_events", schema=None) as batch_op:
        batch_op.create_index("ix_app_events_ts", ["ts"], unique=False)

    op.create_table(
        "kv_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "poll_stats_hourly",
        sa.Column("hour", sa.String(length=32), nullable=False),
        sa.Column("checks", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("relogins", sa.Integer(), nullable=False),
        sa.Column("p50_ms", sa.Double(), nullable=False),
        sa.Column("p95_ms", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("hour"),
    )
    op.create_table(
        "watch_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("agenda_url", sa.String(length=300), nullable=False),
        sa.Column("location_text", sa.String(length=200), nullable=False),
        sa.Column("months_ahead", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.String(length=32), nullable=False),
        sa.Column("closed_at", sa.String(length=32), nullable=True),
        sa.Column("acked_at", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["watch_targets.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("incidents", schema=None) as batch_op:
        batch_op.create_index("ix_incidents_target_status", ["target_id", "status"], unique=False)

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=True),
        sa.Column("at", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Double(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=300), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("deliveries", schema=None) as batch_op:
        batch_op.create_index("ix_deliveries_incident", ["incident_id"], unique=False)

    op.create_table(
        "incident_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("at", sa.String(length=5), nullable=True),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("incident_slots")
    with op.batch_alter_table("deliveries", schema=None) as batch_op:
        batch_op.drop_index("ix_deliveries_incident")

    op.drop_table("deliveries")
    with op.batch_alter_table("incidents", schema=None) as batch_op:
        batch_op.drop_index("ix_incidents_target_status")

    op.drop_table("incidents")
    op.drop_table("watch_targets")
    op.drop_table("poll_stats_hourly")
    op.drop_table("kv_settings")
    with op.batch_alter_table("app_events", schema=None) as batch_op:
        batch_op.drop_index("ix_app_events_ts")

    op.drop_table("app_events")
