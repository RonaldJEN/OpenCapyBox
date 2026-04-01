"""测试 Agent 流式多轮对话（带工具调用）"""
import asyncio
from pathlib import Path
import tempfile

from src.agent.agent import Agent
from src.agent.llm import LLMClient
from src.agent.schema import LLMProvider
from src.agent.tools.glm_search_tool import GLMSearchTool
from src.api.config import get_settings
import os

async def test_agent_stream():
    """测试 Agent 流式多轮对话（带工具调用）"""
    # 加载配置
    settings = get_settings()
    print(settings)
    print("=" * 60)
    print("配置信息：")
    print(f"  LLM Provider: {settings.llm_provider}")
    print(f"  LLM API Base: {settings.llm_api_base}")
    print(f"  LLM Model: {settings.llm_model}")
    print(f"  LLM API Key: {settings.llm_api_key[:10]}..." if settings.llm_api_key else "  LLM API Key: 未设置")
    bocha_appcode = settings.bocha_search_appcode or os.getenv("BOCHA_SEARCH_APPCODE", "")
    print(f"  Bocha AppCode: {bocha_appcode[:10]}..." if bocha_appcode else "  Bocha AppCode: 未设置")
    print("=" * 60)
    print()
    
    # 检查 BOCHA_SEARCH_APPCODE
    if not bocha_appcode:
        print("❌ 错误：未配置 BOCHA_SEARCH_APPCODE，无法使用搜索工具")
        print("💡 请在 .env 文件中设置 BOCHA_SEARCH_APPCODE")
        return False
    
    # 根据配置确定 provider
    if settings.llm_provider.lower() == "openai":
        provider = LLMProvider.OPENAI
    else:
        provider = LLMProvider.ANTHROPIC
    
    # 创建 LLM 客户端
    llm_client = LLMClient(
        api_key=settings.llm_api_key,
        provider=provider,
        api_base=settings.llm_api_base,
        model=settings.llm_model,
    )
    
    # 创建真实的搜索工具
    search_tool = GLMSearchTool(api_key=bocha_appcode)
    
    # 创建临时工作目录
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"📁 临时工作目录: {temp_dir}")
        print()
        
        # 创建 Agent
        agent = Agent(
            llm_client=llm_client,
            system_prompt="你是一个有用的 AI 助手。你可以使用搜索工具来查找信息。",
            tools=[search_tool],
            max_steps=10,
            workspace_dir=temp_dir,
        )
        
        # 添加用户消息
        user_message = """2025/11/25最近在炒作谷歌产业链对标阿里的映射,因为两家都是云也有模型。
同时都有自研的芯片算力和toc客户端。你能帮我对比两家终端,code ide,各种toC的交互产品"""
        print(f"💬 用户消息: {user_message}")
        print()
        agent.add_user_message(user_message)
        
        # 统计信息
        total_content_chunks = 0
        total_thinking_chunks = 0
        total_tool_call_updates = 0
        step_data_list = []
        
        # 定义步骤回调
        last_content_len = 0
        last_thinking_len = 0
        current_step_num = 0
        
        async def step_callback(step_data: dict):
            """处理每个步骤的流式更新"""
            nonlocal total_content_chunks, total_thinking_chunks, total_tool_call_updates
            nonlocal last_content_len, last_thinking_len, current_step_num
            
            # 检测是否是新的步骤
            if step_data['step_number'] != current_step_num:
                current_step_num = step_data['step_number']
                last_content_len = 0
                last_thinking_len = 0
                print(f"\n{'─'*60}")
                print(f"💭 Step {current_step_num}/{agent.max_steps}")
                print(f"{'─'*60}\n")
            
            # 流式更新
            if step_data.get("status") == "streaming":
                # 流式打印思考内容
                if step_data.get("thinking"):
                    thinking = step_data["thinking"]
                    total_thinking_chunks += 1
                    if len(thinking) > last_thinking_len:
                        new_text = thinking[last_thinking_len:]
                        print(f"\033[90m{new_text}\033[0m", end="", flush=True)  # 灰色显示思考
                        last_thinking_len = len(thinking)
                
                # 流式打印助手内容
                if step_data.get("assistant_content"):
                    content = step_data["assistant_content"]
                    total_content_chunks += 1
                    if len(content) > last_content_len:
                        new_text = content[last_content_len:]
                        # 如果是第一次打印内容且之前有思考，先换行
                        if last_content_len == 0 and last_thinking_len > 0:
                            print("\n")
                        print(new_text, end="", flush=True)
                        last_content_len = len(content)
                
                # 统计工具调用更新
                if step_data.get("tool_calls"):
                    total_tool_call_updates += 1
            else:
                # 步骤完成
                step_data_list.append(step_data)
                
                # 如果有流式内容，先换行
                if last_content_len > 0 or last_thinking_len > 0:
                    print("\n")
                
                # 显示工具调用信息
                if step_data.get("tool_calls"):
                    print("\n🔧 工具调用:")
                    for tc in step_data["tool_calls"]:
                        # 确保 tc['input'] 是字符串类型再切片
                        input_str = str(tc['input']) if tc.get('input') else ""
                        print(f"   └─ {tc['name']}")
                        if len(input_str) > 100:
                            print(f"      参数: {input_str[:100]}...")
                        else:
                            print(f"      参数: {input_str}")
                
                # 显示工具结果
                if step_data.get("tool_results"):
                    print("\n✓ 工具结果:")
                    for tr in step_data["tool_results"]:
                        # 安全地获取输出或错误信息，确保是字符串类型
                        output = tr.get("output") or ""
                        error = tr.get("error") or ""
                        result_str = str(output) if output else str(error)
                        if len(result_str) > 200:
                            print(f"   {result_str[:200]}...")
                        else:
                            print(f"   {result_str}")
                
                print(f"\n✅ Step {step_data['step_number']} 完成\n")
        
        print("🚀 开始执行 Agent（流式多轮对话）...")
        print("=" * 60)
        print()
        
        try:
            # 执行 Agent
            result = await agent.run_with_steps(step_callback=step_callback)
            
            print()
            print("=" * 60)
            print("✅ Agent 执行完成！")
            print("=" * 60)
            print()
            print("📊 统计信息：")
            print(f"  总步骤数: {result['step_count']}")
            print(f"  状态: {result['status']}")
            print(f"  内容流式更新次数: {total_content_chunks}")
            print(f"  思考流式更新次数: {total_thinking_chunks}")
            print(f"  工具调用流式更新次数: {total_tool_call_updates}")
            print()
            print("📝 最终回答：")
            print(f"  {result['final_response'][:300]}...")
            print()
            
            return True
            
        except Exception as e:
            print("\n")
            print("=" * 60)
            print(f"❌ Agent 执行失败: {type(e).__name__}")
            print(f"错误信息: {str(e)}")
            print("=" * 60)
            import traceback
            traceback.print_exc()
            return False


async def main():
    """主函数"""
    print("\n" + "🧪" * 30)
    print("Agent 流式多轮对话测试（带工具调用）")
    print("🧪" * 30 + "\n")
    
    # 测试 Agent 流式生成
    result = await test_agent_stream()
    
    print("\n" + "=" * 60)
    if result:
        print("🎉 测试通过！")
    else:
        print("⚠️  测试失败")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())

