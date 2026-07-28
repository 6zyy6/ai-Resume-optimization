from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "targeted_resume_keys",
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("base_resume_id", sa.String(64), nullable=False),
        sa.Column("job_description_id", sa.String(64), nullable=False),
        sa.Column("resume_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["base_resume_id", "owner_user_id"],
            ["resumes.id", "resumes.owner_user_id"],
            name="fk_targeted_key_base_owner",
        ),
        sa.ForeignKeyConstraint(
            ["job_description_id", "owner_user_id"],
            ["job_descriptions.id", "job_descriptions.owner_user_id"],
            name="fk_targeted_key_job_owner",
        ),
        sa.ForeignKeyConstraint(
            ["resume_id", "owner_user_id"],
            ["resumes.id", "resumes.owner_user_id"],
            name="fk_targeted_key_resume_owner",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id",
            "base_resume_id",
            "job_description_id",
        ),
        sa.UniqueConstraint(
            "resume_id",
            "owner_user_id",
            name="uq_targeted_key_resume_owner",
        ),
    )
    _backfill_targeted_resume_keys()
    with op.batch_alter_table("job_descriptions") as batch:
        batch.add_column(sa.Column("task_id", sa.String(64)))
        batch.create_index("ix_job_descriptions_task_id", ["task_id"])


def downgrade() -> None:
    with op.batch_alter_table("job_descriptions") as batch:
        batch.drop_index("ix_job_descriptions_task_id")
        batch.drop_column("task_id")
    op.drop_table("targeted_resume_keys")


def _backfill_targeted_resume_keys() -> None:
    connection = op.get_bind()
    groups = connection.execute(
        sa.text(
            """
            SELECT owner_user_id, base_resume_id, job_description_id
            FROM resumes
            WHERE kind = 'job_targeted'
            GROUP BY owner_user_id, base_resume_id, job_description_id
            ORDER BY owner_user_id, base_resume_id, job_description_id
            """
        )
    ).mappings().all()
    for group in groups:
        resume_id = connection.execute(
            sa.text(
                """
                SELECT id
                FROM resumes
                WHERE owner_user_id = :owner_user_id
                  AND kind = 'job_targeted'
                  AND base_resume_id = :base_resume_id
                  AND job_description_id = :job_description_id
                ORDER BY head_version DESC, id
                LIMIT 1
                """
            ),
            group,
        ).scalar_one()
        connection.execute(
            sa.text(
                """
                INSERT INTO targeted_resume_keys (
                    owner_user_id,
                    base_resume_id,
                    job_description_id,
                    resume_id
                ) VALUES (
                    :owner_user_id,
                    :base_resume_id,
                    :job_description_id,
                    :resume_id
                )
                """
            ),
            {**group, "resume_id": resume_id},
        )
