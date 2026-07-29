import sqlalchemy as sa
from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resume_imports") as batch_op:
        batch_op.add_column(sa.Column("resume_id", sa.String(length=64)))
        batch_op.add_column(sa.Column("version_id", sa.String(length=64)))
        batch_op.create_foreign_key(
            "fk_resume_import_resume_owner",
            "resumes",
            ["resume_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch_op.create_foreign_key(
            "fk_resume_import_version_owner",
            "resume_versions",
            ["version_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch_op.create_index(
            op.f("ix_resume_imports_resume_id"),
            ["resume_id"],
            unique=False,
        )
        batch_op.create_index(
            op.f("ix_resume_imports_version_id"),
            ["version_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("resume_imports") as batch_op:
        batch_op.drop_index(op.f("ix_resume_imports_version_id"))
        batch_op.drop_index(op.f("ix_resume_imports_resume_id"))
        batch_op.drop_constraint(
            "fk_resume_import_version_owner",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_resume_import_resume_owner",
            type_="foreignkey",
        )
        batch_op.drop_column("version_id")
        batch_op.drop_column("resume_id")
