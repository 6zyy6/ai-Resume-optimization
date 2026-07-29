import sqlalchemy as sa
from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intake_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active_owner_key", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("current_question", sa.JSON(), nullable=True),
        sa.Column("answered_question_ids", sa.JSON(), nullable=False),
        sa.Column("skipped_question_ids", sa.JSON(), nullable=False),
        sa.Column("fact_ids", sa.JSON(), nullable=False),
        sa.Column("draft_title", sa.String(length=255), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("resume_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'drafting', 'completed', 'abandoned')",
            name="ck_intake_session_status",
        ),
        sa.ForeignKeyConstraint(
            ["resume_id", "owner_user_id"],
            ["resumes.id", "resumes.owner_user_id"],
            name="fk_intake_session_resume_owner",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "owner_user_id"],
            ["tasks.id", "tasks.owner_user_id"],
            name="fk_intake_session_task_owner",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "active_owner_key",
            name="uq_intake_session_active_owner",
        ),
        sa.UniqueConstraint(
            "id",
            "owner_user_id",
            name="uq_intake_session_owner",
        ),
    )
    op.create_index(
        op.f("ix_intake_sessions_owner_user_id"),
        "intake_sessions",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_intake_sessions_task_id"),
        "intake_sessions",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_intake_sessions_resume_id"),
        "intake_sessions",
        ["resume_id"],
        unique=False,
    )
    op.create_table(
        "intake_answers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("question_id", sa.String(length=64), nullable=False),
        sa.Column("answer_encrypted", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("fact_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "state IN ('answered', 'negative', 'skipped')",
            name="ck_intake_answer_state",
        ),
        sa.ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_intake_answer_fact_owner",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_user_id"],
            ["intake_sessions.id", "intake_sessions.owner_user_id"],
            name="fk_intake_answer_session_owner",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "question_id",
            name="uq_intake_answer_question",
        ),
    )
    op.create_index(
        op.f("ix_intake_answers_owner_user_id"),
        "intake_answers",
        ["owner_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_intake_answers_owner_user_id"),
        table_name="intake_answers",
    )
    op.drop_table("intake_answers")
    op.drop_index(
        op.f("ix_intake_sessions_resume_id"),
        table_name="intake_sessions",
    )
    op.drop_index(
        op.f("ix_intake_sessions_task_id"),
        table_name="intake_sessions",
    )
    op.drop_index(
        op.f("ix_intake_sessions_owner_user_id"),
        table_name="intake_sessions",
    )
    op.drop_table("intake_sessions")
