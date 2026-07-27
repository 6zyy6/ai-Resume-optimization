from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


TABLES: Sequence[str] = (
    "users",
    "user_identities",
    "user_consents",
    "sessions",
    "source_records",
    "experiences",
    "facts",
    "fact_sources",
    "fact_revisions",
    "job_descriptions",
    "resumes",
    "resume_versions",
    "resume_sections",
    "bullet_fact_links",
    "version_operations",
    "jd_requirements",
    "match_analyses",
    "match_items",
    "suggestions",
    "suggestion_fact_links",
    "suggestion_decisions",
    "files",
    "tasks",
    "task_events",
    "ai_runs",
    "ai_trace_events",
    "exports",
    "idempotency_records",
    "usage_ledger",
    "audit_logs",
)


def _create_owned_table(
    name: str,
    *elements: sa.SchemaItem,
    primary_key: Sequence[str] = ("id",),
) -> None:
    op.create_table(
        name,
        *elements,
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint(*primary_key),
    )
    op.create_index(f"ix_{name}_owner_user_id", name, ["owner_user_id"])


def _install_sqlite_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_requires_source_insert
        BEFORE INSERT ON facts
        WHEN NEW.status = 'confirmed'
          AND NOT EXISTS (
            SELECT 1 FROM fact_sources
            WHERE fact_id = NEW.id AND owner_user_id = NEW.owner_user_id
          )
        BEGIN
          SELECT RAISE(ABORT, 'confirmed fact requires a source');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_requires_source_update
        BEFORE UPDATE OF status, confirmed_at ON facts
        WHEN NEW.status = 'confirmed'
          AND NOT EXISTS (
            SELECT 1 FROM fact_sources
            WHERE fact_id = NEW.id AND owner_user_id = NEW.owner_user_id
          )
        BEGIN
          SELECT RAISE(ABORT, 'confirmed fact requires a source');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_keeps_source
        BEFORE DELETE ON fact_sources
        WHEN EXISTS (
          SELECT 1 FROM facts
          WHERE id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND status = 'confirmed'
        )
        AND NOT EXISTS (
          SELECT 1 FROM fact_sources
          WHERE fact_id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND NOT (
              source_record_id = OLD.source_record_id
              AND source_hash = OLD.source_hash
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'confirmed fact requires a source');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_confirmed_fact_keeps_source_update
        BEFORE UPDATE OF fact_id, owner_user_id, source_record_id, source_hash
        ON fact_sources
        WHEN EXISTS (
          SELECT 1 FROM facts
          WHERE id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND status = 'confirmed'
        )
        AND NOT EXISTS (
          SELECT 1 FROM fact_sources
          WHERE fact_id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND NOT (
              source_record_id = OLD.source_record_id
              AND source_hash = OLD.source_hash
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'confirmed fact requires a source');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_resume_versions_no_update
        BEFORE UPDATE ON resume_versions
        BEGIN
          SELECT RAISE(ABORT, 'resume versions are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_resume_versions_no_delete
        BEFORE DELETE ON resume_versions
        BEGIN
          SELECT RAISE(ABORT, 'resume versions are append-only');
        END
        """
    )
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
    op.execute(
        """
        CREATE TRIGGER trg_terminal_task_result_immutable
        BEFORE UPDATE OF result_ref ON tasks
        WHEN OLD.status IN ('succeeded', 'failed', 'cancelled')
          AND NEW.result_ref IS NOT OLD.result_ref
        BEGIN
          SELECT RAISE(ABORT, 'terminal task result is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_suggestion_accept_from_pending
        BEFORE UPDATE OF status ON suggestions
        WHEN NEW.status = 'accepted' AND OLD.status <> 'pending'
        BEGIN
          SELECT RAISE(ABORT, 'suggestion must be pending before acceptance');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_export_version_immutable
        BEFORE UPDATE OF resume_version_id ON exports
        WHEN NEW.resume_version_id <> OLD.resume_version_id
        BEGIN
          SELECT RAISE(ABORT, 'export version is immutable');
        END
        """
    )


def _install_postgresql_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION enforce_confirmed_fact_source()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
          fact_key varchar(64);
          owner_key varchar(64);
        BEGIN
          IF TG_TABLE_NAME = 'facts' THEN
            fact_key := NEW.id;
            owner_key := NEW.owner_user_id;
          ELSE
            fact_key := OLD.fact_id;
            owner_key := OLD.owner_user_id;
          END IF;

          IF EXISTS (
            SELECT 1
            FROM facts f
            WHERE f.id = fact_key
              AND f.owner_user_id = owner_key
              AND f.status = 'confirmed'
              AND NOT EXISTS (
                SELECT 1
                FROM fact_sources fs
                WHERE fs.fact_id = f.id
                  AND fs.owner_user_id = f.owner_user_id
              )
          ) THEN
            RAISE EXCEPTION 'confirmed fact requires a source'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_confirmed_fact_requires_source
        AFTER INSERT OR UPDATE ON facts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_confirmed_fact_source()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_confirmed_fact_keeps_source
        AFTER DELETE OR UPDATE ON fact_sources
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_confirmed_fact_source()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_usage_ledger_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'usage ledger is append-only'
            USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_usage_ledger_append_only
        BEFORE UPDATE OR DELETE ON usage_ledger
        FOR EACH ROW EXECUTE FUNCTION reject_usage_ledger_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_resume_version_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'resume versions are append-only'
            USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_resume_versions_append_only
        BEFORE UPDATE OR DELETE ON resume_versions
        FOR EACH ROW EXECUTE FUNCTION reject_resume_version_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_immutable_state()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_TABLE_NAME = 'tasks'
             AND OLD.status IN ('succeeded', 'failed', 'cancelled')
             AND NEW.result_ref IS DISTINCT FROM OLD.result_ref THEN
            RAISE EXCEPTION 'terminal task result is immutable'
              USING ERRCODE = '23514';
          END IF;
          IF TG_TABLE_NAME = 'suggestions'
             AND NEW.status = 'accepted'
             AND OLD.status <> 'pending' THEN
            RAISE EXCEPTION 'suggestion must be pending before acceptance'
              USING ERRCODE = '23514';
          END IF;
          IF TG_TABLE_NAME = 'exports'
             AND NEW.resume_version_id IS DISTINCT FROM OLD.resume_version_id THEN
            RAISE EXCEPTION 'export version is immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_terminal_task_result_immutable
        BEFORE UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION enforce_immutable_state()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_suggestion_accept_from_pending
        BEFORE UPDATE ON suggestions
        FOR EACH ROW EXECUTE FUNCTION enforce_immutable_state()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_export_version_immutable
        BEFORE UPDATE ON exports
        FOR EACH ROW EXECUTE FUNCTION enforce_immutable_state()
        """
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("locale", sa.String(16), nullable=False),
        sa.Column("email_encrypted", sa.Text(), nullable=True),
        sa.Column("email_lookup_hash", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_lookup_hash"),
    )
    _create_owned_table(
        "user_identities",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("external_subject_hash", sa.String(128), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("type", "external_subject_hash", name="uq_identity_external_subject"),
    )
    _create_owned_table(
        "user_consents",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_owned_table(
        "sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_type", sa.String(32), nullable=False),
        sa.UniqueConstraint("token_hash"),
    )
    _create_owned_table(
        "source_records",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("content_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('question_answer', 'imported_resume', 'user_edit', 'user_confirmation')",
            name="ck_source_record_type",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_source_record_owner"),
    )
    _create_owned_table(
        "experiences",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("organization", sa.String(255), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_experience_owner"),
    )
    _create_owned_table(
        "facts",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("experience_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_confirmed_fact_has_timestamp",
        ),
        sa.ForeignKeyConstraint(
            ["experience_id", "owner_user_id"],
            ["experiences.id", "experiences.owner_user_id"],
            name="fk_fact_experience_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_fact_owner"),
    )
    _create_owned_table(
        "fact_sources",
        sa.Column("fact_id", sa.String(64), nullable=False),
        sa.Column("source_record_id", sa.String(64), nullable=False),
        sa.Column("source_range", sa.JSON(), nullable=True),
        sa.Column("source_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_fact_source_fact_owner",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id", "owner_user_id"],
            ["source_records.id", "source_records.owner_user_id"],
            name="fk_fact_source_record_owner",
        ),
        primary_key=("fact_id", "source_record_id", "source_hash"),
    )
    _create_owned_table(
        "fact_revisions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("fact_id", sa.String(64), nullable=False),
        sa.Column("previous_value_hash", sa.String(128), nullable=False),
        sa.Column("new_value_encrypted", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_fact_revision_fact_owner",
        ),
    )
    _create_owned_table(
        "job_descriptions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("company", sa.String(255), nullable=True),
        sa.Column("raw_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_job_description_owner"),
    )
    _create_owned_table(
        "resumes",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("base_resume_id", sa.String(64), nullable=True),
        sa.Column("job_description_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind <> 'job_targeted' OR (base_resume_id IS NOT NULL AND job_description_id IS NOT NULL)",
            name="ck_targeted_resume_has_base_and_job",
        ),
        sa.ForeignKeyConstraint(
            ["base_resume_id", "owner_user_id"],
            ["resumes.id", "resumes.owner_user_id"],
            name="fk_resume_base_owner",
        ),
        sa.ForeignKeyConstraint(
            ["job_description_id", "owner_user_id"],
            ["job_descriptions.id", "job_descriptions.owner_user_id"],
            name="fk_resume_job_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_resume_owner"),
    )
    _create_owned_table(
        "resume_versions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("resume_id", sa.String(64), nullable=False),
        sa.Column("parent_version_id", sa.String(64), nullable=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_resume_version_parent_owner",
        ),
        sa.ForeignKeyConstraint(
            ["resume_id", "owner_user_id"],
            ["resumes.id", "resumes.owner_user_id"],
            name="fk_resume_version_resume_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_resume_version_owner"),
        sa.UniqueConstraint("resume_id", "snapshot_hash", name="uq_resume_snapshot_hash"),
    )
    _create_owned_table(
        "resume_sections",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("resume_version_id", sa.String(64), nullable=False),
        sa.Column("section_key", sa.String(128), nullable=False),
        sa.Column("index_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_resume_section_version_owner",
        ),
        sa.UniqueConstraint("resume_version_id", "section_key", name="uq_resume_section_key"),
    )
    _create_owned_table(
        "bullet_fact_links",
        sa.Column("resume_version_id", sa.String(64), nullable=False),
        sa.Column("bullet_id", sa.String(64), nullable=False),
        sa.Column("fact_id", sa.String(64), nullable=False),
        sa.Column("claim_range", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_bullet_fact_version_owner",
        ),
        sa.ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_bullet_fact_fact_owner",
        ),
        primary_key=("resume_version_id", "bullet_id", "fact_id"),
    )
    _create_owned_table(
        "version_operations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("operation_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_version_operation_version_owner",
        ),
    )
    _create_owned_table(
        "jd_requirements",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("text_encrypted", sa.Text(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id", "owner_user_id"],
            ["job_descriptions.id", "job_descriptions.owner_user_id"],
            name="fk_jd_requirement_job_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_jd_requirement_owner"),
    )
    _create_owned_table(
        "match_analyses",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("resume_version_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("workflow_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_match_analysis_version_owner",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "owner_user_id"],
            ["job_descriptions.id", "job_descriptions.owner_user_id"],
            name="fk_match_analysis_job_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_match_analysis_owner"),
    )
    _create_owned_table(
        "match_items",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("analysis_id", sa.String(64), nullable=False),
        sa.Column("requirement_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id", "owner_user_id"],
            ["match_analyses.id", "match_analyses.owner_user_id"],
            name="fk_match_item_analysis_owner",
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id", "owner_user_id"],
            ["jd_requirements.id", "jd_requirements.owner_user_id"],
            name="fk_match_item_requirement_owner",
        ),
    )
    _create_owned_table(
        "suggestions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("analysis_id", sa.String(64), nullable=False),
        sa.Column("target_path", sa.String(255), nullable=False),
        sa.Column("original_hash", sa.String(128), nullable=False),
        sa.Column("suggested_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'edited', 'ignored', 'reverted', 'blocked')",
            name="ck_suggestion_status",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id", "owner_user_id"],
            ["match_analyses.id", "match_analyses.owner_user_id"],
            name="fk_suggestion_analysis_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_suggestion_owner"),
    )
    _create_owned_table(
        "suggestion_fact_links",
        sa.Column("suggestion_id", sa.String(64), nullable=False),
        sa.Column("fact_id", sa.String(64), nullable=False),
        sa.Column("claim_range", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["suggestion_id", "owner_user_id"],
            ["suggestions.id", "suggestions.owner_user_id"],
            name="fk_suggestion_fact_suggestion_owner",
        ),
        sa.ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_suggestion_fact_fact_owner",
        ),
        primary_key=("suggestion_id", "fact_id"),
    )
    _create_owned_table(
        "suggestion_decisions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("suggestion_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("edited_text_encrypted", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["suggestion_id", "owner_user_id"],
            ["suggestions.id", "suggestions.owner_user_id"],
            name="fk_suggestion_decision_suggestion_owner",
        ),
    )
    _create_owned_table(
        "files",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_file_owner"),
        sa.UniqueConstraint("object_key"),
    )
    _create_owned_table(
        "tasks",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("result_ref", sa.String(255), nullable=True),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_task_progress"),
        sa.CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'cancelled') OR finished_at IS NOT NULL",
            name="ck_terminal_task_has_finished_at",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_task_owner"),
    )
    _create_owned_table(
        "task_events",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_task_event_progress"),
        sa.ForeignKeyConstraint(
            ["task_id", "owner_user_id"],
            ["tasks.id", "tasks.owner_user_id"],
            name="fk_task_event_task_owner",
        ),
        sa.UniqueConstraint("task_id", "seq", name="uq_task_event_sequence"),
    )
    _create_owned_table(
        "ai_runs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("workflow_type", sa.String(64), nullable=False),
        sa.Column("workflow_version", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("requested_model", sa.String(128), nullable=False),
        sa.Column("response_model", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_token_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("provider_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("cost_cny", sa.Numeric(18, 6), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("tool_count", sa.Integer(), nullable=False),
        sa.Column("schema_valid", sa.Boolean(), nullable=True),
        sa.Column("facts_valid", sa.Boolean(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("fallback_count", sa.Integer(), nullable=False),
        sa.Column("result_ref", sa.String(255), nullable=True),
        sa.Column("prompt_template_version", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "owner_user_id"],
            ["tasks.id", "tasks.owner_user_id"],
            name="fk_ai_run_task_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_ai_run_owner"),
    )
    _create_owned_table(
        "ai_trace_events",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("ai_run_id", sa.String(64), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ai_run_id", "owner_user_id"],
            ["ai_runs.id", "ai_runs.owner_user_id"],
            name="fk_ai_trace_event_run_owner",
        ),
        sa.UniqueConstraint("ai_run_id", "event_seq", name="uq_ai_trace_event_sequence"),
    )
    _create_owned_table(
        "exports",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("resume_version_id", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("file_id", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_export_version_owner",
        ),
        sa.ForeignKeyConstraint(
            ["file_id", "owner_user_id"],
            ["files.id", "files.owner_user_id"],
            name="fk_export_file_owner",
        ),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_export_owner"),
    )
    _create_owned_table(
        "idempotency_records",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("route", sa.String(255), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("body_hash", sa.String(128), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id",
            "route",
            "key",
            name="uq_idempotency_owner_route_key",
        ),
    )
    _create_owned_table(
        "usage_ledger",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("usage_type", sa.String(64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("cost_cny", sa.Numeric(18, 6), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_owned_table(
        "audit_logs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        _install_sqlite_triggers()
    elif dialect_name == "postgresql":
        _install_postgresql_triggers()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_confirmed_fact_requires_source ON facts")
        op.execute("DROP TRIGGER IF EXISTS trg_confirmed_fact_keeps_source ON fact_sources")
        op.execute("DROP TRIGGER IF EXISTS trg_usage_ledger_append_only ON usage_ledger")
        op.execute("DROP TRIGGER IF EXISTS trg_resume_versions_append_only ON resume_versions")
        op.execute("DROP TRIGGER IF EXISTS trg_terminal_task_result_immutable ON tasks")
        op.execute("DROP TRIGGER IF EXISTS trg_suggestion_accept_from_pending ON suggestions")
        op.execute("DROP TRIGGER IF EXISTS trg_export_version_immutable ON exports")
        op.execute("DROP FUNCTION IF EXISTS enforce_confirmed_fact_source()")
        op.execute("DROP FUNCTION IF EXISTS reject_usage_ledger_mutation()")
        op.execute("DROP FUNCTION IF EXISTS reject_resume_version_mutation()")
        op.execute("DROP FUNCTION IF EXISTS enforce_immutable_state()")

    for table_name in reversed(TABLES):
        op.drop_table(table_name)
