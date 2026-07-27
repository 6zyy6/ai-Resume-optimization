import hashlib
import json
from base64 import b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import BulletFactLink, Fact, FactSource, JobDescription, Resume, ResumeVersion, VersionOperation
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.resumes.quality import QualityIssue, check_exportable, claim_ranges


@dataclass
class ResumeError(Exception):
    code: str
    message: str
    status_code: int


@dataclass(frozen=True)
class SavedVersion:
    row: ResumeVersion
    status_code: int
    operation: str
    response: dict[str, Any] | None = None


@dataclass(frozen=True)
class SavedResume:
    response: dict[str, Any]

    @property
    def id(self) -> str:
        return self.response["id"]


def canonical_snapshot(snapshot: dict[str, Any]) -> tuple[dict[str, Any], str]:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.loads(encoded), hashlib.sha256(encoded.encode()).hexdigest()


class ResumeService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.idempotency = IdempotencyService()

    async def create_resume(self, owner_id: str, values: dict[str, Any], idempotency_key: str) -> SavedResume:
        if values["kind"] == "base" and (
            values.get("base_resume_id") is not None
            or values.get("job_description_id") is not None
        ):
            raise ResumeError(
                "VALIDATION_FAILED",
                "Base resumes cannot reference a base resume or job description",
                422,
            )
        async with self.idempotency.transaction(self.sessions) as session:
            canonical = await canonical_user_id(session, owner_id)
            route = "/v1/resumes"
            try:
                claim = await self.idempotency.claim(session, canonical, route, idempotency_key, values)
            except IdempotencyConflict:
                raise ResumeError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if claim.is_replay:
                return SavedResume(claim.replay_response or {})
            reference_owners: dict[str, str | None] = {
                "base_resume_owner_user_id": None,
                "job_description_owner_user_id": None,
            }
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
                reference_owners = {
                    "base_resume_owner_user_id": base.owner_user_id,
                    "job_description_owner_user_id": job.owner_user_id,
                }
            resume = Resume(
                id=new_id("resume"),
                owner_user_id=canonical,
                **values,
                **reference_owners,
            )
            session.add(resume)
            await session.flush()
            response = self._resume_json(resume)
            await self.idempotency.complete(session, claim, 201, response)
            return SavedResume(response)

    async def list_resumes(self, owner_id: str, cursor: str | None = None, limit: int = 20) -> tuple[list[Resume], str | None]:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            query = select(Resume).where(Resume.owner_user_id.in_(owners)).order_by(Resume.created_at, Resume.id)
            if cursor:
                created_at, identifier = _decode_cursor(cursor)
                query = query.where((Resume.created_at > created_at) | ((Resume.created_at == created_at) & (Resume.id > identifier)))
            rows = list((await session.scalars(query.limit(limit + 1))).all())
            next_cursor = _cursor(rows[-2]) if len(rows) > limit else None
            return rows[:limit], next_cursor

    async def get_resume(self, owner_id: str, resume_id: str) -> Resume | None:
        async with self.sessions() as session:
            return await self._resume(session, owner_id, resume_id)

    async def update_resume(self, owner_id: str, resume_id: str, title: str, idempotency_key: str) -> SavedResume:
        async with self.idempotency.transaction(self.sessions) as session:
            canonical = await canonical_user_id(session, owner_id)
            route = f"/v1/resumes/{resume_id}"
            try:
                claim = await self.idempotency.claim(session, canonical, route, idempotency_key, {"title": title})
            except IdempotencyConflict:
                raise ResumeError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if claim.is_replay:
                return SavedResume(claim.replay_response or {})
            resume = await self._locked_resume(session, owner_id, resume_id)
            if resume is None:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404)
            replay = await self.idempotency.recheck(session, claim)
            if replay:
                return SavedResume(replay[1])
            resume.title = title
            await session.flush()
            response = self._resume_json(resume)
            await self.idempotency.complete(session, claim, 200, response)
            return SavedResume(response)

    async def versions(self, owner_id: str, resume_id: str, cursor: str | None = None, limit: int = 20) -> tuple[list[ResumeVersion], str | None] | None:
        async with self.sessions() as session:
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                return None
            query = select(ResumeVersion).where(ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id).order_by(ResumeVersion.created_at, ResumeVersion.id)
            if cursor:
                created_at, identifier = _decode_cursor(cursor)
                query = query.where((ResumeVersion.created_at > created_at) | ((ResumeVersion.created_at == created_at) & (ResumeVersion.id > identifier)))
            rows = list((await session.scalars(query.limit(limit + 1))).all())
            return rows[:limit], _cursor(rows[-2]) if len(rows) > limit else None

    async def save_resume_version(self, owner_id: str, resume_id: str, base_version: int, snapshot: dict[str, Any], idempotency_key: str) -> SavedVersion:
        return await self._append_version(
            owner_id, resume_id, base_version, snapshot, "save", idempotency_key,
            f"/v1/resumes/{resume_id}/versions", {"base_version": base_version, "snapshot": snapshot},
        )

    async def _append_version(self, owner_id: str, resume_id: str, base_version: int, snapshot: dict[str, Any], operation: str, idempotency_key: str, route: str, body: dict[str, Any]) -> SavedVersion:
        async with self.idempotency.transaction(self.sessions) as session:
            canonical = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(session, canonical, route, idempotency_key, body)
            except IdempotencyConflict:
                raise ResumeError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if claim.is_replay:
                response = claim.replay_response or {}
                version = await self._version(session, owner_id, response["id"])
                if version:
                    return SavedVersion(
                        version,
                        claim.replay_status or 200,
                        response.get("operation", operation),
                        response,
                    )
            resume = await self._locked_resume(session, owner_id, resume_id)
            if resume is None:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume not found", 404)
            replay = await self.idempotency.recheck(session, claim)
            if replay:
                response = replay[1]
                version = await self._version(session, owner_id, response["id"])
                if version:
                    return SavedVersion(
                        version,
                        replay[0],
                        response.get("operation", operation),
                        response,
                    )
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
            parent = await session.scalar(select(ResumeVersion).where(ResumeVersion.id == resume.head_version_id, ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id)) if resume.head_version_id else None
            if parent and parent.snapshot_hash == snapshot_hash and operation == "save":
                response = self._version_json(parent, operation)
                await self.idempotency.complete(session, claim, 200, response)
                return SavedVersion(parent, 200, operation, response)
            version = ResumeVersion(id=new_id("rver"), owner_user_id=resume.owner_user_id, resume_id=resume.id, parent_version_id=parent.id if parent else None, snapshot_json=snapshot_json, snapshot_hash=snapshot_hash, created_by=canonical)
            session.add(version)
            await session.flush()
            session.add(VersionOperation(id=new_id("vop"), owner_user_id=resume.owner_user_id, version_id=version.id, operation_type=operation, actor=canonical, metadata_json={"base_version": base_version}))
            for section in snapshot_json.get("sections", []):
                for bullet in section.get("items", []):
                    ranges = claim_ranges(bullet.get("text", ""))
                    for index, fact_id in enumerate(bullet.get("fact_refs", [])):
                        fact = next((item for item in facts if item.id == fact_id), None)
                        if fact is None:
                            raise ResumeError("BULLET_FACT_OWNER_MISMATCH", "Fact and resume must share a canonical owner", 422)
                        start, end = ranges[index]
                        session.add(BulletFactLink(resume_version_id=version.id, bullet_id=bullet["id"], fact_id=fact_id, fact_owner_user_id=fact.owner_user_id, owner_user_id=resume.owner_user_id, claim_range={"start": start, "end": end}))
            resume.head_version += 1
            resume.head_version_id = version.id
            await session.flush()
            response = self._version_json(version, operation)
            await self.idempotency.complete(session, claim, 201, response)
            return SavedVersion(version, 201, operation, response)

    async def restore(self, owner_id: str, resume_id: str, version_id: str, base_version: int, idempotency_key: str) -> SavedVersion:
        async with self.sessions() as session:
            version = await self._version(session, owner_id, version_id)
            if version is None or version.resume_id != resume_id:
                raise ResumeError("RESOURCE_NOT_FOUND", "Resume version not found", 404)
            snapshot = version.snapshot_json
        return await self._append_version(owner_id, resume_id, base_version, snapshot, "restore", idempotency_key, f"/v1/resumes/{resume_id}/versions/{version_id}/restore", {"base_version": base_version, "source_version_id": version_id})

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

    @staticmethod
    def _resume_json(resume: Resume) -> dict[str, Any]:
        return {
            "id": resume.id,
            "kind": resume.kind,
            "title": resume.title,
            "base_resume_id": resume.base_resume_id,
            "job_description_id": resume.job_description_id,
            "version": resume.head_version,
        }

    @staticmethod
    def _version_json(version: ResumeVersion, operation: str) -> dict[str, Any]:
        return {
            "id": version.id,
            "resume_id": version.resume_id,
            "parent_version_id": version.parent_version_id,
            "snapshot": version.snapshot_json,
            "snapshot_hash": version.snapshot_hash,
            "operation": operation,
            "created_at": version.created_at.isoformat(),
        }


def _cursor(row: Resume | ResumeVersion) -> str:
    return urlsafe_b64encode(f"{row.created_at.isoformat()}|{row.id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        parts = b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True).decode().split("|")
        if len(parts) != 2 or not parts[1]:
            raise ValueError("invalid cursor tuple")
        created_at, identifier = parts
        return datetime.fromisoformat(created_at), identifier
    except (UnicodeError, ValueError) as error:
        raise ResumeError("VALIDATION_FAILED", "Invalid cursor", 422) from error
