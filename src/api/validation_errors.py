"""Public request-validation errors that never echo request bodies."""

from __future__ import annotations

from typing import Any

from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse


_PUBLIC_ERROR_KEYS = ("type", "loc", "url")
_PUBLIC_NUMERIC_CONTEXT_KEYS = {
    "decimal_places",
    "max_digits",
    "max_length",
    "min_length",
}
_PUBLIC_LOCATION_ROOTS = {"body", "cookie", "header", "path", "query"}
# These names are consumed by the existing frontend to provide useful upload
# and message-length guidance. Other nested strings may be user-controlled map
# keys, so they are deliberately not reflected.
_PUBLIC_LOCATION_FIELDS = {"content", "file", "idempotency_key", "text"}


def _public_location(location: Any) -> list[str | int]:
    if not isinstance(location, (tuple, list)):
        return []
    result: list[str | int] = []
    for index, part in enumerate(location):
        if isinstance(part, int) and not isinstance(part, bool):
            result.append(part)
        elif index == 0 and str(part) in _PUBLIC_LOCATION_ROOTS:
            result.append(str(part))
        elif str(part) in _PUBLIC_LOCATION_FIELDS:
            result.append(str(part))
        else:
            result.append("[field]")
    return result


def _public_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Strip Pydantic's ``input``/``ctx`` fields before returning a 422.

    FastAPI's default handler includes the rejected input value.  That is
    particularly dangerous for credential-bearing MCP payloads because a
    malformed bearer token or custom header would otherwise be reflected into
    the response and commonly copied into browser/reverse-proxy logs.
    """

    public_errors: list[dict[str, Any]] = []
    for raw_error in exc.errors():
        public = {
            key: raw_error[key]
            for key in _PUBLIC_ERROR_KEYS
            if key in raw_error
        }
        if "loc" in raw_error:
            public["loc"] = _public_location(raw_error["loc"])
        # ``msg`` is also application-controlled for custom validators and can
        # accidentally interpolate a rejected value.  A deterministic public
        # message keeps field locations/types useful without trusting it.
        public["msg"] = "Request validation failed"
        raw_context = raw_error.get("ctx")
        if isinstance(raw_context, dict):
            numeric_context = {
                key: value
                for key, value in raw_context.items()
                if key in _PUBLIC_NUMERIC_CONTEXT_KEYS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            }
            if numeric_context:
                public["ctx"] = numeric_context
        public_errors.append(public)
    return public_errors


async def safe_request_validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": _public_validation_errors(exc)},
    )
