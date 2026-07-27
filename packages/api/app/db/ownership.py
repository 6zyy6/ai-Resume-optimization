from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserAlias


async def canonical_user_id(session: AsyncSession, user_id: str) -> str:
    current = user_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        alias = await session.get(UserAlias, current)
        if alias is None:
            return current
        current = alias.canonical_user_id
    raise ValueError("user alias cycle")


async def authorized_owner_ids(
    session: AsyncSession,
    user_id: str,
) -> tuple[str, ...]:
    canonical = await canonical_user_id(session, user_id)
    owners = {canonical}
    frontier = {canonical}
    while frontier:
        aliases = set(
            await session.scalars(
                select(UserAlias.alias_user_id).where(
                    UserAlias.canonical_user_id.in_(frontier)
                )
            )
        )
        frontier = aliases - owners
        owners.update(frontier)
    return (canonical, *sorted(owners - {canonical}))
