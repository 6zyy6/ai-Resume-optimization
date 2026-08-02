from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import (
    BulletFactLink,
    Fact,
    JdRequirement,
    JobDescription,
    MatchAnalysis,
    MatchItem,
    Resume,
    ResumeVersion,
    Suggestion,
    SuggestionFactLink,
    TargetedResumeKey,
    VersionOperation,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.ai_client import AiCancellation, AiClient
from app.integrations.ai_client import (
    AiExecutionReceipt,
    GenerateSuggestionsBatchPayload,
    GenerateSuggestionsBatchRequest,
    GenerateSuggestionsBatchResult,
    MatchResumeToJdPayload,
    MatchResumeToJdRequest,
    MatchResumeToJdResult,
    RequirementProjection,
    SuggestionSource,
    derive_ai_run_id,
)
from app.modules.ai_runs.service import AiRunService
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.resumes.fact_policy import (
    ConfirmedFactProjection,
    DraftClaim,
    fact_policy_check,
)
from app.modules.tasks.service import TaskAdmission, TaskService


MATCH_CATEGORIES = {
    "proved",
    "underexpressed",
    "needs_confirmation",
    "real_gap",
}


@dataclass(frozen=True)
class ClassifiedRequirement:
    requirement_id: str
    category: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceFact:
    id: str
    kind: str
    value: str
    source_hashes: tuple[str, ...]


@dataclass
class MatchServiceError(Exception):
    code: str
    message: str
    status_code: int


@dataclass(frozen=True)
class MatchAnalysisResult:
    analysis: MatchAnalysis
    items: list[MatchItem]
    suggestions: list[Suggestion]
    suggestion_fact_refs: dict[str, list[str]] = field(default_factory=dict)
    requirement_texts: dict[str, str] = field(default_factory=dict)


def classify_requirements(
    requirements: list[dict[str, str]],
    *,
    facts: tuple[str, ...],
) -> list[ClassifiedRequirement]:
    normalized_facts = tuple(_tokens(value) for value in facts)
    results: list[ClassifiedRequirement] = []
    for requirement in requirements:
        required = _tokens(requirement["text"])
        overlaps = [len(required & fact) for fact in normalized_facts]
        best = max(overlaps, default=0)
        if required and best == len(required):
            category = "proved"
        elif best >= max(1, len(required) // 2):
            category = "underexpressed"
        elif any(token in " ".join(facts).lower() for token in required):
            category = "needs_confirmation"
        else:
            category = "real_gap"
        results.append(
            ClassifiedRequirement(
                requirement_id=requirement["id"],
                category=category,
                evidence_refs=tuple(
                    f"fact:{index}" for index, overlap in enumerate(overlaps) if overlap
                ),
            )
        )
    return results


def _tokens(value: str) -> set[str]:
    latin = re.findall(r"[a-z0-9+#.]+", value.lower())
    chinese = re.findall(r"[\u3400-\u9fff]{2,}", value)
    return set(latin + chinese)


class MatchingService:
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
        *,
        resume_version_id: str,
        job_id: str,
        idempotency_key: str,
        trace_id: str,
        task_service: TaskService,
    ) -> MatchAnalysisResult:
        route = "/v1/match-analyses"
        body = {"resume_version_id": resume_version_id, "job_id": job_id}
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, body
                )
            except IdempotencyConflict as error:
                raise MatchServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                result = await self._result(
                    session, owner_id, (claim.replay_response or {})["id"]
                )
                if result is None:
                    raise RuntimeError("Idempotent match analysis is missing")
                return result
            owners = await authorized_owner_ids(session, owner_id)
            version = await session.scalar(
                select(ResumeVersion).where(
                    ResumeVersion.id == resume_version_id,
                    ResumeVersion.owner_user_id.in_(owners),
                )
            )
            job = await session.scalar(
                select(JobDescription).where(
                    JobDescription.id == job_id,
                    JobDescription.owner_user_id.in_(owners),
                )
            )
            if version is None or job is None:
                raise MatchServiceError(
                    "RESOURCE_NOT_FOUND", "Resume version or job not found", 404
                )
            requirements = list(
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
            if not requirements:
                raise MatchServiceError(
                    "JOB_REQUIREMENTS_REQUIRED",
                    "Parse the job description before matching",
                    409,
                )
            if any(not requirement.confirmed for requirement in requirements):
                raise MatchServiceError(
                    "JOB_REQUIREMENTS_NOT_CONFIRMED",
                    "Confirm every job requirement before matching",
                    409,
                )
            version = await self._target_version(
                session,
                owner,
                version,
                job,
            )
            evidence = await self._evidence_projection(session, version)
            match_payload = self._match_payload(version, requirements, evidence)
            input_hash = _workflow_input_hash(
                "match_resume_to_jd", "resume-match@2", match_payload
            )
            generation_mode = (
                "model"
                if self.ai_client is not None
                else "rule_fallback"
            )
            analysis = MatchAnalysis(
                id=new_id("match"),
                owner_user_id=owner,
                resume_version_id=version.id,
                job_id=job.id,
                job_owner_user_id=job.owner_user_id,
                status="queued",
                generation_mode=generation_mode,
                workflow_version="2",
                ai_run_id=None,
                input_hash=input_hash,
            )
            session.add(analysis)
            await session.flush()
            task = await task_service.create_task_in_session(
                session,
                owner,
                task_type="match_resume_to_job",
                queue="ai.batch",
                trace_id=trace_id,
                idempotency_key=f"match:{idempotency_key}",
                admission=(
                    TaskAdmission.ai()
                    if generation_mode == "model"
                    else TaskAdmission.unmetered()
                ),
                resource_type="match_analysis",
                resource_id=analysis.id,
                payload={
                    "analysis_id": analysis.id,
                    "generation_mode": generation_mode,
                    "match_input_hash": input_hash,
                },
            )
            analysis.task_id = task.id
            await self.idempotency.complete(
                session,
                claim,
                202,
                {
                    "id": analysis.id,
                    "status": analysis.status,
                    "task_id": analysis.task_id,
                },
            )
            return MatchAnalysisResult(
                analysis,
                [],
                [],
                {},
                {
                    requirement.id: requirement.text_encrypted
                    for requirement in requirements
                },
            )

    @staticmethod
    async def _target_version(
        session: AsyncSession,
        owner: str,
        source_version: ResumeVersion,
        job: JobDescription,
    ) -> ResumeVersion:
        source_resume = await session.scalar(
            select(Resume)
            .where(
                Resume.id == source_version.resume_id,
                Resume.owner_user_id == source_version.owner_user_id,
            )
            .with_for_update()
        )
        if source_resume is None:
            raise RuntimeError("Match source resume is missing")
        if source_resume.kind == "job_targeted":
            if source_resume.job_description_id != job.id:
                raise MatchServiceError(
                    "RESUME_JOB_CONFLICT",
                    "Targeted resume belongs to another job",
                    409,
                )
            canonical = await MatchingService._canonical_targeted_resume(
                session,
                owner,
                source_resume.base_resume_id,
                source_resume.base_resume_owner_user_id,
                source_resume.job_description_id,
                source_resume.job_description_owner_user_id,
            )
            if canonical is None or canonical.id == source_resume.id:
                return source_version
            if canonical.head_version_id is None:
                raise RuntimeError("Canonical targeted resume is missing")
            canonical_version = await session.scalar(
                select(ResumeVersion).where(
                    ResumeVersion.id == canonical.head_version_id,
                    ResumeVersion.resume_id == canonical.id,
                    ResumeVersion.owner_user_id == canonical.owner_user_id,
                )
            )
            if canonical_version is None:
                raise RuntimeError("Canonical targeted resume head is missing")
            return canonical_version
        if source_resume.kind != "base":
            raise MatchServiceError(
                "VALIDATION_FAILED",
                "Only base or job-targeted resumes can be matched",
                422,
            )
        targeted = await MatchingService._canonical_targeted_resume(
            session,
            owner,
            source_resume.id,
            source_resume.owner_user_id,
            job.id,
            job.owner_user_id,
        )
        if targeted is not None and targeted.head_version_id:
            current = await session.scalar(
                select(ResumeVersion).where(
                    ResumeVersion.id == targeted.head_version_id,
                    ResumeVersion.resume_id == targeted.id,
                    ResumeVersion.owner_user_id == targeted.owner_user_id,
                )
            )
            if current is None:
                raise RuntimeError("Targeted resume head is missing")
            return current
        if targeted is None:
            targeted = Resume(
                id=new_id("resume"),
                owner_user_id=owner,
                kind="job_targeted",
                title=f"{source_resume.title} · {job.title}",
                base_resume_id=source_resume.id,
                base_resume_owner_user_id=source_resume.owner_user_id,
                job_description_id=job.id,
                job_description_owner_user_id=job.owner_user_id,
                head_version=0,
            )
            session.add(targeted)
            await session.flush()
            session.add(
                TargetedResumeKey(
                    owner_user_id=owner,
                    base_resume_id=source_resume.id,
                    base_resume_owner_user_id=source_resume.owner_user_id,
                    job_description_id=job.id,
                    job_description_owner_user_id=job.owner_user_id,
                    resume_id=targeted.id,
                )
            )
        target_version = ResumeVersion(
            id=new_id("rver"),
            owner_user_id=owner,
            resume_id=targeted.id,
            parent_version_id=None,
            snapshot_json=source_version.snapshot_json,
            snapshot_hash=source_version.snapshot_hash,
            created_by=owner,
        )
        session.add(target_version)
        await session.flush()
        links = list(
            (
                await session.scalars(
                    select(BulletFactLink).where(
                        BulletFactLink.resume_version_id == source_version.id,
                        BulletFactLink.owner_user_id
                        == source_version.owner_user_id,
                    )
                )
            ).all()
        )
        for link in links:
            session.add(
                BulletFactLink(
                    resume_version_id=target_version.id,
                    bullet_id=link.bullet_id,
                    fact_id=link.fact_id,
                    claim_start=link.claim_start,
                    claim_end=link.claim_end,
                    owner_user_id=owner,
                    fact_owner_user_id=link.fact_owner_user_id,
                    claim_range=dict(link.claim_range),
                    fact_value_encrypted_at_link=
                    link.fact_value_encrypted_at_link,
                    fact_status_at_link=link.fact_status_at_link,
                    fact_source_hashes_at_link=list(
                        link.fact_source_hashes_at_link
                    ),
                )
            )
        session.add(
            VersionOperation(
                id=new_id("vop"),
                owner_user_id=owner,
                version_id=target_version.id,
                operation_type="save",
                actor=owner,
                metadata_json={
                    "source": "match_target_seed",
                    "base_resume_version_id": source_version.id,
                    "job_description_id": job.id,
                },
            )
        )
        targeted.head_version = 1
        targeted.head_version_id = target_version.id
        await session.flush()
        return target_version

    @staticmethod
    async def _canonical_targeted_resume(
        session: AsyncSession,
        owner_user_id: str,
        base_resume_id: str | None,
        base_resume_owner_user_id: str | None,
        job_description_id: str | None,
        job_description_owner_user_id: str | None,
    ) -> Resume | None:
        if (
            base_resume_id is None
            or base_resume_owner_user_id is None
            or job_description_id is None
            or job_description_owner_user_id is None
        ):
            return None
        return await session.scalar(
            select(Resume)
            .join(
                TargetedResumeKey,
                (TargetedResumeKey.resume_id == Resume.id)
                & (
                    TargetedResumeKey.owner_user_id
                    == Resume.owner_user_id
                ),
            )
            .where(
                TargetedResumeKey.owner_user_id == owner_user_id,
                TargetedResumeKey.base_resume_id == base_resume_id,
                TargetedResumeKey.base_resume_owner_user_id
                == base_resume_owner_user_id,
                TargetedResumeKey.job_description_id == job_description_id,
                TargetedResumeKey.job_description_owner_user_id
                == job_description_owner_user_id,
            )
        )

    async def attach_task(
        self,
        owner_id: str,
        analysis_id: str,
        task_id: str,
    ) -> MatchAnalysis:
        async with self.sessions.begin() as session:
            owners = await authorized_owner_ids(session, owner_id)
            row = await session.scalar(
                select(MatchAnalysis)
                .where(
                    MatchAnalysis.id == analysis_id,
                    MatchAnalysis.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if row is None:
                raise MatchServiceError(
                    "RESOURCE_NOT_FOUND", "Match analysis not found", 404
                )
            if row.task_id is not None and row.task_id != task_id:
                raise MatchServiceError(
                    "MATCH_TASK_CONFLICT",
                    "Match analysis already has another task",
                    409,
                )
            row.task_id = task_id
            await session.flush()
            return row

    async def process_match(
        self,
        owner_id: str,
        analysis_id: str,
        *,
        trace_id: str,
        task_id: str,
        claim_token: str | None = None,
        task_service: TaskService | None = None,
        cancellation: AiCancellation | None = None,
    ) -> str:
        if (claim_token is None) != (task_service is None):
            raise TypeError("claim_token and task_service must be provided together")
        context = await self._processing_context(owner_id, analysis_id)
        analysis, version, requirements, evidence = context
        if analysis.status == "succeeded":
            return analysis.id
        if analysis.task_id != task_id:
            raise MatchServiceError("MATCH_TASK_CONFLICT", "Match task does not match", 409)
        request_trace_id = trace_id
        active_ai_run_id: str | None = None
        if task_service is not None and claim_token is not None:
            async with self.sessions.begin() as session:
                task = await task_service.claimed_task_in_session(
                    session, owner_id, task_id, claim_token
                )
                if (
                    task.type != "match_resume_to_job"
                    or task.resource_type != "match_analysis"
                    or task.resource_id != analysis_id
                ):
                    raise MatchServiceError(
                        "RESOURCE_NOT_FOUND", "Match analysis not found", 404
                    )
                request_trace_id = task.trace_id
                active_ai_run_id = task.active_ai_run_id

        match_payload = self._match_payload(version, requirements, evidence)
        match_input_hash = _workflow_input_hash(
            "match_resume_to_jd", "resume-match@2", match_payload
        )
        if match_input_hash != analysis.input_hash:
            await self._fail_without_receipt(
                owner_id,
                analysis_id,
                task_id,
                claim_token,
                task_service,
                "MATCH_INPUT_CHANGED",
            )
            return analysis_id

        if analysis.generation_mode == "rule_fallback":
            matches, suggestions = self._rule_fallback(
                requirements, evidence, version.snapshot_json
            )
            async with self.sessions.begin() as session:
                claimed_task = (
                    await task_service.claimed_task_in_session(
                        session, owner_id, task_id, claim_token
                    )
                    if task_service is not None and claim_token is not None
                    else None
                )
                current = await self._locked_analysis(session, owner_id, analysis_id)
                await self._publish_in_session(
                    session,
                    current,
                    matches,
                    suggestions,
                    generation_mode="rule_fallback",
                    match_ai_run_id=None,
                    match_input_hash=match_input_hash,
                    suggestion_ai_run_id=None,
                    suggestion_input_hash=match_input_hash,
                    evidence=evidence,
                )
                current.status = "succeeded"
                if claimed_task is not None:
                    await task_service.complete_task_in_session(
                        session, claimed_task, current.id
                    )
                return current.id

        if self.ai_client is None:
            await self._fail_without_receipt(
                owner_id,
                analysis_id,
                task_id,
                claim_token,
                task_service,
                "AI_NOT_CONFIGURED",
            )
            return analysis_id

        match_request = MatchResumeToJdRequest(
            workflow_type="match_resume_to_jd",
            prompt_template_version="resume-match@2",
            trace_id=request_trace_id,
            task_id=task_id,
            owner_scope_hash=hashlib.sha256(owner_id.encode()).hexdigest(),
            input_version=1,
            input_hash=match_input_hash,
            payload=match_payload,
        )
        expected_match_run_id = derive_ai_run_id(
            task_id, "match", match_input_hash
        )
        match_receipt = await self.ai_client.run(
            match_request,
            (
                cancellation
                if active_ai_run_id in {None, expected_match_run_id}
                else None
            ),
        )
        match_rows, match_error = self._validated_matches(
            match_receipt.result, requirements, evidence, version.snapshot_json
        )
        async with self.sessions.begin() as session:
            claimed_task = await self._receipt_task(
                session,
                owner_id,
                task_id,
                claim_token,
                task_service,
                match_receipt,
            )
            current = await self._locked_analysis(session, owner_id, analysis_id)
            await self._validate_receipt(
                match_receipt,
                task_id=task_id,
                trace_id=request_trace_id,
                input_hash=match_input_hash,
                workflow_type="match_resume_to_jd",
            )
            ai_run = await self.ai_runs.persist_in_session(
                session,
                current.owner_user_id,
                match_receipt,
                workflow_stage="match",
                result_ref=current.id,
            )
            current.ai_run_id = ai_run.id
            current.status = "processing"
            if claimed_task is not None and task_service is not None:
                await task_service.consume_ai_reservation_in_session(
                    session, current.owner_user_id, task_id, ai_run.id
                )
                if claimed_task.active_ai_run_id == ai_run.id:
                    await task_service.settle_ai_run_in_session(
                        session, current.owner_user_id, task_id, ai_run.id
                    )
                if match_receipt.run.status != "succeeded" or match_error:
                    await self._fail_in_session(
                        session,
                        current,
                        claimed_task,
                        task_service,
                        match_receipt.run.error_code
                        or match_error
                        or f"ai_{match_receipt.run.status}",
                    )
                    return current.id
                await task_service.report_progress_in_session(
                    session, claimed_task, "match", 55
                )
            elif match_receipt.run.status != "succeeded" or match_error:
                current.status = "failed"
                return current.id

        suggestion_payload = self._suggestion_payload(
            match_rows, match_payload, version.snapshot_json
        )
        suggestion_input_hash = _workflow_input_hash(
            "generate_suggestions_batch",
            "suggestions-batch@2",
            suggestion_payload,
        )
        suggestion_request = GenerateSuggestionsBatchRequest(
            workflow_type="generate_suggestions_batch",
            prompt_template_version="suggestions-batch@2",
            trace_id=request_trace_id,
            task_id=task_id,
            owner_scope_hash=hashlib.sha256(owner_id.encode()).hexdigest(),
            input_version=1,
            input_hash=suggestion_input_hash,
            payload=suggestion_payload,
        )
        suggestion_receipt = await self.ai_client.run(
            suggestion_request, cancellation
        )
        suggestions, suggestion_error = self._validated_suggestions(
            suggestion_receipt.result,
            suggestion_payload,
            evidence,
        )
        async with self.sessions.begin() as session:
            claimed_task = await self._receipt_task(
                session,
                owner_id,
                task_id,
                claim_token,
                task_service,
                suggestion_receipt,
            )
            current = await self._locked_analysis(session, owner_id, analysis_id)
            await self._validate_receipt(
                suggestion_receipt,
                task_id=task_id,
                trace_id=request_trace_id,
                input_hash=suggestion_input_hash,
                workflow_type="generate_suggestions_batch",
            )
            suggestion_run = await self.ai_runs.persist_in_session(
                session,
                current.owner_user_id,
                suggestion_receipt,
                workflow_stage="suggestions",
                result_ref=current.id,
            )
            if claimed_task is not None and task_service is not None:
                if claimed_task.active_ai_run_id == suggestion_run.id:
                    await task_service.settle_ai_run_in_session(
                        session,
                        current.owner_user_id,
                        task_id,
                        suggestion_run.id,
                    )
                if suggestion_receipt.run.status != "succeeded" or suggestion_error:
                    await self._fail_in_session(
                        session,
                        current,
                        claimed_task,
                        task_service,
                        suggestion_receipt.run.error_code
                        or suggestion_error
                        or f"ai_{suggestion_receipt.run.status}",
                    )
                    return current.id
            elif suggestion_receipt.run.status != "succeeded" or suggestion_error:
                current.status = "failed"
                return current.id
            await self._publish_in_session(
                session,
                current,
                match_rows,
                suggestions,
                generation_mode="model",
                match_ai_run_id=current.ai_run_id,
                match_input_hash=match_input_hash,
                suggestion_ai_run_id=suggestion_run.id,
                suggestion_input_hash=suggestion_input_hash,
                evidence=evidence,
            )
            current.status = "succeeded"
            if claimed_task is not None and task_service is not None:
                await task_service.complete_task_in_session(
                    session, claimed_task, current.id
                )
            return current.id

    async def fail_match(
        self,
        owner_id: str,
        analysis_id: str,
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
            analysis = await self._locked_analysis(session, owner_id, analysis_id)
            if (
                analysis.task_id != task.id
                or task.type != "match_resume_to_job"
                or task.resource_type != "match_analysis"
                or task.resource_id != analysis.id
            ):
                raise MatchServiceError(
                    "RESOURCE_NOT_FOUND", "Match analysis not found", 404
                )
            if task.active_ai_run_id is not None:
                await task_service.settle_ai_run_in_session(
                    session,
                    task.owner_user_id,
                    task.id,
                    task.active_ai_run_id,
                )
            await self._fail_in_session(
                session,
                analysis,
                task,
                task_service,
                error_code,
                release_unused_ai_reservation=True,
            )

    async def get(
        self, owner_id: str, analysis_id: str
    ) -> MatchAnalysisResult | None:
        async with self.sessions() as session:
            return await self._result(session, owner_id, analysis_id)

    async def _result(
        self,
        session: AsyncSession,
        owner_id: str,
        analysis_id: str,
    ) -> MatchAnalysisResult | None:
        owners = await authorized_owner_ids(session, owner_id)
        analysis = await session.scalar(
            select(MatchAnalysis).where(
                MatchAnalysis.id == analysis_id,
                MatchAnalysis.owner_user_id.in_(owners),
            )
        )
        if analysis is None:
            return None
        items = list(
            (
                await session.scalars(
                    select(MatchItem).where(
                        MatchItem.analysis_id == analysis.id,
                        MatchItem.owner_user_id == analysis.owner_user_id,
                    )
                )
            ).all()
        )
        suggestions = list(
            (
                await session.scalars(
                    select(Suggestion).where(
                        Suggestion.analysis_id == analysis.id,
                        Suggestion.owner_user_id == analysis.owner_user_id,
                    )
                )
            ).all()
        )
        suggestion_ids = [row.id for row in suggestions]
        links = (
            list(
                (
                    await session.scalars(
                        select(SuggestionFactLink).where(
                            SuggestionFactLink.suggestion_id.in_(suggestion_ids),
                            SuggestionFactLink.owner_user_id
                            == analysis.owner_user_id,
                        )
                    )
                ).all()
            )
            if suggestion_ids
            else []
        )
        requirement_ids = {
            row.requirement_id
            for row in suggestions
            if row.requirement_id is not None
        }
        requirements = (
            list(
                (
                    await session.scalars(
                        select(JdRequirement).where(
                            JdRequirement.id.in_(requirement_ids),
                            JdRequirement.owner_user_id
                            == analysis.job_owner_user_id,
                        )
                    )
                ).all()
            )
            if requirement_ids
            else []
        )
        fact_refs: dict[str, list[str]] = {
            suggestion.id: [] for suggestion in suggestions
        }
        for link in links:
            fact_refs[link.suggestion_id].append(link.fact_id)
        return MatchAnalysisResult(
            analysis,
            items,
            suggestions,
            fact_refs,
            {row.id: row.text_encrypted for row in requirements},
        )

    async def _processing_context(
        self,
        owner_id: str,
        analysis_id: str,
    ) -> tuple[
        MatchAnalysis,
        ResumeVersion,
        list[JdRequirement],
        tuple[EvidenceFact, ...],
    ]:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            analysis = await session.scalar(
                select(MatchAnalysis).where(
                    MatchAnalysis.id == analysis_id,
                    MatchAnalysis.owner_user_id.in_(owners),
                )
            )
            if analysis is None:
                raise MatchServiceError(
                    "RESOURCE_NOT_FOUND", "Match analysis not found", 404
                )
            version = await session.scalar(
                select(ResumeVersion).where(
                    ResumeVersion.id == analysis.resume_version_id,
                    ResumeVersion.owner_user_id == analysis.owner_user_id,
                )
            )
            if version is None:
                raise RuntimeError("Match resume version is missing")
            requirements = list(
                (
                    await session.scalars(
                        select(JdRequirement)
                        .where(
                            JdRequirement.job_id == analysis.job_id,
                            JdRequirement.owner_user_id
                            == analysis.job_owner_user_id,
                        )
                        .order_by(JdRequirement.priority, JdRequirement.id)
                    )
                ).all()
            )
            if not requirements or any(not item.confirmed for item in requirements):
                raise MatchServiceError(
                    "JOB_REQUIREMENTS_NOT_CONFIRMED",
                    "Confirmed job requirements changed after matching was queued",
                    409,
                )
            evidence = await self._evidence_projection(session, version)
            return analysis, version, requirements, evidence

    @staticmethod
    async def _evidence_projection(
        session: AsyncSession,
        version: ResumeVersion,
    ) -> tuple[EvidenceFact, ...]:
        links = list(
            (
                await session.scalars(
                    select(BulletFactLink).where(
                        BulletFactLink.resume_version_id == version.id,
                        BulletFactLink.owner_user_id == version.owner_user_id,
                        BulletFactLink.fact_status_at_link == "confirmed",
                    )
                )
            ).all()
        )
        by_id: dict[str, EvidenceFact] = {}
        for link in links:
            if not link.fact_source_hashes_at_link:
                continue
            fact = await session.scalar(
                select(Fact).where(
                    Fact.id == link.fact_id,
                    Fact.owner_user_id == link.fact_owner_user_id,
                    Fact.status == "confirmed",
                )
            )
            if fact is None:
                continue
            projection = EvidenceFact(
                id=fact.id,
                kind=fact.kind,
                value=link.fact_value_encrypted_at_link,
                source_hashes=tuple(link.fact_source_hashes_at_link),
            )
            previous = by_id.get(fact.id)
            if previous is not None and previous != projection:
                raise MatchServiceError(
                    "RESUME_EVIDENCE_INVALID",
                    "Resume version has conflicting immutable fact evidence",
                    409,
                )
            by_id[fact.id] = projection
        return tuple(by_id[key] for key in sorted(by_id))

    @staticmethod
    def _match_payload(
        version: ResumeVersion,
        requirements: list[JdRequirement],
        evidence: tuple[EvidenceFact, ...],
    ) -> MatchResumeToJdPayload:
        category_map = {
            "responsibility": "responsibility",
            "must_have": "must_have",
            "preferred": "nice_to_have",
            "other": "implicit_capability",
        }
        return MatchResumeToJdPayload(
            resume_version_id=version.id,
            resume_snapshot_hash=version.snapshot_hash,
            confirmed_facts=tuple(
                {"id": fact.id, "kind": fact.kind, "value": fact.value}
                for fact in evidence
            ),
            confirmed_requirements=tuple(
                RequirementProjection(
                    id=item.id,
                    category=category_map.get(item.type, "implicit_capability"),
                    value=item.text_encrypted,
                )
                for item in requirements
            ),
        )

    @staticmethod
    def _validated_matches(
        result: object,
        requirements: list[JdRequirement],
        evidence: tuple[EvidenceFact, ...],
        snapshot: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not isinstance(result, MatchResumeToJdResult):
            return [], "MATCH_OUTPUT_INVALID"
        requirement_ids = {item.id for item in requirements}
        fact_ids = {item.id for item in evidence}
        valid_paths = set(_snapshot_text_paths(snapshot))
        category_map = {
            "direct": "proved",
            "transferable": "underexpressed",
            "needs_evidence": "needs_confirmation",
            "gap": "real_gap",
        }
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in result.matches:
            if item.requirement_ref not in requirement_ids or item.requirement_ref in seen:
                return [], "MATCH_REQUIREMENT_REFERENCE_INVALID"
            seen.add(item.requirement_ref)
            if any(fact_id not in fact_ids for fact_id in item.fact_refs):
                return [], "MATCH_FACT_REFERENCE_INVALID"
            if (
                len(set(item.resume_target_paths)) != len(item.resume_target_paths)
                or any(path not in valid_paths for path in item.resume_target_paths)
            ):
                return [], "MATCH_TARGET_PATH_INVALID"
            if item.category in {"transferable", "needs_evidence"} and not item.resume_target_paths:
                return [], "MATCH_TARGET_PATH_REQUIRED"
            rows.append(
                {
                    "requirement_id": item.requirement_ref,
                    "category": category_map[item.category],
                    "source_category": item.category,
                    "fact_refs": list(item.fact_refs),
                    "resume_target_paths": list(item.resume_target_paths),
                    "reason_code": item.reason_code,
                }
            )
        if seen != requirement_ids:
            return [], "MATCH_REQUIREMENT_COVERAGE_INVALID"
        return rows, None

    @staticmethod
    def _suggestion_payload(
        matches: list[dict[str, Any]],
        match_payload: MatchResumeToJdPayload,
        snapshot: dict[str, Any],
    ) -> GenerateSuggestionsBatchPayload:
        texts = _snapshot_text_paths(snapshot)
        sources: list[SuggestionSource] = []
        for item in matches:
            if item["source_category"] not in {"transferable", "needs_evidence"}:
                continue
            for path in item["resume_target_paths"]:
                original = texts[path]
                sources.append(
                    SuggestionSource(
                        requirement_ref=item["requirement_id"],
                        category=item["source_category"],
                        fact_refs=tuple(item["fact_refs"]),
                        target_path=path,
                        original_hash=_hash(original),
                        original_text=original,
                    )
                )
        return GenerateSuggestionsBatchPayload(
            matches=tuple(sources),
            confirmed_facts=match_payload.confirmed_facts,
            confirmed_requirements=match_payload.confirmed_requirements,
        )

    @staticmethod
    def _validated_suggestions(
        result: object,
        payload: GenerateSuggestionsBatchPayload,
        evidence: tuple[EvidenceFact, ...],
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not isinstance(result, GenerateSuggestionsBatchResult):
            return [], "SUGGESTION_OUTPUT_INVALID"
        expected = {
            (source.requirement_ref, source.target_path): source
            for source in payload.matches
        }
        projections = tuple(
            ConfirmedFactProjection(
                id=fact.id,
                value=fact.value,
                status="confirmed",
                source_hashes=fact.source_hashes,
            )
            for fact in evidence
        )
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in result.suggestions:
            key = (item.requirement_ref, item.target_path)
            source = expected.get(key)
            if source is None or key in seen:
                return [], "SUGGESTION_REFERENCE_INVALID"
            seen.add(key)
            if item.original_hash != source.original_hash:
                return [], "SUGGESTION_ORIGINAL_HASH_INVALID"
            if source.category == "needs_evidence" and item.proposed_status != "blocked":
                return [], "SUGGESTION_STATUS_INVALID"
            if source.category == "transferable" and item.proposed_status != "pending":
                return [], "SUGGESTION_STATUS_INVALID"
            checked = fact_policy_check(
                item.suggested_text,
                (
                    DraftClaim(
                        text=claim.text,
                        fact_refs=claim.fact_refs,
                        claim_order=claim.claim_order,
                    )
                    for claim in item.atomic_claims
                ),
                projections,
            )
            if checked.issues or (
                item.atomic_claims
                and len(checked.supported_claims) != len(item.atomic_claims)
            ):
                return [], "UNSUPPORTED_CLAIM"
            if item.proposed_status == "pending" and (
                not checked.supported_claims
                or not _claims_cover_text(item.suggested_text, checked.supported_claims)
            ):
                return [], "UNSUPPORTED_CLAIM"
            linked_fact_ids = tuple(
                dict.fromkeys(
                    fact_id
                    for claim in checked.supported_claims
                    for fact_id in claim.fact_refs
                )
            )
            if any(fact_id not in source.fact_refs for fact_id in linked_fact_ids):
                return [], "SUGGESTION_FACT_REFERENCE_INVALID"
            rows.append(
                {
                    "requirement_id": item.requirement_ref,
                    "target_path": item.target_path,
                    "original_hash": item.original_hash,
                    "original_text": source.original_text,
                    "suggested_text": item.suggested_text,
                    "reason": item.reason,
                    "risk_flags": list(item.risk_flags),
                    "status": item.proposed_status,
                    "claims": list(checked.supported_claims),
                    "fact_refs": list(linked_fact_ids),
                }
            )
        if seen != set(expected):
            return [], "SUGGESTION_COVERAGE_INVALID"
        return rows, None

    @staticmethod
    def _rule_fallback(
        requirements: list[JdRequirement],
        evidence: tuple[EvidenceFact, ...],
        snapshot: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        classified = classify_requirements(
            [{"id": item.id, "text": item.text_encrypted} for item in requirements],
            facts=tuple(item.value for item in evidence),
        )
        evidence_by_id = {item.id: item for item in evidence}
        paths = _snapshot_text_paths(snapshot)
        target_path = next(iter(paths), "")
        rows: list[dict[str, Any]] = []
        suggestions: list[dict[str, Any]] = []
        for item in classified:
            fact_refs = [
                evidence[int(reference.split(":", 1)[1])].id
                for reference in item.evidence_refs
                if int(reference.split(":", 1)[1]) < len(evidence)
            ]
            source_category = {
                "proved": "direct",
                "underexpressed": "transferable",
                "needs_confirmation": "needs_evidence",
                "real_gap": "gap",
            }[item.category]
            rows.append(
                {
                    "requirement_id": item.requirement_id,
                    "category": item.category,
                    "source_category": source_category,
                    "fact_refs": fact_refs,
                    "resume_target_paths": [target_path] if target_path else [],
                    "reason_code": "deterministic_token_overlap",
                }
            )
            if item.category not in {"underexpressed", "needs_confirmation"} or not target_path:
                continue
            original = paths[target_path]
            supported = item.category == "underexpressed" and bool(fact_refs)
            suggested = (
                evidence_by_id[fact_refs[0]].value if supported else original
            )
            suggestions.append(
                {
                    "requirement_id": item.requirement_id,
                    "target_path": target_path,
                    "original_hash": _hash(original),
                    "original_text": original,
                    "suggested_text": suggested,
                    "reason": "基于已确认事实进行确定性岗位关联整理",
                    "risk_flags": [] if supported else ["needs_confirmation"],
                    "status": "pending" if supported else "blocked",
                    "claims": [],
                    "fact_refs": fact_refs if supported else [],
                }
            )
        return rows, suggestions

    async def _publish_in_session(
        self,
        session: AsyncSession,
        analysis: MatchAnalysis,
        matches: list[dict[str, Any]],
        suggestions: list[dict[str, Any]],
        *,
        generation_mode: str,
        match_ai_run_id: str | None,
        match_input_hash: str,
        suggestion_ai_run_id: str | None,
        suggestion_input_hash: str,
        evidence: tuple[EvidenceFact, ...],
    ) -> None:
        await self._delete_public_rows(session, analysis)
        requirements = {
            row.id: row
            for row in (
                await session.scalars(
                    select(JdRequirement).where(
                        JdRequirement.job_id == analysis.job_id,
                        JdRequirement.owner_user_id == analysis.job_owner_user_id,
                    )
                )
            ).all()
        }
        for item in matches:
            requirement = requirements[item["requirement_id"]]
            session.add(
                MatchItem(
                    id=new_id("mit"),
                    owner_user_id=analysis.owner_user_id,
                    analysis_id=analysis.id,
                    requirement_id=requirement.id,
                    requirement_owner_user_id=requirement.owner_user_id,
                    category=item["category"],
                    evidence_refs=list(item["fact_refs"]),
                    resume_target_paths=list(item["resume_target_paths"]),
                    reason_code=item["reason_code"],
                    generation_mode=generation_mode,
                    workflow_version="2",
                    ai_run_id=match_ai_run_id,
                    input_hash=match_input_hash,
                )
            )
        evidence_by_id = {item.id: item for item in evidence}
        for item in suggestions:
            suggestion = Suggestion(
                id=new_id("sug"),
                owner_user_id=analysis.owner_user_id,
                analysis_id=analysis.id,
                target_path=item["target_path"],
                original_hash=item["original_hash"],
                original_text_encrypted=item["original_text"],
                suggested_encrypted=item["suggested_text"],
                requirement_id=item["requirement_id"],
                reason=item["reason"],
                risk_flags=list(item["risk_flags"]),
                status=item["status"],
                generation_mode=generation_mode,
                workflow_version="2",
                ai_run_id=suggestion_ai_run_id,
                input_hash=suggestion_input_hash,
            )
            session.add(suggestion)
            await session.flush()
            claim_by_fact: dict[str, dict[str, int]] = {}
            for claim in item["claims"]:
                for fact_id in claim.fact_refs:
                    claim_by_fact.setdefault(
                        fact_id, {"start": claim.start, "end": claim.end}
                    )
            for fact_id in item["fact_refs"]:
                if fact_id not in evidence_by_id:
                    raise RuntimeError("Validated suggestion fact is missing")
                session.add(
                    SuggestionFactLink(
                        suggestion_id=suggestion.id,
                        fact_id=fact_id,
                        owner_user_id=analysis.owner_user_id,
                        claim_range=claim_by_fact.get(
                            fact_id,
                            {"start": 0, "end": len(item["suggested_text"])},
                        ),
                    )
                )
        await session.flush()

    async def _locked_analysis(
        self, session: AsyncSession, owner_id: str, analysis_id: str
    ) -> MatchAnalysis:
        owners = await authorized_owner_ids(session, owner_id)
        analysis = await session.scalar(
            select(MatchAnalysis)
            .where(
                MatchAnalysis.id == analysis_id,
                MatchAnalysis.owner_user_id.in_(owners),
            )
            .with_for_update()
        )
        if analysis is None:
            raise MatchServiceError(
                "RESOURCE_NOT_FOUND", "Match analysis not found", 404
            )
        return analysis

    @staticmethod
    async def _receipt_task(
        session: AsyncSession,
        owner_id: str,
        task_id: str,
        claim_token: str | None,
        task_service: TaskService | None,
        receipt: AiExecutionReceipt,
    ) -> Any | None:
        if task_service is None or claim_token is None:
            return None
        return await task_service.ai_receipt_task_in_session(
            session,
            owner_id,
            task_id,
            claim_token,
            receipt.run.ai_run_id,
            receipt.run.status,
        )

    @staticmethod
    async def _validate_receipt(
        receipt: AiExecutionReceipt,
        *,
        task_id: str,
        trace_id: str,
        input_hash: str,
        workflow_type: str,
    ) -> None:
        if (
            receipt.run.task_id != task_id
            or receipt.run.trace_id != trace_id
            or receipt.run.input_hash != input_hash
            or receipt.run.workflow_type != workflow_type
            or receipt.run.workflow_version != "2"
        ):
            raise MatchServiceError(
                "AI_RECEIPT_MISMATCH",
                "AI receipt does not match the immutable match input",
                502,
            )

    @staticmethod
    async def _fail_in_session(
        session: AsyncSession,
        analysis: MatchAnalysis,
        task: Any,
        task_service: TaskService,
        error_code: str,
        *,
        release_unused_ai_reservation: bool = False,
    ) -> None:
        await MatchingService._delete_public_rows(session, analysis)
        analysis.status = "failed"
        if task.status != "cancelled":
            await task_service.fail_task_in_session(
                session,
                task,
                error_code,
                release_unused_ai_reservation=release_unused_ai_reservation,
            )
        await session.flush()

    async def _fail_without_receipt(
        self,
        owner_id: str,
        analysis_id: str,
        task_id: str,
        claim_token: str | None,
        task_service: TaskService | None,
        error_code: str,
    ) -> None:
        async with self.sessions.begin() as session:
            task = (
                await task_service.claimed_task_in_session(
                    session, owner_id, task_id, claim_token
                )
                if task_service is not None and claim_token is not None
                else None
            )
            analysis = await self._locked_analysis(session, owner_id, analysis_id)
            await self._delete_public_rows(session, analysis)
            analysis.status = "failed"
            if task is not None and task_service is not None:
                await task_service.fail_task_in_session(
                    session,
                    task,
                    error_code,
                    release_unused_ai_reservation=True,
                )

    @staticmethod
    async def _delete_public_rows(
        session: AsyncSession, analysis: MatchAnalysis
    ) -> None:
        suggestion_ids = list(
            (
                await session.scalars(
                    select(Suggestion.id).where(
                        Suggestion.analysis_id == analysis.id,
                        Suggestion.owner_user_id == analysis.owner_user_id,
                    )
                )
            ).all()
        )
        if suggestion_ids:
            await session.execute(
                delete(SuggestionFactLink).where(
                    SuggestionFactLink.suggestion_id.in_(suggestion_ids),
                    SuggestionFactLink.owner_user_id == analysis.owner_user_id,
                )
            )
            await session.execute(
                delete(Suggestion).where(
                    Suggestion.id.in_(suggestion_ids),
                    Suggestion.owner_user_id == analysis.owner_user_id,
                )
            )
        await session.execute(
            delete(MatchItem).where(
                MatchItem.analysis_id == analysis.id,
                MatchItem.owner_user_id == analysis.owner_user_id,
            )
        )

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _workflow_input_hash(
    workflow_type: str,
    prompt_template_version: str,
    payload: Any,
) -> str:
    snapshot = {
        "workflow_type": workflow_type,
        "workflow_version": "2",
        "prompt_template_version": prompt_template_version,
        "locale": "zh-CN",
        "input_version": 1,
        "payload": payload.model_dump(mode="json"),
    }
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_text_paths(snapshot: dict[str, Any]) -> dict[str, str]:
    paths: dict[str, str] = {}
    for section_index, section in enumerate(snapshot.get("sections", [])):
        if not isinstance(section, dict):
            continue
        for item_index, item in enumerate(section.get("items", [])):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                paths[f"/sections/{section_index}/items/{item_index}/text"] = item[
                    "text"
                ]
    return paths


def _claims_cover_text(text: str, claims: tuple[Any, ...]) -> bool:
    covered = [False] * len(text)
    for claim in claims:
        for index in range(claim.start, claim.end):
            covered[index] = True
    return all(
        is_covered or re.fullmatch(r"[\s,，。；;:：、.!！?？()（）\-—]", character)
        for character, is_covered in zip(text, covered, strict=True)
    )
