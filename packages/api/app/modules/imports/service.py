from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import (
    BulletFactLink,
    Fact,
    FactSource,
    File,
    Resume,
    ResumeImport,
    ResumeVersion,
    SourceRecord,
    VersionOperation,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.storage import StoragePort
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.imports.parsers import (
    DOCX_MIME,
    MAX_FILE_BYTES,
    FileParseError,
    parse_resume_file,
)
from app.modules.resumes.service import canonical_snapshot
from app.modules.tasks.service import TaskAdmission, TaskService


@dataclass(frozen=True)
class ImportedFact:
    kind: str
    value: str


@dataclass(frozen=True)
class DraftImport:
    text: str
    draft_facts: tuple[ImportedFact, ...]
    confirmed: bool = False
    confirmed_facts: tuple[ImportedFact, ...] = ()

    @classmethod
    def from_text(cls, text: str) -> "DraftImport":
        facts: list[ImportedFact] = []
        for line in (line.strip() for line in text.splitlines()):
            if not line:
                continue
            label, separator, value = line.partition("：")
            if not separator:
                label, separator, value = line.partition(":")
            facts.append(
                ImportedFact(
                    kind=label.strip() if separator else "resume_text",
                    value=value.strip() if separator else line,
                )
            )
        return cls(text=text, draft_facts=tuple(facts))

    def confirm(self) -> "DraftImport":
        return DraftImport(
            text=self.text,
            draft_facts=self.draft_facts,
            confirmed=True,
            confirmed_facts=self.draft_facts,
        )


@dataclass
class ImportServiceError(Exception):
    code: str
    message: str
    status_code: int


class ImportService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: StoragePort,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.idempotency = IdempotencyService()

    async def create_upload_token(
        self,
        owner_id: str,
        *,
        display_name: str,
        mime: str,
        size: int,
        sha256: str,
        purpose: str,
        idempotency_key: str,
    ) -> tuple[File, str]:
        self._validate_upload_metadata(display_name, mime, size, sha256)
        body = {
            "display_name": display_name,
            "mime": mime,
            "size": size,
            "sha256": sha256,
            "purpose": purpose,
        }
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session,
                    owner,
                    "/v1/files/upload-tokens",
                    idempotency_key,
                    body,
                )
            except IdempotencyConflict as error:
                raise ImportServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                response = claim.replay_response or {}
                row = await session.scalar(
                    select(File).where(
                        File.id == response["id"],
                        File.owner_user_id == owner,
                    )
                )
                if row is None:
                    raise RuntimeError("Idempotent upload token is missing")
                return row, response["upload_url"]
            object_key = f"uploads/{new_id('obj')}"
            row = File(
                id=new_id("file"),
                owner_user_id=owner,
                purpose=purpose,
                display_name=PurePath(display_name).name[:255],
                object_key=object_key,
                sha256=sha256,
                size=size,
                mime=mime,
                status="pending_upload",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            session.add(row)
            await session.flush()
            upload_url = self.storage.upload_url(object_key, mime, size, 600)
            await self.idempotency.complete(
                session,
                claim,
                201,
                {
                    "id": row.id,
                    "status": row.status,
                    "upload_url": upload_url,
                },
            )
            return row, upload_url

    async def confirm_upload(
        self,
        owner_id: str,
        file_id: str,
        idempotency_key: str,
    ) -> File:
        row = await self.get_file(owner_id, file_id)
        if row is None:
            raise ImportServiceError("RESOURCE_NOT_FOUND", "File not found", 404)
        stored = self.storage.get(row.object_key)
        if stored is None:
            raise ImportServiceError("FILE_UPLOAD_INCOMPLETE", "Uploaded object not found", 409)
        if (
            len(stored.content) != row.size
            or stored.sha256 != row.sha256
            or stored.mime != row.mime
        ):
            self.storage.delete(row.object_key)
            raise ImportServiceError(
                "FILE_UPLOAD_MISMATCH",
                "Uploaded object does not match its signed constraints",
                422,
            )
        try:
            parse_resume_file(row.display_name, row.mime, stored.content)
        except FileParseError as error:
            if error.code not in {"SCANNED_PDF", "ENCRYPTED_PDF", "CORRUPT_FILE"}:
                self.storage.delete(row.object_key)
                raise ImportServiceError(error.code, error.message, 422) from error
        route = f"/v1/files/{file_id}/confirm-upload"
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session,
                    owner,
                    route,
                    idempotency_key,
                    {"file_id": file_id},
                )
            except IdempotencyConflict as error:
                raise ImportServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                replay = await session.scalar(
                    select(File).where(
                        File.id == (claim.replay_response or {})["id"],
                        File.owner_user_id == owner,
                    )
                )
                if replay is None:
                    raise RuntimeError("Idempotent file response is missing")
                return replay
            file_row = await self._file(session, owner_id, file_id, lock=True)
            if file_row is None:
                raise ImportServiceError("RESOURCE_NOT_FOUND", "File not found", 404)
            file_row.status = "confirmed"
            file_row.expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            await session.flush()
            await self.idempotency.complete(
                session,
                claim,
                200,
                {"id": file_row.id, "status": file_row.status},
            )
            return file_row

    async def create_import(
        self,
        owner_id: str,
        file_id: str,
        idempotency_key: str,
        *,
        trace_id: str,
        task_service: TaskService,
    ) -> ResumeImport:
        file_row = await self.get_file(owner_id, file_id)
        if file_row is None or file_row.status != "confirmed":
            raise ImportServiceError("RESOURCE_NOT_FOUND", "Confirmed file not found", 404)
        route = "/v1/imports"
        body = {"file_id": file_id}
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, body
                )
            except IdempotencyConflict as error:
                raise ImportServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                replay = await session.scalar(
                    select(ResumeImport).where(
                        ResumeImport.id
                        == (claim.replay_response or {})["id"],
                        ResumeImport.owner_user_id == owner,
                    )
                )
                if replay is None:
                    raise RuntimeError("Idempotent import response is missing")
                return replay
            current = await self._file(session, owner_id, file_id)
            if current is None or current.status != "confirmed":
                raise ImportServiceError("RESOURCE_NOT_FOUND", "Confirmed file not found", 404)
            row = ResumeImport(
                id=new_id("imp"),
                owner_user_id=owner,
                file_id=current.id,
                status="queued",
                parsed_text_encrypted=None,
                draft_facts=[],
                fallback_reason=None,
            )
            session.add(row)
            await session.flush()
            task = await task_service.create_task_in_session(
                session,
                owner,
                task_type="parse_resume_import",
                queue="file.parse",
                trace_id=trace_id,
                idempotency_key=f"import:{idempotency_key}",
                admission=TaskAdmission.unmetered(),
                resource_type="resume_import",
                resource_id=row.id,
                payload={"import_id": row.id},
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
            return row

    async def attach_task(
        self,
        owner_id: str,
        import_id: str,
        task_id: str,
    ) -> ResumeImport:
        async with self.sessions.begin() as session:
            owners = await authorized_owner_ids(session, owner_id)
            row = await session.scalar(
                select(ResumeImport)
                .where(
                    ResumeImport.id == import_id,
                    ResumeImport.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if row is None:
                raise ImportServiceError("RESOURCE_NOT_FOUND", "Import not found", 404)
            if row.task_id is not None and row.task_id != task_id:
                raise ImportServiceError(
                    "IMPORT_TASK_CONFLICT", "Import already has another task", 409
                )
            row.task_id = task_id
            await session.flush()
            return row

    async def process_import(
        self,
        owner_id: str,
        import_id: str,
    ) -> str:
        row = await self.get_import(owner_id, import_id)
        if row is None:
            raise ImportServiceError("RESOURCE_NOT_FOUND", "Import not found", 404)
        if row.status in {"parsed", "needs_paste", "confirmed"}:
            return row.id
        file_row = await self.get_file(owner_id, row.file_id)
        if file_row is None:
            raise ImportServiceError("RESOURCE_NOT_FOUND", "File not found", 404)
        stored = self.storage.get(file_row.object_key)
        if stored is None:
            raise ImportServiceError("RESOURCE_NOT_FOUND", "Stored file not found", 404)
        parsed_text: str | None = None
        draft_facts: list[dict[str, str]] = []
        fallback_reason: str | None = None
        status = "parsed"
        try:
            parsed = parse_resume_file(
                file_row.display_name, file_row.mime, stored.content
            )
            draft = DraftImport.from_text(parsed.text)
            parsed_text = parsed.text
            draft_facts = [
                {"kind": item.kind, "value": item.value}
                for item in draft.draft_facts
            ]
        except FileParseError as error:
            if error.code not in {"SCANNED_PDF", "ENCRYPTED_PDF", "CORRUPT_FILE"}:
                raise ImportServiceError(error.code, error.message, 422) from error
            status = "needs_paste"
            fallback_reason = error.code
        async with self.sessions.begin() as session:
            owners = await authorized_owner_ids(session, owner_id)
            current = await session.scalar(
                select(ResumeImport)
                .where(
                    ResumeImport.id == import_id,
                    ResumeImport.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if current is None:
                raise ImportServiceError("RESOURCE_NOT_FOUND", "Import not found", 404)
            if current.status == "queued":
                current.status = status
                current.parsed_text_encrypted = parsed_text
                current.draft_facts = draft_facts
                current.fallback_reason = fallback_reason
                await session.flush()
            return current.id

    async def get_import(self, owner_id: str, import_id: str) -> ResumeImport | None:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            return await session.scalar(
                select(ResumeImport).where(
                    ResumeImport.id == import_id,
                    ResumeImport.owner_user_id.in_(owners),
                )
            )

    async def confirm_import(
        self,
        owner_id: str,
        import_id: str,
        facts: list[dict[str, Any]],
        idempotency_key: str,
        *,
        title: str,
    ) -> tuple[ResumeImport, list[str]]:
        route = f"/v1/imports/{import_id}/confirm"
        body = {"facts": facts, "title": title}
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, body
                )
            except IdempotencyConflict as error:
                raise ImportServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                response = claim.replay_response or {}
                row = await session.scalar(
                    select(ResumeImport).where(
                        ResumeImport.id == response["id"],
                        ResumeImport.owner_user_id == owner,
                    )
                )
                if row is None:
                    raise RuntimeError("Idempotent import response is missing")
                return row, response["fact_ids"]
            owners = await authorized_owner_ids(session, owner_id)
            row = await session.scalar(
                select(ResumeImport)
                .where(
                    ResumeImport.id == import_id,
                    ResumeImport.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if row is None:
                raise ImportServiceError("RESOURCE_NOT_FOUND", "Import not found", 404)
            if row.status == "queued":
                raise ImportServiceError(
                    "IMPORT_NOT_READY", "Import parsing is not complete", 409
                )
            if row.status == "confirmed":
                raise ImportServiceError(
                    "IMPORT_ALREADY_FINALIZED",
                    "Import has already been finalized",
                    409,
                )
            if row.status == "needs_paste" and not facts:
                raise ImportServiceError(
                    "FILE_PARSE_FAILED",
                    "Paste resume facts before confirmation",
                    422,
                )
            selected = self._selected_facts(row, facts)
            if not selected:
                raise ImportServiceError(
                    "IMPORT_FACTS_REQUIRED",
                    "Select at least one imported fact",
                    422,
                )
            fact_ids: list[str] = []
            fact_rows: list[tuple[Fact, list[str]]] = []
            for item, source_value in selected:
                value = item["value"].strip()
                source_hash = hashlib.sha256(source_value.encode()).hexdigest()
                source = SourceRecord(
                    id=new_id("src"),
                    owner_user_id=owner,
                    source_type=(
                        "imported_resume"
                        if row.status == "parsed"
                        else "user_confirmation"
                    ),
                    source_ref=row.id,
                    content_encrypted=source_value,
                )
                fact = Fact(
                    id=new_id("fact"),
                    owner_user_id=owner,
                    kind=item["kind"].strip() or "resume_text",
                    value_encrypted=value,
                    status="unconfirmed",
                )
                session.add_all([source, fact])
                await session.flush()
                session.add(
                    FactSource(
                        fact_id=fact.id,
                        source_record_id=source.id,
                        owner_user_id=owner,
                        source_range={"start": 0, "end": len(source_value)},
                        source_hash=source_hash,
                    )
                )
                await session.flush()
                fact.status = "confirmed"
                fact.confirmed_at = datetime.now(timezone.utc)
                fact_ids.append(fact.id)
                source_hashes = [source_hash]
                if row.status == "parsed" and value != source_value:
                    confirmed_hash = hashlib.sha256(value.encode()).hexdigest()
                    confirmed_source = SourceRecord(
                        id=new_id("src"),
                        owner_user_id=owner,
                        source_type="user_confirmation",
                        source_ref=row.id,
                        content_encrypted=value,
                    )
                    session.add(confirmed_source)
                    await session.flush()
                    session.add(
                        FactSource(
                            fact_id=fact.id,
                            source_record_id=confirmed_source.id,
                            owner_user_id=owner,
                            source_range={"start": 0, "end": len(value)},
                            source_hash=confirmed_hash,
                        )
                    )
                    source_hashes.append(confirmed_hash)
                fact_rows.append((fact, source_hashes))
            resume = Resume(
                id=new_id("resume"),
                owner_user_id=owner,
                kind="base",
                title=title,
                head_version=0,
                head_version_id=None,
            )
            session.add(resume)
            await session.flush()
            items: list[dict[str, Any]] = []
            link_values: list[tuple[str, Fact, list[str]]] = []
            for fact, source_hashes in fact_rows:
                bullet_id = new_id("bullet")
                items.append(
                    {
                        "id": bullet_id,
                        "text": fact.value_encrypted,
                        "fact_refs": [fact.id],
                    }
                )
                link_values.append((bullet_id, fact, source_hashes))
            snapshot, snapshot_hash = canonical_snapshot(
                {
                    "schema_version": "1",
                    "title": title,
                    "target": None,
                    "sections": [
                        {
                            "id": new_id("section"),
                            "type": "experience",
                            "title": "导入经历",
                            "items": items,
                        }
                    ],
                }
            )
            version = ResumeVersion(
                id=new_id("rver"),
                owner_user_id=owner,
                resume_id=resume.id,
                parent_version_id=None,
                snapshot_json=snapshot,
                snapshot_hash=snapshot_hash,
                created_by=owner,
            )
            session.add(version)
            await session.flush()
            session.add(
                VersionOperation(
                    id=new_id("vop"),
                    owner_user_id=owner,
                    version_id=version.id,
                    operation_type="save",
                    actor=owner,
                    metadata_json={"source": "resume_import", "import_id": row.id},
                )
            )
            for bullet_id, fact, source_hashes in link_values:
                session.add(
                    BulletFactLink(
                        resume_version_id=version.id,
                        bullet_id=bullet_id,
                        fact_id=fact.id,
                        fact_owner_user_id=fact.owner_user_id,
                        owner_user_id=owner,
                        claim_start=0,
                        claim_end=len(fact.value_encrypted),
                        claim_range={
                            "start": 0,
                            "end": len(fact.value_encrypted),
                        },
                        fact_value_encrypted_at_link=fact.value_encrypted,
                        fact_status_at_link=fact.status,
                        fact_source_hashes_at_link=source_hashes,
                    )
                )
            resume.head_version = 1
            resume.head_version_id = version.id
            row.status = "confirmed"
            row.confirmed_at = datetime.now(timezone.utc)
            row.resume_id = resume.id
            row.version_id = version.id
            await session.flush()
            response = {
                "id": row.id,
                "status": row.status,
                "fact_ids": fact_ids,
                "resume_id": row.resume_id,
                "version_id": row.version_id,
            }
            await self.idempotency.complete(session, claim, 200, response)
            return row, fact_ids

    @staticmethod
    def _selected_facts(
        row: ResumeImport,
        facts: list[dict[str, Any]],
    ) -> list[tuple[dict[str, str], str]]:
        if not facts:
            return [
                (
                    {"kind": item["kind"], "value": item["value"]},
                    item["value"],
                )
                for item in row.draft_facts
                if item.get("value", "").strip()
            ]
        selected: list[tuple[dict[str, str], str]] = []
        indexes: set[int] = set()
        for item in facts:
            kind = item["kind"].strip() or "resume_text"
            value = item["value"].strip()
            if not value:
                raise ImportServiceError(
                    "IMPORT_DRAFT_FACT_INVALID",
                    "Selected imported facts cannot be empty",
                    422,
                )
            index = item.get("draft_index")
            if index is None and row.status == "parsed":
                exact = [
                    candidate_index
                    for candidate_index, candidate in enumerate(row.draft_facts)
                    if candidate["kind"] == kind and candidate["value"] == value
                ]
                index = exact[0] if len(exact) == 1 else None
            if row.status == "parsed":
                if (
                    index is None
                    or index >= len(row.draft_facts)
                    or index in indexes
                ):
                    raise ImportServiceError(
                        "IMPORT_DRAFT_FACT_INVALID",
                        "Selected fact does not reference one parsed draft fact",
                        422,
                    )
                indexes.add(index)
                source_value = row.draft_facts[index]["value"]
            else:
                source_value = value
            selected.append(({"kind": kind, "value": value}, source_value))
        return selected

    async def delete_file(
        self,
        owner_id: str,
        file_id: str,
        idempotency_key: str,
    ) -> None:
        async with self.sessions() as session:
            row = await self._file(
                session, owner_id, file_id, include_deleted=True
            )
        if row is None:
            raise ImportServiceError("RESOURCE_NOT_FOUND", "File not found", 404)
        if row.deleted_at is None:
            self.storage.delete(row.object_key)
        route = f"/v1/files/{file_id}"
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, {"file_id": file_id}
                )
            except IdempotencyConflict as error:
                raise ImportServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                return
            current = await self._file(
                session,
                owner_id,
                file_id,
                lock=True,
                include_deleted=True,
            )
            if current is None:
                raise ImportServiceError("RESOURCE_NOT_FOUND", "File not found", 404)
            current.status = "deleted"
            current.deleted_at = datetime.now(timezone.utc)
            await session.flush()
            await self.idempotency.complete(
                session, claim, 204, {"id": current.id, "status": "deleted"}
            )

    async def get_file(self, owner_id: str, file_id: str) -> File | None:
        async with self.sessions() as session:
            return await self._file(session, owner_id, file_id)

    async def cleanup_expired_files(
        self,
        now: datetime | None = None,
        *,
        limit: int = 100,
    ) -> int:
        cutoff = now or datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            rows = list(
                (
                    await session.scalars(
                        select(File)
                        .where(
                            File.expires_at <= cutoff,
                            File.deleted_at.is_(None),
                        )
                        .order_by(File.expires_at, File.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for row in rows:
                row.status = "expired_cleanup"
            await session.flush()
        if not rows:
            return 0
        targets = [(row.id, row.object_key) for row in rows]
        for _, object_key in targets:
            self.storage.delete(object_key)
        async with self.sessions.begin() as session:
            current_rows = list(
                (
                    await session.scalars(
                        select(File)
                        .where(
                            File.id.in_([file_id for file_id, _ in targets]),
                            File.expires_at <= cutoff,
                            File.deleted_at.is_(None),
                            File.status == "expired_cleanup",
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for row in current_rows:
                row.status = "deleted"
                row.deleted_at = cutoff
            await session.flush()
            return len(current_rows)

    async def _file(
        self,
        session: AsyncSession,
        owner_id: str,
        file_id: str,
        *,
        lock: bool = False,
        include_deleted: bool = False,
    ) -> File | None:
        owners = await authorized_owner_ids(session, owner_id)
        query = select(File).where(
            File.id == file_id,
            File.owner_user_id.in_(owners),
        )
        if not include_deleted:
            query = query.where(File.deleted_at.is_(None))
        return await session.scalar(query.with_for_update() if lock else query)

    @staticmethod
    def _validate_upload_metadata(
        display_name: str,
        mime: str,
        size: int,
        sha256: str,
    ) -> None:
        if size <= 0 or size > MAX_FILE_BYTES:
            raise ImportServiceError("FILE_TOO_LARGE", "Files must be 1 byte to 10 MiB", 422)
        suffix = PurePath(display_name).suffix.lower()
        expected_mime = {
            ".pdf": "application/pdf",
            ".docx": DOCX_MIME,
            ".txt": "text/plain",
        }.get(suffix)
        if expected_mime is None or mime not in {
            expected_mime,
            f"{expected_mime}; charset=utf-8",
        }:
            raise ImportServiceError(
                "FILE_TYPE_UNSUPPORTED",
                "Only PDF, DOCX and UTF-8 TXT are accepted",
                422,
            )
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ImportServiceError("VALIDATION_FAILED", "sha256 must be lowercase hexadecimal", 422)
