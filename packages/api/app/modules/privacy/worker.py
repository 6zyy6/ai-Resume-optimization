from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import (
    AiRun,
    AiTraceEvent,
    AuditLog,
    BulletFactLink,
    Experience,
    Export,
    Fact,
    FactRevision,
    FactSource,
    File,
    IdempotencyRecord,
    IntakeAnswer,
    IntakeSession,
    JdRequirement,
    JobDescription,
    MatchAnalysis,
    MatchItem,
    Outbox,
    Resume,
    ResumeImport,
    ResumeSection,
    ResumeVersion,
    Session,
    SourceRecord,
    Suggestion,
    SuggestionDecision,
    SuggestionFactLink,
    TargetedResumeKey,
    Task,
    TaskEvent,
    UsageLedger,
    User,
    UserAlias,
    UserConsent,
    UserIdentity,
    VersionOperation,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.storage import StoragePort


class PrivacyWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: StoragePort,
    ) -> None:
        self.sessions = sessions
        self.storage = storage

    async def export_data(self, owner_id: str, task_id: str) -> str:
        object_key = f"privacy/data-exports/{task_id}.json"
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            existing = await session.scalar(
                select(File).where(
                    File.owner_user_id.in_(owners),
                    File.object_key == object_key,
                    File.deleted_at.is_(None),
                )
            )
            if existing is not None and self.storage.get(object_key) is not None:
                return existing.id
            payload = await self._export_payload(session, owners)
        content = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        stored = self.storage.put(object_key, content, "application/json")
        now = datetime.now(timezone.utc)
        try:
            async with self.sessions.begin() as session:
                owner = await canonical_user_id(session, owner_id)
                existing = await session.scalar(
                    select(File)
                    .where(
                        File.owner_user_id == owner,
                        File.object_key == object_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    existing.sha256 = stored.sha256
                    existing.size = len(stored.content)
                    existing.status = "confirmed"
                    existing.deleted_at = None
                    existing.expires_at = now + timedelta(days=7)
                    return existing.id
                row = File(
                    id=new_id("file"),
                    owner_user_id=owner,
                    purpose="data_export",
                    display_name="account-data.json",
                    object_key=object_key,
                    sha256=stored.sha256,
                    size=len(stored.content),
                    mime="application/json",
                    status="confirmed",
                    expires_at=now + timedelta(days=7),
                )
                session.add(row)
                await session.flush()
                return row.id
        except BaseException:
            self.storage.delete(object_key)
            raise

    async def delete_account(
        self,
        owner_id: str,
        task_id: str,
    ) -> str:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            object_keys = tuple(
                await session.scalars(
                    select(File.object_key).where(File.owner_user_id.in_(owners))
                )
            )
        for object_key in object_keys:
            self.storage.delete(object_key)

        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            owner = await canonical_user_id(session, owner_id)
            owners = await authorized_owner_ids(session, owner)
            for model in (
                AiTraceEvent,
                SuggestionDecision,
                SuggestionFactLink,
                MatchItem,
                Suggestion,
                MatchAnalysis,
                JdRequirement,
                Export,
                ResumeImport,
                BulletFactLink,
                ResumeSection,
                VersionOperation,
                IntakeAnswer,
                FactRevision,
                FactSource,
                TargetedResumeKey,
                ResumeVersion,
                IntakeSession,
                Resume,
                Fact,
                SourceRecord,
                Experience,
                JobDescription,
                File,
                AiRun,
                UsageLedger,
                AuditLog,
                UserConsent,
                UserIdentity,
                Session,
                IdempotencyRecord,
                Outbox,
            ):
                await session.execute(
                    delete(model).where(model.owner_user_id.in_(owners))
                )
            await session.execute(
                delete(TaskEvent).where(
                    TaskEvent.owner_user_id.in_(owners),
                    TaskEvent.task_id != task_id,
                )
            )
            await session.execute(
                delete(Task).where(
                    Task.owner_user_id.in_(owners),
                    Task.id != task_id,
                )
            )
            await session.execute(
                delete(UserAlias).where(
                    UserAlias.alias_user_id.in_(owners),
                )
            )
            alias_users = tuple(item for item in owners if item != owner)
            if alias_users:
                await session.execute(delete(User).where(User.id.in_(alias_users)))
            await session.execute(
                update(User)
                .where(User.id == owner)
                .values(
                    status="deleted",
                    email_encrypted=None,
                    email_lookup_hash=None,
                    password_hash=None,
                    deleted_at=now,
                )
            )
        return owner

    async def _export_payload(
        self,
        session: AsyncSession,
        owners: tuple[str, ...],
    ) -> dict[str, Any]:
        account = await session.execute(
            select(User.id, User.status, User.locale, User.created_at).where(
                User.id == owners[0]
            )
        )
        user = account.mappings().one()
        return {
            "account": {
                "user_id": user["id"],
                "status": user["status"],
                "locale": user["locale"],
                "created_at": _json_value(user["created_at"]),
            },
            "consents": await _rows(session, UserConsent, owners),
            "experiences": await _rows(session, Experience, owners),
            "sources": await _rows(
                session,
                SourceRecord,
                owners,
                {"content_encrypted": "content"},
            ),
            "facts": await _rows(
                session,
                Fact,
                owners,
                {"value_encrypted": "value"},
            ),
            "fact_sources": await _rows(session, FactSource, owners),
            "resumes": await _rows(session, Resume, owners),
            "resume_versions": await _rows(
                session,
                ResumeVersion,
                owners,
                {"snapshot_json": "snapshot"},
            ),
            "jobs": await _rows(
                session,
                JobDescription,
                owners,
                {"raw_encrypted": "raw"},
            ),
            "job_requirements": await _rows(
                session,
                JdRequirement,
                owners,
                {"text_encrypted": "text"},
            ),
            "imports": await _rows(session, ResumeImport, owners),
            "matches": await _rows(session, MatchAnalysis, owners),
            "match_items": await _rows(session, MatchItem, owners),
            "suggestions": await _rows(
                session,
                Suggestion,
                owners,
                {
                    "original_text_encrypted": "original_text",
                    "suggested_encrypted": "suggested_text",
                },
            ),
            "suggestion_decisions": await _rows(
                session,
                SuggestionDecision,
                owners,
                {"edited_text_encrypted": "edited_text"},
            ),
            "usage": await _rows(session, UsageLedger, owners),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": "1",
        }


async def _rows(
    session: AsyncSession,
    model,
    owners: tuple[str, ...],
    rename: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(model).where(model.owner_user_id.in_(owners))
        )
    ).all()
    names = rename or {}
    return [
        {
            names.get(column.name, column.name): _json_value(
                getattr(row, column.name)
            )
            for column in model.__table__.columns
            if column.name != "owner_user_id"
        }
        for row in rows
    ]


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value
