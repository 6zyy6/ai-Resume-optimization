from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import JdRequirement, JobDescription
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.ai_client import AiClient
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
            current.status = "queued"
            task = await task_service.create_task_in_session(
                session,
                owner,
                task_type="parse_job",
                queue="ai.interactive",
                trace_id=trace_id,
                idempotency_key=f"job-parse:{idempotency_key}",
                admission=TaskAdmission.ai(),
                resource_type="job_description",
                resource_id=current.id,
                payload={"job_id": current.id},
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
    ) -> str:
        job = await self.get(owner_id, job_id)
        if job is None:
            raise JobServiceError("RESOURCE_NOT_FOUND", "Job not found", 404)
        if job.status == "parsed":
            return job.id
        requirements = self._parse_lines(job.raw_encrypted)
        if self.ai_client is not None:
            result = await self.ai_client.run(
                workflow_type="parse_jd",
                workflow_version="1",
                trace_id=trace_id,
                task_id=task_id,
                facts=[],
                input_data={"raw": job.raw_encrypted},
            )
            candidate = result.get("result", result).get("requirements")
            if isinstance(candidate, list) and candidate:
                requirements = self._validate_ai_requirements(candidate)
        async with self.sessions.begin() as session:
            current = await self._job(session, owner_id, job_id, lock=True)
            if current is None:
                raise JobServiceError("RESOURCE_NOT_FOUND", "Job not found", 404)
            if current.status == "parsed":
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
                )
                for item in requirements
            ]
            session.add_all(rows)
            current.status = "parsed"
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
        lines = [
            re.sub(r"^[\s•\-*\d.)、]+", "", line).strip()
            for line in raw.splitlines()
        ]
        lines = [line for line in lines if len(line) >= 2]
        if not lines:
            lines = [raw.strip()]
        return [
            {
                "text": line,
                "type": (
                    "must_have"
                    if re.search(r"必须|要求|熟练|精通|required|must", line, re.I)
                    else "preferred"
                ),
                "priority": index + 1,
            }
            for index, line in enumerate(lines[:100])
        ]

    @staticmethod
    def _validate_ai_requirements(values: list[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        type_map = {
            "responsibility": "responsibility",
            "must_have": "must_have",
            "nice_to_have": "preferred",
            "implicit_capability": "other",
        }
        for index, item in enumerate(values[:100]):
            text = (
                item.get("text", item.get("value"))
                if isinstance(item, dict)
                else None
            )
            if not isinstance(text, str) or not text:
                raise JobServiceError(
                    "AI_SCHEMA_INVALID", "AI job requirements are invalid", 502
                )
            item_type = item.get("type", item.get("category", "other"))
            rows.append(
                {
                    "text": text[:5000],
                    "type": (
                        type_map.get(item_type, item_type)
                        if type_map.get(item_type, item_type)
                        in {"must_have", "preferred", "responsibility", "other"}
                        else "other"
                    ),
                    "priority": int(item.get("priority", index + 1)),
                }
            )
        return rows
