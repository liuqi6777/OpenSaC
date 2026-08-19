from __future__ import annotations

from fastapi import HTTPException


class SessionClosingError(RuntimeError):
    pass


class SessionCleanupError(RuntimeError):
    pass


class ExecIdConflictError(RuntimeError):
    pass


class ExecIndeterminateError(RuntimeError):
    pass


class WorkerDrainingError(RuntimeError):
    pass


class SessionCapacityError(RuntimeError):
    pass


class SessionCreateConflictError(RuntimeError):
    pass


class SessionLostError(RuntimeError):
    pass


class SessionExpiredError(RuntimeError):
    pass


def contract_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": retryable},
        headers=headers,
    )
