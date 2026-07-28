from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BulletFactLink, ResumeVersion


@dataclass(frozen=True)
class PersistedFactEvidence:
    fact_id: str
    owner_user_id: str
    value_encrypted: str
    status: str
    source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class PersistedClaimEvidence:
    bullet_id: str
    start: int
    end: int
    facts: tuple[PersistedFactEvidence, ...]


@dataclass(frozen=True)
class VersionEvidenceProjection:
    version_id: str
    owner_user_id: str
    snapshot: dict[str, Any]
    claims: tuple[PersistedClaimEvidence, ...]


async def load_version_evidence(
    session: AsyncSession,
    version: ResumeVersion,
) -> VersionEvidenceProjection:
    rows = (
        await session.scalars(
            select(BulletFactLink).where(
                BulletFactLink.resume_version_id == version.id,
                BulletFactLink.owner_user_id == version.owner_user_id,
            )
        )
    ).all()
    grouped: dict[tuple[str, int, int], list[PersistedFactEvidence]] = {}
    for row in rows:
        key = (row.bullet_id, row.claim_start, row.claim_end)
        grouped.setdefault(key, []).append(
            PersistedFactEvidence(
                fact_id=row.fact_id,
                owner_user_id=row.fact_owner_user_id,
                value_encrypted=row.fact_value_encrypted_at_link,
                status=row.fact_status_at_link,
                source_hashes=tuple(row.fact_source_hashes_at_link),
            )
        )
    claims = tuple(
        PersistedClaimEvidence(
            bullet_id=bullet_id,
            start=start,
            end=end,
            facts=tuple(sorted(facts, key=lambda fact: fact.fact_id)),
        )
        for (bullet_id, start, end), facts in sorted(grouped.items())
    )
    return VersionEvidenceProjection(
        version_id=version.id,
        owner_user_id=version.owner_user_id,
        snapshot=version.snapshot_json,
        claims=claims,
    )
