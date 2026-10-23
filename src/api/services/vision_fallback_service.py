"""视觉挂件：主模型不支持图片时，由管理员指定的视觉模型代为看图。

两个入口共用本服务：
- 用户上传图片：发送时为每张图生成完整描述（describe）。
- read_image_file：按主模型提出的问题回答（answer），未提问时返回完整描述。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter

from src.agent.llm import LLMClient
from src.agent.schema import Message
from src.agent.schema.run_context import current_run_context
from src.api.model_registry import ModelConfig

logger = logging.getLogger(__name__)

# 每张图回答的软上限（字）。read_image_file 据此估算一次能读几张图，两处必须同源。
VISION_ANSWER_MAX_CHARS = 2000

VISION_SYSTEM_PROMPT = f"""你是视觉助手，替一个不能直接看图的 AI 助手查看图片，并把图片内容如实转述给它。

规则：
1. 只转述图片中实际可见的内容，不推测图片之外的信息，不给建议或评价。
2. 图中文字逐字转录，保留原语言、数字、单位和符号；有层级的排版按原层级转录。
3. 表格转成 Markdown 表格。
4. 图表写明类型、标题、坐标轴与刻度、图例或系列；数据点用 Markdown 表格列出，三列为类别（年份、项目等）、系列（图例名）、数值，一行一个数据点；读不出的数值写"（不确定）"，不得按趋势推算或补齐。
5. 界面截图写明页面或窗口、主要区域、控件状态，弹窗和报错照录原文。
6. 看不清或无法确定的地方标注"（不确定）"，不要猜测。
7. 用中文回答，转录的原文保持原样。
8. 每张图片的回答不超过 {VISION_ANSWER_MAX_CHARS} 字；内容过多时优先保留文字、数值和结构，省略装饰性细节。"""


class VisionFallbackError(ValueError):
    """视觉挂件调用失败；message 面向用户与主模型，说明哪张图、什么原因。"""


@dataclass(frozen=True)
class VisionImage:
    name: str
    data_url: str


class VisionFallbackService:
    def __init__(self, config: ModelConfig):
        self.config = config
        self._client = LLMClient.from_model_config(config)

    async def describe(self, image: VisionImage, *, focus: str | None = None) -> str:
        """为单张图片生成完整描述；focus 为用户发送图片时附带的文字。"""
        instruction = f"请完整描述这张图片（文件名：{image.name}）。"
        if focus:
            instruction += (
                f"\n用户发送这张图片时说：「{focus}」。"
                "描述时优先覆盖与此相关的内容，但不要省略图片中的其他重要信息。"
            )
        return await self._ask([image], instruction, kind="describe")

    async def answer(self, images: list[VisionImage], question: str | None) -> str:
        """按问题回答一组图片；question 为空时依次完整描述。"""
        names = "、".join(image.name for image in images)
        if question:
            instruction = (
                f"图片按顺序为：{names}。\n请回答：{question}\n"
                "只依据图片内容回答；图片中找不到答案时直接说明。"
            )
        else:
            instruction = f"图片按顺序为：{names}。请依次完整描述每张图片，并标明对应文件名。"
        return await self._ask(images, instruction, kind="answer")

    async def _ask(self, images: list[VisionImage], instruction: str, *, kind: str) -> str:
        names = ", ".join(image.name for image in images)
        content: list[dict] = [
            {"type": "image_url", "image_url": {"url": image.data_url}} for image in images
        ]
        content.append({"type": "text", "text": instruction})
        started_at = perf_counter()
        # 挂件沿用自身目录配置：清掉当前 run 对主模型的推理覆盖（思考开关 / 推理等级）。
        run_context_token = current_run_context.set(None)
        try:
            response = await self._client.generate(messages=[
                Message(role="system", content=VISION_SYSTEM_PROMPT),
                Message(role="user", content=content),
            ])
        except Exception as exc:
            raise VisionFallbackError(
                f"视觉助手 {self.config.display_name} 识别图片 {names} 失败："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            current_run_context.reset(run_context_token)
        text = response.content.strip()
        usage = response.usage
        logger.info(
            "vision_fallback %s: model=%s images=%d prompt_tokens=%s completion_tokens=%s latency_s=%.2f",
            kind,
            self.config.id,
            len(images),
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
            perf_counter() - started_at,
        )
        if not text:
            raise VisionFallbackError(
                f"视觉助手 {self.config.display_name} 识别图片 {names} 失败：返回内容为空"
            )
        return text
