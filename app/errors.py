"""One error shape for the whole API: {"error": {"code", "message", "details"}}."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

_STATUS_CODES = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed", 409: "conflict", 429: "rate_limited"}


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list | None = None, headers: dict | None = None):
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = details or [], headers


def _body(code: str, message: str, details: list | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or []}}


def install(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(_body(exc.code, exc.message, exc.details), exc.status, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        return JSONResponse(_body(code, str(exc.detail)), exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"][1:]) or str(e["loc"][0]), "message": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(_body("validation_error", "Request validation failed", details), 422)

    @app.exception_handler(RedisError)
    async def _redis_error(_: Request, exc: RedisError):
        log.error("redis unavailable: %s", exc)
        return JSONResponse(_body("dependency_unavailable", "Temporarily unavailable"), 503)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(_body("internal_error", "Internal server error"), 500)
