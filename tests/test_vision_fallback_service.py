"""视觉挂件服务：请求构造、返回处理与失败语义。"""

from unittest.mock import AsyncMock

import pytest

from src.agent.schema import LLMResponse, TokenUsage
from src.agent.schema.run_context import (
    AgentRunContext,
    ResolvedReasoningContext,
    current_run_context,
)
from src.api.model_registry import ModelConfig
from src.api.services.vision_fallback_service import (
    VISION_SYSTEM_PROMPT,
    VisionFallbackError,
    VisionFallbackService,
    VisionImage,
)


def _vision_config() -> ModelConfig:
    return ModelConfig(
        id="vision-model",
        display_name="视觉模型",
        provider="openai",
        api_base="https://v.example.com/v1",
        api_key="test-key",
        model_name="v1",
        supports_image=True,
        max_images=4,
    )


def _service(generate: AsyncMock) -> VisionFallbackService:
    service = VisionFallbackService(_vision_config())
    service._client = type("Client", (), {"generate": generate})()
    return service


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


IMAGE_A = VisionImage(name="a.png", data_url="data:image/png;base64,AAAA")
IMAGE_B = VisionImage(name="b.png", data_url="data:image/png;base64,BBBB")


async def test_describe_sends_system_prompt_image_and_focus():
    generate = AsyncMock(return_value=_response("  一张折线图  "))

    text = await _service(generate).describe(IMAGE_A, focus="看看销量")

    assert text == "一张折线图"
    messages = generate.await_args.kwargs["messages"]
    assert messages[0].role == "system"
    assert messages[0].content == VISION_SYSTEM_PROMPT
    user_content = messages[1].content
    assert user_content[0] == {"type": "image_url", "image_url": {"url": IMAGE_A.data_url}}
    assert user_content[-1]["type"] == "text"
    assert "a.png" in user_content[-1]["text"]
    assert "看看销量" in user_content[-1]["text"]


async def test_answer_puts_question_after_all_images():
    generate = AsyncMock(return_value=_response("第三季度 42"))

    text = await _service(generate).answer([IMAGE_A, IMAGE_B], "第三季度是多少")

    assert text == "第三季度 42"
    user_content = generate.await_args.kwargs["messages"][1].content
    assert [block["type"] for block in user_content] == ["image_url", "image_url", "text"]
    assert "第三季度是多少" in user_content[-1]["text"]
    assert "a.png、b.png" in user_content[-1]["text"]


async def test_call_ignores_main_model_run_reasoning_override():
    """read_image_file 在主模型 run 内调用挂件时，主模型的思考开关 / 推理等级不得传给挂件。"""
    seen_contexts: list = []

    async def generate(**kwargs):
        seen_contexts.append(current_run_context.get())
        return _response("ok")

    main_run = AgentRunContext(reasoning=ResolvedReasoningContext(mode="enabled", effort="high"))
    token = current_run_context.set(main_run)
    try:
        await _service(AsyncMock(side_effect=generate)).describe(IMAGE_A)
        assert current_run_context.get() is main_run
    finally:
        current_run_context.reset(token)

    assert seen_contexts == [None]


async def test_provider_error_raises_vision_error_naming_images():
    generate = AsyncMock(side_effect=TimeoutError("read timed out"))

    with pytest.raises(VisionFallbackError) as exc_info:
        await _service(generate).answer([IMAGE_A, IMAGE_B], "q")

    message = str(exc_info.value)
    assert "视觉模型" in message
    assert "a.png, b.png" in message
    assert "TimeoutError" in message
    assert isinstance(exc_info.value.__cause__, TimeoutError)
