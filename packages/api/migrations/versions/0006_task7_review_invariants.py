from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _merge_duplicate_targeted_resumes()
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


def _merge_duplicate_targeted_resumes() -> None:
    connection = op.get_bind()
    while True:
        duplicate = connection.execute(
            sa.text(
                """
                SELECT owner_user_id, base_resume_id, job_description_id
                FROM resumes
                WHERE kind = 'job_targeted'
                GROUP BY owner_user_id, base_resume_id, job_description_id
                HAVING COUNT(*) > 1
                ORDER BY owner_user_id, base_resume_id, job_description_id
                LIMIT 1
                """
            )
        ).mappings().first()
        if duplicate is None:
            return
        rows = connection.execute(
            sa.text(
                """
                SELECT id, head_version
                FROM resumes
                WHERE owner_user_id = :owner_user_id
                  AND kind = 'job_targeted'
                  AND base_resume_id = :base_resume_id
                  AND job_description_id = :job_description_id
                ORDER BY head_version DESC, id
                """
            ),
            duplicate,
        ).mappings().all()
        survivor_id = rows[0]["id"]
        for row in rows[1:]:
            duplicate_id = row["id"]
            connection.execute(
                sa.text(
                    """
                    UPDATE resume_versions
                    SET resume_id = :survivor_id
                    WHERE resume_id = :duplicate_id
                      AND owner_user_id = :owner_user_id
                    """
                ),
                {
                    "survivor_id": survivor_id,
                    "duplicate_id": duplicate_id,
                    "owner_user_id": duplicate["owner_user_id"],
                },
            )
            connection.execute(
                sa.text(
                    """
                    UPDATE resumes
                    SET base_resume_id = :survivor_id
                    WHERE base_resume_id = :duplicate_id
                      AND base_resume_owner_user_id = :owner_user_id
                    """
                ),
                {
                    "survivor_id": survivor_id,
                    "duplicate_id": duplicate_id,
                    "owner_user_id": duplicate["owner_user_id"],
                },
            )
            connection.execute(
                sa.text(
                    """
                    DELETE FROM resumes
                    WHERE id = :duplicate_id
                      AND owner_user_id = :owner_user_id
                    """
                ),
                {
                    "duplicate_id": duplicate_id,
                    "owner_user_id": duplicate["owner_user_id"],
                },
            )
