"""Agent 核心類測試"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from src.agent.agent import Agent, Colors
from src.agent.tools.base import Tool, ToolResult
from src.agent.schema import Message
from tests.helpers import MockLLMClient, MockTool


class TestColors:
    """終端顏色類測試"""

    def test_color_codes_exist(self):
        """測試顏色代碼存在"""
        assert Colors.RESET == "\033[0m"
        assert Colors.BOLD == "\033[1m"
        assert Colors.RED == "\033[31m"
        assert Colors.GREEN == "\033[32m"
        assert Colors.BLUE == "\033[34m"

    def test_bright_colors(self):
        """測試亮色代碼"""
        assert Colors.BRIGHT_WHITE == "\033[97m"
        assert Colors.BRIGHT_CYAN == "\033[96m"


class TestAgent:
    """Agent 類測試"""

    @pytest.fixture
    def agent(self, mock_llm_client, mock_tool, tmp_path):
        """創建 Agent 實例"""
        return Agent(
            llm_client=mock_llm_client,
            system_prompt="You are a helpful assistant.",
            tools=[mock_tool],
            workspace_dir=str(tmp_path / "workspace"),
            max_steps=10,
        )

    def test_agent_initialization(self, agent, tmp_path):
        """測試 Agent 初始化"""
        assert agent.max_steps == 10
        assert agent.workspace_dir.exists()
        assert len(agent.tools) == 1
        assert "mock_tool" in agent.tools

    def test_system_prompt_injection(self, agent):
        """測試系統提示注入上下文"""
        # 確認系統消息是第一條
        assert agent.messages[0].role == "system"
        # 確認包含時間信息
        assert "当前日期" in agent.messages[0].content or "Current" in agent.messages[0].content

    def test_add_user_message(self, agent):
        """測試添加用戶消息"""
        initial_count = len(agent.messages)
        agent.add_user_message("Hello, Agent!")

        assert len(agent.messages) == initial_count + 1
        assert agent.messages[-1].role == "user"
        assert agent.messages[-1].content == "Hello, Agent!"

    def test_workspace_creation(self, mock_llm_client, mock_tool, tmp_path):
        """測試工作空間創建"""
        workspace_path = tmp_path / "new_workspace"
        agent = Agent(
            llm_client=mock_llm_client,
            system_prompt="Test",
            tools=[mock_tool],
            workspace_dir=str(workspace_path),
        )

        assert workspace_path.exists()

    def test_tools_dict(self, agent, mock_tool):
        """測試工具字典"""
        assert agent.tools["mock_tool"] == mock_tool

    def test_token_estimation_cache(self, agent):
        """測試 Token 計算緩存"""
        # 第一次計算
        tokens1 = agent._estimate_tokens()
        
        # 再次計算（應該使用緩存）
        tokens2 = agent._estimate_tokens()
        
        assert tokens1 == tokens2

    def test_token_estimation_recalculate(self, agent):
        """測試強制重新計算 Token"""
        tokens1 = agent._estimate_tokens()
        
        # 添加消息後強制重新計算
        agent.add_user_message("New message")
        tokens2 = agent._estimate_tokens(force_recalculate=True)
        
        # 新消息應該增加 Token 數
        assert tokens2 > tokens1

    def test_token_estimation_image_url_uses_fixed_value(self, agent):
        """測試含 image_url 的消息使用固定 token 值而非 base64 文本計算"""
        # 模擬一張大圖片（5MB base64）
        fake_base64 = "data:image/jpeg;base64," + "A" * (5 * 1024 * 1024)
        agent.add_user_message([
            {"type": "text", "text": "描述這張圖片"},
            {"type": "image_url", "image_url": {"url": fake_base64}},
        ])
        tokens = agent._estimate_tokens(force_recalculate=True)
        # 若 base64 被當文本計算，token 會超過 100 萬
        # 使用固定值（1000）後，總 token 應遠小於 10000
        assert tokens < 10000, f"Token 估算異常高: {tokens}，image_url 的 base64 可能被當文本計算"

    def test_token_estimation_video_url_uses_fixed_value(self, agent):
        """測試含 video_url 的消息使用固定 token 值"""
        agent.add_user_message([
            {"type": "text", "text": "描述此視頻"},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + "X" * 100000}},
        ])
        tokens = agent._estimate_tokens(force_recalculate=True)
        assert tokens < 20000, f"Token 估算異常高: {tokens}，video_url 可能被當文本計算"

    def test_token_estimation_fallback_image_url(self, agent):
        """測試 fallback 估算也正確處理 image_url"""
        fake_base64 = "data:image/jpeg;base64," + "A" * (5 * 1024 * 1024)
        agent.add_user_message([
            {"type": "image_url", "image_url": {"url": fake_base64}},
        ])
        tokens = agent._estimate_tokens_fallback()
        # fallback: 圖片用 2500 chars ÷ 2.5 = 1000 tokens
        assert tokens < 10000, f"Fallback token 估算異常高: {tokens}"

    def test_token_estimation_multiple_images(self, agent):
        """測試多張圖片的 token 估算"""
        blocks = [{"type": "text", "text": "看看這些圖片"}]
        for i in range(5):
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{'A' * 1000000}"},
            })
        agent.add_user_message(blocks)
        tokens = agent._estimate_tokens(force_recalculate=True)
        # 5 張圖 × 1000 tokens = 5000 + text tokens + system tokens < 10000
        assert tokens < 15000, f"多圖 token 估算異常: {tokens}"


class TestAgentWithMultipleTools:
    """多工具 Agent 測試"""

    @pytest.fixture
    def multi_tool_agent(self, tmp_path):
        """創建多工具 Agent"""
        tools = [
            MockTool("tool1"),
            MockTool("tool2"),
            MockTool("tool3"),
        ]
        
        return Agent(
            llm_client=MockLLMClient(),
            system_prompt="You have multiple tools.",
            tools=tools,
            workspace_dir=str(tmp_path),
        )

    def test_multiple_tools_registered(self, multi_tool_agent):
        """測試多個工具註冊"""
        assert len(multi_tool_agent.tools) == 3
        assert "tool1" in multi_tool_agent.tools
        assert "tool2" in multi_tool_agent.tools
        assert "tool3" in multi_tool_agent.tools

    def test_tool_name_uniqueness(self, multi_tool_agent):
        """測試工具名稱唯一性"""
        tool_names = list(multi_tool_agent.tools.keys())
        assert len(tool_names) == len(set(tool_names))



# NOTE: TestMessage 测试已统一到 test_schema.py，此处不再重复。
