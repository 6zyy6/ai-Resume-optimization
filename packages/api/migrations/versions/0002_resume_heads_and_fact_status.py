from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resumes") as batch:
        batch.add_column(sa.Column("head_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("head_version_id", sa.String(64), nullable=True))
    with op.batch_alter_table("resume_versions") as batch:
        batch.drop_constraint("uq_resume_snapshot_hash", type_="unique")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""
        CREATE TRIGGER trg_resume_versions_no_update
        BEFORE UPDATE ON resume_versions
        BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
        """)
        op.execute("""
        CREATE TRIGGER trg_resume_versions_no_delete
        BEFORE DELETE ON resume_versions
        BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
        """)
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        for trigger in (
            "trg_confirmed_fact_requires_source_insert",
            "trg_confirmed_fact_requires_source_update",
            "trg_confirmed_fact_keeps_source",
            "trg_confirmed_fact_keeps_source_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    with op.batch_alter_table("facts") as batch:
        batch.create_check_constraint("ck_fact_status", "status IN ('unconfirmed', 'confirmed', 'rejected')")
    if sqlite:
        op.execute("""
        CREATE TRIGGER trg_confirmed_fact_requires_source_update
        BEFORE UPDATE OF status, confirmed_at ON facts
        WHEN NEW.status = 'confirmed' AND NOT EXISTS (
          SELECT 1 FROM fact_sources WHERE fact_id = NEW.id AND owner_user_id = NEW.owner_user_id
        ) BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """)
        op.execute("""
        CREATE TRIGGER trg_confirmed_fact_keeps_source
        BEFORE DELETE ON fact_sources
        WHEN EXISTS (SELECT 1 FROM facts WHERE id = OLD.fact_id AND owner_user_id = OLD.owner_user_id AND status = 'confirmed')
          AND NOT EXISTS (SELECT 1 FROM fact_sources WHERE fact_id = OLD.fact_id AND owner_user_id = OLD.owner_user_id AND NOT (source_record_id = OLD.source_record_id AND source_hash = OLD.source_hash))
        BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """)
        op.execute("""
        CREATE TRIGGER trg_confirmed_fact_keeps_source_update
        BEFORE UPDATE OF fact_id, owner_user_id, source_record_id, source_hash ON fact_sources
        WHEN EXISTS (SELECT 1 FROM facts WHERE id = OLD.fact_id AND owner_user_id = OLD.owner_user_id AND status = 'confirmed')
          AND NOT EXISTS (SELECT 1 FROM fact_sources WHERE fact_id = OLD.fact_id AND owner_user_id = OLD.owner_user_id AND NOT (source_record_id = OLD.source_record_id AND source_hash = OLD.source_hash))
        BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """)
        op.execute("""
        CREATE TRIGGER trg_confirmed_fact_requires_source_insert
        BEFORE INSERT ON facts
        WHEN NEW.status = 'confirmed' AND NOT EXISTS (
          SELECT 1 FROM fact_sources WHERE fact_id = NEW.id AND owner_user_id = NEW.owner_user_id
        ) BEGIN SELECT RAISE(ABORT, 'confirmed fact requires a source'); END
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for trigger in (
            "trg_confirmed_fact_requires_source_insert",
            "trg_confirmed_fact_requires_source_update",
            "trg_confirmed_fact_keeps_source",
            "trg_confirmed_fact_keeps_source_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    with op.batch_alter_table("facts") as batch:
        batch.drop_constraint("ck_fact_status", type_="check")
    with op.batch_alter_table("resume_versions") as batch:
        batch.create_unique_constraint("uq_resume_snapshot_hash", ["resume_id", "snapshot_hash"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""
        CREATE TRIGGER trg_resume_versions_no_update
        BEFORE UPDATE ON resume_versions
        BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
        """)
        op.execute("""
        CREATE TRIGGER trg_resume_versions_no_delete
        BEFORE DELETE ON resume_versions
        BEGIN SELECT RAISE(ABORT, 'resume versions are append-only'); END
        """)
    with op.batch_alter_table("resumes") as batch:
        batch.drop_column("head_version_id")
        batch.drop_column("head_version")
