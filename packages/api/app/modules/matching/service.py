from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import (
    Fact,
    JdRequirement,
    JobDescription,
    MatchAnalysis,
    MatchItem,
    ResumeVersion,
    Suggestion,
    SuggestionFactLink,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.integrations.ai_client import AiClient
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService


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
            await self.idempotency.complete(
                session,
                claim,
                202,
                {"id": analysis.id, "status": analysis.status},
            )
            return MatchAnalysisResult(analysis, match_items, suggestions)

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
                version = await session.get(
                    ResumeVersion, result.analysis.resume_version_id
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
        return MatchAnalysisResult(analysis, items, suggestions)

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
                    item.evidence_refs = list(match.get("fact_refs", []))
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
