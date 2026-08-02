import sqlalchemy as sa
from alembic import op


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _drop_resume_version_append_only() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_resume_versions_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_resume_versions_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_resume_head_matches_version")
        op.execute("DROP TRIGGER IF EXISTS trg_resume_head_insert_matches_version")
    else:
        op.execute(
            "DROP TRIGGER IF EXISTS trg_resume_versions_append_only "
            "ON resume_versions"
        )


def _create_resume_version_append_only() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER trg_resume_head_matches_version "
            "BEFORE UPDATE OF head_version, head_version_id ON resumes "
            "WHEN NEW.head_version_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM resume_versions WHERE id = NEW.head_version_id "
            "AND resume_id = NEW.id AND owner_user_id = NEW.owner_user_id) "
            "BEGIN SELECT RAISE(ABORT, "
            "'resume head must reference its own version'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_resume_head_insert_matches_version "
            "BEFORE INSERT ON resumes "
            "WHEN NEW.head_version_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM resume_versions WHERE id = NEW.head_version_id "
            "AND resume_id = NEW.id AND owner_user_id = NEW.owner_user_id) "
            "BEGIN SELECT RAISE(ABORT, "
            "'resume head must reference its own version'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_resume_versions_no_update "
            "BEFORE UPDATE ON resume_versions BEGIN "
            "SELECT RAISE(ABORT, 'resume versions are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_resume_versions_no_delete "
            "BEFORE DELETE ON resume_versions BEGIN "
            "SELECT RAISE(ABORT, 'resume versions are append-only'); END"
        )
    else:
        op.execute(
            "CREATE TRIGGER trg_resume_versions_append_only "
            "BEFORE UPDATE OR DELETE ON resume_versions FOR EACH ROW "
            "EXECUTE FUNCTION reject_resume_version_mutation()"
        )


def upgrade() -> None:
    _drop_resume_version_append_only()
    with op.batch_alter_table("resume_versions") as batch_op:
        batch_op.add_column(
            sa.Column("generation_mode", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workflow_version", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("ai_run_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("input_hash", sa.String(length=128), nullable=True)
        )

    op.execute(
        "UPDATE resume_versions SET generation_mode = 'manual' "
        "WHERE generation_mode IS NULL"
    )

    with op.batch_alter_table("resume_versions") as batch_op:
        batch_op.alter_column(
            "generation_mode",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="manual",
        )
        batch_op.create_check_constraint(
            "ck_resume_version_generation_mode",
            "generation_mode IN ('manual', 'model', 'rule_fallback')",
        )
        batch_op.create_check_constraint(
            "ck_resume_version_generation_provenance",
            "(generation_mode = 'manual' AND workflow_version IS NULL "
            "AND ai_run_id IS NULL AND input_hash IS NULL) OR "
            "(generation_mode = 'model' AND workflow_version IS NOT NULL "
            "AND ai_run_id IS NOT NULL AND input_hash IS NOT NULL) OR "
            "(generation_mode = 'rule_fallback' AND workflow_version IS NOT NULL "
            "AND ai_run_id IS NULL AND input_hash IS NOT NULL)",
        )
        batch_op.create_foreign_key(
            "fk_resume_version_ai_run_owner",
            "ai_runs",
            ["ai_run_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch_op.create_index(
            "ix_resume_versions_ai_run_id",
            ["ai_run_id"],
            unique=False,
        )
    _create_resume_version_append_only()


def downgrade() -> None:
    bind = op.get_bind()
    generated = bind.execute(
        sa.text(
            "SELECT id FROM resume_versions "
            "WHERE generation_mode != 'manual' OR workflow_version IS NOT NULL "
            "OR ai_run_id IS NOT NULL OR input_hash IS NOT NULL LIMIT 1"
        )
    ).first()
    if generated is not None:
        raise RuntimeError(
            "cannot downgrade resume generation provenance while generated versions exist"
        )

    _drop_resume_version_append_only()
    with op.batch_alter_table("resume_versions") as batch_op:
        batch_op.drop_index("ix_resume_versions_ai_run_id")
        batch_op.drop_constraint(
            "fk_resume_version_ai_run_owner",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_resume_version_generation_provenance",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_resume_version_generation_mode",
            type_="check",
        )
        batch_op.drop_column("input_hash")
        batch_op.drop_column("ai_run_id")
        batch_op.drop_column("workflow_version")
        batch_op.drop_column("generation_mode")
    _create_resume_version_append_only()
