import sqlalchemy as sa
from alembic import op


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


_WORKFLOW_STAGE_CASE = """
CASE workflow_type
  WHEN 'analyze_intake_answer' THEN 'analysis'
  WHEN 'compose_resume_draft' THEN 'draft'
  WHEN 'parse_jd' THEN 'parse'
  WHEN 'match_resume_to_jd' THEN 'match'
  WHEN 'generate_suggestions_batch' THEN 'suggestions'
END
"""


def _drop_usage_update_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_usage_ledger_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_usage_ledger_guard_update")
        op.execute("DROP TRIGGER IF EXISTS trg_usage_ledger_no_delete")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_usage_ledger_append_only ON usage_ledger")
        op.execute("DROP TRIGGER IF EXISTS trg_usage_ledger_guard_update ON usage_ledger")
        op.execute("DROP FUNCTION IF EXISTS reject_usage_ledger_mutation()")
        op.execute("DROP FUNCTION IF EXISTS guard_usage_ledger_update()")


def _create_usage_lifecycle_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_usage_ledger_guard_update
            BEFORE UPDATE ON usage_ledger
            WHEN NOT (
              OLD.state = 'reserved'
              AND NEW.state IN ('consumed', 'released')
              AND NEW.id = OLD.id
              AND NEW.owner_user_id = OLD.owner_user_id
              AND NEW.usage_type = OLD.usage_type
              AND NEW.quantity = OLD.quantity
              AND NEW.cost_cny = OLD.cost_cny
              AND NEW.trace_id = OLD.trace_id
              AND NEW.task_id IS OLD.task_id
              AND NEW.created_at = OLD.created_at
              AND (
                (NEW.state = 'consumed' AND NEW.ai_run_id IS NOT NULL)
                OR (NEW.state = 'released' AND NEW.ai_run_id IS NULL)
              )
            )
            BEGIN
              SELECT RAISE(ABORT, 'invalid usage ledger state transition');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_usage_ledger_no_delete
            BEFORE DELETE ON usage_ledger
            BEGIN
              SELECT RAISE(ABORT, 'usage ledger cannot be deleted');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION guard_usage_ledger_update()
            RETURNS TRIGGER AS $$
            BEGIN
              IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'usage ledger cannot be deleted';
              END IF;
              IF NOT (
                OLD.state = 'reserved'
                AND NEW.state IN ('consumed', 'released')
                AND NEW.id = OLD.id
                AND NEW.owner_user_id = OLD.owner_user_id
                AND NEW.usage_type = OLD.usage_type
                AND NEW.quantity = OLD.quantity
                AND NEW.cost_cny = OLD.cost_cny
                AND NEW.trace_id = OLD.trace_id
                AND NEW.task_id IS NOT DISTINCT FROM OLD.task_id
                AND NEW.created_at = OLD.created_at
                AND (
                  (NEW.state = 'consumed' AND NEW.ai_run_id IS NOT NULL)
                  OR (NEW.state = 'released' AND NEW.ai_run_id IS NULL)
                )
              ) THEN
                RAISE EXCEPTION 'invalid usage ledger state transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_usage_ledger_guard_update
            BEFORE UPDATE OR DELETE ON usage_ledger
            FOR EACH ROW EXECUTE FUNCTION guard_usage_ledger_update()
            """
        )


def _create_usage_append_only_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_usage_ledger_no_update
            BEFORE UPDATE ON usage_ledger
            BEGIN
              SELECT RAISE(ABORT, 'usage ledger is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_usage_ledger_no_delete
            BEFORE DELETE ON usage_ledger
            BEGIN
              SELECT RAISE(ABORT, 'usage ledger is append-only');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_usage_ledger_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
              RAISE EXCEPTION 'usage ledger is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_usage_ledger_append_only
            BEFORE UPDATE OR DELETE ON usage_ledger
            FOR EACH ROW EXECUTE FUNCTION reject_usage_ledger_mutation()
            """
        )


def upgrade() -> None:
    bind = op.get_bind()
    unsupported = bind.execute(
        sa.text(
            f"SELECT id FROM ai_runs "
            f"WHERE ({_WORKFLOW_STAGE_CASE}) IS NULL LIMIT 1"
        )
    ).first()
    if unsupported is not None:
        raise RuntimeError("unsupported legacy AI workflow cannot map to a business stage")
    duplicate = bind.execute(
        sa.text(
            f"""
            SELECT task_id, ({_WORKFLOW_STAGE_CASE}) AS mapped_stage, input_hash
            FROM ai_runs
            GROUP BY task_id, mapped_stage, input_hash
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError("duplicate AI run stable key after workflow stage mapping")
    with op.batch_alter_table("ai_runs") as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(length=32)))
        batch_op.add_column(sa.Column("error_code", sa.String(length=128)))
        batch_op.add_column(sa.Column("workflow_stage", sa.String(length=64)))
        batch_op.add_column(sa.Column("receipt_hash", sa.String(length=64)))
        batch_op.alter_column(
            "provider",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.alter_column(
            "requested_model",
            existing_type=sa.String(length=128),
            nullable=True,
        )
        batch_op.alter_column(
            "provider_cost",
            existing_type=sa.Numeric(18, 6),
            type_=sa.Numeric(38, 18),
            nullable=False,
        )
        batch_op.alter_column(
            "prompt_template_version",
            existing_type=sa.String(length=64),
            type_=sa.String(length=128),
            nullable=False,
        )
    op.execute(
        f"UPDATE ai_runs SET status = 'succeeded', workflow_stage = ({_WORKFLOW_STAGE_CASE})"
    )
    with op.batch_alter_table("ai_runs") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch_op.alter_column(
            "workflow_stage",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_ai_run_task_stage_input",
            ["task_id", "workflow_stage", "input_hash"],
        )

    _drop_usage_update_guard()
    with op.batch_alter_table("usage_ledger") as batch_op:
        batch_op.add_column(sa.Column("state", sa.String(length=16)))
        batch_op.add_column(sa.Column("task_id", sa.String(length=64)))
        batch_op.add_column(sa.Column("ai_run_id", sa.String(length=64)))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE usage_ledger SET state = 'consumed', updated_at = created_at"
    )
    with op.batch_alter_table("usage_ledger") as batch_op:
        batch_op.alter_column("state", existing_type=sa.String(length=16), nullable=False)
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_usage_ledger_state",
            "state IN ('reserved', 'consumed', 'released')",
        )
        batch_op.create_foreign_key(
            "fk_usage_ledger_task_owner",
            "tasks",
            ["task_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch_op.create_unique_constraint(
            "uq_usage_ledger_task_owner_type",
            ["task_id", "owner_user_id", "usage_type"],
        )
        batch_op.create_index(
            "ix_usage_ledger_owner_state_created",
            ["owner_user_id", "state", "created_at"],
            unique=False,
        )
    _create_usage_lifecycle_guard()


def downgrade() -> None:
    bind = op.get_bind()
    non_consumed = bind.execute(
        sa.text("SELECT id FROM usage_ledger WHERE state != 'consumed' LIMIT 1")
    ).first()
    if non_consumed is not None:
        raise RuntimeError("cannot downgrade usage ledger with non-consumed rows")
    precise_cost = bind.execute(
        sa.text(
            "SELECT id FROM ai_runs "
            "WHERE provider_cost != ROUND(provider_cost, 6) LIMIT 1"
        )
    ).first()
    if precise_cost is not None:
        raise RuntimeError("cannot downgrade AI run provider cost without precision loss")
    wide_template = bind.execute(
        sa.text(
            "SELECT id FROM ai_runs "
            "WHERE LENGTH(prompt_template_version) > 64 LIMIT 1"
        )
    ).first()
    if wide_template is not None:
        raise RuntimeError("cannot downgrade AI run prompt template without truncation")
    _drop_usage_update_guard()
    with op.batch_alter_table("usage_ledger") as batch_op:
        batch_op.drop_index("ix_usage_ledger_owner_state_created")
        batch_op.drop_constraint("uq_usage_ledger_task_owner_type", type_="unique")
        batch_op.drop_constraint("fk_usage_ledger_task_owner", type_="foreignkey")
        batch_op.drop_constraint("ck_usage_ledger_state", type_="check")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("ai_run_id")
        batch_op.drop_column("task_id")
        batch_op.drop_column("state")
    _create_usage_append_only_guard()

    op.execute(
        "UPDATE ai_runs SET provider = COALESCE(provider, 'unknown'), "
        "requested_model = COALESCE(requested_model, 'unknown')"
    )
    with op.batch_alter_table("ai_runs") as batch_op:
        batch_op.drop_constraint("uq_ai_run_task_stage_input", type_="unique")
        batch_op.alter_column(
            "requested_model",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.alter_column(
            "provider",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "provider_cost",
            existing_type=sa.Numeric(38, 18),
            type_=sa.Numeric(18, 6),
            nullable=False,
        )
        batch_op.alter_column(
            "prompt_template_version",
            existing_type=sa.String(length=128),
            type_=sa.String(length=64),
            nullable=False,
        )
        batch_op.drop_column("workflow_stage")
        batch_op.drop_column("receipt_hash")
        batch_op.drop_column("error_code")
        batch_op.drop_column("status")
