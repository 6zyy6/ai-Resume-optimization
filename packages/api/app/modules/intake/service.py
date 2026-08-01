from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import (
    BulletFactLink,
    AiTraceEvent,
    Fact,
    FactCandidate,
    FactSource,
    IntakeAnswer,
    IntakeSession,
    Outbox,
    Resume,
    ResumeVersion,
    SourceRecord,
    Task,
    UsageLedger,
    VersionOperation,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.ai_client import (
    AiCancellation,
    AiClient,
    AnalyzeIntakeRequest,
    AnalyzeIntakeResult,
    AnalyzeIntakePayload,
    FactProjection,
)
from app.modules.ai_runs.service import AiRunService
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
NEGATIVE_CLAIM = re.compile(
    r"(?:(?:并|完全)?没有|没|从未|不曾|未曾|并未|未)(?:真正|能)?"
    r"(?:负责|参与|完成|承担|做过|参加)(?:过)?(?:这个|该|相关)?"
    r"(?:项目|任务|工作|实习|活动|课程)?"
)
MULTI_NEGATIVE_MARKERS = (
    "完全没有",
    "并没有",
    "没有",
    "从未",
    "不曾",
    "并未",
    "未能",
)
SINGLE_NEGATIVE_MARKERS = frozenset("没不未无")
NEGATION_CLAUSE_BOUNDARIES = ("但", "不过", "然而", "可是", "却")
NEGATION_MAX_GAP = 4
NUMBER_TOKEN = re.compile(
    r"(?<![\d.,])(?P<number>[+\-−]?(?:"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+))"
    r"(?P<percent>[%％]?)(?![\d.,])"
)
CHINESE_NUMBER = (
    r"[零〇一二两三四五六七八九十百千万亿点俩双半廿卅"
    r"壹贰叁肆伍陆柒捌玖拾佰仟萬億]+"
)
CHINESE_NUMBER_SUFFIX = (
    r"小时|分钟|个月|人|名|次|项|个|天|周|月|年|份|家|位|届|场|分|元|倍|成"
)
CHINESE_NUMBER_TOKEN = re.compile(
    rf"(?P<prefix>百分之|第|前|过)?(?P<number>{CHINESE_NUMBER})"
    rf"(?P<suffix>(?:余|多)(?:{CHINESE_NUMBER_SUFFIX})?"
    rf"|{CHINESE_NUMBER_SUFFIX}|[%％])?"
)
UNPARSED_QUANTITY = re.compile(
    rf"(?:百分之|第|前|过)(?:若干|好几|几|数)"
    rf"(?:{CHINESE_NUMBER_SUFFIX}|[%％])?"
    rf"|(?:若干|好几|几|数)(?:{CHINESE_NUMBER_SUFFIX}|[%％])"
)
CJK_CHARACTER = re.compile(r"[\u3400-\u9fff]")


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
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ai_client: AiClient | None = None,
    ) -> None:
        self.sessions = sessions
        self.ai_client = ai_client
        self.ai_runs = AiRunService()
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
            if active is not None and restart:
                live_analysis = await session.scalar(
                    select(IntakeAnswer.id).where(
                        IntakeAnswer.session_id == active.id,
                        IntakeAnswer.owner_user_id == active.owner_user_id,
                        IntakeAnswer.analysis_status.in_(
                            ("queued", "running", "waiting_for_confirmation")
                        ),
                    )
                )
                if live_analysis is not None:
                    raise IntakeError(
                        "INTAKE_ANALYSIS_IN_PROGRESS",
                        "Wait for or finish the current answer analysis before restarting",
                        409,
                    )
                failed_outboxes = (
                    await session.scalars(
                        select(Outbox)
                        .join(
                            IntakeAnswer,
                            and_(
                                IntakeAnswer.analysis_task_id == Outbox.task_id,
                                IntakeAnswer.owner_user_id
                                == Outbox.owner_user_id,
                            ),
                        )
                        .join(
                            Task,
                            and_(
                                Task.id == Outbox.task_id,
                                Task.owner_user_id == Outbox.owner_user_id,
                            ),
                        )
                        .where(
                            IntakeAnswer.session_id == active.id,
                            IntakeAnswer.owner_user_id == active.owner_user_id,
                            IntakeAnswer.analysis_status == "failed",
                            Task.type == "analyze_intake_answer",
                            Task.resource_type == "intake_answer",
                            Task.resource_id == IntakeAnswer.id,
                        )
                        .with_for_update()
                    )
                ).all()
                for failed_outbox in failed_outboxes:
                    _clear_analysis_snapshot(failed_outbox)
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
        *,
        trace_id: str,
        task_service: TaskService,
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
            pending_candidates = int(
                await session.scalar(
                    select(func.count())
                    .select_from(FactCandidate)
                    .join(
                        IntakeAnswer,
                        and_(
                            IntakeAnswer.id == FactCandidate.intake_answer_id,
                            IntakeAnswer.owner_user_id
                            == FactCandidate.owner_user_id,
                        ),
                    )
                    .where(
                        IntakeAnswer.session_id == row.id,
                        IntakeAnswer.owner_user_id == row.owner_user_id,
                        FactCandidate.owner_user_id == row.owner_user_id,
                        FactCandidate.status == "pending",
                    )
                )
                or 0
            )
            if pending_candidates:
                raise IntakeError(
                    "INTAKE_FACT_REVIEW_REQUIRED",
                    "Review all pending fact candidates before answering",
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
            answer_id = new_id("ians")
            answer_row = IntakeAnswer(
                id=answer_id,
                owner_user_id=row.owner_user_id,
                session_id=row.id,
                question_id=values["question_id"],
                answer_encrypted=answer or None,
                state=state,
                fact_id=None,
                analysis_status="queued" if state == "answered" else "idle",
                analysis_input_version=1 if state == "answered" else None,
                analysis_input_hash=None,
                next_question_source="rule" if state != "answered" else None,
            )
            session.add(answer_row)
            if state == "answered":
                analysis_snapshot = await self._build_analysis_snapshot(
                    session,
                    row,
                    answer_row,
                )
                answer_row.analysis_input_hash = _semantic_hash(analysis_snapshot)
                try:
                    task = await task_service.create_task_in_session(
                        session,
                        owner,
                        task_type="analyze_intake_answer",
                        queue="ai.interactive",
                        trace_id=trace_id,
                        idempotency_key=f"intake-answer-analysis:{answer_id}",
                        admission=TaskAdmission.ai(),
                        resource_type="intake_answer",
                        resource_id=answer_id,
                        payload={
                            "intake_session_id": row.id,
                            "intake_answer_id": answer_id,
                            "analysis_input_version": 1,
                            "analysis_input_hash": answer_row.analysis_input_hash,
                            "analysis_snapshot": analysis_snapshot,
                        },
                    )
                except TaskServiceError as error:
                    raise IntakeError(
                        error.code,
                        error.message,
                        error.status_code,
                    ) from error
                answer_row.analysis_task_id = task.id
            row.answered_question_ids = [
                *row.answered_question_ids,
                values["question_id"],
            ]
            if state == "skipped":
                row.skipped_question_ids = [
                    *row.skipped_question_ids,
                    values["question_id"],
                ]
            row.current_question = (
                None
                if state == "answered"
                else _next_question(values["question_id"], answer, state)
            )
            row.version += 1
            row.updated_at = datetime.now(timezone.utc)
            await session.flush()
            response = await self._response(session, row)
            status_code = 202 if state == "answered" else 200
            await self.idempotency.complete(session, claim, status_code, response)
            return SavedIntake(response, status_code)

    async def process_answer_analysis(
        self,
        owner_id: str,
        answer_id: str,
        *,
        task_id: str,
        claim_token: str,
        task_service: TaskService,
        cancellation: AiCancellation | None = None,
    ) -> str:
        if self.ai_client is None:
            raise RuntimeError("analyze_intake_answer requires an AI client")
        async with self.sessions.begin() as session:
            task = await task_service.claimed_task_in_session(
                session,
                owner_id,
                task_id,
                claim_token,
            )
            answer_row, intake = await self._analysis_resource(
                session,
                owner_id,
                answer_id,
                task_id,
                lock=True,
            )
            _require_analysis_graph(task, answer_row, intake)
            answer_row.analysis_status = "running"
            request = await self._analysis_request(session, task, answer_row, intake)

        try:
            receipt = await self.ai_client.run(request, cancellation)
        except Exception:
            async with self.sessions.begin() as session:
                answer_row, _ = await self._analysis_resource(
                    session,
                    owner_id,
                    answer_id,
                    task_id,
                    lock=True,
                )
                answer_row.analysis_status = "failed"
            raise

        async with self.sessions.begin() as session:
            task = await task_service.ai_receipt_task_in_session(
                session,
                owner_id,
                task_id,
                claim_token,
                receipt.run.ai_run_id,
                receipt.run.status,
            )
            answer_row, intake = await self._analysis_resource(
                session,
                owner_id,
                answer_id,
                task_id,
                lock=True,
            )
            _require_analysis_graph(task, answer_row, intake)
            if (
                receipt.run.task_id != task.id
                or receipt.run.trace_id != task.trace_id
                or receipt.run.input_hash != answer_row.analysis_input_hash
            ):
                raise IntakeError(
                    "AI_RECEIPT_MISMATCH",
                    "AI receipt does not match the immutable answer input",
                    502,
                )
            await session.execute(
                update(IntakeAnswer)
                .where(
                    IntakeAnswer.id == answer_row.id,
                    IntakeAnswer.owner_user_id == answer_row.owner_user_id,
                )
                .values(analysis_status=answer_row.analysis_status)
            )
            ai_run = await self.ai_runs.persist_in_session(
                session,
                answer_row.owner_user_id,
                receipt,
                workflow_stage="analysis",
                result_ref=answer_row.id,
            )
            await task_service.consume_ai_reservation_in_session(
                session,
                answer_row.owner_user_id,
                task.id,
                ai_run.id,
            )
            if task.active_ai_run_id == ai_run.id:
                await task_service.settle_ai_run_in_session(
                    session,
                    answer_row.owner_user_id,
                    task.id,
                    ai_run.id,
                )
            if receipt.run.status != "succeeded":
                answer_row.analysis_status = "failed"
                if task.status == "running":
                    await task_service.fail_task_in_session(
                        session,
                        task,
                        receipt.run.error_code or f"ai_{receipt.run.status}",
                    )
                return answer_row.id
            if not isinstance(receipt.result, AnalyzeIntakeResult):
                raise IntakeError(
                    "AI_SCHEMA_INVALID",
                    "AI answer analysis result is missing",
                    502,
                )
            valid, invalid = _validated_candidates(answer_row, receipt.result)
            existing_keys = set(
                (
                    await session.execute(
                        select(
                            FactCandidate.kind,
                            FactCandidate.value_encrypted,
                            FactCandidate.source_start,
                            FactCandidate.source_end,
                            FactCandidate.source_hash,
                        ).where(
                            FactCandidate.intake_answer_id == answer_row.id,
                            FactCandidate.owner_user_id == answer_row.owner_user_id,
                        )
                    )
                ).tuples()
            )
            for candidate in valid:
                key = _candidate_key(candidate)
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                session.add(
                    FactCandidate(
                        id=new_id("fc"),
                        owner_user_id=answer_row.owner_user_id,
                        intake_answer_id=answer_row.id,
                        kind=candidate.kind,
                        value_encrypted=candidate.value,
                        source_start=candidate.source_range.start,
                        source_end=candidate.source_range.end,
                        source_hash=candidate.source_hash,
                        status="pending",
                        decision_mode=(
                            "edit_only"
                            if "conflict" in candidate.risk_flags
                            else "accept_or_edit"
                        ),
                        ai_run_id=ai_run.id,
                    )
                )
            await self._append_candidate_validation_events(
                session,
                answer_row.owner_user_id,
                ai_run.id,
                invalid,
            )
            next_question, source = _analysis_next_question(
                answer_row,
                receipt.result,
            )
            intake.current_question = next_question
            intake.updated_at = datetime.now(timezone.utc)
            answer_row.next_question_source = source
            answer_row.analysis_status = (
                "waiting_for_confirmation" if valid else "completed"
            )
            outbox = await session.scalar(
                select(Outbox)
                .where(
                    Outbox.task_id == task.id,
                    Outbox.owner_user_id == task.owner_user_id,
                )
                .with_for_update()
            )
            _clear_analysis_snapshot(outbox)
            await task_service.complete_task_in_session(
                session,
                task,
                answer_row.id,
            )
            await session.flush()
            return answer_row.id

    async def mark_answer_analysis_failed(
        self,
        owner_id: str,
        answer_id: str,
        task_id: str,
    ) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(IntakeAnswer)
                .where(
                    IntakeAnswer.id == answer_id,
                    IntakeAnswer.owner_user_id == owner_id,
                    IntakeAnswer.analysis_task_id == task_id,
                    IntakeAnswer.analysis_status.in_(("queued", "running")),
                )
                .values(analysis_status="failed")
            )

    async def retry_analysis(
        self,
        owner_id: str,
        session_id: str,
        values: dict[str, Any],
        idempotency_key: str,
        *,
        trace_id: str,
        task_service: TaskService,
    ) -> SavedIntake:
        route = f"/v1/intake-sessions/{session_id}/analysis/retry"
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
                    claim.replay_status or 202,
                )
            intake = await self._session(session, owner_id, session_id, lock=True)
            if intake is None:
                raise IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404)
            if intake.version != values["base_version"]:
                raise IntakeError(
                    "INTAKE_VERSION_CONFLICT",
                    "Intake session has changed",
                    409,
                )
            answer_row = await self._latest_analysis_answer(
                session,
                intake,
                lock=True,
            )
            if answer_row is None or answer_row.analysis_status != "failed":
                raise IntakeError(
                    "INTAKE_ANALYSIS_NOT_FAILED",
                    "Only a failed answer analysis can be retried",
                    409,
                )
            old_task = await session.scalar(
                select(Task)
                .where(
                    Task.id == answer_row.analysis_task_id,
                    Task.owner_user_id == answer_row.owner_user_id,
                    Task.type == "analyze_intake_answer",
                    Task.resource_type == "intake_answer",
                    Task.resource_id == answer_row.id,
                    Task.status.in_(("failed", "cancelled")),
                )
                .with_for_update()
            )
            if old_task is None:
                raise IntakeError(
                    "INTAKE_ANALYSIS_GRAPH_INVALID",
                    "Failed answer analysis task is invalid",
                    409,
                )
            _require_saved_answer_input(answer_row)
            old_outbox = await session.scalar(
                select(Outbox).where(
                    Outbox.task_id == old_task.id,
                    Outbox.owner_user_id == old_task.owner_user_id,
                )
            )
            analysis_snapshot, _ = _validated_analysis_snapshot(
                old_task,
                answer_row,
                intake,
                old_outbox,
            )
            reservation = await session.scalar(
                select(UsageLedger)
                .where(
                    UsageLedger.task_id == old_task.id,
                    UsageLedger.owner_user_id == old_task.owner_user_id,
                    UsageLedger.usage_type == "ai_task",
                )
                .with_for_update()
            )
            if reservation is not None and reservation.state == "reserved":
                await task_service.release_ai_reservation_in_session(
                    session,
                    old_task.owner_user_id,
                    old_task.id,
                )
            try:
                task = await task_service.create_task_in_session(
                    session,
                    owner,
                    task_type="analyze_intake_answer",
                    queue="ai.interactive",
                    trace_id=trace_id,
                    idempotency_key=f"intake-answer-retry:{idempotency_key}",
                    admission=TaskAdmission.ai(is_retry=True),
                    resource_type="intake_answer",
                    resource_id=answer_row.id,
                    payload={
                        "intake_session_id": intake.id,
                        "intake_answer_id": answer_row.id,
                        "analysis_input_version": answer_row.analysis_input_version,
                        "analysis_input_hash": answer_row.analysis_input_hash,
                        "analysis_snapshot": analysis_snapshot,
                    },
                )
            except TaskServiceError as error:
                raise IntakeError(error.code, error.message, error.status_code) from error
            _clear_analysis_snapshot(old_outbox)
            answer_row.analysis_task_id = task.id
            answer_row.analysis_status = "queued"
            answer_row.next_question_source = None
            intake.current_question = None
            intake.version += 1
            intake.updated_at = datetime.now(timezone.utc)
            await session.flush()
            response = await self._response(session, intake)
            await self.idempotency.complete(session, claim, 202, response)
            return SavedIntake(response, 202)

    async def continue_analysis(
        self,
        owner_id: str,
        session_id: str,
        values: dict[str, Any],
        idempotency_key: str,
        *,
        task_service: TaskService,
    ) -> SavedIntake:
        route = f"/v1/intake-sessions/{session_id}/analysis/continue"
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
            intake = await self._session(session, owner_id, session_id, lock=True)
            if intake is None:
                raise IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404)
            if intake.version != values["base_version"]:
                raise IntakeError(
                    "INTAKE_VERSION_CONFLICT",
                    "Intake session has changed",
                    409,
                )
            answer_row = await self._latest_analysis_answer(
                session,
                intake,
                lock=True,
            )
            if answer_row is None or answer_row.analysis_status != "failed":
                raise IntakeError(
                    "INTAKE_ANALYSIS_NOT_FAILED",
                    "Only a failed answer analysis can use rule continuation",
                    409,
                )
            _require_saved_answer_input(answer_row)
            task = await session.scalar(
                select(Task)
                .where(
                    Task.id == answer_row.analysis_task_id,
                    Task.owner_user_id == answer_row.owner_user_id,
                    Task.type == "analyze_intake_answer",
                    Task.resource_type == "intake_answer",
                    Task.resource_id == answer_row.id,
                    Task.status.in_(("failed", "cancelled")),
                )
                .with_for_update()
            )
            if task is None:
                raise IntakeError(
                    "INTAKE_ANALYSIS_GRAPH_INVALID",
                    "Failed answer analysis task is invalid",
                    409,
                )
            reservation = await session.scalar(
                select(UsageLedger)
                .where(
                    UsageLedger.task_id == task.id,
                    UsageLedger.owner_user_id == task.owner_user_id,
                    UsageLedger.usage_type == "ai_task",
                )
                .with_for_update()
            )
            if reservation is not None and reservation.state == "reserved":
                await task_service.release_ai_reservation_in_session(
                    session,
                    task.owner_user_id,
                    task.id,
                )
            outbox = await session.scalar(
                select(Outbox)
                .where(
                    Outbox.task_id == task.id,
                    Outbox.owner_user_id == task.owner_user_id,
                )
                .with_for_update()
            )
            _clear_analysis_snapshot(outbox)
            intake.current_question = _next_question(
                answer_row.question_id,
                answer_row.answer_encrypted or "",
                "answered",
            )
            intake.version += 1
            intake.updated_at = datetime.now(timezone.utc)
            answer_row.analysis_status = "completed"
            answer_row.next_question_source = "fallback"
            await session.flush()
            response = await self._response(session, intake)
            await self.idempotency.complete(session, claim, 200, response)
            return SavedIntake(response, 200)

    async def _latest_analysis_answer(
        self,
        session: AsyncSession,
        intake: IntakeSession,
        *,
        lock: bool,
    ) -> IntakeAnswer | None:
        query = (
            select(IntakeAnswer)
            .where(
                IntakeAnswer.session_id == intake.id,
                IntakeAnswer.owner_user_id == intake.owner_user_id,
                IntakeAnswer.state == "answered",
            )
            .order_by(IntakeAnswer.created_at.desc(), IntakeAnswer.id.desc())
            .limit(1)
        )
        return await session.scalar(query.with_for_update() if lock else query)

    async def decide_candidate(
        self,
        owner_id: str,
        session_id: str,
        candidate_id: str,
        values: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        route = (
            f"/v1/intake-sessions/{session_id}/fact-candidates/"
            f"{candidate_id}/decision"
        )
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
            intake = await self._session(session, owner_id, session_id, lock=True)
            if intake is None:
                raise IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404)
            if intake.status != "active":
                raise IntakeError(
                    "INTAKE_NOT_ACTIVE",
                    "Intake session is not accepting candidate decisions",
                    409,
                )
            owners = await authorized_owner_ids(session, owner_id)
            candidate = await session.scalar(
                select(FactCandidate)
                .join(
                    IntakeAnswer,
                    and_(
                        IntakeAnswer.id == FactCandidate.intake_answer_id,
                        IntakeAnswer.owner_user_id == FactCandidate.owner_user_id,
                    ),
                )
                .where(
                    FactCandidate.id == candidate_id,
                    FactCandidate.owner_user_id.in_(owners),
                    IntakeAnswer.session_id == session_id,
                    IntakeAnswer.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if candidate is None:
                raise IntakeError("RESOURCE_NOT_FOUND", "Fact candidate not found", 404)
            if candidate.status != "pending":
                raise IntakeError(
                    "FACT_CANDIDATE_ALREADY_DECIDED",
                    "Fact candidate was already decided",
                    409,
                )
            if intake.version != values["base_version"]:
                raise IntakeError(
                    "INTAKE_VERSION_CONFLICT",
                    "Intake session has changed",
                    409,
                )
            decision = values["decision"]
            edited_value = (values.get("value") or "").strip()
            if decision == "accept" and candidate.decision_mode != "accept_or_edit":
                raise IntakeError(
                    "FACT_CANDIDATE_DECISION_NOT_ALLOWED",
                    "This candidate must be edited or rejected",
                    422,
                )
            if decision == "edit" and (
                not edited_value or edited_value == candidate.value_encrypted
            ):
                raise IntakeError(
                    "FACT_CANDIDATE_EDIT_INVALID",
                    "Edited candidate value must be non-empty and changed",
                    422,
                )
            answer_row = await session.scalar(
                select(IntakeAnswer)
                .where(
                    IntakeAnswer.id == candidate.intake_answer_id,
                    IntakeAnswer.owner_user_id == candidate.owner_user_id,
                    IntakeAnswer.session_id == intake.id,
                )
                .with_for_update()
            )
            if answer_row is None:
                raise IntakeError("RESOURCE_NOT_FOUND", "Intake answer not found", 404)
            if answer_row.analysis_status != "waiting_for_confirmation":
                raise IntakeError(
                    "INTAKE_FACT_REVIEW_NOT_ACTIVE",
                    "Fact candidate review is no longer active",
                    409,
                )
            fact = None
            source = None
            source_range = None
            source_hash = None
            now = datetime.now(timezone.utc)
            if decision == "accept":
                answer_text = answer_row.answer_encrypted or ""
                source = SourceRecord(
                    id=new_id("src"),
                    owner_user_id=candidate.owner_user_id,
                    source_type="question_answer",
                    source_ref=f"intake-answer:{answer_row.id}",
                    content_encrypted=answer_text,
                )
                fact_value = candidate.value_encrypted
                source_range = {
                    "start": candidate.source_start,
                    "end": candidate.source_end,
                }
                source_hash = candidate.source_hash
            elif decision == "edit":
                source = SourceRecord(
                    id=new_id("src"),
                    owner_user_id=candidate.owner_user_id,
                    source_type="fact_candidate_edit",
                    source_ref=f"fact-candidate:{candidate.id}",
                    content_encrypted=edited_value,
                )
                fact_value = edited_value
                source_range = {"start": 0, "end": len(edited_value)}
                source_hash = hashlib.sha256(edited_value.encode()).hexdigest()
            if source is not None:
                fact = Fact(
                    id=new_id("fact"),
                    owner_user_id=candidate.owner_user_id,
                    kind=candidate.kind,
                    value_encrypted=fact_value,
                    status="unconfirmed",
                    confirmed_at=None,
                )
                session.add_all((source, fact))
                await session.flush()
                session.add(
                    FactSource(
                        fact_id=fact.id,
                        source_record_id=source.id,
                        owner_user_id=candidate.owner_user_id,
                        source_range=source_range,
                        source_hash=source_hash,
                    )
                )
                await session.flush()
                fact.status = "confirmed"
                fact.confirmed_at = now
                candidate.fact_id = fact.id
                if decision == "edit":
                    candidate.decision_source_id = source.id
                intake.fact_ids = [*intake.fact_ids, fact.id]
            candidate.status = {
                "accept": "accepted",
                "edit": "edited",
                "reject": "rejected",
            }[decision]
            candidate.decided_at = now
            candidate.decided_by = owner
            await session.flush()
            pending = int(
                await session.scalar(
                    select(func.count())
                    .select_from(FactCandidate)
                    .where(
                        FactCandidate.intake_answer_id == answer_row.id,
                        FactCandidate.owner_user_id == answer_row.owner_user_id,
                        FactCandidate.status == "pending",
                    )
                )
                or 0
            )
            if pending == 0:
                answer_row.analysis_status = "completed"
            intake.version += 1
            intake.updated_at = now
            response = _candidate_decision_response(
                candidate,
                fact,
                intake,
                current_question=(None if pending else intake.current_question),
            )
            await self.idempotency.complete(session, claim, 200, response)
            return response

    async def _analysis_resource(
        self,
        session: AsyncSession,
        owner_id: str,
        answer_id: str,
        task_id: str,
        *,
        lock: bool,
    ) -> tuple[IntakeAnswer, IntakeSession]:
        owners = await authorized_owner_ids(session, owner_id)
        query = select(IntakeAnswer).where(
            IntakeAnswer.id == answer_id,
            IntakeAnswer.owner_user_id.in_(owners),
            IntakeAnswer.analysis_task_id == task_id,
        )
        answer_row = await session.scalar(query.with_for_update() if lock else query)
        if answer_row is None:
            raise IntakeError("RESOURCE_NOT_FOUND", "Intake answer not found", 404)
        intake = await session.scalar(
            select(IntakeSession)
            .where(
                IntakeSession.id == answer_row.session_id,
                IntakeSession.owner_user_id == answer_row.owner_user_id,
            )
            .with_for_update()
        )
        if intake is None:
            raise IntakeError("RESOURCE_NOT_FOUND", "Intake session not found", 404)
        return answer_row, intake

    async def _analysis_request(
        self,
        session: AsyncSession,
        task,
        answer_row: IntakeAnswer,
        intake: IntakeSession,
    ) -> AnalyzeIntakeRequest:
        _require_saved_answer_input(answer_row)
        outbox = await session.scalar(
            select(Outbox).where(
                Outbox.task_id == task.id,
                Outbox.owner_user_id == task.owner_user_id,
            )
        )
        snapshot, payload = _validated_analysis_snapshot(
            task,
            answer_row,
            intake,
            outbox,
        )
        return AnalyzeIntakeRequest(
            workflow_type=snapshot["workflow_type"],
            workflow_version=snapshot["workflow_version"],
            prompt_template_version=snapshot["prompt_template_version"],
            trace_id=task.trace_id,
            task_id=task.id,
            owner_scope_hash=hashlib.sha256(
                answer_row.owner_user_id.encode()
            ).hexdigest(),
            locale=snapshot["locale"],
            input_version=answer_row.analysis_input_version,
            input_hash=answer_row.analysis_input_hash,
            payload=payload,
        )

    async def _build_analysis_snapshot(
        self,
        session: AsyncSession,
        intake: IntakeSession,
        answer: IntakeAnswer,
    ) -> dict[str, Any]:
        facts: list[Fact] = []
        if intake.fact_ids:
            facts = list(
                (
                    await session.scalars(
                        select(Fact).where(
                            Fact.id.in_(intake.fact_ids),
                            Fact.owner_user_id == intake.owner_user_id,
                            Fact.status == "confirmed",
                        )
                    )
                ).all()
            )
        facts_by_id = {fact.id: fact for fact in facts}
        question = dict(intake.current_question or _question(answer.question_id))
        payload = AnalyzeIntakePayload(
            session_id_hash=hashlib.sha256(intake.id.encode()).hexdigest(),
            answer_id=answer.id,
            question_id=answer.question_id,
            question_reason=question.get("reason") or "general",
            answer_text=answer.answer_encrypted or "",
            answer_state=answer.state,
            confirmed_facts=tuple(
                FactProjection(id=fact.id, kind=fact.kind, value=fact.value_encrypted)
                for fact_id in intake.fact_ids
                if (fact := facts_by_id.get(fact_id)) is not None
            ),
            covered_slots=(),
            missing_slots=(),
            asked_question_ids=tuple(
                [*intake.answered_question_ids, answer.question_id]
            ),
        )
        return {
            "workflow_type": "analyze_intake_answer",
            "workflow_version": "2",
            "prompt_template_version": "intake-answer@2",
            "locale": "zh-CN",
            "input_version": 1,
            "question": question,
            "payload": payload.model_dump(mode="json"),
        }

    @staticmethod
    async def _append_candidate_validation_events(
        session: AsyncSession,
        owner_id: str,
        ai_run_id: str,
        invalid: list[tuple[int, str]],
    ) -> None:
        if not invalid:
            return
        last_seq = int(
            await session.scalar(
                select(func.coalesce(func.max(AiTraceEvent.event_seq), 0)).where(
                    AiTraceEvent.ai_run_id == ai_run_id,
                    AiTraceEvent.owner_user_id == owner_id,
                )
            )
            or 0
        )
        now = datetime.now(timezone.utc)
        session.add_all(
            [
                AiTraceEvent(
                    id=new_id("aie"),
                    owner_user_id=owner_id,
                    ai_run_id=ai_run_id,
                    event_seq=last_seq + offset,
                    event_type="fact_validation_failed",
                    payload={
                        "error_code": "fact_validation_failed",
                        "schema_path": f"$.fact_candidates[{index}]",
                        "risk_flags": [reason],
                    },
                    created_at=now,
                )
                for offset, (index, reason) in enumerate(invalid, start=1)
            ]
        )

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
            pending_candidates = int(
                await session.scalar(
                    select(func.count())
                    .select_from(FactCandidate)
                    .join(
                        IntakeAnswer,
                        and_(
                            IntakeAnswer.id == FactCandidate.intake_answer_id,
                            IntakeAnswer.owner_user_id
                            == FactCandidate.owner_user_id,
                        ),
                    )
                    .where(
                        IntakeAnswer.session_id == row.id,
                        IntakeAnswer.owner_user_id == row.owner_user_id,
                        FactCandidate.owner_user_id == row.owner_user_id,
                        FactCandidate.status == "pending",
                    )
                )
                or 0
            )
            if pending_candidates:
                raise IntakeError(
                    "INTAKE_FACT_REVIEW_REQUIRED",
                    "Review all pending fact candidates before generating a draft",
                    409,
                )
            latest_answer = await self._latest_analysis_answer(
                session,
                row,
                lock=False,
            )
            if latest_answer is not None and latest_answer.analysis_status in {
                "queued",
                "running",
                "waiting_for_confirmation",
                "failed",
            }:
                raise IntakeError(
                    "INTAKE_ANALYSIS_NOT_READY",
                    "Finish or resolve the latest answer analysis before generating a draft",
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
        latest_answer = await session.scalar(
            select(IntakeAnswer)
            .where(
                IntakeAnswer.session_id == row.id,
                IntakeAnswer.owner_user_id == row.owner_user_id,
            )
            .order_by(IntakeAnswer.created_at.desc(), IntakeAnswer.id.desc())
            .limit(1)
        )
        candidate_rows = list(
            (
                await session.execute(
                    select(FactCandidate, IntakeAnswer.answer_encrypted)
                    .join(
                        IntakeAnswer,
                        and_(
                            IntakeAnswer.id == FactCandidate.intake_answer_id,
                            IntakeAnswer.owner_user_id
                            == FactCandidate.owner_user_id,
                        ),
                    )
                    .where(
                        IntakeAnswer.session_id == row.id,
                        IntakeAnswer.owner_user_id == row.owner_user_id,
                        FactCandidate.owner_user_id == row.owner_user_id,
                    )
                    .order_by(
                        IntakeAnswer.created_at,
                        FactCandidate.source_start,
                        FactCandidate.id,
                    )
                )
            ).all()
        )
        pending_candidates = any(
            candidate.status == "pending" for candidate, _ in candidate_rows
        )
        return {
            "id": row.id,
            "status": row.status,
            "version": row.version,
            "current_question": None if pending_candidates else row.current_question,
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
            "fact_candidates": [
                {
                    "id": candidate.id,
                    "intake_answer_id": candidate.intake_answer_id,
                    "kind": candidate.kind,
                    "value": candidate.value_encrypted,
                    "source_excerpt": (answer or "")[
                        candidate.source_start : candidate.source_end
                    ],
                    "source_start": candidate.source_start,
                    "source_end": candidate.source_end,
                    "source_hash": candidate.source_hash,
                    "status": candidate.status,
                    "decision_mode": candidate.decision_mode,
                    "ai_run_id": candidate.ai_run_id,
                }
                for candidate, answer in candidate_rows
            ],
            "analysis_task_id": (
                latest_answer.analysis_task_id if latest_answer is not None else None
            ),
            "analysis_status": (
                latest_answer.analysis_status if latest_answer is not None else "idle"
            ),
            "task_id": row.task_id,
            "resume_id": row.resume_id,
        }


def _answer_state(answer: str, skipped: bool) -> str:
    if skipped:
        return "skipped"
    normalized = re.sub(r"[\s，。！？,.!?；;：:]+", "", answer)
    if normalized in NEGATIVE_ANSWERS or re.fullmatch(
        r"(?:暂时)?(?:没有|无)(?:相关|类似|这方面)?(?:经历|经验|项目|任务|内容|实习|兼职|工作|课程|社团|志愿活动)?(?:了)?",
        normalized,
    ) or NEGATIVE_CLAIM.fullmatch(normalized):
        return "negative"
    return "answered"


def _require_analysis_graph(task, answer: IntakeAnswer, intake: IntakeSession) -> None:
    if (
        task.type != "analyze_intake_answer"
        or task.resource_type != "intake_answer"
        or task.resource_id != answer.id
        or answer.analysis_task_id != task.id
        or answer.session_id != intake.id
        or intake.status != "active"
        or answer.owner_user_id != intake.owner_user_id
        or task.owner_user_id != answer.owner_user_id
    ):
        raise IntakeError(
            "INTAKE_ANALYSIS_GRAPH_INVALID",
            "Intake answer analysis resources do not match",
            409,
        )


def _require_saved_answer_input(answer: IntakeAnswer) -> None:
    if (
        answer.state != "answered"
        or answer.analysis_input_version != 1
        or not isinstance(answer.analysis_input_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", answer.analysis_input_hash) is None
    ):
        raise IntakeError(
            "INTAKE_ANALYSIS_INPUT_CHANGED",
            "Saved answer analysis input is no longer valid",
            409,
        )


def _semantic_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validated_analysis_snapshot(
    task: Task,
    answer: IntakeAnswer,
    intake: IntakeSession,
    outbox: Outbox | None,
) -> tuple[dict[str, Any], AnalyzeIntakePayload]:
    task_payload = outbox.payload if outbox is not None else None
    snapshot = (
        task_payload.get("analysis_snapshot")
        if isinstance(task_payload, dict)
        else None
    )
    expected_snapshot_keys = {
        "workflow_type",
        "workflow_version",
        "prompt_template_version",
        "locale",
        "input_version",
        "question",
        "payload",
    }
    question = snapshot.get("question") if isinstance(snapshot, dict) else None
    try:
        semantic_payload = AnalyzeIntakePayload.model_validate(
            snapshot.get("payload") if isinstance(snapshot, dict) else None,
            strict=False,
        )
    except Exception as error:
        raise IntakeError(
            "INTAKE_ANALYSIS_SNAPSHOT_INVALID",
            "Saved answer analysis snapshot is invalid",
            409,
        ) from error
    valid = (
        outbox is not None
        and outbox.task_id == task.id
        and outbox.owner_user_id == task.owner_user_id
        and isinstance(task_payload, dict)
        and task_payload.get("task_id") == task.id
        and task_payload.get("intake_session_id") == intake.id
        and task_payload.get("intake_answer_id") == answer.id
        and task_payload.get("analysis_input_version")
        == answer.analysis_input_version
        and task_payload.get("analysis_input_hash") == answer.analysis_input_hash
        and isinstance(snapshot, dict)
        and set(snapshot) == expected_snapshot_keys
        and snapshot.get("workflow_type") == "analyze_intake_answer"
        and snapshot.get("workflow_version") == "2"
        and snapshot.get("prompt_template_version") == "intake-answer@2"
        and snapshot.get("locale") == "zh-CN"
        and snapshot.get("input_version") == answer.analysis_input_version
        and isinstance(question, dict)
        and set(question) == {"id", "type", "prompt", "reason"}
        and question.get("id") == answer.question_id
        and semantic_payload.session_id_hash
        == hashlib.sha256(intake.id.encode()).hexdigest()
        and semantic_payload.answer_id == answer.id
        and semantic_payload.question_id == answer.question_id
        and semantic_payload.answer_text == (answer.answer_encrypted or "")
        and semantic_payload.answer_state == answer.state
        and _semantic_hash(snapshot) == answer.analysis_input_hash
    )
    if not valid:
        raise IntakeError(
            "INTAKE_ANALYSIS_SNAPSHOT_INVALID",
            "Saved answer analysis snapshot is invalid",
            409,
        )
    return snapshot, semantic_payload


def _validated_candidates(
    answer: IntakeAnswer,
    result: AnalyzeIntakeResult,
) -> tuple[list[Any], list[tuple[int, str]]]:
    answer_text = answer.answer_encrypted or ""
    valid: list[Any] = []
    invalid: list[tuple[int, str]] = []
    seen: set[tuple[str, str, int, int, str]] = set()
    for index, candidate in enumerate(result.fact_candidates):
        start = candidate.source_range.start
        end = candidate.source_range.end
        reason = None
        source_slice = ""
        if candidate.source_answer_id != answer.id:
            reason = "source_answer_mismatch"
        elif not (0 <= start < end <= len(answer_text)):
            reason = "source_range_invalid"
        else:
            source_slice = answer_text[start:end]
            if hashlib.sha256(source_slice.encode()).hexdigest() != candidate.source_hash:
                reason = "source_hash_mismatch"
            elif (
                _answer_state(source_slice, False) != "answered"
                or _source_negates_candidate(source_slice, candidate.value)
            ):
                reason = "negative_source"
            elif _has_unparsed_quantity(candidate.value):
                reason = "unsupported_numeric"
            elif not _number_tokens(candidate.value).issubset(
                _number_tokens(source_slice)
            ):
                reason = "unsupported_numeric"
            elif not _has_lexical_support(source_slice, candidate.value):
                reason = "unsupported_lexical"
        key = _candidate_key(candidate)
        if reason is None and key in seen:
            reason = "duplicate_candidate"
        if reason is not None:
            invalid.append((index, reason))
            continue
        seen.add(key)
        valid.append(candidate)
    return valid, invalid


def _candidate_key(candidate) -> tuple[str, str, int, int, str]:
    return (
        candidate.kind,
        candidate.value,
        candidate.source_range.start,
        candidate.source_range.end,
        candidate.source_hash,
    )


def _number_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for match in NUMBER_TOKEN.finditer(value):
        raw = match.group("number").replace(",", "").replace("−", "-")
        try:
            number = Decimal(raw)
        except InvalidOperation:
            continue
        normalized = format(number.normalize(), "f")
        if number == 0:
            normalized = "0"
        if match.group("percent"):
            normalized = f"{normalized}%"
        tokens.add(normalized)
    for match in CHINESE_NUMBER_TOKEN.finditer(value):
        number = _canonical_chinese_number(match.group("number"))
        tokens.add(
            f"zh:{match.group('prefix') or ''}:{number}:"
            f"{match.group('suffix') or ''}"
        )
    return tokens


def _canonical_chinese_number(value: str) -> str:
    expanded = value.replace("廿", "二十").replace("卅", "三十")
    return expanded.translate(
        str.maketrans(
            {
                "两": "二",
                "〇": "零",
                "壹": "一",
                "贰": "二",
                "叁": "三",
                "肆": "四",
                "伍": "五",
                "陆": "六",
                "柒": "七",
                "捌": "八",
                "玖": "九",
                "拾": "十",
                "佰": "百",
                "仟": "千",
                "萬": "万",
                "億": "亿",
            }
        )
    )


def _has_unparsed_quantity(value: str) -> bool:
    return bool(UNPARSED_QUANTITY.search(value))


def _source_negates_candidate(source: str, candidate: str) -> bool:
    source_cjk, anchors = _candidate_core_anchors(source, candidate)
    return any(_anchor_is_negated(source_cjk, anchor) for anchor in anchors) or any(
        _has_detached_negative_clause(source_cjk, anchor) for anchor in anchors
    )


def _has_lexical_support(source: str, candidate: str) -> bool:
    _, anchors = _candidate_core_anchors(source, candidate)
    if anchors:
        return True
    normalized_source = _normalized_lexeme(source)
    normalized_candidate = _normalized_lexeme(candidate)
    return bool(
        normalized_candidate
        and normalized_candidate in normalized_source
    )


def _candidate_core_anchors(
    source: str,
    candidate: str,
) -> tuple[str, tuple[int, ...]]:
    source_cjk = "".join(CJK_CHARACTER.findall(source))
    candidate_cjk = "".join(CJK_CHARACTER.findall(candidate))
    if not candidate_cjk:
        return source_cjk, ()
    if candidate_cjk in source_cjk:
        return source_cjk, _occurrences(source_cjk, candidate_cjk)
    if len(candidate_cjk) == 1:
        return source_cjk, _occurrences(source_cjk, candidate_cjk)
    source_bigrams = {
        source_cjk[index : index + 2]
        for index in range(len(source_cjk) - 1)
    }
    for index in range(len(candidate_cjk) - 1):
        core = candidate_cjk[index : index + 2]
        if core in source_bigrams:
            return source_cjk, _occurrences(source_cjk, core)
    return source_cjk, ()


def _occurrences(value: str, needle: str) -> tuple[int, ...]:
    positions: list[int] = []
    start = 0
    while (position := value.find(needle, start)) >= 0:
        positions.append(position)
        start = position + 1
    return tuple(positions)


def _anchor_is_negated(source: str, anchor: int) -> bool:
    prefix = source[:anchor]
    boundary_positions = [
        position + len(marker)
        for marker in NEGATION_CLAUSE_BOUNDARIES
        if (position := prefix.rfind(marker)) >= 0
    ]
    boundary = max(boundary_positions, default=0)
    segment = prefix[boundary:]
    for marker in MULTI_NEGATIVE_MARKERS:
        position = segment.rfind(marker)
        if (
            position >= 0
            and len(segment) - position - len(marker) <= NEGATION_MAX_GAP
        ):
            return True
    return bool(segment and segment[-1] in SINGLE_NEGATIVE_MARKERS)


def _has_detached_negative_clause(source: str, anchor: int) -> bool:
    prefix = source[:anchor]
    boundary_positions = [
        position
        for marker in NEGATION_CLAUSE_BOUNDARIES
        if (position := prefix.rfind(marker)) >= 0
    ]
    if not boundary_positions:
        return False
    prior_clauses = prefix[: max(boundary_positions)]
    return any(marker in prior_clauses for marker in MULTI_NEGATIVE_MARKERS)


def _normalized_lexeme(value: str) -> str:
    return re.sub(r"[\s，。！？,.!?；;：:]+", "", value)


def _clear_analysis_snapshot(outbox: Outbox | None) -> None:
    if outbox is None or "analysis_snapshot" not in outbox.payload:
        return
    payload = dict(outbox.payload)
    payload.pop("analysis_snapshot", None)
    outbox.payload = payload


def _analysis_next_question(
    answer: IntakeAnswer,
    result: AnalyzeIntakeResult,
) -> tuple[dict[str, Any] | None, str]:
    next_question = _next_question(
        answer.question_id,
        answer.answer_encrypted or "",
        "answered",
    )
    candidate = result.question_candidate
    if (
        next_question is not None
        and candidate is not None
        and candidate.slot == next_question["id"]
    ):
        return {**next_question, "prompt": candidate.text}, "model"
    return next_question, "rule"


def _question(question_id: str) -> dict[str, Any]:
    if question_id == INITIAL_QUESTION["id"]:
        return INITIAL_QUESTION
    question = QUESTIONS.get(question_id)
    if question is None:
        raise IntakeError(
            "INTAKE_QUESTION_INVALID",
            "Saved intake question is invalid",
            409,
        )
    return question


def _candidate_decision_response(
    candidate: FactCandidate,
    fact: Fact | None,
    intake: IntakeSession,
    *,
    current_question: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate.id,
        "status": candidate.status,
        "fact_summary": (
            {
                "id": fact.id,
                "kind": fact.kind,
                "value": fact.value_encrypted,
                "status": fact.status,
            }
            if fact is not None
            else None
        ),
        "session_version": intake.version,
        "current_question": current_question,
    }


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
