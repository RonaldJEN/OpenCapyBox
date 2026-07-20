# 模型注册与切换 (Models) — Spec

## 1. 模块职责边界

- 数据库驱动的模型目录管理：`llm_models` 是运行时模型配置事实源。
- 运行时优先从 DB 加载模型目录；DB 目录为空/不可用或显式传入 `yaml_path` 的测试/工具场景仍支持 fallback 到 `models.yaml`。
- `models.yaml` 负责首次 seed 模型目录，并继续承载 embedding 模型配置。
- 模型列表与详情查询必须按当前用户的模型权限过滤。
- 模型行为配置：`reasoning_format`、`reasoning_split`、`enable_thinking`、多模态能力、上下文窗口与输出上限。
- 对外 `supports_thinking` 表示当前模型变体实际启用且可展示的思考能力：`reasoning_format=none` 为 false；OpenAI 变体还须启用运行时实际用于拆分思考内容的 `reasoning_split`。单独设置 `enable_thinking` 不足以声明支持，因此存量目录中的 No Thinking 变体即使仍保留旧 reasoning format，也不得在前端宣称支持思考。
- 默认模型配置：普通对话、Cron、Subagent 分别有默认模型；Cron/Subagent 未显式配置时继承普通默认模型。
- 模型权限包：默认权限包自动应用给所有普通用户，管理员可为用户额外绑定权限包。
- 不负责：LLM 调用实现、Token 计费、模型部署。

## 2. 数据模型

### `llm_models`

运行时模型目录。管理员通过管理后台创建、更新、停用或删除模型；`ModelRegistry` 从 DB 加载为 `ModelConfig`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `model_id` | str | 模型唯一标识 |
| `display_name` | str | 模型显示名称 |
| `provider` | str | SDK 协议（`anthropic` / `openai`） |
| `api_base` | str | API 基础 URL（管理端可见，普通 API 不返回） |
| `api_key` | str | API Key 或 `${ENV_VAR}` 引用（不对外明文暴露） |
| `model_name` | str | 发送给供应商 API 的实际模型名 |
| `max_tokens` | int | 最大输出 token 数 |
| `context_window` | int | 上下文窗口大小 |
| `reasoning_format` | str | 推理格式配置 |
| `reasoning_split` | bool | 是否发送 `reasoning_split` |
| `enable_thinking` | bool | 是否启用思维链模式 |
| `supports_image` / `max_images` | bool / int | 图片输入能力与上限 |
| `supports_video` / `max_videos` | bool / int | 视频输入能力与上限 |
| `enabled` | bool | 是否可被用户选择 |
| `tags_json` | json | 前端标签 |

### `llm_model_settings`

单例配置行（`id=1`）：

| 字段 | 说明 |
|------|------|
| `default_model_id` | 普通 Web 会话默认模型 |
| `cron_default_model_id` | Cron 默认模型；为空时使用普通默认模型 |
| `subagent_default_model_id` | Subagent 默认模型；为空时使用普通默认模型 |

### 模型权限表

- `model_permission_groups`：权限包，`is_default=true` 的默认权限包自动应用给所有普通用户。
- `model_permission_group_models`：权限包包含的模型，模型必须存在且启用。
- `user_model_permission_groups`：用户额外绑定的权限包；默认权限包不得手动绑定。

## 3. API 契约

### GET /api/models

- 鉴权：需要当前登录用户。
- 响应只返回当前用户可访问且启用的模型；管理员可访问全部启用模型。
- `default_model` / `subagent_default_model` 必须落在当前用户可访问模型集合内；若全局默认不可访问，则回退到该用户可访问列表的第一个模型。
- 响应不包含 `api_key`、`api_base` 等敏感字段。

```json
{
  "models": [
    {
      "id": "model-id",
      "name": "Display Name",
      "provider": "openai",
      "supports_thinking": true,
      "supports_image": false,
      "max_images": 0,
      "supports_video": false,
      "max_videos": 0,
      "max_tokens": 16384,
      "context_window": 128000,
      "enabled": true,
      "tags": ["fast"]
    }
  ],
  "default_model": "model-id",
  "subagent_default_model": "model-id"
}
```

### GET /api/models/{model_id}

- 鉴权：需要当前登录用户。
- 只能查询当前用户可访问的模型；普通用户无权访问时返回 403。
- 模型不存在或停用返回 404。

### 管理端模型 API

管理端模型目录、默认模型、权限包与用户权限绑定统一由 Admin API 提供，见 [admin-spec.md](./admin-spec.md) 的模型管理部分。

## 4. 行为语义与不变量

### 配置驱动零硬编码

- 所有模型行为由 DB 中的 `LLMModel` / `ModelConfig` 描述。
- 代码中不得按模型名写特殊判断分支。
- `LLMClient` 根据 `provider` 自动选择 AnthropicClient 或 OpenAIClient。
- 管理端模型创建/更新/删除、默认模型变更、权限包模型变更后，必须触发 `reload_model_registry()`。

### 启动 seed

- 当 `llm_models` 为空时，从 `models.yaml` 导入初始模型，并把启用模型加入默认权限包。
- `models.yaml` 中的 `default_model`、`cron_default_model`、`subagent_default_model` 会写入 `llm_model_settings`。
- DB 已有模型时，不再用 `models.yaml` 覆盖运行时目录。

### 默认模型消费方

| 字段 | 消费方 | 说明 |
|------|--------|------|
| `default_model_id` | 普通 Web 会话、未指定模型的新 Session | 用户对话主 Agent 的默认模型 |
| `cron_default_model_id` | Cron worker 创建的临时 Agent | 定时任务无人值守执行的默认模型；不通过 `/api/models` 暴露 |
| `subagent_default_model_id` | `AgentService` 的 subagent runner、`SubagentGraphService.create_edge()` | `sub_agent` child Agent run 和 graph 边的默认模型 |

`sub_agent` 创建 child Agent 时使用 `get_subagent_default()`，不继承父会话当前选择的模型，避免主 Agent 与子任务的模型职责混在一起。

### Fallback 机制

- Fallback 链由当前 registry 中其他启用模型自动生成，不再由 `models.yaml` 的模型字段单独配置。
- Fallback 候选会排除当前主模型，并且必须保持多模态能力不降级（例如主模型支持图片/视频时，fallback 也必须满足同等能力与数量上限）。
- One-shot failover：不持久切换，下次调用仍尝试主模型。
- Fallback client 缓存避免重复 HTTP 连接。

### 权限语义

- 管理员可使用全部启用模型。
- 普通用户拥有默认权限包 + 额外绑定权限包中的启用模型。
- 用户没有任何可用模型时，`GET /api/models` 返回 403，并提示联系管理员配置模型权限。
- Session 创建、模型切换、Agent 初始化前都必须校验当前用户是否可访问目标模型。

## 5. 失败模式与错误处理

| 场景 | 行为 |
|------|------|
| DB 模型目录为空且 `models.yaml` 加载失败 | 启动 seed 失败，模型注册不可用 |
| 模型不存在或停用 | 返回 404 |
| 当前用户无权访问模型 | 返回 403 |
| 当前用户无可用模型 | 返回 403 |
| 管理端创建重复模型 ID | 返回 409 |
| 默认模型指向停用模型 | 返回 400 |
| 删除被默认模型或历史 Session 使用的模型且未提供替换模型 | 返回 400/409 |
| 所有 fallback 耗尽 | 抛出 `RetryExhaustedError` |
| LLM stream chunk 超时 | 触发重试 |

## 6. 可观测性

- 启动时日志：seed 结果、已加载模型列表、默认模型。
- 管理端模型变更日志与 registry reload 日志。
- Fallback 切换日志。
- 重试日志（含延迟）。
- Stream 超时日志。

## 7. 非目标

- 不做模型用量统计/计费。
- 不做负载均衡。
- 不做模型性能基准测试。
- 不做 API Key 明文回显或轮换审计。
