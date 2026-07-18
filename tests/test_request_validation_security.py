from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from src.api.schemas.chat import SendMessageRequest
from src.api.schemas.mcp import UserMcpServerCreate
from src.api.validation_errors import safe_request_validation_exception_handler


def test_validation_error_never_echoes_mcp_credentials():
    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError,
        safe_request_validation_exception_handler,
    )

    @app.post("/mcp-validation-probe")
    async def probe(_payload: UserMcpServerCreate):
        return {"ok": True}

    secret = "SUPER_SECRET_MCP_BEARER"
    response = TestClient(app).post(
        "/mcp-validation-probe",
        json={
            "name": "probe",
            "url": "https://example.com/mcp",
            "auth_type": "none",
            "bearer_token": secret,
        },
    )

    assert response.status_code == 422
    assert secret not in response.text
    assert all("input" not in error for error in response.json()["detail"])
    assert all(
        not set(error.get("ctx", {})) - {"max_length", "min_length", "max_digits", "decimal_places"}
        for error in response.json()["detail"]
    )


def test_main_app_registers_safe_validation_handler():
    from src.api.main import app

    assert (
        app.exception_handlers[RequestValidationError]
        is safe_request_validation_exception_handler
    )


def test_validation_error_does_not_echo_user_controlled_mapping_keys():
    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError,
        safe_request_validation_exception_handler,
    )

    @app.post("/mapping-probe")
    async def probe(_payload: dict[str, int]):
        return {"ok": True}

    secret_key = "SUPER_SECRET_DYNAMIC_KEY"
    response = TestClient(app).post(
        "/mapping-probe",
        json={secret_key: "not-an-integer"},
    )

    assert response.status_code == 422
    assert secret_key not in response.text


def test_preferred_skill_key_location_and_length_context_remain_public():
    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError,
        safe_request_validation_exception_handler,
    )

    @app.post("/preferred-skill-validation-probe")
    async def probe(_payload: SendMessageRequest):
        return {"ok": True}

    response = TestClient(app).post(
        "/preferred-skill-validation-probe",
        json={
            "content": [{"type": "text", "text": "hello"}],
            "preferred_skill_keys": ["x" * 129],
        },
    )

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["type"] == "string_too_long"
    assert error["loc"] == ["body", "preferred_skill_keys", 0]
    assert error["ctx"] == {"max_length": 128}
