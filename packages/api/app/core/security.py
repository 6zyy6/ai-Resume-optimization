from fastapi import Request

from app.core.middleware import get_request_context


def get_actor_id(request: Request) -> str | None:
    return get_request_context(request).actor_id
