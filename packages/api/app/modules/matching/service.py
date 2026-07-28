from __future__ import annotations

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
    VersionOperation,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.ai_client import AiClient
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
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
            version = await self._target_version(
                session,
                owner,
                version,
                job,
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
            facts = list(
                (
                    await session.scalars(
                        select(Fact).where(
                            Fact.owner_user_id.in_(owners),
                            Fact.status == "confirmed",
                        )
                    )
                ).all()
            )
            classified = classify_requirements(
                [
                    {"id": item.id, "text": item.text_encrypted}
                    for item in requirements
                ],
                facts=tuple(item.value_encrypted for item in facts),
            )
            analysis = MatchAnalysis(
                id=new_id("match"),
                owner_user_id=owner,
                resume_version_id=version.id,
                job_id=job.id,
                status="queued",
                workflow_version="match-resume-to-jd@1",
            )
            session.add(analysis)
            await session.flush()
            match_items: list[MatchItem] = []
            suggestions: list[Suggestion] = []
            target_path, original_text = _first_bullet(version.snapshot_json)
            for result, requirement in zip(classified, requirements, strict=True):
                fact_indexes = [
                    int(reference.split(":", 1)[1])
                    for reference in result.evidence_refs
                ]
                fact_refs = [
                    facts[index].id
                    for index in fact_indexes
                    if 0 <= index < len(facts)
                ]
                item = MatchItem(
                    id=new_id("mit"),
                    owner_user_id=owner,
                    analysis_id=analysis.id,
                    requirement_id=requirement.id,
                    category=result.category,
                    evidence_refs=fact_refs,
                )
                session.add(item)
                match_items.append(item)
                if result.category not in {"underexpressed", "needs_confirmation"}:
                    continue
                risk_flags = (
                    [] if result.category == "underexpressed" and fact_refs
                    else ["needs_confirmation"]
                )
                evidence_text = "、".join(
                    fact.value_encrypted
                    for fact in facts
                    if fact.id in fact_refs
                )
                suggested = (
                    f"{evidence_text}：{original_text}"
                    if evidence_text
                    else original_text
                )
                suggestion = Suggestion(
                    id=new_id("sug"),
                    owner_user_id=owner,
                    analysis_id=analysis.id,
                    target_path=target_path,
                    original_hash=_hash(original_text),
                    original_text_encrypted=original_text,
                    suggested_encrypted=suggested,
                    requirement_id=requirement.id,
                    reason="使用已确认经历加强与岗位要求的关联表达",
                    risk_flags=risk_flags,
                    status="pending" if target_path and not risk_flags else "blocked",
                )
                session.add(suggestion)
                await session.flush()
                for fact_id in fact_refs:
                    session.add(
                        SuggestionFactLink(
                            suggestion_id=suggestion.id,
                            fact_id=fact_id,
                            owner_user_id=owner,
                            claim_range={"start": 0, "end": len(suggested)},
                        )
                    )
                suggestions.append(suggestion)
            await session.flush()
            task = await task_service.create_task_in_session(
                session,
                owner,
                task_type="match_resume_to_job",
                queue="ai.batch",
                trace_id=trace_id,
                idempotency_key=f"match:{idempotency_key}",
                admission=TaskAdmission.ai(),
                resource_type="match_analysis",
                resource_id=analysis.id,
                payload={"analysis_id": analysis.id},
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
                match_items,
                suggestions,
                {
                    suggestion.id: [
                        link.fact_id
                        for link in session.new
                        if isinstance(link, SuggestionFactLink)
                        and link.suggestion_id == suggestion.id
                    ]
                    for suggestion in suggestions
                },
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
            return source_version
        if source_resume.kind != "base":
            raise MatchServiceError(
                "VALIDATION_FAILED",
                "Only base or job-targeted resumes can be matched",
                422,
            )
        targeted = await session.scalar(
            select(Resume).where(
                Resume.owner_user_id == source_resume.owner_user_id,
                Resume.kind == "job_targeted",
                Resume.base_resume_id == source_resume.id,
                Resume.job_description_id == job.id,
            )
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
    ) -> str:
        result = await self.get(owner_id, analysis_id)
        if result is None:
            raise MatchServiceError(
                "RESOURCE_NOT_FOUND", "Match analysis not found", 404
            )
        if result.analysis.status == "succeeded":
            return result.analysis.id
        if self.ai_client is not None:
            async with self.sessions() as session:
                owners = await authorized_owner_ids(session, owner_id)
                facts = list(
                    (
                        await session.scalars(
                            select(Fact).where(
                                Fact.owner_user_id.in_(owners),
                                Fact.status == "confirmed",
                            )
                        )
                    ).all()
                )
                requirements = list(
                    (
                        await session.scalars(
                            select(JdRequirement).where(
                                JdRequirement.job_id == result.analysis.job_id,
                                JdRequirement.owner_user_id.in_(owners),
                            )
                        )
                    ).all()
                )
                version = await session.scalar(
                    select(ResumeVersion).where(
                        ResumeVersion.id == result.analysis.resume_version_id,
                        ResumeVersion.owner_user_id.in_(owners),
                    )
                )
            if version is None:
                raise RuntimeError("Match resume version is missing")
            ai_result = await self.ai_client.run(
                workflow_type="match_resume_to_jd",
                workflow_version="1",
                trace_id=trace_id,
                task_id=task_id,
                facts=[
                    {
                        "id": fact.id,
                        "kind": fact.kind,
                        "value": fact.value_encrypted,
                        "status": fact.status,
                    }
                    for fact in facts
                ],
                input_data={
                    "resume_snapshot": version.snapshot_json,
                    "jd_requirements": [
                        {
                            "id": item.id,
                            "type": item.type,
                            "priority": item.priority,
                            "text": item.text_encrypted,
                        }
                        for item in requirements
                    ],
                },
            )
            matches = ai_result.get("result", ai_result).get("matches")
            if isinstance(matches, list):
                await self._apply_ai_matches(
                    owner_id, analysis_id, matches
                )
        async with self.sessions.begin() as session:
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
            analysis.status = "succeeded"
            await session.flush()
            return analysis.id

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
                            == analysis.owner_user_id,
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

    async def _apply_ai_matches(
        self,
        owner_id: str,
        analysis_id: str,
        matches: list[Any],
    ) -> None:
        category_map = {
            "direct": "proved",
            "transferable": "underexpressed",
            "needs_evidence": "needs_confirmation",
            "gap": "real_gap",
        }
        async with self.sessions.begin() as session:
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
            items = list(
                (
                    await session.scalars(
                        select(MatchItem).where(
                            MatchItem.analysis_id == analysis_id,
                            MatchItem.owner_user_id.in_(owners),
                        )
                    )
                ).all()
            )
            by_requirement = {item.requirement_id: item for item in items}
            facts = list(
                (
                    await session.scalars(
                        select(Fact).where(
                            Fact.owner_user_id == analysis.owner_user_id,
                            Fact.status == "confirmed",
                        )
                    )
                ).all()
            )
            facts_by_id = {fact.id: fact for fact in facts}
            requirements = list(
                (
                    await session.scalars(
                        select(JdRequirement).where(
                            JdRequirement.job_id == analysis.job_id,
                            JdRequirement.owner_user_id
                            == analysis.owner_user_id,
                        )
                    )
                ).all()
            )
            requirements_by_id = {
                requirement.id: requirement for requirement in requirements
            }
            version = await session.scalar(
                select(ResumeVersion).where(
                    ResumeVersion.id == analysis.resume_version_id,
                    ResumeVersion.owner_user_id == analysis.owner_user_id,
                )
            )
            if version is None:
                raise RuntimeError("Match resume version is missing")
            target_path, original_text = _first_bullet(version.snapshot_json)
            existing = list(
                (
                    await session.scalars(
                        select(Suggestion).where(
                            Suggestion.analysis_id == analysis.id,
                            Suggestion.owner_user_id
                            == analysis.owner_user_id,
                        )
                    )
                ).all()
            )
            by_suggestion_requirement = {
                row.requirement_id: row
                for row in existing
                if row.requirement_id is not None
            }
            for match in matches:
                if not isinstance(match, dict):
                    continue
                category = category_map.get(match.get("category"))
                if category is None:
                    continue
                for requirement_id in match.get("requirement_refs", []):
                    item = by_requirement.get(requirement_id)
                    if item is None:
                        continue
                    item.category = category
                    item.evidence_refs = [
                        fact_id
                        for fact_id in match.get("fact_refs", [])
                        if fact_id in facts_by_id
                    ]
            for item in items:
                suggestion = by_suggestion_requirement.get(item.requirement_id)
                if item.category not in {
                    "underexpressed",
                    "needs_confirmation",
                }:
                    if suggestion is not None:
                        await session.execute(
                            delete(SuggestionFactLink).where(
                                SuggestionFactLink.suggestion_id
                                == suggestion.id,
                                SuggestionFactLink.owner_user_id
                                == analysis.owner_user_id,
                            )
                        )
                        await session.delete(suggestion)
                    continue
                fact_refs = [
                    fact_id
                    for fact_id in item.evidence_refs
                    if fact_id in facts_by_id
                ]
                evidence_text = "、".join(
                    facts_by_id[fact_id].value_encrypted
                    for fact_id in fact_refs
                )
                suggested_text = (
                    f"{evidence_text}：{original_text}"
                    if evidence_text
                    else original_text
                )
                risk_flags = (
                    []
                    if item.category == "underexpressed" and fact_refs
                    else ["needs_confirmation"]
                )
                if suggestion is None:
                    suggestion = Suggestion(
                        id=new_id("sug"),
                        owner_user_id=analysis.owner_user_id,
                        analysis_id=analysis.id,
                        target_path=target_path,
                        original_hash=_hash(original_text),
                        original_text_encrypted=original_text,
                        suggested_encrypted=suggested_text,
                        requirement_id=item.requirement_id,
                        reason="使用已确认经历加强与岗位要求的关联表达",
                        risk_flags=risk_flags,
                        status=(
                            "pending"
                            if target_path and not risk_flags
                            else "blocked"
                        ),
                    )
                    session.add(suggestion)
                    await session.flush()
                else:
                    suggestion.suggested_encrypted = suggested_text
                    suggestion.risk_flags = risk_flags
                    suggestion.status = (
                        "pending"
                        if target_path and not risk_flags
                        else "blocked"
                    )
                    await session.execute(
                        delete(SuggestionFactLink).where(
                            SuggestionFactLink.suggestion_id == suggestion.id,
                            SuggestionFactLink.owner_user_id
                            == analysis.owner_user_id,
                        )
                    )
                for fact_id in fact_refs:
                    session.add(
                        SuggestionFactLink(
                            suggestion_id=suggestion.id,
                            fact_id=fact_id,
                            owner_user_id=analysis.owner_user_id,
                            claim_range={
                                "start": 0,
                                "end": len(suggested_text),
                            },
                        )
                    )
            await session.flush()


def _first_bullet(snapshot: dict[str, Any]) -> tuple[str, str]:
    for section_index, section in enumerate(snapshot.get("sections", [])):
        for item_index, item in enumerate(section.get("items", [])):
            if isinstance(item.get("text"), str):
                return (
                    f"/sections/{section_index}/items/{item_index}/text",
                    item["text"],
                )
    return "", ""


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()
