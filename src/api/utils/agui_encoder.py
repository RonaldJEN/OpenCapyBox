"""AG-UI 事件編碼工具

簡化版本：僅保留 EventEncoder 用於 SSE 數據編碼。
事件生成由 Agent 層的 event_emitter.py 負責。
"""

import json
from typing import Any, Dict

from src.agent.schema.agui_events import BaseEvent


class EventEncoder:
    """AG-UI 事件编解码器 - 负责 SSE 格式转换"""

    def encode(self, event: BaseEvent) -> str:
        """将 Pydantic 事件对象编码为 SSE 格式

        Args:
            event: 继承自 BaseEvent 的 Pydantic 对象

        Returns:
            SSE 格式字符串 "data: {...}\n\n"
        """
        # 使用 model_dump_json 确保正确处理复杂类型和别名
        # by_alias=True: 确保输出驼峰命名的字段 (如 threadId)
        # exclude_none=True: 排除空值字段，减少数据量
        json_str = event.model_dump_json(by_alias=True, exclude_none=True)
        return f"data: {json_str}\n\n"

    def encode_dict(self, event_dict: Dict[str, Any]) -> str:
        """将字典编码为 SSE 格式"""
        return f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n"


