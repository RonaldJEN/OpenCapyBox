# 模型注册与切换 (Models) — Spec

## 1. 模块职责边界

- 模型注册表管理（models.yaml 驱动）
- 模型列表与详情查询（公开 API，无需鉴权）
- 模型行为配置（reasoning_format, reasoning_split, enable_thinking 等）
- Fallback 链配置与执行
- 不负责：LLM 调用实现、Token 计费、模型部署

## 2. 数据模型

无数据库表。模型配置存储在 `models.yaml` 文件中。

### ModelConfig 核心字段

| 字段 | 类型 | 说明 |
|------|------|------|
| id | str | 模型唯一标识 |
| name | str | 模型显示名称 |
| provider | str | 提供商标识（anthropic / openai） |
| api_key | str | API 密钥（敏感，不对外暴露） |
| api_base | str | API 基础 URL（敏感，不对外暴露） |
| supports_thinking | bool | 是否支持思维链 |
| supports_image | bool | 是否支持图片输入 |
| max_tokens | int | 最大生成 token 数 |
| max_images | int | 单次请求最大图片数 |
| tags | list[str] | 模型标签 |
| reasoning_format | str | 推理格式配置 |
| reasoning_split | str | 推理分隔符 |
| enable_thinking | bool | 是否启用思维链模式 |
| fallback | list[str] | Fallback 模型 ID 链 |
| token_limit | int | 传递给 Agent 上下文管理的 token 上限 |
| context_window | int | 上下文窗口大小 |
| max_output_tokens | int | 最大输出 token 数 |

### ModelRegistry

- 从 `models.yaml` 加载
- `get_model(model_id) -> ModelConfig`
- `list_models() -> list[ModelConfig]`
- `get_default_model() -> str`
- `cron_default_model` 支持
- `get_cron_default() -> ModelConfig`
- `get_subagent_default() -> ModelConfig`

## 3. API 契约

### `models.yaml` 顶层默认模型

| 字段 | 必填 | 说明 |
|------|------|------|
| `default_model` | 是 | 普通会话默认模型 |
| `cron_default_model` | 否 | Cron 任务默认模型；不配置时继承 `default_model` |
| `subagent_default_model` | 否 | `sub_agent` child Agent 与 Subagent graph 默认模型；不配置时继承 `default_model` |

**无需鉴权（公开端点）**

### GET /api/models

- **Response 200**:
  ```json
  {
    "models": [
      {
        "id": "...",
        "name": "...",
        "provider": "...",
        "supports_thinking": true,
        "max_tokens": 4096,
        "tags": ["..."]
      }
    ],
    "default_model": "model-id",
    "subagent_default_model": "model-id"
  }
  ```
- 排除敏感字段（api_key, api_base）
- **Error 500**: `"模型配置加载失败"`

### GET /api/models/{model_id}

- **Response 200**: model public dict（不含敏感字段）
- **Error 404**: `"模型 '{model_id}' 不存在或已停用。可用模型: [...]"`

## 4. 行为语义与不变量

### 配置驱动零硬编码

- 所有模型行为由 `models.yaml` 中的 `ModelConfig` 描述
- 代码中无模型名判断分支
- LLMClient 根据 `provider` 自动选择 AnthropicClient 或 OpenAIClient

### 默认模型消费方

| 字段 | 消费方 | 说明 |
|------|--------|------|
| `default_model` | 普通 Web 会话、未指定模型的新 Session | 用户对话主 Agent 的默认模型 |
| `cron_default_model` | Cron worker 创建的临时 Agent | 定时任务无人值守执行的默认模型；不通过 `/api/models` 暴露 |
| `subagent_default_model` | `AgentService` 的 subagent runner、`SubagentGraphService.create_edge()` | `sub_agent` child Agent run 和 graph 边的默认模型 |

`sub_agent` 创建 child Agent 时使用 `get_subagent_default()`，不继承父会话当前选择的模型，避免主 Agent 与子任务的模型职责混在一起。child Agent 的 LLM fallback 仍按 `AgentService.initialize_agent()` 的普通 Agent 初始化规则，从 registry 中其他启用模型构造 one-shot fallback 链。

### Fallback 机制

- `models.yaml` 中 `fallback` 字段定义备用模型链
- 主模型重试耗尽 → 通知回调 → 依次尝试 fallback 模型
- One-shot failover：不持久切换，下次调用仍尝试主模型
- Fallback client 缓存避免重复 HTTP 连接

### 重试策略 (RetryConfig)

| 参数 | 值 |
|------|-----|
| max_retries | 3 |
| initial_delay | 0.5s |
| max_delay | 30s |
| exponential_base | 2.0 |
| max_increment | 1.0s |

- 实际延迟序列: 0.5 → 1.0 → 1.5 → 2.0 → 2.5s
- Stream chunk timeout: 100s

### Token 估算

- 文本：chars / 2.5（约 tiktoken 90% 精度），有缓存
- 图片：1000 tokens / 张
- 视频：5000 tokens / 段

## 5. 失败模式与错误处理

| 场景 | 行为 |
|------|------|
| models.yaml 加载失败 | 返回 500 |
| 模型不存在/已停用 | 返回 404 + 可用模型列表 |
| 所有 fallback 耗尽 | 抛出 RetryExhaustedError |
| LLM stream chunk 超时 (100s) | 触发重试 |

## 6. 可观测性

- 启动时日志：已加载模型列表
- Fallback 切换日志
- 重试日志（含延迟）
- Stream 超时日志

## 7. 非目标

- 不做运行时动态添加/删除模型（需改 yaml 重启）
- 不做模型用量统计/计费
- 不做负载均衡
- 不做模型性能基准测试
- 不做 API Key 轮换
