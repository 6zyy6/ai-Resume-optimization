from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("files") as batch:
        batch.add_column(
            sa.Column("display_name", sa.String(255), nullable=False, server_default="resume")
        )
        batch.add_column(
            sa.Column(
                "status",
                sa.String(32),
                nullable=False,
                server_default="pending_upload",
            )
        )
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True)))

    op.create_table(
        "resume_imports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("file_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("parsed_text_encrypted", sa.Text()),
        sa.Column("draft_facts", sa.JSON(), nullable=False),
        sa.Column("fallback_reason", sa.String(64)),
        sa.Column("task_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["file_id", "owner_user_id"],
            ["files.id", "files.owner_user_id"],
            name="fk_resume_import_file_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_resume_import_owner"),
    )
    op.create_index(
        "ix_resume_imports_owner_user_id",
        "resume_imports",
        ["owner_user_id"],
    )

    with op.batch_alter_table("suggestions") as batch:
        batch.add_column(
            sa.Column(
                "original_text_encrypted",
                sa.Text(),
                nullable=False,
                server_default="",
            )
        )
        batch.add_column(sa.Column("requirement_id", sa.String(64)))
        batch.add_column(
            sa.Column("reason", sa.Text(), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("risk_flags", sa.JSON(), nullable=False, server_default="[]")
        )

    with op.batch_alter_table("suggestion_decisions") as batch:
        batch.add_column(sa.Column("final_version_id", sa.String(64)))

    with op.batch_alter_table("match_analyses") as batch:
        batch.add_column(sa.Column("task_id", sa.String(64)))

    with op.batch_alter_table("exports") as batch:
        batch.add_column(
            sa.Column(
                "download_name",
                sa.String(255),
                nullable=False,
                server_default="resume.pdf",
            )
        )
        batch.add_column(sa.Column("task_id", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("exports") as batch:
        batch.drop_column("task_id")
        batch.drop_column("download_name")
    with op.batch_alter_table("match_analyses") as batch:
        batch.drop_column("task_id")
    with op.batch_alter_table("suggestion_decisions") as batch:
        batch.drop_column("final_version_id")
    with op.batch_alter_table("suggestions") as batch:
        batch.drop_column("risk_flags")
        batch.drop_column("reason")
        batch.drop_column("requirement_id")
        batch.drop_column("original_text_encrypted")
    op.drop_index("ix_resume_imports_owner_user_id", table_name="resume_imports")
    op.drop_table("resume_imports")
    with op.batch_alter_table("files") as batch:
        batch.drop_column("deleted_at")
        batch.drop_column("status")
        batch.drop_column("display_name")
