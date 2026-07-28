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
from app.modules.resumes.quality import QualityIssue, supports_high_risk_entities


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


@dataclass(frozen=True)
class ClaimLink:
    bullet_id: str
    start: int
    end: int
    fact: Fact


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

    async def save_resume_version(
        self,
        owner_id: str,
        resume_id: str,
        base_version: int,
        snapshot: dict[str, Any],
        idempotency_key: str,
        claim_evidence: list[dict[str, Any]] | None = None,
    ) -> SavedVersion:
        evidence = claim_evidence or []
        return await self._append_version(
            owner_id,
            resume_id,
            base_version,
            snapshot,
            "save",
            idempotency_key,
            f"/v1/resumes/{resume_id}/versions",
            {
                "base_version": base_version,
                "snapshot": snapshot,
                "claim_evidence": evidence,
            },
            claim_evidence=evidence,
        )

    async def _append_version(
        self,
        owner_id: str,
        resume_id: str,
        base_version: int,
        snapshot: dict[str, Any] | None,
        operation: str,
        idempotency_key: str,
        route: str,
        body: dict[str, Any],
        *,
        claim_evidence: list[dict[str, Any]] | None = None,
        source_version_id: str | None = None,
    ) -> SavedVersion:
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
            if source_version_id is not None:
                source = await self._version(session, owner_id, source_version_id)
                if source is None or source.resume_id != resume.id:
                    raise ResumeError(
                        "RESOURCE_NOT_FOUND",
                        "Resume version not found",
                        404,
                    )
                snapshot = source.snapshot_json
                links = [
                    ClaimLink(
                        bullet_id=link.bullet_id,
                        start=link.claim_range["start"],
                        end=link.claim_range["end"],
                        fact=await session.get(Fact, link.fact_id),
                    )
                    for link in (
                        await session.scalars(
                            select(BulletFactLink).where(
                                BulletFactLink.resume_version_id == source.id,
                                BulletFactLink.owner_user_id == source.owner_user_id,
                            )
                        )
                    ).all()
                ]
            else:
                if snapshot is None:
                    raise ResumeError("VALIDATION_FAILED", "Snapshot is required", 422)
                links = await self._validate_claim_evidence(
                    session,
                    owner_id,
                    snapshot,
                    claim_evidence or [],
                )
            snapshot_json, snapshot_hash = canonical_snapshot(snapshot)
            parent = await session.scalar(select(ResumeVersion).where(ResumeVersion.id == resume.head_version_id, ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id)) if resume.head_version_id else None
            if (
                parent
                and parent.snapshot_hash == snapshot_hash
                and operation == "save"
                and await self._links_match(session, parent.id, links)
            ):
                response = self._version_json(parent, operation)
                await self.idempotency.complete(session, claim, 200, response)
                return SavedVersion(parent, 200, operation, response)
            version = ResumeVersion(id=new_id("rver"), owner_user_id=resume.owner_user_id, resume_id=resume.id, parent_version_id=parent.id if parent else None, snapshot_json=snapshot_json, snapshot_hash=snapshot_hash, created_by=canonical)
            session.add(version)
            await session.flush()
            session.add(VersionOperation(id=new_id("vop"), owner_user_id=resume.owner_user_id, version_id=version.id, operation_type=operation, actor=canonical, metadata_json={"base_version": base_version}))
            for link in links:
                session.add(
                    BulletFactLink(
                        resume_version_id=version.id,
                        bullet_id=link.bullet_id,
                        fact_id=link.fact.id,
                        fact_owner_user_id=link.fact.owner_user_id,
                        owner_user_id=resume.owner_user_id,
                        claim_range={"start": link.start, "end": link.end},
                    )
                )
            resume.head_version += 1
            resume.head_version_id = version.id
            await session.flush()
            response = self._version_json(version, operation)
            await self.idempotency.complete(session, claim, 201, response)
            return SavedVersion(version, 201, operation, response)

    async def restore(self, owner_id: str, resume_id: str, version_id: str, base_version: int, idempotency_key: str) -> SavedVersion:
        return await self._append_version(
            owner_id,
            resume_id,
            base_version,
            None,
            "restore",
            idempotency_key,
            f"/v1/resumes/{resume_id}/versions/{version_id}/restore",
            {"base_version": base_version, "source_version_id": version_id},
            source_version_id=version_id,
        )

    async def quality(self, owner_id: str, resume_id: str) -> list[QualityIssue] | None:
        async with self.sessions() as session:
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                return None
            version = await session.scalar(select(ResumeVersion).where(ResumeVersion.id == resume.head_version_id, ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id)) if resume.head_version_id else None
            if version is None:
                return []
            evidence = await self._version_claim_evidence(session, version.id)
            try:
                await self._validate_claim_evidence(
                    session,
                    owner_id,
                    version.snapshot_json,
                    evidence,
                )
            except ResumeError as error:
                return [QualityIssue(error.code, "claim_evidence", error.message)]
            return []

    async def _validate_claim_evidence(
        self,
        session: AsyncSession,
        owner_id: str,
        snapshot: dict[str, Any],
        claim_evidence: list[dict[str, Any]],
    ) -> list[ClaimLink]:
        bullets = {
            bullet["id"]: bullet.get("text", "")
            for section in snapshot.get("sections", [])
            for bullet in section.get("items", [])
        }
        grouped: dict[str, list[dict[str, Any]]] = {
            bullet_id: [] for bullet_id in bullets
        }
        for item in claim_evidence:
            bullet_id = item["bullet_id"]
            if bullet_id not in bullets:
                raise ResumeError(
                    "CLAIM_EVIDENCE_UNKNOWN_BULLET",
                    "Claim evidence references an unknown bullet",
                    422,
                )
            start, end = item["start"], item["end"]
            if start < 0 or end <= start or end > len(bullets[bullet_id]):
                raise ResumeError(
                    "CLAIM_EVIDENCE_RANGE_INVALID",
                    "Claim evidence range is outside the bullet",
                    422,
                )
            grouped[bullet_id].append(item)

        for bullet_id, text in bullets.items():
            items = sorted(
                grouped[bullet_id],
                key=lambda item: (item["start"], item["end"]),
            )
            for previous, current in zip(items, items[1:]):
                if current["start"] < previous["end"]:
                    raise ResumeError(
                        "CLAIM_EVIDENCE_RANGE_OVERLAP",
                        "Claim evidence ranges overlap",
                        422,
                    )
            if text and (
                not items
                or items[0]["start"] != 0
                or items[-1]["end"] != len(text)
                or any(
                    current["start"] != previous["end"]
                    for previous, current in zip(items, items[1:])
                )
            ):
                raise ResumeError(
                    "CLAIM_EVIDENCE_COVERAGE_REQUIRED",
                    "Claim evidence must cover the entire bullet",
                    422,
                )

        owners = await authorized_owner_ids(session, owner_id)
        links: list[ClaimLink] = []
        seen_links: set[tuple[str, str]] = set()
        for item in claim_evidence:
            facts: list[Fact] = []
            for fact_id in item["fact_refs"]:
                fact = await session.get(Fact, fact_id)
                if fact is None or fact.owner_user_id not in owners:
                    raise ResumeError(
                        "CLAIM_EVIDENCE_FACT_OWNER_INVALID",
                        "Claim evidence fact belongs to another owner",
                        422,
                    )
                if fact.status != "confirmed":
                    raise ResumeError(
                        "CLAIM_EVIDENCE_FACT_NOT_CONFIRMED",
                        "Claim evidence fact is not confirmed",
                        422,
                    )
                source = await session.scalar(
                    select(FactSource.fact_id).where(
                        FactSource.fact_id == fact.id,
                        FactSource.owner_user_id == fact.owner_user_id,
                    )
                )
                if source is None:
                    raise ResumeError(
                        "CLAIM_EVIDENCE_FACT_SOURCE_REQUIRED",
                        "Claim evidence fact requires a source",
                        422,
                    )
                facts.append(fact)
            claim = bullets[item["bullet_id"]][item["start"] : item["end"]]
            evidence = " ".join(fact.value_encrypted for fact in facts)
            if not facts or not supports_high_risk_entities(claim, evidence):
                raise ResumeError(
                    "CLAIM_EVIDENCE_FACT_MISMATCH",
                    "Claim evidence does not support high-risk claim entities",
                    422,
                )
            for fact in facts:
                link_key = (item["bullet_id"], fact.id)
                if link_key in seen_links:
                    raise ResumeError(
                        "CLAIM_EVIDENCE_RANGE_OVERLAP",
                        "A fact cannot map to overlapping bullet claims",
                        422,
                    )
                seen_links.add(link_key)
                links.append(
                    ClaimLink(
                        bullet_id=item["bullet_id"],
                        start=item["start"],
                        end=item["end"],
                        fact=fact,
                    )
                )
        return links

    async def _links_match(
        self,
        session: AsyncSession,
        version_id: str,
        links: list[ClaimLink],
    ) -> bool:
        persisted = (
            await session.scalars(
                select(BulletFactLink).where(
                    BulletFactLink.resume_version_id == version_id
                )
            )
        ).all()
        return {
            (
                link.bullet_id,
                link.fact_id,
                link.fact_owner_user_id,
                link.claim_range["start"],
                link.claim_range["end"],
            )
            for link in persisted
        } == {
            (
                link.bullet_id,
                link.fact.id,
                link.fact.owner_user_id,
                link.start,
                link.end,
            )
            for link in links
        }

    async def _version_claim_evidence(
        self,
        session: AsyncSession,
        version_id: str,
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, int, int], list[str]] = {}
        links = (
            await session.scalars(
                select(BulletFactLink).where(
                    BulletFactLink.resume_version_id == version_id
                )
            )
        ).all()
        for link in links:
            key = (
                link.bullet_id,
                link.claim_range["start"],
                link.claim_range["end"],
            )
            grouped.setdefault(key, []).append(link.fact_id)
        return [
            {
                "bullet_id": bullet_id,
                "start": start,
                "end": end,
                "fact_refs": sorted(fact_refs),
            }
            for (bullet_id, start, end), fact_refs in sorted(grouped.items())
        ]

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
