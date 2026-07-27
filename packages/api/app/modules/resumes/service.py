import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import Fact, JobDescription, Resume, ResumeVersion, VersionOperation
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
                if not await self._resume(session, owner_id, values["base_resume_id"]):
                    raise ResumeError("RESOURCE_NOT_FOUND", "Base resume not found", 404)
                owners = await authorized_owner_ids(session, owner_id)
                job = await session.scalar(select(JobDescription).where(JobDescription.id == values["job_description_id"], JobDescription.owner_user_id.in_(owners)))
                if job is None:
                    raise ResumeError("RESOURCE_NOT_FOUND", "Job description not found", 404)
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
            return list((await session.scalars(select(ResumeVersion).where(ResumeVersion.resume_id == resume.id).order_by(ResumeVersion.created_at, ResumeVersion.id))).all())

    async def save_resume_version(self, owner_id: str, resume_id: str, base_version: int, snapshot: dict[str, Any], operation: str, idempotency_key: str) -> SavedVersion:
        body = {"base_version": base_version, "snapshot": snapshot, "operation": operation}
        async with self.sessions.begin() as session:
            canonical = await canonical_user_id(session, owner_id)
            route = f"/v1/resumes/{resume_id}/versions"
            try:
                replay = await self.idempotency.replay(session, canonical, route, idempotency_key, body)
            except IdempotencyConflict:
                raise ResumeError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if replay:
                version = await self._version(session, owner_id, replay[1]["id"])
                if version:
                    return SavedVersion(version, replay[0], replay[1].get("operation", operation))
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404)
            version_count = int(await session.scalar(select(func.count()).select_from(ResumeVersion).where(ResumeVersion.resume_id == resume.id)) or 0)
            if base_version != version_count:
                raise ResumeError("RESUME_VERSION_CONFLICT", "Resume has changed", 409)
            snapshot_json, snapshot_hash = canonical_snapshot(snapshot)
            facts = list((await session.scalars(select(Fact).where(Fact.owner_user_id == resume.owner_user_id))).all())
            issues = check_exportable(snapshot_json, facts)
            if issues:
                raise ResumeError("BULLET_FACTS_INVALID", "Resume bullets must reference confirmed facts", 422)
            existing = await session.scalar(select(ResumeVersion).where(ResumeVersion.resume_id == resume.id, ResumeVersion.snapshot_hash == snapshot_hash))
            if existing and operation != "restore":
                response = {"id": existing.id, "operation": operation}
                await self.idempotency.store(session, canonical, route, idempotency_key, body, 200, response)
                return SavedVersion(existing, 200, operation)
            parent = await session.scalar(select(ResumeVersion).where(ResumeVersion.resume_id == resume.id).order_by(ResumeVersion.created_at.desc(), ResumeVersion.id.desc()))
            version = ResumeVersion(id=new_id("rver"), owner_user_id=resume.owner_user_id, resume_id=resume.id, parent_version_id=parent.id if parent else None, snapshot_json=snapshot_json, snapshot_hash=snapshot_hash, created_by=canonical)
            session.add(version)
            await session.flush()
            session.add(VersionOperation(id=new_id("vop"), owner_user_id=resume.owner_user_id, version_id=version.id, operation_type=operation, actor=canonical, metadata_json={"base_version": base_version}))
            await session.flush()
            await self.idempotency.store(session, canonical, route, idempotency_key, body, 201, {"id": version.id, "operation": operation})
            return SavedVersion(version, 201, operation)

    async def restore(self, owner_id: str, resume_id: str, version_id: str, base_version: int, idempotency_key: str) -> SavedVersion:
        async with self.sessions() as session:
            version = await self._version(session, owner_id, version_id)
            if version is None or version.resume_id != resume_id:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume version not found", 404)
            snapshot = version.snapshot_json
        return await self.save_resume_version(owner_id, resume_id, base_version, snapshot, "restore", idempotency_key)

    async def quality(self, owner_id: str, resume_id: str) -> list[QualityIssue] | None:
        async with self.sessions() as session:
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                return None
            version = await session.scalar(select(ResumeVersion).where(ResumeVersion.resume_id == resume.id).order_by(ResumeVersion.created_at.desc(), ResumeVersion.id.desc()))
            if version is None:
                return []
            facts = list((await session.scalars(select(Fact).where(Fact.owner_user_id == resume.owner_user_id))).all())
            return check_exportable(version.snapshot_json, facts)

    async def _resume(self, session: AsyncSession, owner_id: str, resume_id: str) -> Resume | None:
        owners = await authorized_owner_ids(session, owner_id)
        return await session.scalar(select(Resume).where(Resume.id == resume_id, Resume.owner_user_id.in_(owners)))

    async def _version(self, session: AsyncSession, owner_id: str, version_id: str) -> ResumeVersion | None:
        owners = await authorized_owner_ids(session, owner_id)
        return await session.scalar(select(ResumeVersion).where(ResumeVersion.id == version_id, ResumeVersion.owner_user_id.in_(owners)))
