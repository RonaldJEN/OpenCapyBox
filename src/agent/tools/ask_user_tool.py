"""AskUserQuestionTool — 让 Agent 主动向用户提问

通过 AG-UI 的 Interrupt-Resume 机制实现 Human-in-the-Loop：
1. Agent 调用 ask_user → agent loop 拦截，发出 RUN_FINISHED(outcome="interrupt")
2. 前端渲染问题卡片 → 用户选择/输入答案
3. 前端 POST /resume → Agent 恢复执行，答案作为 tool_result 注入对话历史
"""

from typing import Any

from .base import Tool, ToolResult

# ask_user 的 tool name 常量，方便 agent.py 引用
ASK_USER_TOOL_NAME = "ask_user"


class AskUserQuestionTool(Tool):
    """向用户提出问题并等待回答

    此工具的 execute() 不会被正常调用 — agent 主循环在检测到 ask_user
    调用时会拦截并触发 AG-UI interrupt 流程。
    """

    @property
    def name(self) -> str:
        return ASK_USER_TOOL_NAME

    @property
    def description(self) -> str:
        return "Ask the user multiple-choice questions to gather information, clarify ambiguity, understand preferences, or make decisions. Only call ONCE per response — put all questions (up to 4) in one call."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Questions to ask the user (1-4 questions).",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question text to display.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Short label (≤20 chars) for the question tag.",
                                "maxLength": 20,
                            },
                            "options": {
                                "type": "array",
                                "description": "Available choices (2-4 options).",
                                "minItems": 2,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Display text for this option.",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Explanation of what this option means.",
                                        },
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Allow selecting multiple options.",
                                "default": False,
                            },
                        },
                        "required": ["question", "header", "options"],
                    },
                },
            },
            "required": ["questions"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        """防御性实现 — 正常情况下不应被调用。

        Agent 主循环在工具执行前拦截 ask_user 并触发 interrupt。
        如果意外到达这里，返回错误提示。
        """
        return ToolResult(
            success=False,
            error=(
                "ask_user should be intercepted by the agent loop. "
                "If you see this, the interrupt mechanism failed."
            ),
        )
