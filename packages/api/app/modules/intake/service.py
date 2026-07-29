from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import (
    BulletFactLink,
    Fact,
    FactSource,
    IntakeAnswer,
    IntakeSession,
    Resume,
    ResumeVersion,
    SourceRecord,
    VersionOperation,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.resumes.service import canonical_snapshot
from app.modules.tasks.service import TaskAdmission, TaskService, TaskServiceError


INITIAL_QUESTION = {
    "id": "experience_radar",
    "type": "deep_answer",
    "prompt": "最近一年，你完成过什么需要持续投入的任务？",
    "reason": None,
}

QUESTIONS = {
    "course_role": {
        "id": "course_role",
        "type": "short_answer",
        "prompt": "在这项课程经历中，你具体负责什么？",
        "reason": "ambiguous_role",
    },
    "work_role": {
        "id": "work_role",
        "type": "short_answer",
        "prompt": "在这段实习或兼职中，你承担了什么职责？",
        "reason": "ambiguous_role",
    },
    "community_role": {
        "id": "community_role",
        "type": "short_answer",
        "prompt": "在社团或志愿经历中，你具体推动了哪部分工作？",
        "reason": "ambiguous_role",
    },
    "project_role": {
        "id": "project_role",
        "type": "short_answer",
        "prompt": "在这项任务中，你亲自完成了哪部分工作？",
        "reason": "ambiguous_role",
    },
    "course_probe": {
        "id": "course_probe",
        "type": "deep_answer",
        "prompt": "课程、社团、兼职或志愿服务中，有没有一件你投入较久的事？",
        "reason": None,
    },
    "work_probe": {
        "id": "work_probe",
        "type": "deep_answer",
        "prompt": "你是否参加过实习、兼职，或为他人解决过一个实际问题？",
        "reason": None,
    },
    "community_probe": {
        "id": "community_probe",
        "type": "deep_answer",
        "prompt": "你在社团、班级或志愿活动中承担过什么具体任务？",
        "reason": None,
    },
    "project_probe": {
        "id": "project_probe",
        "type": "deep_answer",
        "prompt": "有没有一个个人项目、比赛或作品能代表你的投入？",
        "reason": None,
    },
    "skill_probe": {
        "id": "skill_probe",
        "type": "deep_answer",
        "prompt": "你最近学会并实际使用过哪项工具或技能？",
        "reason": None,
    },
    "achievement_probe": {
        "id": "achievement_probe",
        "type": "deep_answer",
        "prompt": "有没有一次被认可、获奖或明显进步的经历？",
        "reason": None,
    },
    "experience_challenge": {
        "id": "experience_challenge",
        "type": "deep_answer",
        "prompt": "这项经历中最难解决的问题是什么？",
        "reason": None,
    },
    "experience_action": {
        "id": "experience_action",
        "type": "deep_answer",
        "prompt": "为解决这个问题，你采取了哪些具体行动？",
        "reason": "ambiguous_role",
    },
    "experience_collaboration": {
        "id": "experience_collaboration",
        "type": "short_answer",
        "prompt": "你和谁协作，你本人负责的边界是什么？",
        "reason": "ambiguous_role",
    },
    "experience_result": {
        "id": "experience_result",
        "type": "deep_answer",
        "prompt": "这项经历最后产生了什么可验证的结果？",
        "reason": "missing_unit",
    },
    "experience_learning": {
        "id": "experience_learning",
        "type": "short_answer",
        "prompt": "这项经历最能证明你的哪项能力？",
        "reason": None,
    },
    "experience_next": {
        "id": "experience_next",
        "type": "deep_answer",
        "prompt": "除此之外，还有哪段经历最能说明你的能力？",
        "reason": None,
    },
}

NEGATIVE_ANSWERS = frozenset({"没有", "不知道", "不清楚", "无", "跳过"})


@dataclass
class IntakeError(Exception):
    code: str
    message: str
    status_code: int


@dataclass(frozen=True)
class SavedIntake:
    response: dict[str, Any]
    status_code: int


class IntakeService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.idempotency = IdempotencyService()

    async def start(
        self,
        owner_id: str,
        *,
        restart: bool,
        idempotency_key: str,
    ) -> SavedIntake:
        body = {"restart": restart}
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            claim = await self._claim(
                session,
                owner,
                "/v1/intake-sessions",
                idempotency_key,
                body,
            )
            if claim.is_replay:
                return SavedIntake(
                    claim.replay_response or {},
                    claim.replay_status or 200,
                )
            active = await session.scalar(
                select(IntakeSession)
                .where(
                    IntakeSession.owner_user_id == owner,
                    IntakeSession.active_owner_key == owner,
                    IntakeSession.status.in_(("active", "drafting")),
                )
                .with_for_update()
            )
            if active is not None and not restart:
                response = await self._response(session, active)
                await self.idempotency.complete(session, claim, 200, response)
                return SavedIntake(response, 200)
            if active is not None and active.status == "drafting":
                raise IntakeError(
                    "INTAKE_DRAFT_IN_PROGRESS",
                    "Wait for or cancel the current draft task before restarting",
                    409,
                )
            if active is not None:
                active.status = "abandoned"
                active.active_owner_key = None
                active.updated_at = datetime.now(timezone.utc)
            row = IntakeSession(
                id=new_id("intake"),
                owner_user_id=owner,
                status="active",
                active_owner_key=owner,
                version=0,
                current_question=INITIAL_QUESTION,
                answered_question_ids=[],
                skipped_question_ids=[],
                fact_ids=[],
            )
            session.add(row)
            await session.flush()
            response = await self._response(session, row)
            await self.idempotency.complete(session, claim, 201, response)
            return SavedIntake(response, 201)

    async def get(self, owner_id: str, session_id: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await self._session(session, owner_id, session_id)
            return await self._response(session, row) if row is not None else None

    async def answer(
        self,
        owner_id: str,
        session_id: str,
        values: dict[str, Any],
        idempotency_key: str,
    ) -> SavedIntake:
        route = f"/v1/intake-sessions/{session_id}/answers"
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            claim = await self._claim(
                session,
                owner,
                route,
                idempotency_key,
                values,
            )
            if claim.is_replay:
                return SavedIntake(
                    claim.replay_response or {},
                    claim.replay_status or 200,
                )
            row = await self._session(
                session,
                owner_id,
                session_id,
                lock=True,
            )
            if row is None:
                raise IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404)
            if row.status != "active":
                raise IntakeError(
                    "INTAKE_NOT_ACTIVE",
                    "Intake session is not accepting answers",
                    409,
                )
            if row.version != values["base_version"]:
                raise IntakeError(
                    "INTAKE_VERSION_CONFLICT",
                    "Intake session has changed",
                    409,
                )
            question = row.current_question
            if question is None or question["id"] != values["question_id"]:
                raise IntakeError(
                    "INTAKE_QUESTION_CONFLICT",
                    "Question is no longer current",
                    409,
                )
            answer = (values.get("answer") or "").strip()
            state = _answer_state(answer, values["skipped"])
            fact_id = None
            if state == "answered":
                fact_id = await self._create_fact(
                    session,
                    row,
                    question_id=values["question_id"],
                    answer=answer,
                )
            session.add(
                IntakeAnswer(
                    id=new_id("ians"),
                    owner_user_id=row.owner_user_id,
                    session_id=row.id,
                    question_id=values["question_id"],
                    answer_encrypted=answer or None,
                    state=state,
                    fact_id=fact_id,
                )
            )
            row.answered_question_ids = [
                *row.answered_question_ids,
                values["question_id"],
            ]
            if state == "skipped":
                row.skipped_question_ids = [
                    *row.skipped_question_ids,
                    values["question_id"],
                ]
            if fact_id is not None:
                row.fact_ids = [*row.fact_ids, fact_id]
            row.current_question = _next_question(
                values["question_id"],
                answer,
                state,
            )
            row.version += 1
            row.updated_at = datetime.now(timezone.utc)
            await session.flush()
            response = await self._response(session, row)
            await self.idempotency.complete(session, claim, 200, response)
            return SavedIntake(response, 200)

    async def queue_draft(
        self,
        owner_id: str,
        session_id: str,
        values: dict[str, Any],
        idempotency_key: str,
        *,
        trace_id: str,
        task_service: TaskService,
    ) -> dict[str, Any]:
        route = f"/v1/intake-sessions/{session_id}/drafts"
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            claim = await self._claim(
                session,
                owner,
                route,
                idempotency_key,
                values,
            )
            if claim.is_replay:
                return claim.replay_response or {}
            row = await self._session(
                session,
                owner_id,
                session_id,
                lock=True,
            )
            if row is None:
                raise IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404)
            if row.status != "active":
                raise IntakeError(
                    "INTAKE_NOT_ACTIVE",
                    "Intake session is not ready for a draft",
                    409,
                )
            if row.version != values["base_version"]:
                raise IntakeError(
                    "INTAKE_VERSION_CONFLICT",
                    "Intake session has changed",
                    409,
                )
            ready_facts = await self._ready_facts(session, row)
            if len(ready_facts) < 2:
                raise IntakeError(
                    "INTAKE_FACTS_NOT_READY",
                    "Confirm at least two sourced facts before generating a draft",
                    422,
                )
            try:
                task = await task_service.create_task_in_session(
                    session,
                    owner,
                    task_type="generate_intake_draft",
                    queue="ai.interactive",
                    trace_id=trace_id,
                    idempotency_key=f"intake-draft:{idempotency_key}",
                    admission=TaskAdmission.ai(),
                    resource_type="intake_session",
                    resource_id=row.id,
                    payload={"intake_session_id": row.id},
                )
            except TaskServiceError as error:
                raise IntakeError(error.code, error.message, error.status_code) from error
            row.status = "drafting"
            row.draft_title = values["title"]
            row.task_id = task.id
            row.version += 1
            row.updated_at = datetime.now(timezone.utc)
            response = {
                "session_id": row.id,
                "task_id": task.id,
                "status": "queued",
                "version": row.version,
            }
            await self.idempotency.complete(session, claim, 202, response)
            return response

    async def process_draft(
        self,
        owner_id: str,
        session_id: str,
        *,
        task_id: str,
        claim_token: str,
        task_service: TaskService,
    ) -> str:
        async with self.sessions.begin() as session:
            task = await task_service.claimed_task_in_session(
                session,
                owner_id,
                task_id,
                claim_token,
            )
            row = await self._session(
                session,
                owner_id,
                session_id,
                lock=True,
            )
            if row is None or row.task_id != task.id:
                raise IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404)
            if row.resume_id is not None:
                await task_service.complete_task_in_session(
                    session,
                    task,
                    row.resume_id,
                )
                return row.resume_id
            ready_facts = await self._ready_facts(session, row)
            if len(ready_facts) < 2:
                raise IntakeError(
                    "INTAKE_FACTS_NOT_READY",
                    "Confirmed facts changed before draft generation",
                    422,
                )
            resume_id = new_id("resume")
            version_id = new_id("rver")
            items: list[dict[str, Any]] = []
            evidence: list[tuple[str, Fact, tuple[str, ...]]] = []
            for fact, source_hashes in ready_facts:
                bullet_id = new_id("bullet")
                items.append(
                    {
                        "id": bullet_id,
                        "text": fact.value_encrypted,
                        "fact_refs": [fact.id],
                    }
                )
                evidence.append((bullet_id, fact, source_hashes))
            snapshot, snapshot_hash = canonical_snapshot(
                {
                    "schema_version": "1",
                    "title": row.draft_title or "基础简历",
                    "target": None,
                    "sections": [
                        {
                            "id": new_id("section"),
                            "type": "experience",
                            "title": "经历",
                            "items": items,
                        }
                    ],
                }
            )
            resume = Resume(
                id=resume_id,
                owner_user_id=row.owner_user_id,
                kind="base",
                title=row.draft_title or "基础简历",
                head_version=0,
                head_version_id=None,
            )
            session.add(resume)
            await session.flush()
            version = ResumeVersion(
                id=version_id,
                owner_user_id=row.owner_user_id,
                resume_id=resume_id,
                parent_version_id=None,
                snapshot_json=snapshot,
                snapshot_hash=snapshot_hash,
                created_by=row.owner_user_id,
            )
            session.add(version)
            await session.flush()
            resume.head_version = 1
            resume.head_version_id = version_id
            session.add(
                VersionOperation(
                    id=new_id("vop"),
                    owner_user_id=row.owner_user_id,
                    version_id=version_id,
                    operation_type="save",
                    actor=row.owner_user_id,
                    metadata_json={"source": "intake_session", "session_id": row.id},
                )
            )
            for bullet_id, fact, source_hashes in evidence:
                session.add(
                    BulletFactLink(
                        resume_version_id=version_id,
                        bullet_id=bullet_id,
                        fact_id=fact.id,
                        fact_owner_user_id=fact.owner_user_id,
                        owner_user_id=row.owner_user_id,
                        claim_start=0,
                        claim_end=len(fact.value_encrypted),
                        claim_range={
                            "start": 0,
                            "end": len(fact.value_encrypted),
                        },
                        fact_value_encrypted_at_link=fact.value_encrypted,
                        fact_status_at_link=fact.status,
                        fact_source_hashes_at_link=list(source_hashes),
                    )
                )
            row.resume_id = resume_id
            row.status = "completed"
            row.active_owner_key = None
            row.current_question = None
            row.updated_at = datetime.now(timezone.utc)
            await task_service.complete_task_in_session(
                session,
                task,
                resume_id,
            )
            await session.flush()
            return resume_id

    async def _claim(
        self,
        session: AsyncSession,
        owner: str,
        route: str,
        key: str,
        body: dict[str, Any],
    ):
        try:
            return await self.idempotency.claim(
                session,
                owner,
                route,
                key,
                body,
            )
        except IdempotencyConflict as error:
            raise IntakeError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was reused with a different request",
                409,
            ) from error

    async def _session(
        self,
        session: AsyncSession,
        owner_id: str,
        session_id: str,
        *,
        lock: bool = False,
    ) -> IntakeSession | None:
        owners = await authorized_owner_ids(session, owner_id)
        query = select(IntakeSession).where(
            IntakeSession.id == session_id,
            IntakeSession.owner_user_id.in_(owners),
        )
        return await session.scalar(query.with_for_update() if lock else query)

    async def _create_fact(
        self,
        session: AsyncSession,
        row: IntakeSession,
        *,
        question_id: str,
        answer: str,
    ) -> str:
        source = SourceRecord(
            id=new_id("src"),
            owner_user_id=row.owner_user_id,
            source_type="question_answer",
            source_ref=f"intake:{row.id}:{question_id}",
            content_encrypted=answer,
        )
        fact = Fact(
            id=new_id("fact"),
            owner_user_id=row.owner_user_id,
            kind=_fact_kind(question_id),
            value_encrypted=answer,
            status="unconfirmed",
        )
        session.add_all((source, fact))
        await session.flush()
        session.add(
            FactSource(
                fact_id=fact.id,
                source_record_id=source.id,
                owner_user_id=row.owner_user_id,
                source_range={"start": 0, "end": len(answer)},
                source_hash=hashlib.sha256(answer.encode()).hexdigest(),
            )
        )
        await session.flush()
        return fact.id

    async def _ready_facts(
        self,
        session: AsyncSession,
        row: IntakeSession,
    ) -> list[tuple[Fact, tuple[str, ...]]]:
        if not row.fact_ids:
            return []
        facts = list(
            (
                await session.scalars(
                    select(Fact).where(
                        Fact.id.in_(row.fact_ids),
                        Fact.owner_user_id == row.owner_user_id,
                        Fact.status == "confirmed",
                    )
                )
            ).all()
        )
        ready: list[tuple[Fact, tuple[str, ...]]] = []
        for fact in facts:
            hashes = tuple(
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
            if hashes:
                ready.append((fact, hashes))
        return ready

    async def _response(
        self,
        session: AsyncSession,
        row: IntakeSession,
    ) -> dict[str, Any]:
        facts: list[Fact] = []
        if row.fact_ids:
            facts = list(
                (
                    await session.scalars(
                        select(Fact).where(
                            Fact.id.in_(row.fact_ids),
                            Fact.owner_user_id == row.owner_user_id,
                        )
                    )
                ).all()
            )
        facts_by_id = {fact.id: fact for fact in facts}
        return {
            "id": row.id,
            "status": row.status,
            "version": row.version,
            "current_question": row.current_question,
            "completed_count": len(row.answered_question_ids),
            "remaining_estimate": max(0, 8 - len(row.answered_question_ids)),
            "answered_question_ids": list(row.answered_question_ids),
            "skipped_question_ids": list(row.skipped_question_ids),
            "fact_summaries": [
                {
                    "id": fact.id,
                    "kind": fact.kind,
                    "value": fact.value_encrypted,
                    "status": fact.status,
                }
                for fact_id in row.fact_ids
                if (fact := facts_by_id.get(fact_id)) is not None
            ],
            "task_id": row.task_id,
            "resume_id": row.resume_id,
        }


def _answer_state(answer: str, skipped: bool) -> str:
    if skipped:
        return "skipped"
    normalized = re.sub(r"[\s，。！？,.!?；;：:]+", "", answer)
    if normalized in NEGATIVE_ANSWERS or re.fullmatch(
        r"(?:暂时)?(?:没有|无)(?:相关|类似|这方面)?(?:经历|经验|项目|任务|内容)?(?:了)?",
        normalized,
    ):
        return "negative"
    return "answered"


def _fact_kind(question_id: str) -> str:
    if question_id.endswith("_role"):
        return "role"
    if question_id == "experience_result":
        return "result"
    return "experience"


def _next_question(
    question_id: str,
    answer: str,
    state: str,
) -> dict[str, Any] | None:
    if question_id == "experience_radar":
        if state != "answered":
            return QUESTIONS["course_probe"]
        return _role_question(answer)
    if question_id in {
        "course_probe",
        "work_probe",
        "community_probe",
        "project_probe",
        "skill_probe",
        "achievement_probe",
    }:
        if state == "answered":
            return _role_question(answer)
        fallback = {
            "course_probe": "work_probe",
            "work_probe": "community_probe",
            "community_probe": "project_probe",
            "project_probe": "skill_probe",
            "skill_probe": "achievement_probe",
            "achievement_probe": "experience_next",
        }
        return QUESTIONS[fallback[question_id]]
    if question_id in {
        "course_role",
        "work_role",
        "community_role",
        "project_role",
    }:
        return QUESTIONS["experience_challenge"]
    if question_id == "experience_challenge":
        return QUESTIONS["experience_action"]
    if question_id == "experience_action":
        return QUESTIONS["experience_collaboration"]
    if question_id == "experience_collaboration":
        return QUESTIONS["experience_result"]
    if question_id == "experience_result":
        return QUESTIONS["experience_learning"]
    if question_id == "experience_learning":
        return QUESTIONS["experience_next"]
    return None


def _role_question(answer: str) -> dict[str, Any]:
    if "课程" in answer:
        return QUESTIONS["course_role"]
    if "实习" in answer or "兼职" in answer:
        return QUESTIONS["work_role"]
    if "社团" in answer or "志愿" in answer or "班级" in answer:
        return QUESTIONS["community_role"]
    return QUESTIONS["project_role"]
