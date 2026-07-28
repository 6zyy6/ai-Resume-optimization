from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("queue", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["task_id", "owner_user_id"],
            ["tasks.id", "tasks.owner_user_id"],
            name="fk_outbox_task_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_outbox_task"),
    )
    op.create_index("ix_outbox_owner_user_id", "outbox", ["owner_user_id"])
    op.create_index(
        "ix_outbox_dispatch_ready",
        "outbox",
        ["dispatched_at", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_dispatch_ready", table_name="outbox")
    op.drop_index("ix_outbox_owner_user_id", table_name="outbox")
    op.drop_table("outbox")
