from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _legacy_hash(kind: str, owner: str, identifier: str) -> str:
    return hashlib.sha256(f"{kind}:{owner}:{identifier}".encode()).hexdigest()


def _backfill() -> None:
    bind = op.get_bind()
    analyses = bind.execute(
        sa.text("SELECT id, owner_user_id, created_at FROM match_analyses")
    ).mappings()
    for row in analyses:
        bind.execute(
            sa.text(
                "UPDATE match_analyses SET generation_mode = 'rule_fallback', "
                "workflow_version = 'legacy-rule-fallback@1', ai_run_id = NULL, "
                "input_hash = :input_hash, updated_at = :updated_at "
                "WHERE id = :id AND owner_user_id = :owner"
            ),
            {
                "input_hash": _legacy_hash("match", row["owner_user_id"], row["id"]),
                "updated_at": row["created_at"],
                "id": row["id"],
                "owner": row["owner_user_id"],
            },
        )
    items = bind.execute(
        sa.text("SELECT id, owner_user_id FROM match_items")
    ).mappings()
    for row in items:
        bind.execute(
            sa.text(
                "UPDATE match_items SET resume_target_paths = '[]', "
                "reason_code = 'legacy_rule_fallback', "
                "generation_mode = 'rule_fallback', "
                "workflow_version = 'legacy-rule-fallback@1', ai_run_id = NULL, "
                "input_hash = :input_hash WHERE id = :id AND owner_user_id = :owner"
            ),
            {
                "input_hash": _legacy_hash("match-item", row["owner_user_id"], row["id"]),
                "id": row["id"],
                "owner": row["owner_user_id"],
            },
        )
    suggestions = bind.execute(
        sa.text("SELECT id, owner_user_id, created_at FROM suggestions")
    ).mappings()
    for row in suggestions:
        bind.execute(
            sa.text(
                "UPDATE suggestions SET generation_mode = 'rule_fallback', "
                "workflow_version = 'legacy-rule-fallback@1', ai_run_id = NULL, "
                "input_hash = :input_hash, updated_at = :updated_at "
                "WHERE id = :id AND owner_user_id = :owner"
            ),
            {
                "input_hash": _legacy_hash("suggestion", row["owner_user_id"], row["id"]),
                "updated_at": row["created_at"],
                "id": row["id"],
                "owner": row["owner_user_id"],
            },
        )


def upgrade() -> None:
    with op.batch_alter_table("match_analyses") as batch:
        batch.add_column(sa.Column("generation_mode", sa.String(32), nullable=True))
        batch.add_column(sa.Column("ai_run_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("input_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("match_items") as batch:
        batch.add_column(sa.Column("resume_target_paths", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("reason_code", sa.String(128), nullable=True))
        batch.add_column(sa.Column("generation_mode", sa.String(32), nullable=True))
        batch.add_column(sa.Column("workflow_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("ai_run_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("input_hash", sa.String(64), nullable=True))
    with op.batch_alter_table("suggestions") as batch:
        batch.add_column(sa.Column("generation_mode", sa.String(32), nullable=True))
        batch.add_column(sa.Column("workflow_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("ai_run_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("input_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))

    _backfill()

    with op.batch_alter_table("match_analyses") as batch:
        for name, column_type in (
            ("generation_mode", sa.String(32)),
            ("input_hash", sa.String(64)),
            ("updated_at", sa.DateTime(timezone=True)),
        ):
            batch.alter_column(name, existing_type=column_type, nullable=False)
        batch.create_check_constraint(
            "ck_match_analysis_generation_mode",
            "generation_mode IN ('model', 'rule_fallback')",
        )
        batch.create_check_constraint(
            "ck_match_analysis_generation_provenance",
            "generation_mode <> 'rule_fallback' OR ai_run_id IS NULL",
        )
        batch.create_foreign_key(
            "fk_match_analysis_ai_run_owner",
            "ai_runs",
            ["ai_run_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_index("ix_match_analyses_ai_run_id", ["ai_run_id"])
    with op.batch_alter_table("match_items") as batch:
        for name, column_type in (
            ("resume_target_paths", sa.JSON()),
            ("reason_code", sa.String(128)),
            ("generation_mode", sa.String(32)),
            ("workflow_version", sa.String(64)),
            ("input_hash", sa.String(64)),
        ):
            batch.alter_column(name, existing_type=column_type, nullable=False)
        batch.create_check_constraint(
            "ck_match_item_generation_mode",
            "generation_mode IN ('model', 'rule_fallback')",
        )
        batch.create_check_constraint(
            "ck_match_item_generation_provenance",
            "(generation_mode = 'model' AND ai_run_id IS NOT NULL) OR "
            "(generation_mode = 'rule_fallback' AND ai_run_id IS NULL)",
        )
        batch.create_foreign_key(
            "fk_match_item_ai_run_owner",
            "ai_runs",
            ["ai_run_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_index("ix_match_items_ai_run_id", ["ai_run_id"])
    with op.batch_alter_table("suggestions") as batch:
        for name, column_type in (
            ("generation_mode", sa.String(32)),
            ("workflow_version", sa.String(64)),
            ("input_hash", sa.String(64)),
            ("updated_at", sa.DateTime(timezone=True)),
        ):
            batch.alter_column(name, existing_type=column_type, nullable=False)
        batch.create_check_constraint(
            "ck_suggestion_generation_mode",
            "generation_mode IN ('model', 'rule_fallback')",
        )
        batch.create_check_constraint(
            "ck_suggestion_generation_provenance",
            "(generation_mode = 'model' AND ai_run_id IS NOT NULL) OR "
            "(generation_mode = 'rule_fallback' AND ai_run_id IS NULL)",
        )
        batch.create_foreign_key(
            "fk_suggestion_ai_run_owner",
            "ai_runs",
            ["ai_run_id", "owner_user_id"],
            ["id", "owner_user_id"],
        )
        batch.create_index("ix_suggestions_ai_run_id", ["ai_run_id"])


def downgrade() -> None:
    generated = op.get_bind().execute(
        sa.text(
            "SELECT id FROM match_analyses WHERE generation_mode != 'rule_fallback' "
            "OR workflow_version != 'legacy-rule-fallback@1' OR ai_run_id IS NOT NULL "
            "LIMIT 1"
        )
    ).first()
    if generated is not None:
        raise RuntimeError(
            "cannot downgrade match provenance while newly generated analyses exist"
        )

    with op.batch_alter_table("suggestions") as batch:
        batch.drop_index("ix_suggestions_ai_run_id")
        batch.drop_constraint("fk_suggestion_ai_run_owner", type_="foreignkey")
        batch.drop_constraint("ck_suggestion_generation_provenance", type_="check")
        batch.drop_constraint("ck_suggestion_generation_mode", type_="check")
        for name in ("updated_at", "input_hash", "ai_run_id", "workflow_version", "generation_mode"):
            batch.drop_column(name)
    with op.batch_alter_table("match_items") as batch:
        batch.drop_index("ix_match_items_ai_run_id")
        batch.drop_constraint("fk_match_item_ai_run_owner", type_="foreignkey")
        batch.drop_constraint("ck_match_item_generation_provenance", type_="check")
        batch.drop_constraint("ck_match_item_generation_mode", type_="check")
        for name in (
            "input_hash",
            "ai_run_id",
            "workflow_version",
            "generation_mode",
            "reason_code",
            "resume_target_paths",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("match_analyses") as batch:
        batch.drop_index("ix_match_analyses_ai_run_id")
        batch.drop_constraint("fk_match_analysis_ai_run_owner", type_="foreignkey")
        batch.drop_constraint("ck_match_analysis_generation_provenance", type_="check")
        batch.drop_constraint("ck_match_analysis_generation_mode", type_="check")
        for name in ("updated_at", "input_hash", "ai_run_id", "generation_mode"):
            batch.drop_column(name)
