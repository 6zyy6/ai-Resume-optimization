from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import Export, File, ResumeVersion
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.storage import StoragePort
from app.modules.exports.templates import (
    ExportBlocked,
    render_resume_pdf,
    sanitize_download_name,
)
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.resumes.evidence_projection import load_version_evidence
from app.modules.resumes.quality import (
    high_risk_terms,
    responsibility_claim_supported,
    supports_high_risk_entities,
)
from app.modules.tasks.service import TaskAdmission, TaskService


@dataclass
class ExportServiceError(Exception):
    code: str
    message: str
    status_code: int


@dataclass(frozen=True)
class ExportResult:
    export: Export
    download_url: str | None
    download_expires_in: int | None


class ExportService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: StoragePort,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.idempotency = IdempotencyService()

    async def create(
        self,
        owner_id: str,
        *,
        resume_version_id: str,
        template_version: str,
        download_name: str | None,
        idempotency_key: str,
        trace_id: str,
        task_service: TaskService,
    ) -> ExportResult:
        version = await self._version(owner_id, resume_version_id)
        if version is None:
            raise ExportServiceError(
                "RESOURCE_NOT_FOUND", "Resume version not found", 404
            )
        name = sanitize_download_name(
            download_name or f"{version.snapshot_json.get('title', 'resume')}.pdf"
        )
        export_id = new_id("exp")
        file_id = new_id("file")
        object_key = f"exports/{new_id('obj')}"
        route = "/v1/exports"
        body = {
            "resume_version_id": resume_version_id,
            "template_version": template_version,
            "download_name": download_name,
        }
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, body
                )
            except IdempotencyConflict as error:
                raise ExportServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                saved = await session.scalar(
                    select(Export).where(
                        Export.id == (claim.replay_response or {})["id"],
                        Export.owner_user_id == owner,
                    )
                )
                if saved is None:
                    raise RuntimeError("Idempotent export response is missing")
                file_row = await session.scalar(
                    select(File).where(
                        File.id == saved.file_id,
                        File.owner_user_id == saved.owner_user_id,
                    )
                )
                if file_row is None:
                    raise RuntimeError("Idempotent export file is missing")
                return self._result(saved, file_row)
            current = await self._owned_version(
                session, owner_id, resume_version_id
            )
            if current is None:
                raise ExportServiceError(
                    "RESOURCE_NOT_FOUND", "Resume version not found", 404
                )
            file_row = File(
                id=file_id,
                owner_user_id=owner,
                purpose="resume_export",
                display_name=name,
                object_key=object_key,
                sha256="",
                size=0,
                mime="application/pdf",
                status="pending_generation",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
            session.add(file_row)
            await session.flush()
            row = Export(
                id=export_id,
                owner_user_id=owner,
                resume_version_id=current.id,
                template_version=template_version,
                file_id=file_row.id,
                content_hash=current.snapshot_hash,
                status="queued",
                download_name=name,
            )
            session.add(row)
            await session.flush()
            task = await task_service.create_task_in_session(
                session,
                owner,
                task_type="render_resume_export",
                queue="file.export",
                trace_id=trace_id,
                idempotency_key=f"export:{idempotency_key}",
                admission=TaskAdmission.unmetered(),
                resource_type="export",
                resource_id=row.id,
                payload={"export_id": row.id},
            )
            row.task_id = task.id
            await self.idempotency.complete(
                session,
                claim,
                202,
                {
                    "id": row.id,
                    "status": row.status,
                    "task_id": row.task_id,
                },
            )
            return self._result(row, file_row)

    async def attach_task(
        self,
        owner_id: str,
        export_id: str,
        task_id: str,
    ) -> Export:
        async with self.sessions.begin() as session:
            owners = await authorized_owner_ids(session, owner_id)
            row = await session.scalar(
                select(Export)
                .where(
                    Export.id == export_id,
                    Export.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if row is None:
                raise ExportServiceError("RESOURCE_NOT_FOUND", "Export not found", 404)
            if row.task_id is not None and row.task_id != task_id:
                raise ExportServiceError(
                    "EXPORT_TASK_CONFLICT", "Export already has another task", 409
                )
            row.task_id = task_id
            await session.flush()
            return row

    async def process_export(
        self,
        owner_id: str,
        export_id: str,
    ) -> str:
        result = await self.get(owner_id, export_id)
        if result is None:
            raise ExportServiceError("RESOURCE_NOT_FOUND", "Export not found", 404)
        if result.export.status == "succeeded":
            return result.export.id
        version = await self._version(owner_id, result.export.resume_version_id)
        if version is None:
            raise ExportServiceError(
                "RESOURCE_NOT_FOUND", "Resume version not found", 404
            )
        await self._assert_facts(owner_id, version)
        try:
            rendered = render_resume_pdf(
                version.snapshot_json, result.export.template_version
            )
        except ExportBlocked as error:
            code = str(error).split(":", 1)[0]
            raise ExportServiceError(code, str(error), 422) from error
        async with self.sessions() as session:
            file_row = await session.scalar(
                select(File).where(
                    File.id == result.export.file_id,
                    File.owner_user_id == result.export.owner_user_id,
                )
            )
        if file_row is None:
            raise RuntimeError("Export file record is missing")
        stored = self.storage.put(
            file_row.object_key, rendered.pdf, "application/pdf"
        )
        async with self.sessions.begin() as session:
            owners = await authorized_owner_ids(session, owner_id)
            current = await session.scalar(
                select(Export)
                .where(
                    Export.id == export_id,
                    Export.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if current is None:
                self.storage.delete(file_row.object_key)
                raise ExportServiceError("RESOURCE_NOT_FOUND", "Export not found", 404)
            current_file = await session.scalar(
                select(File)
                .where(
                    File.id == current.file_id,
                    File.owner_user_id == current.owner_user_id,
                )
                .with_for_update()
            )
            if current_file is None:
                self.storage.delete(file_row.object_key)
                raise RuntimeError("Export file record is missing")
            if current.status == "succeeded":
                return current.id
            current.content_hash = rendered.snapshot_hash
            current.status = "succeeded"
            current_file.sha256 = stored.sha256
            current_file.size = len(stored.content)
            current_file.status = "confirmed"
            await session.flush()
            return current.id

    async def get(self, owner_id: str, export_id: str) -> ExportResult | None:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            row = await session.scalar(
                select(Export).where(
                    Export.id == export_id,
                    Export.owner_user_id.in_(owners),
                )
            )
            if row is None:
                return None
            file_row = await session.scalar(
                select(File).where(
                    File.id == row.file_id,
                    File.owner_user_id == row.owner_user_id,
                    File.deleted_at.is_(None),
                )
            )
            if file_row is None:
                raise ExportServiceError(
                    "RESOURCE_NOT_FOUND", "Export file not found", 404
                )
            if row.status == "succeeded" and self.storage.get(file_row.object_key) is None:
                raise ExportServiceError(
                    "RESOURCE_NOT_FOUND", "Export file not found", 404
                )
            return self._result(row, file_row)

    def _result(self, row: Export, file_row: File) -> ExportResult:
        succeeded = row.status == "succeeded"
        return ExportResult(
            export=row,
            download_url=(
                self.storage.download_url(
                    file_row.object_key, row.download_name, 600
                )
                if succeeded
                else None
            ),
            download_expires_in=600 if succeeded else None,
        )

    async def _version(
        self, owner_id: str, version_id: str
    ) -> ResumeVersion | None:
        async with self.sessions() as session:
            return await self._owned_version(session, owner_id, version_id)

    @staticmethod
    async def _owned_version(
        session: AsyncSession,
        owner_id: str,
        version_id: str,
    ) -> ResumeVersion | None:
        owners = await authorized_owner_ids(session, owner_id)
        return await session.scalar(
            select(ResumeVersion).where(
                ResumeVersion.id == version_id,
                ResumeVersion.owner_user_id.in_(owners),
            )
        )

    async def _assert_facts(
        self, owner_id: str, version: ResumeVersion
    ) -> None:
        bullets: dict[str, str] = {}
        for section in version.snapshot_json.get("sections", []):
            for item in section.get("items", []):
                if item.get("text"):
                    bullets[item.get("id", "")] = item["text"]
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            current = await session.scalar(
                select(ResumeVersion).where(
                    ResumeVersion.id == version.id,
                    ResumeVersion.owner_user_id.in_(owners),
                )
            )
            if current is None:
                raise ExportServiceError(
                    "RESOURCE_NOT_FOUND", "Resume version not found", 404
                )
            projection = await load_version_evidence(session, current)
        claims_by_bullet = {
            bullet_id: sorted(
                (claim for claim in projection.claims if claim.bullet_id == bullet_id),
                key=lambda claim: (claim.start, claim.end),
            )
            for bullet_id in bullets
        }
        for bullet_id, text in bullets.items():
            claims = claims_by_bullet[bullet_id]
            cursor = 0
            for claim in claims:
                if claim.start != cursor or claim.end <= claim.start or claim.end > len(text):
                    self._blocked()
                if (
                    not claim.facts
                    or any(
                        fact.owner_user_id != projection.owner_user_id
                        or fact.status != "confirmed"
                        or not fact.source_hashes
                        for fact in claim.facts
                    )
                ):
                    self._blocked()
                claim_text = text[claim.start : claim.end]
                evidence = " ".join(
                    fact.value_encrypted for fact in claim.facts
                )
                if not responsibility_claim_supported(
                    claim_text,
                    (fact.value_encrypted for fact in claim.facts),
                ):
                    self._blocked()
                claim_terms = high_risk_terms(claim_text)
                exact_fact_match = any(
                    claim_text.strip().casefold()
                    == fact.value_encrypted.strip().casefold()
                    for fact in claim.facts
                )
                if (
                    not supports_high_risk_entities(claim_text, evidence)
                    or (
                        claim_terms
                        and not claim_terms <= high_risk_terms(evidence)
                    )
                    or (not claim_terms and not exact_fact_match)
                ):
                    self._blocked()
                cursor = claim.end
            if cursor != len(text):
                self._blocked()

    @staticmethod
    def _blocked() -> None:
        raise ExportServiceError(
            "EXPORT_BLOCKED_BY_FACTS",
            "Export contains pending, missing, or unsupported facts",
            422,
        )
