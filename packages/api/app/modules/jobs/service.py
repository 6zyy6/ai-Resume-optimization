from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import JdRequirement, JobDescription, Outbox, Task
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.ai_client import (
    AiCancellation,
    AiClient,
    AiExecutionReceipt,
    MAX_AI_TEXT_LENGTH,
    ParseJdPayload,
    ParseJdRequest,
    ParseJdResult,
)
from app.modules.ai_runs.service import AiRunService
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.tasks.service import TaskAdmission, TaskService


@dataclass
class JobServiceError(Exception):
    code: str
    message: str
    status_code: int


class JobService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ai_client: AiClient | None = None,
    ) -> None:
        self.sessions = sessions
        self.ai_client = ai_client
        self.ai_runs = AiRunService()
        self.idempotency = IdempotencyService()

    async def create(
        self,
        owner_id: str,
        values: dict[str, Any],
        idempotency_key: str,
    ) -> JobDescription:
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, "/v1/jobs", idempotency_key, values
                )
            except IdempotencyConflict as error:
                raise JobServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                row = await session.scalar(
                    select(JobDescription).where(
                        JobDescription.id
                        == (claim.replay_response or {})["id"],
                        JobDescription.owner_user_id == owner,
                    )
                )
                if row is None:
                    raise RuntimeError("Idempotent job response is missing")
                return row
            row = JobDescription(
                id=new_id("job"),
                owner_user_id=owner,
                title=values["title"],
                company=values.get("company"),
                raw_encrypted=values["raw"],
                status="draft",
            )
            session.add(row)
            await session.flush()
            await self.idempotency.complete(
                session, claim, 201, {"id": row.id, "status": row.status}
            )
            return row

    async def parse(
        self,
        owner_id: str,
        job_id: str,
        idempotency_key: str,
        *,
        trace_id: str,
        task_service: TaskService,
    ) -> tuple[JobDescription, list[JdRequirement]]:
        route = f"/v1/jobs/{job_id}/parse"
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, {"job_id": job_id}
                )
            except IdempotencyConflict as error:
                raise JobServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                current = await self._job(session, owner_id, job_id)
                if current is None:
                    raise RuntimeError("Idempotent queued job is missing")
                return current, await self._requirements(session, current)
            current = await self._job(session, owner_id, job_id, lock=True)
            if current is None:
                raise JobServiceError("RESOURCE_NOT_FOUND", "Job not found", 404)
            if len(current.raw_encrypted) > MAX_AI_TEXT_LENGTH:
                raise JobServiceError(
                    "JD_TEXT_TOO_LONG",
                    f"Job description must not exceed {MAX_AI_TEXT_LENGTH} characters",
                    422,
                )
            existing_task = (
                await session.scalar(
                    select(Task).where(
                        Task.id == current.task_id,
                        Task.owner_user_id == owner,
                        Task.type == "parse_job",
                    )
                )
                if current.task_id
                else None
            )
            if current.status == "parsed" or (
                current.status == "queued"
                and existing_task is not None
                and existing_task.status in {"queued", "running"}
            ):
                requirements = await self._requirements(session, current)
                await self.idempotency.complete(
                    session,
                    claim,
                    202,
                    {
                        "id": current.id,
                        "status": current.status,
                        "task_id": current.task_id,
                    },
                )
                return current, requirements
            current.status = "queued"
            parse_snapshot = self._build_parse_snapshot(current)
            parse_input_hash = _semantic_hash(parse_snapshot)
            generation_mode = (
                "model" if self.ai_client is not None else "rule_fallback"
            )
            task = await task_service.create_task_in_session(
                session,
                owner,
                task_type="parse_job",
                queue="ai.interactive",
                trace_id=trace_id,
                idempotency_key=f"job-parse:{idempotency_key}",
                admission=(
                    TaskAdmission.ai()
                    if generation_mode == "model"
                    else TaskAdmission.unmetered()
                ),
                resource_type="job_description",
                resource_id=current.id,
                payload={
                    "job_id": current.id,
                    "generation_mode": generation_mode,
                    "parse_input_hash": parse_input_hash,
                    "parse_snapshot": parse_snapshot,
                },
            )
            current.task_id = task.id
            await session.flush()
            await self.idempotency.complete(
                session,
                claim,
                202,
                {
                    "id": current.id,
                    "status": current.status,
                    "task_id": current.task_id,
                },
            )
            return current, []

    async def process_parse(
        self,
        owner_id: str,
        job_id: str,
        *,
        trace_id: str,
        task_id: str,
        claim_token: str,
        task_service: TaskService,
        cancellation: AiCancellation | None = None,
    ) -> str:
        del trace_id
        async with self.sessions.begin() as session:
            task = await task_service.claimed_task_in_session(
                session, owner_id, task_id, claim_token
            )
            job = await self._job(session, owner_id, job_id, lock=True)
            self._require_parse_graph(job, task)
            if job.status == "parsed":
                await task_service.complete_task_in_session(session, task, job.id)
                return job.id
            generation_mode, input_hash, payload = await self._parse_input(
                session, task, job
            )
            if payload.jd_text != job.raw_encrypted:
                await self._fail_parse_in_session(
                    session,
                    job,
                    task,
                    task_service,
                    "JD_PARSE_SOURCE_CHANGED",
                    release_unused_ai_reservation=True,
                )
                return job.id
            task.stage = "jd_parse_processing"
            await session.flush()
            request_trace_id = task.trace_id

        receipt: AiExecutionReceipt | None = None
        if generation_mode == "model":
            if self.ai_client is None:
                raise JobServiceError(
                    "AI_NOT_CONFIGURED", "AI job parsing is not configured", 503
                )
            request = ParseJdRequest(
                workflow_type="parse_jd",
                workflow_version="2",
                prompt_template_version="jd-parse@2",
                trace_id=request_trace_id,
                task_id=task_id,
                owner_scope_hash=hashlib.sha256(owner_id.encode()).hexdigest(),
                locale="zh-CN",
                input_version=1,
                input_hash=input_hash,
                payload=payload,
            )
            receipt = await self.ai_client.run(request, cancellation)
        else:
            requirements = self._parse_lines(payload.jd_text)

        async with self.sessions.begin() as session:
            claimed_task = (
                await task_service.ai_receipt_task_in_session(
                    session,
                    owner_id,
                    task_id,
                    claim_token,
                    receipt.run.ai_run_id,
                    receipt.run.status,
                )
                if receipt is not None
                else await task_service.claimed_task_in_session(
                    session, owner_id, task_id, claim_token
                )
            )
            current = await self._job(session, owner_id, job_id, lock=True)
            if current is None:
                raise JobServiceError("RESOURCE_NOT_FOUND", "Job not found", 404)
            self._require_parse_graph(current, claimed_task)
            if claimed_task.status != "cancelled":
                current_mode, current_hash, current_payload = await self._parse_input(
                    session, claimed_task, current
                )
                if current_payload.jd_text != current.raw_encrypted:
                    await self._fail_parse_in_session(
                        session,
                        current,
                        claimed_task,
                        task_service,
                        "JD_PARSE_SOURCE_CHANGED",
                        release_unused_ai_reservation=True,
                    )
                    return current.id
                if (
                    current_mode != generation_mode
                    or current_hash != input_hash
                    or current_payload != payload
                ):
                    raise JobServiceError(
                        "JD_PARSE_INPUT_CHANGED",
                        "Saved job parsing input no longer matches the task",
                        409,
                    )
            if current.status == "parsed":
                await task_service.complete_task_in_session(
                    session,
                    claimed_task,
                    current.id,
                )
                return current.id
            ai_run_id = None
            if receipt is not None:
                if (
                    receipt.run.task_id != task_id
                    or receipt.run.trace_id != request_trace_id
                    or receipt.run.input_hash != input_hash
                    or receipt.run.workflow_type != "parse_jd"
                    or receipt.run.workflow_version != "2"
                ):
                    raise JobServiceError(
                        "AI_RECEIPT_MISMATCH",
                        "AI receipt does not match the immutable job input",
                        502,
                    )
                invalid = False
                requirements = []
                if receipt.run.status == "succeeded":
                    if isinstance(receipt.result, ParseJdResult):
                        requirements, invalid = self._validated_model_requirements(
                            receipt.result, payload.jd_text
                        )
                    else:
                        invalid = True
                current.status = "processing"
                await session.flush()
                ai_run = await self.ai_runs.persist_in_session(
                    session,
                    current.owner_user_id,
                    receipt,
                    workflow_stage="parse",
                    result_ref=current.id,
                )
                ai_run_id = ai_run.id
                await task_service.consume_ai_reservation_in_session(
                    session, current.owner_user_id, claimed_task.id, ai_run.id
                )
                if claimed_task.active_ai_run_id == ai_run.id:
                    await task_service.settle_ai_run_in_session(
                        session, current.owner_user_id, claimed_task.id, ai_run.id
                    )
                if receipt.run.status != "succeeded" or invalid or not requirements:
                    if receipt.run.status != "succeeded":
                        failure_code = (
                            receipt.run.error_code or f"ai_{receipt.run.status}"
                        )
                    elif invalid:
                        failure_code = "JD_REQUIREMENT_SOURCE_INVALID"
                    else:
                        failure_code = "JD_REQUIREMENTS_EMPTY"
                    await self._fail_parse_in_session(
                        session,
                        current,
                        claimed_task,
                        task_service,
                        failure_code,
                    )
                    return current.id
            elif not requirements:
                await self._fail_parse_in_session(
                    session,
                    current,
                    claimed_task,
                    task_service,
                    "JD_REQUIREMENTS_EMPTY",
                )
                return current.id
            await session.execute(
                delete(JdRequirement).where(
                    JdRequirement.job_id == current.id,
                    JdRequirement.owner_user_id == current.owner_user_id,
                )
            )
            rows = [
                JdRequirement(
                    id=new_id("req"),
                    owner_user_id=current.owner_user_id,
                    job_id=current.id,
                    type=item["type"],
                    priority=item["priority"],
                    text_encrypted=item["text"],
                    confirmed=False,
                    source_start=item["source_start"],
                    source_end=item["source_end"],
                    source_hash=item["source_hash"],
                    explicitness=item["explicitness"],
                    confidence_band=item["confidence_band"],
                    generation_mode=generation_mode,
                    workflow_version="2",
                    ai_run_id=ai_run_id,
                    input_hash=input_hash,
                )
                for item in requirements
            ]
            session.add_all(rows)
            current.status = "parsed"
            await task_service.complete_task_in_session(
                session,
                claimed_task,
                current.id,
            )
            await session.flush()
            return current.id

    async def update_requirement(
        self,
        owner_id: str,
        job_id: str,
        requirement_id: str,
        values: dict[str, Any],
        idempotency_key: str,
    ) -> JdRequirement:
        route = f"/v1/jobs/{job_id}/requirements/{requirement_id}"
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, values
                )
            except IdempotencyConflict as error:
                raise JobServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                row = await session.scalar(
                    select(JdRequirement).where(
                        JdRequirement.id == requirement_id,
                        JdRequirement.owner_user_id == owner,
                    )
                )
                if row is None:
                    raise RuntimeError("Idempotent requirement response is missing")
                return row
            owners = await authorized_owner_ids(session, owner_id)
            row = await session.scalar(
                select(JdRequirement)
                .where(
                    JdRequirement.id == requirement_id,
                    JdRequirement.job_id == job_id,
                    JdRequirement.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if row is None:
                raise JobServiceError(
                    "RESOURCE_NOT_FOUND", "Requirement not found", 404
                )
            for key, value in values.items():
                if value is not None:
                    setattr(
                        row,
                        "text_encrypted" if key == "text" else key,
                        value,
                    )
            await session.flush()
            await self.idempotency.complete(
                session, claim, 200, {"id": row.id, "confirmed": row.confirmed}
            )
            return row

    async def get(self, owner_id: str, job_id: str) -> JobDescription | None:
        async with self.sessions() as session:
            return await self._job(session, owner_id, job_id)

    async def get_with_requirements(
        self,
        owner_id: str,
        job_id: str,
    ) -> tuple[JobDescription, list[JdRequirement]] | None:
        async with self.sessions() as session:
            row = await self._job(session, owner_id, job_id)
            if row is None:
                return None
            return row, await self._requirements(session, row)

    async def _job(
        self,
        session: AsyncSession,
        owner_id: str,
        job_id: str,
        *,
        lock: bool = False,
    ) -> JobDescription | None:
        owners = await authorized_owner_ids(session, owner_id)
        query = select(JobDescription).where(
            JobDescription.id == job_id,
            JobDescription.owner_user_id.in_(owners),
        )
        return await session.scalar(query.with_for_update() if lock else query)

    @staticmethod
    async def _requirements(
        session: AsyncSession,
        job: JobDescription,
    ) -> list[JdRequirement]:
        return list(
            (
                await session.scalars(
                    select(JdRequirement)
                    .where(
                        JdRequirement.job_id == job.id,
                        JdRequirement.owner_user_id == job.owner_user_id,
                    )
                    .order_by(JdRequirement.priority, JdRequirement.id)
                )
            ).all()
        )

    @staticmethod
    def _parse_lines(raw: str) -> list[dict[str, Any]]:
        lines: list[tuple[str, int, int]] = []
        offset = 0
        for raw_line in raw.splitlines(keepends=True):
            content = raw_line.rstrip("\r\n")
            value = re.sub(r"^[\s•\-*\d.)、]+", "", content).strip()
            if len(value) >= 2:
                local_start = content.find(value)
                lines.append(
                    (value, offset + local_start, offset + local_start + len(value))
                )
            offset += len(raw_line)
        if not lines and raw.strip():
            value = raw.strip()
            start = raw.find(value)
            lines = [(value, start, start + len(value))]
        return [
            {
                "text": line,
                "type": (
                    "must_have"
                    if re.search(r"必须|要求|熟练|精通|required|must", line, re.I)
                    else "preferred"
                ),
                "priority": index + 1,
                "source_start": start,
                "source_end": end,
                "source_hash": hashlib.sha256(line.encode()).hexdigest(),
                "explicitness": "explicit",
                "confidence_band": "high",
            }
            for index, (line, start, end) in enumerate(lines[:100])
        ]

    @staticmethod
    def _validated_model_requirements(
        result: ParseJdResult,
        raw: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        if len(result.requirements) > 100:
            return [], True
        rows: list[dict[str, Any]] = []
        type_map = {
            "responsibility": "responsibility",
            "must_have": "must_have",
            "nice_to_have": "preferred",
            "implicit_capability": "other",
        }
        for item in result.requirements[:100]:
            start = item.source_range.start
            end = item.source_range.end
            if start < 0 or end <= start or end > len(raw) or raw[start:end] != item.value:
                return [], True
            rows.append(
                {
                    "text": item.value,
                    "type": type_map[item.category],
                    "priority": item.priority,
                    "source_start": start,
                    "source_end": end,
                    "source_hash": hashlib.sha256(raw[start:end].encode()).hexdigest(),
                    "explicitness": item.explicitness,
                    "confidence_band": item.confidence_band,
                }
            )
        return rows, False

    @staticmethod
    def _build_parse_snapshot(job: JobDescription) -> dict[str, Any]:
        payload = ParseJdPayload(
            jd_text=job.raw_encrypted,
            job_title=job.title,
            allowed_categories=(
                "responsibility",
                "must_have",
                "nice_to_have",
                "implicit_capability",
            ),
        )
        return {
            "workflow_type": "parse_jd",
            "workflow_version": "2",
            "prompt_template_version": "jd-parse@2",
            "locale": "zh-CN",
            "input_version": 1,
            "payload": payload.model_dump(mode="json"),
        }

    async def _parse_input(
        self,
        session: AsyncSession,
        task: Task,
        job: JobDescription,
    ) -> tuple[str, str, ParseJdPayload]:
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.task_id == task.id,
                Outbox.owner_user_id == task.owner_user_id,
            )
        )
        task_payload = outbox.payload if outbox is not None else None
        snapshot = (
            task_payload.get("parse_snapshot")
            if isinstance(task_payload, dict)
            else None
        )
        try:
            payload = ParseJdPayload.model_validate(
                snapshot.get("payload") if isinstance(snapshot, dict) else None,
                strict=False,
            )
        except Exception as error:
            raise JobServiceError(
                "JD_PARSE_SNAPSHOT_INVALID",
                "Saved job parsing input is invalid",
                409,
            ) from error
        generation_mode = (
            task_payload.get("generation_mode")
            if isinstance(task_payload, dict)
            else None
        )
        input_hash = (
            task_payload.get("parse_input_hash")
            if isinstance(task_payload, dict)
            else None
        )
        valid = (
            outbox is not None
            and isinstance(task_payload, dict)
            and task_payload.get("task_id") == task.id
            and task_payload.get("job_id") == job.id
            and generation_mode in {"model", "rule_fallback"}
            and isinstance(input_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", input_hash) is not None
            and isinstance(snapshot, dict)
            and set(snapshot)
            == {
                "workflow_type",
                "workflow_version",
                "prompt_template_version",
                "locale",
                "input_version",
                "payload",
            }
            and snapshot.get("workflow_type") == "parse_jd"
            and snapshot.get("workflow_version") == "2"
            and snapshot.get("prompt_template_version") == "jd-parse@2"
            and snapshot.get("locale") == "zh-CN"
            and snapshot.get("input_version") == 1
            and _semantic_hash(snapshot) == input_hash
        )
        if not valid:
            raise JobServiceError(
                "JD_PARSE_SNAPSHOT_INVALID",
                "Saved job parsing input is invalid",
                409,
            )
        return generation_mode, input_hash, payload

    @staticmethod
    def _require_parse_graph(job: JobDescription | None, task: Task) -> None:
        if (
            job is None
            or task.type != "parse_job"
            or task.resource_type != "job_description"
            or task.resource_id != job.id
            or job.task_id != task.id
            or job.owner_user_id != task.owner_user_id
        ):
            raise JobServiceError("RESOURCE_NOT_FOUND", "Job not found", 404)

    @staticmethod
    async def _fail_parse_in_session(
        session: AsyncSession,
        job: JobDescription,
        task: Task | None,
        task_service: TaskService | None,
        error_code: str,
        *,
        release_unused_ai_reservation: bool = False,
    ) -> None:
        await session.execute(
            delete(JdRequirement).where(
                JdRequirement.job_id == job.id,
                JdRequirement.owner_user_id == job.owner_user_id,
            )
        )
        job.status = "draft" if task is not None and task.status == "cancelled" else "failed"
        if task is None or task_service is None or task.status == "cancelled":
            await session.flush()
            return
        if task.active_ai_run_id is not None:
            await task_service.settle_ai_run_in_session(
                session,
                task.owner_user_id,
                task.id,
                task.active_ai_run_id,
            )
        await task_service.fail_task_in_session(
            session,
            task,
            error_code,
            release_unused_ai_reservation=release_unused_ai_reservation,
        )

    async def fail_parse(
        self,
        owner_id: str,
        job_id: str,
        task_id: str,
        claim_token: str,
        error_code: str,
        *,
        task_service: TaskService,
    ) -> None:
        async with self.sessions.begin() as session:
            task = await task_service.claimed_task_in_session(
                session, owner_id, task_id, claim_token
            )
            job = await self._job(session, owner_id, job_id, lock=True)
            self._require_parse_graph(job, task)
            await self._fail_parse_in_session(
                session,
                job,
                task,
                task_service,
                error_code,
                release_unused_ai_reservation=True,
            )


def _semantic_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
