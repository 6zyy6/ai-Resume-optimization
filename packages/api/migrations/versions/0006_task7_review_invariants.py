from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_descriptions") as batch:
        batch.add_column(sa.Column("task_id", sa.String(64)))
        batch.create_index("ix_job_descriptions_task_id", ["task_id"])
    with op.batch_alter_table("resumes") as batch:
        batch.create_unique_constraint(
            "uq_targeted_resume_per_job",
            [
                "owner_user_id",
                "kind",
                "base_resume_id",
                "job_description_id",
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("resumes") as batch:
        batch.drop_constraint(
            "uq_targeted_resume_per_job",
            type_="unique",
        )
    with op.batch_alter_table("job_descriptions") as batch:
        batch.drop_index("ix_job_descriptions_task_id")
        batch.drop_column("task_id")
