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
from app.modules.resumes.evidence_projection import (
    VersionEvidenceProjection,
    load_version_evidence,
)
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
    fact_id: str
    fact_owner_user_id: str
    fact_value_encrypted: str
    fact_status: str
    fact_source_hashes: tuple[str, ...]


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

    async def versions(self, owner_id: str, resume_id: str, cursor: str | None = None, limit: int = 20) -> tuple[list[SavedVersion], str | None] | None:
        async with self.sessions() as session:
            resume = await self._resume(session, owner_id, resume_id)
            if resume is None:
                return None
            query = select(ResumeVersion).where(ResumeVersion.resume_id == resume.id, ResumeVersion.owner_user_id == resume.owner_user_id).order_by(ResumeVersion.created_at, ResumeVersion.id)
            if cursor:
                created_at, identifier = _decode_cursor(cursor)
                query = query.where((ResumeVersion.created_at > created_at) | ((ResumeVersion.created_at == created_at) & (ResumeVersion.id > identifier)))
            rows = list((await session.scalars(query.limit(limit + 1))).all())
            page_rows = rows[:limit]
            operations_by_version: dict[str, list[str]] = {
                row.id: [] for row in page_rows
            }
            if page_rows:
                operations = (
                    await session.scalars(
                        select(VersionOperation).where(
                            VersionOperation.version_id.in_(
                                [row.id for row in page_rows]
                            ),
                            VersionOperation.owner_user_id == resume.owner_user_id,
                        )
                    )
                ).all()
                for operation in operations:
                    operations_by_version[operation.version_id].append(
                        operation.operation_type
                    )
            saved: list[SavedVersion] = []
            for row in page_rows:
                operation_types = operations_by_version[row.id]
                if (
                    len(operation_types) != 1
                    or operation_types[0] not in {"save", "restore"}
                ):
                    raise ResumeError(
                        "RESUME_VERSION_OPERATION_INVALID",
                        "Resume version operation history is invalid",
                        500,
                    )
                saved.append(SavedVersion(row, 200, operation_types[0]))
            return saved, _cursor(rows[-2]) if len(rows) > limit else None

    async def save_resume_version(
        self,
        owner_id: str,
        resume_id: str,
        base_version: int,
        snapshot: dict[str, Any],
        idempotency_key: str,
        claim_evidence: list[dict[str, Any]] | None = None,
    ) -> SavedVersion:
        self._validate_unique_bullet_ids(snapshot)
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
                projection = await load_version_evidence(session, source)
                snapshot = projection.snapshot
                links = [
                    ClaimLink(
                        bullet_id=claim.bullet_id,
                        start=claim.start,
                        end=claim.end,
                        fact_id=fact.fact_id,
                        fact_owner_user_id=fact.owner_user_id,
                        fact_value_encrypted=fact.value_encrypted,
                        fact_status=fact.status,
                        fact_source_hashes=fact.source_hashes,
                    )
                    for claim in projection.claims
                    for fact in claim.facts
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
                        fact_id=link.fact_id,
                        fact_owner_user_id=link.fact_owner_user_id,
                        owner_user_id=resume.owner_user_id,
                        claim_start=link.start,
                        claim_end=link.end,
                        claim_range={"start": link.start, "end": link.end},
                        fact_value_encrypted_at_link=link.fact_value_encrypted,
                        fact_status_at_link=link.fact_status,
                        fact_source_hashes_at_link=list(link.fact_source_hashes),
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
            projection = await load_version_evidence(session, version)
            try:
                self._validate_persisted_evidence(projection)
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
        bullets = self._validate_claim_structure(snapshot, claim_evidence)
        owners = await authorized_owner_ids(session, owner_id)
        links: list[ClaimLink] = []
        seen_links: set[tuple[str, int, int, str]] = set()
        for item in claim_evidence:
            facts: list[tuple[Fact, tuple[str, ...]]] = []
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
                source_hashes = tuple(
                    sorted(
                        (
                            await session.scalars(
                                select(FactSource.source_hash).where(
                                    FactSource.fact_id == fact.id,
                                    FactSource.owner_user_id == fact.owner_user_id,
                                )
                            )
                        ).all()
                    )
                )
                if not source_hashes:
                    raise ResumeError(
                        "CLAIM_EVIDENCE_FACT_SOURCE_REQUIRED",
                        "Claim evidence fact requires a source",
                        422,
                    )
                facts.append((fact, source_hashes))
            claim = bullets[item["bullet_id"]][item["start"] : item["end"]]
            evidence = " ".join(fact.value_encrypted for fact, _ in facts)
            if not facts or not supports_high_risk_entities(claim, evidence):
                raise ResumeError(
                    "CLAIM_EVIDENCE_FACT_MISMATCH",
                    "Claim evidence does not support high-risk claim entities",
                    422,
                )
            for fact, source_hashes in facts:
                link_key = (
                    item["bullet_id"],
                    item["start"],
                    item["end"],
                    fact.id,
                )
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
                        fact_id=fact.id,
                        fact_owner_user_id=fact.owner_user_id,
                        fact_value_encrypted=fact.value_encrypted,
                        fact_status=fact.status,
                        fact_source_hashes=source_hashes,
                    )
                )
        return links

    def _validate_claim_structure(
        self,
        snapshot: dict[str, Any],
        claim_evidence: list[dict[str, Any]],
    ) -> dict[str, str]:
        self._validate_unique_bullet_ids(snapshot)
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
        return bullets

    def _validate_persisted_evidence(
        self,
        projection: VersionEvidenceProjection,
    ) -> None:
        evidence = [
            {
                "bullet_id": claim.bullet_id,
                "start": claim.start,
                "end": claim.end,
                "fact_refs": [fact.fact_id for fact in claim.facts],
            }
            for claim in projection.claims
        ]
        bullets = self._validate_claim_structure(projection.snapshot, evidence)
        for claim in projection.claims:
            if not claim.facts:
                raise ResumeError(
                    "CLAIM_EVIDENCE_FACT_MISMATCH",
                    "Claim evidence does not support high-risk claim entities",
                    422,
                )
            for fact in claim.facts:
                if fact.status != "confirmed":
                    raise ResumeError(
                        "CLAIM_EVIDENCE_FACT_NOT_CONFIRMED",
                        "Claim evidence fact is not confirmed",
                        422,
                    )
                if not fact.source_hashes:
                    raise ResumeError(
                        "CLAIM_EVIDENCE_FACT_SOURCE_REQUIRED",
                        "Claim evidence fact requires a source",
                        422,
                    )
            claim_text = bullets[claim.bullet_id][claim.start : claim.end]
            fact_values = " ".join(
                fact.value_encrypted for fact in claim.facts
            )
            if not supports_high_risk_entities(claim_text, fact_values):
                raise ResumeError(
                    "CLAIM_EVIDENCE_FACT_MISMATCH",
                    "Claim evidence does not support high-risk claim entities",
                    422,
                )

    @staticmethod
    def _validate_unique_bullet_ids(snapshot: dict[str, Any]) -> None:
        bullet_ids = [
            bullet["id"]
            for section in snapshot.get("sections", [])
            for bullet in section.get("items", [])
        ]
        if len(bullet_ids) != len(set(bullet_ids)):
            raise ResumeError(
                "CLAIM_EVIDENCE_DUPLICATE_BULLET_ID",
                "Bullet IDs must be unique within a resume snapshot",
                422,
            )

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
                link.fact_value_encrypted_at_link,
                link.fact_status_at_link,
                tuple(link.fact_source_hashes_at_link),
            )
            for link in persisted
        } == {
            (
                link.bullet_id,
                link.fact_id,
                link.fact_owner_user_id,
                link.start,
                link.end,
                link.fact_value_encrypted,
                link.fact_status,
                link.fact_source_hashes,
            )
            for link in links
        }

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
