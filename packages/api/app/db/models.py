from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DDL,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class OwnerMixin:
    @declared_attr
    def owner_user_id(cls) -> Mapped[str]:
        return mapped_column(ForeignKey("users.id"), nullable=False, index=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    email_encrypted: Mapped[str | None] = mapped_column(Text)
    email_lookup_hash: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserAlias(Base):
    __tablename__ = "user_aliases"
    __table_args__ = (
        CheckConstraint(
            "alias_user_id <> canonical_user_id",
            name="ck_user_alias_not_self",
        ),
    )

    alias_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"),
        primary_key=True,
    )
    canonical_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )


class UserIdentity(OwnerMixin, Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("type", "external_subject_hash", name="uq_identity_external_subject"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_subject_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserConsent(OwnerMixin, Base):
    __tablename__ = "user_consents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Session(OwnerMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_type: Mapped[str] = mapped_column(String(32), nullable=False)


class SourceRecord(OwnerMixin, Base):
    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_source_record_owner"),
        CheckConstraint(
            "source_type IN ('question_answer', 'imported_resume', 'user_edit', 'user_confirmation')",
            name="ck_source_record_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255))
    content_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Experience(OwnerMixin, Base):
    __tablename__ = "experiences"
    __table_args__ = (UniqueConstraint("id", "owner_user_id", name="uq_experience_owner"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    organization: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)


class Fact(OwnerMixin, Base):
    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_fact_owner"),
        ForeignKeyConstraint(
            ["experience_id", "owner_user_id"],
            ["experiences.id", "experiences.owner_user_id"],
            name="fk_fact_experience_owner",
        ),
        CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_confirmed_fact_has_timestamp",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experience_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FactSource(OwnerMixin, Base):
    __tablename__ = "fact_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_fact_source_fact_owner",
        ),
        ForeignKeyConstraint(
            ["source_record_id", "owner_user_id"],
            ["source_records.id", "source_records.owner_user_id"],
            name="fk_fact_source_record_owner",
        ),
    )

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_range: Mapped[dict | None] = mapped_column(JSON)
    source_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FactRevision(OwnerMixin, Base):
    __tablename__ = "fact_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_fact_revision_fact_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_value_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    new_value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Resume(OwnerMixin, Base):
    __tablename__ = "resumes"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_resume_owner"),
        ForeignKeyConstraint(
            ["base_resume_id", "owner_user_id"],
            ["resumes.id", "resumes.owner_user_id"],
            name="fk_resume_base_owner",
        ),
        ForeignKeyConstraint(
            ["job_description_id", "owner_user_id"],
            ["job_descriptions.id", "job_descriptions.owner_user_id"],
            name="fk_resume_job_owner",
        ),
        CheckConstraint(
            "kind <> 'job_targeted' OR (base_resume_id IS NOT NULL AND job_description_id IS NOT NULL)",
            name="ck_targeted_resume_has_base_and_job",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    base_resume_id: Mapped[str | None] = mapped_column(String(64))
    job_description_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ResumeVersion(OwnerMixin, Base):
    __tablename__ = "resume_versions"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_resume_version_owner"),
        ForeignKeyConstraint(
            ["resume_id", "owner_user_id"],
            ["resumes.id", "resumes.owner_user_id"],
            name="fk_resume_version_resume_owner",
        ),
        ForeignKeyConstraint(
            ["parent_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_resume_version_parent_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resume_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(String(64))
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ResumeSection(OwnerMixin, Base):
    __tablename__ = "resume_sections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_resume_section_version_owner",
        ),
        UniqueConstraint("resume_version_id", "section_key", name="uq_resume_section_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resume_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    section_key: Mapped[str] = mapped_column(String(128), nullable=False)
    index_data: Mapped[dict] = mapped_column(JSON, nullable=False)


class BulletFactLink(OwnerMixin, Base):
    __tablename__ = "bullet_fact_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_bullet_fact_version_owner",
        ),
        ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_bullet_fact_fact_owner",
        ),
    )

    resume_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bullet_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_range: Mapped[dict] = mapped_column(JSON, nullable=False)


class VersionOperation(OwnerMixin, Base):
    __tablename__ = "version_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_version_operation_version_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class JobDescription(OwnerMixin, Base):
    __tablename__ = "job_descriptions"
    __table_args__ = (UniqueConstraint("id", "owner_user_id", name="uq_job_description_owner"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255))
    raw_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class JdRequirement(OwnerMixin, Base):
    __tablename__ = "jd_requirements"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_jd_requirement_owner"),
        ForeignKeyConstraint(
            ["job_id", "owner_user_id"],
            ["job_descriptions.id", "job_descriptions.owner_user_id"],
            name="fk_jd_requirement_job_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    text_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class MatchAnalysis(OwnerMixin, Base):
    __tablename__ = "match_analyses"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_match_analysis_owner"),
        ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_match_analysis_version_owner",
        ),
        ForeignKeyConstraint(
            ["job_id", "owner_user_id"],
            ["job_descriptions.id", "job_descriptions.owner_user_id"],
            name="fk_match_analysis_job_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resume_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MatchItem(OwnerMixin, Base):
    __tablename__ = "match_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["analysis_id", "owner_user_id"],
            ["match_analyses.id", "match_analyses.owner_user_id"],
            name="fk_match_item_analysis_owner",
        ),
        ForeignKeyConstraint(
            ["requirement_id", "owner_user_id"],
            ["jd_requirements.id", "jd_requirements.owner_user_id"],
            name="fk_match_item_requirement_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requirement_id: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False)


class Suggestion(OwnerMixin, Base):
    __tablename__ = "suggestions"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_suggestion_owner"),
        ForeignKeyConstraint(
            ["analysis_id", "owner_user_id"],
            ["match_analyses.id", "match_analyses.owner_user_id"],
            name="fk_suggestion_analysis_owner",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'edited', 'ignored', 'reverted', 'blocked')",
            name="ck_suggestion_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_path: Mapped[str] = mapped_column(String(255), nullable=False)
    original_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    suggested_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SuggestionFactLink(OwnerMixin, Base):
    __tablename__ = "suggestion_fact_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["suggestion_id", "owner_user_id"],
            ["suggestions.id", "suggestions.owner_user_id"],
            name="fk_suggestion_fact_suggestion_owner",
        ),
        ForeignKeyConstraint(
            ["fact_id", "owner_user_id"],
            ["facts.id", "facts.owner_user_id"],
            name="fk_suggestion_fact_fact_owner",
        ),
    )

    suggestion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_range: Mapped[dict] = mapped_column(JSON, nullable=False)


class SuggestionDecision(OwnerMixin, Base):
    __tablename__ = "suggestion_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["suggestion_id", "owner_user_id"],
            ["suggestions.id", "suggestions.owner_user_id"],
            name="fk_suggestion_decision_suggestion_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    suggestion_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    edited_text_encrypted: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class File(OwnerMixin, Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("id", "owner_user_id", name="uq_file_owner"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Task(OwnerMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_task_owner"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_task_progress"),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'cancelled') OR finished_at IS NOT NULL",
            name="ck_terminal_task_has_finished_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    result_ref: Mapped[str | None] = mapped_column(String(255))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class TaskEvent(OwnerMixin, Base):
    __tablename__ = "task_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "owner_user_id"],
            ["tasks.id", "tasks.owner_user_id"],
            name="fk_task_event_task_owner",
        ),
        UniqueConstraint("task_id", "seq", name="uq_task_event_sequence"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_task_event_progress"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AiRun(OwnerMixin, Base):
    __tablename__ = "ai_runs"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_ai_run_owner"),
        ForeignKeyConstraint(
            ["task_id", "owner_user_id"],
            ["tasks.id", "tasks.owner_user_id"],
            name="fk_ai_run_task_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    response_model: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    schema_valid: Mapped[bool | None] = mapped_column(Boolean)
    facts_valid: Mapped[bool | None] = mapped_column(Boolean)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_ref: Mapped[str | None] = mapped_column(String(255))
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(128), nullable=False)


class AiTraceEvent(OwnerMixin, Base):
    __tablename__ = "ai_trace_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["ai_run_id", "owner_user_id"],
            ["ai_runs.id", "ai_runs.owner_user_id"],
            name="fk_ai_trace_event_run_owner",
        ),
        UniqueConstraint("ai_run_id", "event_seq", name="uq_ai_trace_event_sequence"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ai_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Export(OwnerMixin, Base):
    __tablename__ = "exports"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_export_owner"),
        ForeignKeyConstraint(
            ["resume_version_id", "owner_user_id"],
            ["resume_versions.id", "resume_versions.owner_user_id"],
            name="fk_export_version_owner",
        ),
        ForeignKeyConstraint(
            ["file_id", "owner_user_id"],
            ["files.id", "files.owner_user_id"],
            name="fk_export_file_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resume_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class IdempotencyRecord(OwnerMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "route", "key", name="uq_idempotency_owner_route_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_json: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageLedger(OwnerMixin, Base):
    __tablename__ = "usage_ledger"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    usage_type: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditLog(OwnerMixin, Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


Job = JobDescription
UsageRow = UsageLedger


event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_confirmed_fact_requires_source_insert
        BEFORE INSERT ON facts
        WHEN NEW.status = 'confirmed'
          AND NOT EXISTS (
            SELECT 1
            FROM fact_sources
            WHERE fact_id = NEW.id
              AND owner_user_id = NEW.owner_user_id
          )
        BEGIN
          SELECT RAISE(ABORT, 'confirmed fact requires a source');
        END
        """
    ).execute_if(dialect="sqlite"),
)

event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_confirmed_fact_keeps_source_update
        BEFORE UPDATE OF fact_id, owner_user_id, source_record_id, source_hash
        ON fact_sources
        WHEN EXISTS (
          SELECT 1
          FROM facts
          WHERE id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND status = 'confirmed'
        )
        AND NOT EXISTS (
          SELECT 1
          FROM fact_sources
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
    ).execute_if(dialect="sqlite"),
)

event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_resume_versions_no_update
        BEFORE UPDATE ON resume_versions
        BEGIN
          SELECT RAISE(ABORT, 'resume versions are append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)

event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_resume_versions_no_delete
        BEFORE DELETE ON resume_versions
        BEGIN
          SELECT RAISE(ABORT, 'resume versions are append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)

event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_usage_ledger_no_update
        BEFORE UPDATE ON usage_ledger
        BEGIN
          SELECT RAISE(ABORT, 'usage ledger is append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)

event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_usage_ledger_no_delete
        BEFORE DELETE ON usage_ledger
        BEGIN
          SELECT RAISE(ABORT, 'usage ledger is append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)

event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_confirmed_fact_requires_source_update
        BEFORE UPDATE OF status, confirmed_at ON facts
        WHEN NEW.status = 'confirmed'
          AND NOT EXISTS (
            SELECT 1
            FROM fact_sources
            WHERE fact_id = NEW.id
              AND owner_user_id = NEW.owner_user_id
          )
        BEGIN
          SELECT RAISE(ABORT, 'confirmed fact requires a source');
        END
        """
    ).execute_if(dialect="sqlite"),
)

event.listen(
    Base.metadata,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_confirmed_fact_keeps_source
        BEFORE DELETE ON fact_sources
        WHEN EXISTS (
          SELECT 1
          FROM facts
          WHERE id = OLD.fact_id
            AND owner_user_id = OLD.owner_user_id
            AND status = 'confirmed'
        )
        AND NOT EXISTS (
          SELECT 1
          FROM fact_sources
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
    ).execute_if(dialect="sqlite"),
)
