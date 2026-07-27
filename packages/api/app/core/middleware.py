from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.ids import new_id


@dataclass(frozen=True)
class RequestContext:
    trace_id: str
    request_id: str
    actor_id: str | None


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        context = RequestContext(
            trace_id=request.headers.get("X-Trace-Id") or new_id("tr"),
            request_id=request.headers.get("X-Request-Id") or new_id("req"),
            actor_id=None,
        )
        request.state.context = context
        response = await call_next(request)
        response.headers["X-Request-Id"] = context.request_id
        response.headers["X-Trace-Id"] = context.trace_id
        return response


def get_request_context(request: Request) -> RequestContext:
    return request.state.context
