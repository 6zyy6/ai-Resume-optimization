from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import (
    BulletFactLink,
    Fact,
    FactSource,
    MatchAnalysis,
    Resume,
    ResumeVersion,
    Suggestion,
    SuggestionDecision,
    SuggestionFactLink,
    VersionOperation,
)
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService
from app.modules.resumes.service import canonical_snapshot
from app.modules.resumes.fact_policy import (
    ConfirmedFactProjection,
    DraftClaim,
    fact_policy_check,
)


@dataclass(frozen=True)
class SuggestionDecisionResult:
    status: str
    text: str
    version_id: str
    parent_version_id: str


class SuggestionConflict(ValueError):
    pass


@dataclass
class SuggestionServiceError(Exception):
    code: str
    message: str
    status_code: int


@dataclass(frozen=True)
class SavedSuggestionDecision:
    suggestion: Suggestion
    version: ResumeVersion
    decision: SuggestionDecision


def apply_suggestion_decision(
    *,
    suggestion: dict,
    decision: str,
    current_text: str,
    current_version_id: str,
    edited_text: str | None = None,
) -> SuggestionDecisionResult:
    if suggestion["status"] == "blocked" and decision in {"accept", "edit"}:
        raise SuggestionConflict("FACT_NOT_CONFIRMED")
    allowed = {
        "pending": {"accept", "edit", "ignore"},
        "blocked": {"ignore"},
        "accepted": {"revert"},
        "edited": {"revert"},
        "ignored": {"revert"},
    }
    if decision not in allowed.get(suggestion["status"], set()):
        raise SuggestionConflict("SUGGESTION_TRANSITION_INVALID")
    original = suggestion["original_text"]
    expected_hash = hashlib.sha256(original.encode()).hexdigest()
    provided_hash = suggestion["original_hash"]
    if provided_hash != expected_hash or (
        decision != "revert" and current_text != original
    ):
        raise SuggestionConflict("SUGGESTION_BASE_CONFLICT")
    if decision == "edit" and not edited_text:
        raise SuggestionConflict("SUGGESTION_EDIT_REQUIRED")
    text = {
        "accept": suggestion["suggested_text"],
        "edit": edited_text,
        "ignore": current_text,
        "revert": original,
    }[decision]
    return SuggestionDecisionResult(
        status={
            "accept": "accepted",
            "edit": "edited",
            "ignore": "ignored",
            "revert": "reverted",
        }[decision],
        text=text or "",
        version_id=new_id("rver"),
        parent_version_id=current_version_id,
    )


class SuggestionService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.idempotency = IdempotencyService()

    async def decide(
        self,
        owner_id: str,
        suggestion_id: str,
        decision: str,
        *,
        edited_text: str | None,
        idempotency_key: str,
    ) -> SavedSuggestionDecision:
        route = f"/v1/suggestions/{suggestion_id}/{decision}"
        body = {"edited_text": edited_text}
        async with self.idempotency.transaction(self.sessions) as session:
            owner = await canonical_user_id(session, owner_id)
            try:
                claim = await self.idempotency.claim(
                    session, owner, route, idempotency_key, body
                )
            except IdempotencyConflict as error:
                raise SuggestionServiceError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was reused with a different request",
                    409,
                ) from error
            if claim.is_replay:
                response = claim.replay_response or {}
                suggestion = await session.scalar(
                    select(Suggestion).where(
                        Suggestion.id == suggestion_id,
                        Suggestion.owner_user_id == owner,
                    )
                )
                version = await session.scalar(
                    select(ResumeVersion).where(
                        ResumeVersion.id == response["version_id"],
                        ResumeVersion.owner_user_id == owner,
                    )
                )
                saved_decision = await session.scalar(
                    select(SuggestionDecision).where(
                        SuggestionDecision.id == response["decision_id"],
                        SuggestionDecision.owner_user_id == owner,
                    )
                )
                if suggestion is None or version is None or saved_decision is None:
                    raise RuntimeError("Idempotent suggestion decision is missing")
                return SavedSuggestionDecision(suggestion, version, saved_decision)
            owners = await authorized_owner_ids(session, owner_id)
            suggestion = await session.scalar(
                select(Suggestion)
                .where(
                    Suggestion.id == suggestion_id,
                    Suggestion.owner_user_id.in_(owners),
                )
                .with_for_update()
            )
            if suggestion is None:
                raise SuggestionServiceError(
                    "RESOURCE_NOT_FOUND", "Suggestion not found", 404
                )
            if suggestion.status == "blocked" and decision in {"accept", "edit"}:
                raise SuggestionServiceError(
                    "FACT_NOT_CONFIRMED",
                    "Suggestion has claims requiring confirmation",
                    422,
                )
            allowed = {
                "pending": {"accept", "edit", "ignore"},
                "blocked": {"ignore"},
                "accepted": {"revert"},
                "edited": {"revert"},
                "ignored": {"revert"},
            }
            if decision not in allowed.get(suggestion.status, set()):
                raise SuggestionServiceError(
                    "SUGGESTION_TRANSITION_INVALID",
                    "Suggestion decision is invalid for its current status",
                    409,
                )
            if decision == "edit" and not edited_text:
                raise SuggestionServiceError(
                    "VALIDATION_FAILED", "Edited text is required", 422
                )
            analysis = await session.scalar(
                select(MatchAnalysis).where(
                    MatchAnalysis.id == suggestion.analysis_id,
                    MatchAnalysis.owner_user_id == suggestion.owner_user_id,
                )
            )
            if analysis is None:
                raise RuntimeError("Suggestion analysis is missing")
            if analysis.status != "succeeded":
                raise SuggestionServiceError(
                    "MATCH_ANALYSIS_NOT_READY",
                    "Match analysis is not ready for suggestion decisions",
                    409,
                )
            seed_version = await session.scalar(
                select(ResumeVersion).where(
                    ResumeVersion.id == analysis.resume_version_id,
                    ResumeVersion.owner_user_id == analysis.owner_user_id,
                )
            )
            if seed_version is None:
                raise RuntimeError("Suggestion resume version is missing")
            resume = await session.scalar(
                select(Resume)
                .where(
                    Resume.id == seed_version.resume_id,
                    Resume.owner_user_id == seed_version.owner_user_id,
                )
                .with_for_update()
            )
            if resume is None:
                raise RuntimeError("Suggestion resume is missing")
            current = (
                await session.scalar(
                    select(ResumeVersion).where(
                        ResumeVersion.id == resume.head_version_id,
                        ResumeVersion.owner_user_id == resume.owner_user_id,
                    )
                )
                if resume.head_version_id
                else seed_version
            )
            if current is None:
                raise RuntimeError("Current resume version is missing")
            snapshot = json.loads(json.dumps(current.snapshot_json))
            current_text = _pointer_get(snapshot, suggestion.target_path)
            if decision != "revert" and (
                hashlib.sha256(current_text.encode()).hexdigest()
                != suggestion.original_hash
            ):
                raise SuggestionServiceError(
                    "SUGGESTION_BASE_CONFLICT",
                    "Target text changed after suggestion generation",
                    409,
                )
            fact_rows, fact_projection, expected_fact_count = (
                await self._confirmed_facts(session, suggestion)
            )
            if decision in {"accept", "edit"} and (
                not fact_rows or len(fact_rows) != expected_fact_count
            ):
                raise SuggestionServiceError(
                    "FACT_NOT_CONFIRMED",
                    "Accepted text requires confirmed fact evidence",
                    422,
                )
            new_text = {
                "accept": suggestion.suggested_encrypted,
                "edit": edited_text,
                "ignore": current_text,
                "revert": suggestion.original_text_encrypted,
            }[decision]
            if decision in {"accept", "edit"}:
                checked = fact_policy_check(
                    new_text or "",
                    (
                        DraftClaim(
                            text=new_text or "",
                            fact_refs=tuple(fact.id for fact in fact_rows),
                            claim_order=0,
                        ),
                    ),
                    fact_projection,
                )
                if checked.issues or len(checked.supported_claims) != 1:
                    raise SuggestionServiceError(
                        "FACT_NOT_CONFIRMED",
                        "Suggested text contains claims not supported by confirmed facts",
                        422,
                    )
            _pointer_set(snapshot, suggestion.target_path, new_text or "")
            canonical, snapshot_hash = canonical_snapshot(snapshot)
            version = ResumeVersion(
                id=new_id("rver"),
                owner_user_id=resume.owner_user_id,
                resume_id=resume.id,
                parent_version_id=current.id,
                snapshot_json=canonical,
                snapshot_hash=snapshot_hash,
                created_by=owner,
            )
            session.add(version)
            await session.flush()
            await self._copy_evidence(session, current, version)
            if decision in {"accept", "edit"}:
                await self._link_suggestion_evidence(
                    session, suggestion, version, fact_rows, new_text or ""
                )
            resume.head_version += 1
            resume.head_version_id = version.id
            status = {
                "accept": "accepted",
                "edit": "edited",
                "ignore": "ignored",
                "revert": "reverted",
            }[decision]
            suggestion.status = status
            saved_decision = SuggestionDecision(
                id=new_id("sdec"),
                owner_user_id=resume.owner_user_id,
                suggestion_id=suggestion.id,
                decision=decision,
                edited_text_encrypted=edited_text,
                final_version_id=version.id,
                decided_at=datetime.now(timezone.utc),
            )
            session.add_all(
                [
                    saved_decision,
                    VersionOperation(
                        id=new_id("vop"),
                        owner_user_id=resume.owner_user_id,
                        version_id=version.id,
                        operation_type="save",
                        actor=owner,
                        metadata_json={
                            "source": "suggestion",
                            "suggestion_id": suggestion.id,
                            "decision": decision,
                        },
                    ),
                ]
            )
            await session.flush()
            response = {
                "suggestion_id": suggestion.id,
                "status": suggestion.status,
                "version_id": version.id,
                "decision_id": saved_decision.id,
            }
            await self.idempotency.complete(session, claim, 201, response)
            return SavedSuggestionDecision(suggestion, version, saved_decision)

    @staticmethod
    async def _confirmed_facts(
        session: AsyncSession,
        suggestion: Suggestion,
    ) -> tuple[list[Fact], tuple[ConfirmedFactProjection, ...], int]:
        links = list(
            (
                await session.scalars(
                    select(SuggestionFactLink).where(
                        SuggestionFactLink.suggestion_id == suggestion.id,
                        SuggestionFactLink.owner_user_id == suggestion.owner_user_id,
                    )
                )
            ).all()
        )
        facts: list[Fact] = []
        projections: list[ConfirmedFactProjection] = []
        for link in links:
            fact = await session.scalar(
                select(Fact).where(
                    Fact.id == link.fact_id,
                    Fact.owner_user_id == link.owner_user_id,
                    Fact.status == "confirmed",
                )
            )
            if fact is None:
                continue
            source_hashes = tuple(
                (
                    await session.scalars(
                        select(FactSource.source_hash).where(
                            FactSource.fact_id == fact.id,
                            FactSource.owner_user_id == fact.owner_user_id,
                        )
                    )
                ).all()
            )
            if not source_hashes:
                continue
            facts.append(fact)
            projections.append(
                ConfirmedFactProjection(
                    id=fact.id,
                    value=fact.value_encrypted,
                    status=fact.status,
                    source_hashes=source_hashes,
                )
            )
        return facts, tuple(projections), len(links)

    @staticmethod
    async def _copy_evidence(
        session: AsyncSession,
        source: ResumeVersion,
        target: ResumeVersion,
    ) -> None:
        rows = list(
            (
                await session.scalars(
                    select(BulletFactLink).where(
                        BulletFactLink.resume_version_id == source.id,
                        BulletFactLink.owner_user_id == source.owner_user_id,
                    )
                )
            ).all()
        )
        for row in rows:
            session.add(
                BulletFactLink(
                    resume_version_id=target.id,
                    bullet_id=row.bullet_id,
                    fact_id=row.fact_id,
                    claim_start=row.claim_start,
                    claim_end=row.claim_end,
                    owner_user_id=target.owner_user_id,
                    fact_owner_user_id=row.fact_owner_user_id,
                    claim_range=dict(row.claim_range),
                    fact_value_encrypted_at_link=row.fact_value_encrypted_at_link,
                    fact_status_at_link=row.fact_status_at_link,
                    fact_source_hashes_at_link=list(row.fact_source_hashes_at_link),
                )
            )
        await session.flush()

    @staticmethod
    async def _link_suggestion_evidence(
        session: AsyncSession,
        suggestion: Suggestion,
        version: ResumeVersion,
        facts: list[Fact],
        text: str,
    ) -> None:
        bullet_id = _pointer_bullet_id(version.snapshot_json, suggestion.target_path)
        if not bullet_id:
            return
        await session.execute(
            delete(BulletFactLink).where(
                BulletFactLink.resume_version_id == version.id,
                BulletFactLink.bullet_id == bullet_id,
                BulletFactLink.owner_user_id == version.owner_user_id,
            )
        )
        for fact in facts:
            source_hashes = list(
                (
                    await session.scalars(
                        select(FactSource.source_hash).where(
                            FactSource.fact_id == fact.id,
                            FactSource.owner_user_id == fact.owner_user_id,
                        )
                    )
                ).all()
            )
            session.add(
                BulletFactLink(
                    resume_version_id=version.id,
                    bullet_id=bullet_id,
                    fact_id=fact.id,
                    claim_start=0,
                    claim_end=len(text),
                    owner_user_id=version.owner_user_id,
                    fact_owner_user_id=fact.owner_user_id,
                    claim_range={"start": 0, "end": len(text)},
                    fact_value_encrypted_at_link=fact.value_encrypted,
                    fact_status_at_link=fact.status,
                    fact_source_hashes_at_link=source_hashes,
                )
            )
        await session.flush()


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise SuggestionServiceError(
            "SUGGESTION_TARGET_INVALID", "Suggestion target is invalid", 500
        )
    return [
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer[1:].split("/")
    ]


def _pointer_get(document: dict[str, Any], pointer: str) -> str:
    current: Any = document
    try:
        for part in _pointer_parts(pointer):
            current = current[int(part)] if isinstance(current, list) else current[part]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise SuggestionServiceError(
            "SUGGESTION_TARGET_INVALID", "Suggestion target no longer exists", 409
        ) from error
    if not isinstance(current, str):
        raise SuggestionServiceError(
            "SUGGESTION_TARGET_INVALID", "Suggestion target is not text", 409
        )
    return current


def _pointer_set(document: dict[str, Any], pointer: str, value: str) -> None:
    parts = _pointer_parts(pointer)
    current: Any = document
    try:
        for part in parts[:-1]:
            current = current[int(part)] if isinstance(current, list) else current[part]
        final = parts[-1]
        if isinstance(current, list):
            current[int(final)] = value
        else:
            current[final] = value
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise SuggestionServiceError(
            "SUGGESTION_TARGET_INVALID", "Suggestion target no longer exists", 409
        ) from error


def _pointer_bullet_id(snapshot: dict[str, Any], pointer: str) -> str | None:
    parts = _pointer_parts(pointer)
    if len(parts) < 5 or parts[-1] != "text":
        return None
    try:
        item_pointer = "/" + "/".join(parts[:-1])
        current: Any = snapshot
        for part in _pointer_parts(item_pointer):
            current = current[int(part)] if isinstance(current, list) else current[part]
        return current.get("id") if isinstance(current, dict) else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
