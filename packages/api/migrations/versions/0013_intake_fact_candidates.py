import sqlalchemy as sa
from alembic import op


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("source_records") as batch_op:
        batch_op.drop_constraint("ck_source_record_type", type_="check")
        batch_op.create_check_constraint(
            "ck_source_record_type",
            "source_type IN ('question_answer', 'imported_resume', 'user_edit', 'user_confirmation', 'fact_candidate_edit')",
        )

    with op.batch_alter_table("intake_answers") as batch_op:
        batch_op.create_unique_constraint(
            "uq_intake_answer_owner",
            ["id", "owner_user_id"],
        )
        batch_op.add_column(
            sa.Column(
                "analysis_status",
                sa.String(length=32),
                nullable=False,
                server_default="idle",
            )
        )
        batch_op.add_column(
            sa.Column("analysis_task_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("analysis_input_version", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("analysis_input_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("next_question_source", sa.String(length=16), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_intake_answer_analysis_status",
            "analysis_status IN ('idle', 'queued', 'running', 'waiting_for_confirmation', 'failed', 'completed')",
        )
        batch_op.create_check_constraint(
            "ck_intake_answer_next_question_source",
            "next_question_source IS NULL OR next_question_source IN ('rule', 'model', 'fallback')",
        )
        batch_op.create_foreign_key(
            "fk_intake_answer_analysis_task_owner",
            "tasks",
            ["analysis_task_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch_op.create_unique_constraint(
            "uq_intake_answer_analysis_task_owner",
            ["analysis_task_id", "owner_user_id"],
        )
        batch_op.create_index(
            "ix_intake_answers_analysis_claim",
            ["owner_user_id", "analysis_status", "analysis_task_id"],
            unique=False,
        )

    op.create_table(
        "fact_candidates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("intake_answer_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("source_start", sa.Integer(), nullable=False),
        sa.Column("source_end", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision_mode", sa.String(length=32), nullable=False),
        sa.Column("ai_run_id", sa.String(length=64), nullable=False),
        sa.Column("decision_source_id", sa.String(length=64), nullable=True),
        sa.Column("fact_id", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'edited', 'rejected')",
            name="ck_fact_candidate_status",
        ),
        sa.CheckConstraint(
            "decision_mode IN ('accept_or_edit', 'edit_only')",
            name="ck_fact_candidate_decision_mode",
        ),
        sa.CheckConstraint(
            "source_start >= 0 AND source_end > source_start",
            name="ck_fact_candidate_source_range",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL AND decided_by IS NULL "
            "AND decision_source_id IS NULL AND fact_id IS NULL) OR "
            "(status = 'accepted' AND decision_mode = 'accept_or_edit' "
            "AND decided_at IS NOT NULL AND decided_by IS NOT NULL "
            "AND decision_source_id IS NULL AND fact_id IS NOT NULL) OR "
            "(status = 'edited' AND decided_at IS NOT NULL "
            "AND decided_by IS NOT NULL AND decision_source_id IS NOT NULL "
            "AND fact_id IS NOT NULL) OR "
            "(status = 'rejected' AND decided_at IS NOT NULL "
            "AND decided_by IS NOT NULL AND decision_source_id IS NULL "
            "AND fact_id IS NULL)",
            name="ck_fact_candidate_decision_state",
        ),
        sa.ForeignKeyConstraint(
            ["ai_run_id", "owner_user_id"],
            ["ai_runs.id", "ai_runs.owner_user_id"],
            name="fk_fact_candidate_ai_run_owner",
        ),
        sa.ForeignKeyConstraint(
            ["decision_source_id", "owner_user_id"],
            ["source_records.id", "source_records.owner_user_id"],
            name="fk_fact_candidate_decision_source_owner",
        ),
        sa.ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_fact_candidate_fact_owner",
        ),
        sa.ForeignKeyConstraint(
            ["intake_answer_id", "owner_user_id"],
            ["intake_answers.id", "intake_answers.owner_user_id"],
            name="fk_fact_candidate_answer_owner",
        ),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "owner_user_id", name="uq_fact_candidate_owner"
        ),
        sa.UniqueConstraint(
            "intake_answer_id",
            "kind",
            "value_encrypted",
            "source_start",
            "source_end",
            "source_hash",
            name="uq_fact_candidate_answer_value_source",
        ),
    )
    op.create_index(
        "ix_fact_candidates_ai_run_id",
        "fact_candidates",
        ["ai_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_fact_candidates_session_response",
        "fact_candidates",
        ["owner_user_id", "intake_answer_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_fact_candidates_owner_user_id"),
        "fact_candidates",
        ["owner_user_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_candidates = bind.execute(
        sa.text("SELECT id FROM fact_candidates LIMIT 1")
    ).first()
    has_candidate_edits = bind.execute(
        sa.text(
            "SELECT id FROM source_records "
            "WHERE source_type = 'fact_candidate_edit' LIMIT 1"
        )
    ).first()
    has_analysis_state = bind.execute(
        sa.text(
            "SELECT id FROM intake_answers WHERE analysis_status != 'idle' "
            "OR analysis_task_id IS NOT NULL OR analysis_input_version IS NOT NULL "
            "OR analysis_input_hash IS NOT NULL OR next_question_source IS NOT NULL "
            "LIMIT 1"
        )
    ).first()
    if any((has_candidates, has_candidate_edits, has_analysis_state)):
        raise RuntimeError(
            "cannot downgrade intake analysis while Task 4 semantics exist"
        )

    op.drop_index(
        op.f("ix_fact_candidates_owner_user_id"),
        table_name="fact_candidates",
    )
    op.drop_index(
        "ix_fact_candidates_session_response",
        table_name="fact_candidates",
    )
    op.drop_index(
        "ix_fact_candidates_ai_run_id",
        table_name="fact_candidates",
    )
    op.drop_table("fact_candidates")

    with op.batch_alter_table("intake_answers") as batch_op:
        batch_op.drop_index("ix_intake_answers_analysis_claim")
        batch_op.drop_constraint(
            "uq_intake_answer_analysis_task_owner", type_="unique"
        )
        batch_op.drop_constraint("uq_intake_answer_owner", type_="unique")
        batch_op.drop_constraint(
            "fk_intake_answer_analysis_task_owner", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "ck_intake_answer_next_question_source", type_="check"
        )
        batch_op.drop_constraint(
            "ck_intake_answer_analysis_status", type_="check"
        )
        batch_op.drop_column("next_question_source")
        batch_op.drop_column("analysis_input_hash")
        batch_op.drop_column("analysis_input_version")
        batch_op.drop_column("analysis_task_id")
        batch_op.drop_column("analysis_status")

    with op.batch_alter_table("source_records") as batch_op:
        batch_op.drop_constraint("ck_source_record_type", type_="check")
        batch_op.create_check_constraint(
            "ck_source_record_type",
            "source_type IN ('question_answer', 'imported_resume', 'user_edit', 'user_confirmation')",
        )
