import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.db.models import Fact, FactRevision, FactSource, SourceRecord
from app.db.ownership import authorized_owner_ids, canonical_user_id
from app.modules.idempotency.service import IdempotencyConflict, IdempotencyService


@dataclass(frozen=True)
class FactError(Exception):
    code: str
    message: str
    status_code: int


class FactService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.idempotency = IdempotencyService()

    async def list_facts(self, owner_id: str) -> list[Fact]:
        async with self.sessions() as session:
            owners = await authorized_owner_ids(session, owner_id)
            return list((await session.scalars(select(Fact).where(Fact.owner_user_id.in_(owners)).order_by(Fact.created_at, Fact.id))).all())

    async def get_fact(self, owner_id: str, fact_id: str) -> Fact | None:
        async with self.sessions() as session:
            return await self._fact(session, owner_id, fact_id)

    async def create_fact(
        self,
        owner_id: str,
        *,
        kind: str,
        value: str,
        sources: list[dict[str, Any]],
        status: str = "unconfirmed",
        idempotency_key: str | None = None,
    ) -> Fact:
        body = {"kind": kind, "value": value, "sources": sources, "status": status}
        async with self.sessions.begin() as session:
            canonical = await canonical_user_id(session, owner_id)
            if idempotency_key:
                try:
                    replay = await self.idempotency.replay(session, canonical, "/v1/facts", idempotency_key, body)
                except IdempotencyConflict:
                    raise FactError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
                if replay:
                    result = await self._fact(session, owner_id, replay[1]["id"])
                    if result:
                        return result

            if status == "confirmed" and not sources:
                raise FactError("FACT_SOURCE_REQUIRED", "Confirmed facts require a source", 422)
            fact = Fact(id=new_id("fact"), owner_user_id=canonical, kind=kind, value_encrypted=value, status="unconfirmed")
            session.add(fact)
            await session.flush()
            await self._add_sources(session, fact, sources)
            if status == "confirmed":
                fact.status = "confirmed"
                fact.confirmed_at = datetime.now(timezone.utc)
                await session.flush()
            elif status == "rejected":
                fact.status = "rejected"
                await session.flush()
            if idempotency_key:
                await self.idempotency.store(session, canonical, "/v1/facts", idempotency_key, body, 201, {"id": fact.id})
            return fact

    async def update_fact(self, owner_id: str, fact_id: str, values: dict[str, Any], idempotency_key: str) -> Fact:
        async with self.sessions.begin() as session:
            canonical = await canonical_user_id(session, owner_id)
            try:
                replay = await self.idempotency.replay(session, canonical, f"/v1/facts/{fact_id}", idempotency_key, values)
            except IdempotencyConflict:
                raise FactError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if replay:
                result = await self._fact(session, owner_id, fact_id)
                if result:
                    return result
            fact = await self._fact(session, owner_id, fact_id)
            if fact is None:
                raise FactError("RESOURCE_NOT_FOUND", "Fact not found", 404)
            material_change = (
                values.get("value") is not None and values["value"] != fact.value_encrypted
            ) or (values.get("kind") is not None and values["kind"] != fact.kind)
            if values.get("value") is not None and values["value"] != fact.value_encrypted:
                session.add(FactRevision(id=new_id("frev"), fact_id=fact.id, owner_user_id=fact.owner_user_id, previous_value_hash=hashlib.sha256(fact.value_encrypted.encode()).hexdigest(), new_value_encrypted=values["value"], actor=canonical))
                fact.value_encrypted = values["value"]
            if values.get("kind") is not None:
                fact.kind = values["kind"]
            if material_change:
                fact.status = "unconfirmed"
                fact.confirmed_at = None
            await session.flush()
            await self.idempotency.store(session, canonical, f"/v1/facts/{fact_id}", idempotency_key, values, 200, {"id": fact.id})
            return fact

    async def set_status(self, owner_id: str, fact_id: str, status: str, idempotency_key: str) -> Fact:
        async with self.sessions.begin() as session:
            canonical = await canonical_user_id(session, owner_id)
            route = f"/v1/facts/{fact_id}/{status}"
            try:
                replay = await self.idempotency.replay(session, canonical, route, idempotency_key, {})
            except IdempotencyConflict:
                raise FactError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was reused with a different request", 409)
            if replay:
                result = await self._fact(session, owner_id, fact_id)
                if result:
                    return result
            fact = await self._fact(session, owner_id, fact_id)
            if fact is None:
                raise FactError("RESOURCE_NOT_FOUND", "Fact not found", 404)
            if status == "confirmed":
                has_source = await session.scalar(select(FactSource.fact_id).where(FactSource.fact_id == fact.id, FactSource.owner_user_id == fact.owner_user_id))
                if has_source is None:
                    raise FactError("FACT_SOURCE_REQUIRED", "Confirmed facts require a source", 422)
                fact.confirmed_at = datetime.now(timezone.utc)
            else:
                fact.confirmed_at = None
            fact.status = status
            await session.flush()
            await self.idempotency.store(session, canonical, route, idempotency_key, {}, 200, {"id": fact.id})
            return fact

    async def sources(self, owner_id: str, fact_id: str) -> list[SourceRecord] | None:
        async with self.sessions() as session:
            fact = await self._fact(session, owner_id, fact_id)
            if fact is None:
                return None
            return list((await session.scalars(select(SourceRecord).join(FactSource, FactSource.source_record_id == SourceRecord.id).where(FactSource.fact_id == fact.id, FactSource.owner_user_id == fact.owner_user_id))).all())

    async def source_ids(self, session: AsyncSession, fact: Fact) -> list[str]:
        return list((await session.scalars(select(FactSource.source_record_id).where(FactSource.fact_id == fact.id, FactSource.owner_user_id == fact.owner_user_id))).all())

    async def _fact(self, session: AsyncSession, owner_id: str, fact_id: str) -> Fact | None:
        owners = await authorized_owner_ids(session, owner_id)
        return await session.scalar(select(Fact).where(Fact.id == fact_id, Fact.owner_user_id.in_(owners)))

    async def _add_sources(self, session: AsyncSession, fact: Fact, sources: list[dict[str, Any]]) -> None:
        for item in sources:
            source = SourceRecord(id=new_id("src"), owner_user_id=fact.owner_user_id, source_type=item["source_type"], source_ref=item.get("source_ref"), content_encrypted=item["content"])
            session.add(source)
            await session.flush()
            session.add(FactSource(fact_id=fact.id, source_record_id=source.id, owner_user_id=fact.owner_user_id, source_range=item.get("source_range"), source_hash=hashlib.sha256(source.content_encrypted.encode()).hexdigest()))
        await session.flush()
