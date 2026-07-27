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
    op.execute("""
    UPDATE resumes
    SET head_version = (
      SELECT COUNT(*) FROM resume_versions rv
      WHERE rv.resume_id = resumes.id AND rv.owner_user_id = resumes.owner_user_id
    ), head_version_id = (
      SELECT rv.id FROM resume_versions rv
      WHERE rv.resume_id = resumes.id AND rv.owner_user_id = resumes.owner_user_id
      ORDER BY rv.created_at DESC, rv.id DESC LIMIT 1
    )
    """)
    with op.batch_alter_table("resume_versions") as batch:
        batch.drop_constraint("uq_resume_snapshot_hash", type_="unique")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""
        CREATE TRIGGER trg_resume_head_matches_version
        BEFORE UPDATE OF head_version, head_version_id ON resumes
        WHEN NEW.head_version_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM resume_versions
          WHERE id = NEW.head_version_id AND resume_id = NEW.id AND owner_user_id = NEW.owner_user_id
        ) BEGIN SELECT RAISE(ABORT, 'resume head must reference its own version'); END
        """)
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
    duplicate = op.get_bind().execute(sa.text("""
      SELECT 1 FROM resume_versions
      GROUP BY resume_id, snapshot_hash HAVING COUNT(*) > 1 LIMIT 1
    """)).scalar()
    if duplicate:
        raise RuntimeError("cannot downgrade 0002 while restored duplicate snapshots exist")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_resume_head_matches_version")
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
