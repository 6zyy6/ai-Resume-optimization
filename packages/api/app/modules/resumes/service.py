import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import BulletFactLink, Fact, FactSource, JobDescription, Resume, ResumeVersion, VersionOperation
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.resumes.quality import QualityIssue, check_exportable


@dataclass(frozen=True)
class ResumeError(Exception):
    code: str
    message: str
    status_code: int


@dataclass(frozen=True)
class SavedVersion:
    row: ResumeVersion
    status_code: int
    operation: str


def canonical_snapshot(snapshot: dict[str, Any]) -> tuple[dict[str, Any], str]:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.loads(encoded), hashlib.sha256(encoded.encode()).hexdigest()


class ResumeService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.idempotency = IdempotencyService()

    async def create_resume(self, owner_id: str, values: dict[str, Any], idempotency_key: str) -> Resume:
        async with self.sessions.begin() as session:
            canonical = await canonical_user_id(session, owner_id)
            route = "/v1/resumes"
            try:
                replay = await self.idempotency.replay(session, canonical, route, idempotency_key, values)
            except IdempotencyConflict:
                raise ResumeError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if replay:
                resume = await self._resume(session, owner_id, replay[1]["id"])
                if resume:
                    return resume
            if values["kind"] == "job_targeted":
                if not values.get("base_resume_id") or not values.get("job_description_id"):
                    raise ResumeError("VALIDATION_FAILED", "Targeted resumes require base_resume_id and job_description_id", 422)
                base = await self._resume(session, owner_id, values["base_resume_id"])
                if base is None:
                    raise ResumeError("RESOURCE_NOT_FOUND", "Base resume not found", 404)
                owners = await authorized_owner_ids(session, owner_id)
                job = await session.scalar(select(JobDescription).where(JobDescription.id == values["job_description_id"], JobDescription.owner_user_id.in_(owners)))
                if job is None:
                    raise ResumeError("RESOURCE_NOT_FOUND", "Job description not found", 404)
                if base.owner_user_id != job.owner_user_id:
                    raise ResumeError("VALIDATION_FAILED", "Base resume and job description must share an owner", 422)
                canonical = base.owner_user_id
            resume = Resume(id=new_id("resume"), owner_user_id=canonical, **values)
            session.add(resume)
            await session.flush()
            await self.idempotency.store(session, canonical, route, idempotency_key, values, 201, {"id": resume.id})
            return resume

    async def list_resumes(self, owner_id: str) -> list[Resume]:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            return list((await session.scalars(select(Resume).where(Resume.owner_user_id.in_(owners)).order_by(Resume.created_at, Resume.id))).all())

    async def get_resume(self, owner_id: str, resume_id: str) -> Resume | None:
        async with self.sessions() as session:
            return await self._resume(session, owner_id, resume_id)

    async def update_resume(self, owner_id: str, resume_id: str, title: str, idempotency_key: str) -> Resume:
        async with self.sessions.begin() as session:
            canonical = await canonical_user_id(session, owner_id)
            route = f"/v1/resumes/{resume_id}"
            try:
                replay = await self.idempotency.replay(session, canonical, route, idempotency_key, {"title": title})
            except IdempotencyConflict:
                raise ResumeError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if replay:
                resume = await self._resume(session, owner_id, resume_id)
                if resume:
                    return resume
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404)
            resume.title = title
            await session.flush()
            await self.idempotency.store(session, canonical, route, idempotency_key, {"title": title}, 200, {"id": resume.id})
            return resume

    async def versions(self, owner_id: str, resume_id: str) -> list[ResumeVersion] | None:
        async with self.sessions() as session:
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                return None
            return list((await session.scalars(select(ResumeVersion).where(ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id).order_by(ResumeVersion.created_at, ResumeVersion.id))).all())

    async def save_resume_version(self, owner_id: str, resume_id: str, base_version: int, snapshot: dict[str, Any], operation: str, idempotency_key: str, *, route: str | None = None, idempotency_body: dict[str, Any] | None = None) -> SavedVersion:
        if operation != "save" and route is None:
            raise ResumeError("VALIDATION_FAILED", "Only normal saves are accepted here", 422)
        body = idempotency_body or {"base_version": base_version, "snapshot": snapshot}
        async with self.sessions.begin() as session:
            canonical = await canonical_user_id(session, owner_id)
            route = route or f"/v1/resumes/{resume_id}/versions"
            try:
                replay = await self.idempotency.replay(session, canonical, route, idempotency_key, body)
            except IdempotencyConflict:
                raise ResumeError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if replay:
                version = await self._version(session, owner_id, replay[1]["id"])
                if version:
                    return SavedVersion(version, replay[0], replay[1].get("operation", operation))
            resume = await self._locked_resume(session, owner_id, resume_id)
            if resume is None:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404)
            if base_version != resume.head_version:
                raise ResumeError("RESUME_VERSION_CONFLICT", "Resume has changed", 409)
            snapshot_json, snapshot_hash = canonical_snapshot(snapshot)
            owners = await authorized_owner_ids(session, owner_id)
            facts = list((await session.scalars(select(Fact).where(Fact.owner_user_id.in_(owners)))).all())
            sourced_fact_ids = set((await session.scalars(select(FactSource.fact_id).where(FactSource.owner_user_id.in_(owners)))).all())
            facts = [fact for fact in facts if fact.id in sourced_fact_ids]
            issues = check_exportable(snapshot_json, facts)
            if issues:
                raise ResumeError("BULLET_FACTS_INVALID", "Resume bullets must reference confirmed facts", 422)
            existing = await session.scalar(select(ResumeVersion).where(ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id, ResumeVersion.snapshot_hash == snapshot_hash))
            if existing and operation != "restore":
                response = {"id": existing.id, "operation": operation}
                await self.idempotency.store(session, canonical, route, idempotency_key, body, 200, response)
                return SavedVersion(existing, 200, operation)
            parent = await session.scalar(select(ResumeVersion).where(ResumeVersion.id == resume.head_version_id, ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id)) if resume.head_version_id else None
            version = ResumeVersion(id=new_id("rver"), owner_user_id=resume.owner_user_id, resume_id=resume.id, parent_version_id=parent.id if parent else None, snapshot_json=snapshot_json, snapshot_hash=snapshot_hash, created_by=canonical)
            session.add(version)
            await session.flush()
            session.add(VersionOperation(id=new_id("vop"), owner_user_id=resume.owner_user_id, version_id=version.id, operation_type=operation, actor=canonical, metadata_json={"base_version": base_version}))
            for section in snapshot_json.get("sections", []):
                for bullet in section.get("items", []):
                    for fact_id in bullet.get("fact_refs", []):
                        fact = next((item for item in facts if item.id == fact_id), None)
                        if fact is None or fact.owner_user_id != resume.owner_user_id:
                            continue
                        session.add(BulletFactLink(resume_version_id=version.id, bullet_id=bullet["id"], fact_id=fact_id, owner_user_id=resume.owner_user_id, claim_range={"start": 0, "end": len(bullet.get("text", ""))}))
            resume.head_version += 1
            resume.head_version_id = version.id
            await session.flush()
            await self.idempotency.store(session, canonical, route, idempotency_key, body, 201, {"id": version.id, "operation": operation})
            return SavedVersion(version, 201, operation)

    async def restore(self, owner_id: str, resume_id: str, version_id: str, base_version: int, idempotency_key: str) -> SavedVersion:
        async with self.sessions() as session:
            version = await self._version(session, owner_id, version_id)
            if version is None or version.resume_id != resume_id:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume version not found", 404)
            snapshot = version.snapshot_json
        return await self.save_resume_version(owner_id, resume_id, base_version, snapshot, "restore", idempotency_key, route=f"/v1/resumes/{resume_id}/versions/{version_id}/restore", idempotency_body={"base_version": base_version, "source_version_id": version_id})

    async def quality(self, owner_id: str, resume_id: str) -> list[QualityIssue] | None:
        async with self.sessions() as session:
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                return None
            version = await session.scalar(select(ResumeVersion).where(ResumeVersion.id == resume.head_version_id, ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id)) if resume.head_version_id else None
            if version is None:
                return []
            owners = await authorized_owner_ids(session, owner_id)
            facts = list((await session.scalars(select(Fact).where(Fact.owner_user_id.in_(owners)))).all())
            sourced_fact_ids = set((await session.scalars(select(FactSource.fact_id).where(FactSource.owner_user_id.in_(owners)))).all())
            facts = [fact for fact in facts if fact.id in sourced_fact_ids]
            return check_exportable(version.snapshot_json, facts)

    async def _resume(self, session: AsyncSession, owner_id: str, resume_id: str) -> Resume | None:
        owners = await authorized_owner_ids(session, owner_id)
        return await session.scalar(select(Resume).where(Resume.id == resume_id, Resume.owner_user_id.in_(owners)))

    async def _version(self, session: AsyncSession, owner_id: str, version_id: str) -> ResumeVersion | None:
        owners = await authorized_owner_ids(session, owner_id)
        return await session.scalar(select(ResumeVersion).where(ResumeVersion.id == version_id, ResumeVersion.owner_user_id.in_(owners)))

    async def _locked_resume(self, session: AsyncSession, owner_id: str, resume_id: str) -> Resume | None:
        owners = await authorized_owner_ids(session, owner_id)
        return await session.scalar(select(Resume).where(Resume.id == resume_id, Resume.owner_user_id.in_(owners)).with_for_update())
