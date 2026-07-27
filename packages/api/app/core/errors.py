from typing import Any

from fastapi import HTTPException


def createApiError(
    code: str,
    message: str,
    request_id: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "details": details or {},
            }
        },
    )
