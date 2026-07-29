from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("active_ai_run_id", sa.String(128)))
        batch.add_column(
            sa.Column("ai_cancel_requested_at", sa.DateTime(timezone=True))
        )
        batch.add_column(
            sa.Column("ai_cancel_acknowledged_at", sa.DateTime(timezone=True))
        )
        batch.create_index(
            "ix_tasks_active_ai_run_id",
            ["active_ai_run_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_active_ai_run_id")
        batch.drop_column("ai_cancel_acknowledged_at")
        batch.drop_column("ai_cancel_requested_at")
        batch.drop_column("active_ai_run_id")
