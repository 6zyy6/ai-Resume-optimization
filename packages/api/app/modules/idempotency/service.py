import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import IdempotencyRecord, User
from app.db.ownership import authorized_owner_ids, canonical_user_id


IDEMPOTENCY_TTL = timedelta(hours=24)


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class IdempotencyClaim:
    row: IdempotencyRecord
    replay_status: int | None = None
    replay_response: dict[str, Any] | None = None

    @property
    def is_replay(self) -> bool:
        return self.replay_response is not None


def semantic_hash(body: Any) -> str:
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class IdempotencyService:
    @asynccontextmanager
    async def transaction(
        self,
        sessions: async_sessionmaker[AsyncSession],
    ) -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                await session.begin()
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def claim(
        self,
        session: AsyncSession,
        owner_user_id: str,
        route: str,
        key: str,
        body: Any,
    ) -> IdempotencyClaim:
        canonical = await canonical_user_id(session, owner_user_id)
        await session.scalar(
            select(User).where(User.id == canonical).with_for_update()
        )
        owners = await authorized_owner_ids(session, canonical)
        body_hash = semantic_hash(body)
        row = await session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.owner_user_id.in_(owners),
                IdempotencyRecord.route == route,
                IdempotencyRecord.key == key,
            )
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if row is not None and _as_utc(row.expires_at) <= now:
            await session.delete(row)
            await session.flush()
            row = None
        if row is not None:
            if row.body_hash != body_hash:
                raise IdempotencyConflict
            if row.response_status is None or row.response_json is None:
                raise RuntimeError("idempotency claim committed without a response")
            return IdempotencyClaim(
                row,
                row.response_status,
                _json_copy(row.response_json),
            )
        row = IdempotencyRecord(
            id=new_id("idem"),
            owner_user_id=canonical,
            route=route,
            key=key,
            body_hash=body_hash,
            response_status=None,
            response_json=None,
            created_at=now,
            expires_at=now + IDEMPOTENCY_TTL,
        )
        session.add(row)
        await session.flush()
        return IdempotencyClaim(row)

    async def recheck(
        self,
        session: AsyncSession,
        claim: IdempotencyClaim,
    ) -> tuple[int, dict[str, Any]] | None:
        if claim.is_replay:
            return claim.replay_status or 200, _json_copy(claim.replay_response or {})
        row = await session.scalar(
            select(IdempotencyRecord)
            .where(IdempotencyRecord.id == claim.row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is not None and row.response_status is not None and row.response_json is not None:
            return row.response_status, _json_copy(row.response_json)
        return None

    async def complete(
        self,
        session: AsyncSession,
        claim: IdempotencyClaim,
        status: int,
        response: dict[str, Any],
    ) -> None:
        claim.row.response_status = status
        claim.row.response_json = _json_copy(response)
        await session.flush()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
