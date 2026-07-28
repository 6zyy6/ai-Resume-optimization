from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("usage_type", sa.String(64), nullable=True))
        batch.add_column(sa.Column("claim_token", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_index(
            "ix_tasks_active_usage",
            ["owner_user_id", "usage_type", "status"],
        )
    with op.batch_alter_table("outbox") as batch:
        batch.add_column(
            sa.Column("exhausted_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox") as batch:
        batch.drop_column("exhausted_at")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_active_usage")
        batch.drop_column("claim_lease_expires_at")
        batch.drop_column("claim_token")
        batch.drop_column("usage_type")
