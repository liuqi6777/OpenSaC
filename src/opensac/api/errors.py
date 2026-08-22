from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse

from opensac.broker.policy import BudgetExceeded


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


@dataclass(frozen=True)
class ErrorContract:
    status_code: int
    code: str
    retryable: bool
    headers: dict[str, str] | None = None


ERROR_CONTRACTS: dict[type[Exception], ErrorContract] = {
    WorkerDrainingError: ErrorContract(503, "worker_draining", True),
    SessionCapacityError: ErrorContract(
        429,
        "capacity_exhausted",
        True,
        headers={"Retry-After": "1"},
    ),
    SessionCreateConflictError: ErrorContract(409, "session_request_conflict", False),
    SessionLostError: ErrorContract(410, "worker_restarted", False),
    SessionExpiredError: ErrorContract(410, "session_expired", False),
    SessionClosingError: ErrorContract(409, "session_closing", False),
    SessionCleanupError: ErrorContract(503, "cleanup_failed", True),
    ExecIdConflictError: ErrorContract(409, "exec_id_conflict", False),
    ExecIndeterminateError: ErrorContract(409, "exec_indeterminate", False),
    BudgetExceeded: ErrorContract(409, "budget_exhausted", False),
}


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


async def _contract_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    contract = ERROR_CONTRACTS[type(exc)]
    return await http_exception_handler(
        request,
        contract_error(
            contract.status_code,
            contract.code,
            str(exc),
            retryable=contract.retryable,
            headers=contract.headers,
        ),
    )


def install_exception_handlers(app: FastAPI) -> None:
    for error_type in ERROR_CONTRACTS:
        app.add_exception_handler(error_type, _contract_exception_handler)
