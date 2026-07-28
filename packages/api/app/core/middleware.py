from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.errors import createApiError
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


class CsrfProtectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and "session" in request.cookies
            and self._is_cross_site(request)
        ):
            error = createApiError(
                "CSRF_ORIGIN_FORBIDDEN",
                "Cross-site cookie-authenticated write is forbidden",
                get_request_context(request).request_id,
                403,
            )
            return JSONResponse(status_code=403, content=error.detail)
        return await call_next(request)

    @staticmethod
    def _is_cross_site(request: Request) -> bool:
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site.lower() not in {
            "same-origin",
            "same-site",
            "none",
        }:
            return True
        origin = request.headers.get("Origin")
        if origin is None:
            return False
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return True
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        target_scheme = (
            forwarded_proto
            if forwarded_proto in {"http", "https"}
            else request.url.scheme
        )
        try:
            target_port = request.url.port or (
                443 if target_scheme == "https" else 80
            )
            origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return True
        return (
            parsed.scheme != target_scheme
            or parsed.hostname.lower() != (request.url.hostname or "").lower()
            or origin_port != target_port
        )


def get_request_context(request: Request) -> RequestContext:
    return request.state.context
