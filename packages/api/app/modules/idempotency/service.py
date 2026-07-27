import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.db.models import IdempotencyRecord


IDEMPOTENCY_TTL = timedelta(hours=24)


class IdempotencyConflict(ValueError):
    pass


def semantic_hash(body: Any) -> str:
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class IdempotencyService:
    async def replay(
        self,
        session: AsyncSession,
        owner_user_id: str,
        route: str,
        key: str,
        body: Any,
    ) -> tuple[int, dict[str, Any]] | None:
        row = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_user_id == owner_user_id,
                IdempotencyRecord.route == route,
                IdempotencyRecord.key == key,
            )
        )
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            await session.delete(row)
            await session.flush()
            return None
        if row.body_hash != semantic_hash(body):
            raise IdempotencyConflict
        return row.response_status or 200, row.response_json or {}

    async def store(
        self,
        session: AsyncSession,
        owner_user_id: str,
        route: str,
        key: str,
        body: Any,
        status: int,
        response: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            IdempotencyRecord(
                id=new_id("idem"),
                owner_user_id=owner_user_id,
                route=route,
                key=key,
                body_hash=semantic_hash(body),
                response_status=status,
                response_json=response,
                created_at=now,
                expires_at=now + IDEMPOTENCY_TTL,
            )
        )
        await session.flush()
