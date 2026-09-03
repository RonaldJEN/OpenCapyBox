# 模型注册与切换 (Models) — Spec

## 1. 模块职责边界

- 数据库驱动的模型目录管理：`llm_models` 是运行时模型配置事实源。
- 运行时模型目录只从 DB 加载；仅当模型表完全为空或显式传入 `yaml_path` 的测试/工具场景，才使用 `models.yaml`。
- DB 连接或查询失败必须直接暴露，禁止静默切换到 YAML；只有成功确认模型表为空才允许 YAML 初始化路径。
- `models.yaml` 负责首次 seed 模型目录，并继续承载 embedding 模型配置。
- 模型列表与详情查询必须按当前用户的模型权限过滤。
- 模型行为配置：`openai_protocol`、`reasoning_format`、独立思考内容能力标记 `reasoning_split`、三态 `thinking_mode`、`thinking_wire_format`、`reasoning_effort`、多模态能力、上下文窗口与输出上限。
- 对外 `supports_thinking` 表示目录配置能够展示思考内容：`reasoning_format=none` 或默认显式关闭时为 false；Anthropic 模型在其余情况下为 true；OpenAI 兼容模型还必须满足 `reasoning_split=true`、默认显式开启，或 `supported_reasoning_efforts` 至少包含一个非 `off` 等级。`reasoning_split` 只表示网关能返回独立思考内容，不参与 Responses 请求编码。
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
| `openai_protocol` | str/null | OpenAI API 协议：`responses` 或 `chat_completions`；仅对 `provider=openai` 有效 |
| `api_base` | str | API 基础 URL（管理端可见，普通 API 不返回） |
| `api_key` | str | API Key 或 `${ENV_VAR}` 引用（不对外明文暴露） |
| `model_name` | str | 发送给供应商 API 的实际模型名 |
| `max_tokens` | int | 最大输出 token 数 |
| `context_window` | int | 上下文窗口大小 |
| `auto_compact_token_limit` | int/null | 可选自动压缩阈值，最终不得超过 `(context_window - max_tokens) * 80%` |
| `tool_output_truncation_bytes` | int | 通用工具结果记录截断策略，必须大于 0，默认 42667 bytes；按 1.2 倍序列化余量得到 51200 UTF-8 bytes 正文预算；自行严格限流的内建工具成功结果可豁免 |
| `reasoning_format` | str | 推理格式配置 |
| `reasoning_split` | bool | 目录能力标记：网关是否返回独立的可展示思考内容；Responses 下不发送，Chat Completions 兼容路径发送同名扩展参数 |
| `thinking_mode` | str | 内部传输字段：`provider_default` 省略开关；`enabled` / `disabled` 显式发送 true / false；管理端不单独暴露 |
| `thinking_wire_format` | str | 思考开关请求协议：`none`、`enable_thinking` 布尔值或 `thinking_object`（`thinking.type`） |
| `enable_thinking` | bool | 旧版相容字段；仅在 `thinking_mode=provider_default` 时，true 等同 enabled |
| `reasoning_effort` | str/null | 默认的分级推理值；`off` / `on` 为开关保留字，不得写入此字段；管理端与 `thinking_mode` 合并展示为“默认推理等级” |
| `supported_reasoning_efforts_json` | json/null | 模型允许用户按轮选择的有序等级目录；`off` / `on` 必须像其他等级一样显式声明，空数组表示不提供选择器 |
| `supports_image` / `max_images` | bool / int | 图片输入能力与上限 |
| `supports_video` / `max_videos` | bool / int | 视频输入能力与上限 |
| `enabled` | bool | 是否可被用户选择 |
| `tags_json` | json | 前端标签 |

模型配置必须满足 `context_window > max_tokens`。自动压缩默认在 `(context_window - max_tokens) * 80%` 触发；自定义阈值只允许把它调低。普通请求不再使用旧的 3000-token reserve、8192 floor 或单项 10k-token 本地硬拒绝。

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
      "supports_reasoning_control": true,
      "thinking_mode": "enabled",
      "thinking_wire_format": "thinking_object",
      "reasoning_effort": "high",
      "default_reasoning_level": "high",
      "supported_reasoning_efforts": ["off", "high", "max"],
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
- `LLMClient` 先根据 `provider` 选择 Anthropic 或 OpenAI，再按 `openai_protocol` 选择 `OpenAIResponsesClient` 或 `OpenAIClient`。存量 OpenAI 模型的 `NULL` 按 `chat_completions` 解释；不自动探测、不因请求失败切换协议。
- Responses 下 `apply_patch` 投影为带 Lark grammar 的 freeform custom tool；Chat Completions 下投影为参数结构为 `{patch: string}` 的 function tool。两条路径在 Agent 内部统一为 `arguments.patch`。
- OpenAI Responses 请求把非空 `reasoning_effort` 编码为 `reasoning.effort`；`thinking_wire_format` 仅在网关需要显式开关时写入扩展字段。`reasoning_split` 保留为模型目录能力信息，不再发送到 Responses 请求。
- `thinking_mode` 是供应商传输细节。管理端只编辑“默认推理等级”：空值映射 `provider_default`，`off` 映射 `disabled`，`on` 映射 `enabled`，其他值映射 `enabled + reasoning_effort`。该等级是二元组的有损展示投影；编辑时若等级未变必须保留原始二元组，仅在新建或等级实际改变时执行反向映射。
- `supports_thinking` 描述目录是否声明了可展示的思考能力。OpenAI 兼容模型的判定依据为：未显式关闭、`reasoning_format!=none`，并且 `reasoning_split=true`、`effective_thinking_mode=enabled` 或白名单含非 `off` 等级三者至少满足一项；仅有 `provider_default`、旧 reasoning format 且没有上述能力信号的存量 No Thinking 变体必须返回 false。`supports_reasoning_control` 则仅由 OpenAI 兼容模型非空的 `supported_reasoning_efforts` 声明，不能从 reasoning format 或 `supports_thinking` 猜测可选档位。
- `supported_reasoning_efforts` 是服务端有序白名单，不按模型名推断，也不由前端自动添加 `off`。规范化时去除首尾空白、拒绝空项并按首次出现位置去重；管理端必须持久化 `ModelConfig` 的规范化结果，禁止数据库/API 保留重复项而运行时另行去重。默认等级非空且白名单非空时必须包含在白名单中；按轮选择必须精确命中该列表。白名单为空的存量模型只表示不提供按轮选择器，不得因此阻止管理端保存其他字段。
- `reasoning_effort` 与白名单元素必须是字符串。YAML 1.1 会把裸 `on` / `off` / `yes` / `no` 解析成布尔值，因此白名单中的这些值必须加引号；解析成非字符串时抛出明确 `ValueError`，禁止 `str()` 兜底成 `"True"` / `"False"`。`reasoning_effort` 只承载分级强度，必须拒绝保留字 `off` / `on`；开关只能由 `thinking_mode` 表达，避免同时发送互相冲突的开关与顶层强度。
- `thinking_wire_format=none` 表示该网关无法编码思考开关，但仍可接收顶层 `reasoning_effort`。因此 `none` 允许 `high` / `max` 这类分级值，但必须拒绝白名单中的 `off` / `on`，以及没有 `reasoning_effort` 的显式开关默认值——这些配置一旦落库就会静默丢弃用户的开关选择。
- 上述开关默认值校验必须基于 `effective_thinking_mode` 而非原始 `thinking_mode`：`enable_thinking=true` + `thinking_mode=provider_default` + `wire=none` 同样会让 `supports_thinking` / 默认等级对外显示为开启，实际请求却不携带任何开关，属于必须拒绝的不一致配置。
- 管理端限制白名单最多 20 项、单项最多 40 字符且不得为空；非 OpenAI provider 创建或更新时将 `thinking_wire_format` 归一化为 `none`，无需等待重启迁移。
- 推理等级是网关级透传参数。同一 Run 的推理快照在 failover 到备用模型时原样传递，不按备用模型白名单过滤或自动降级；无法编码该快照的客户端必须跳过。每个 fallback 使用自身的 `openai_protocol`，不得继承主模型协议。`provider_default + null` 无需编码，可跨 provider；其他快照只尝试 OpenAI 兼容客户端，其中 `thinking_wire_format=none` 仅在存在具体 `reasoning_effort` 时兼容。
- 管理端模型创建/更新/删除、默认模型变更、权限包模型变更后，必须触发 `reload_model_registry()`；模型目录变更还必须失效进程内 Agent 缓存，运行中会话延迟到本轮结束后重建。

### 升级说明：思考能力与请求传输解耦

历史 Chat Completions 实现曾把 `reasoning_split` 同时当作网关请求参数和思考能力信号，并把 `enable_thinking` 嵌套在它的分支内。Responses 迁移后该语义废止：能力由目录描述，请求只由 `thinking_mode`、`thinking_wire_format` 与 `reasoning_effort` 编码。

现在两者完全独立，**不回滚**这一行为：

- `reasoning_split` 不进入 Responses 请求；Chat Completions 兼容路径继续把它作为旧网关扩展字段发送，同时参与 `supports_thinking` 的目录投影。
- `thinking_mode` 与 `reasoning_effort` 的实际传输完全由 `thinking_wire_format` 和 Responses `reasoning.effort` 决定。

### 启动 seed

- `models.yaml` 只在模型表为空时 seed；数据库已有模型后，启动和字段迁移均不得再从 YAML 回填或覆盖模型配置。
- `models.yaml` 中 OpenAI 模型未声明 `openai_protocol` 时默认 seed 为 `chat_completions`；Anthropic 模型归一化为 `NULL`。
- seed 必须复用与运行时 registry 完全相同的 `ModelConfig` 构造与校验（共享 `model_config_from_yaml_entry`），写入的是校验并归一化后的值。禁止把 `models.yaml` 原始字段直接落库：否则未加引号的 `[off, high]` 会以布尔 `false` 写入，再在读取时被转成字符串 `"False"`，形成不报错的错误推理等级。
- 非法 `models.yaml` 必须让 seed 直接失败，不得部分写入。
- 从 DB 读取 `supported_reasoning_efforts_json` 时不得用 `str(item)` 强转：非字符串项必须报错暴露损坏数据，而不是静默生成一个永远匹配不上的等级。`tags` 是自由标签，不适用该严格规则。

- 当 `llm_models` 为空时，从 `models.yaml` 导入初始模型，并把启用模型加入默认权限包。
- `models.yaml` 中的 `default_model`、`cron_default_model`、`subagent_default_model` 会写入 `llm_model_settings`。
- DB 已有模型时，不再用 `models.yaml` 覆盖运行时目录。
- 新增数据库列只使用数据库迁移默认值；已有模型记录绝不从 `models.yaml` 回填。

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
- 每次切换到更小窗口 fallback 前，若当前输入达到目标模型 `(context_window - max_tokens) * 80%` 阈值，先由 primary 执行本地压缩；遇到 context/invalid request/usage limit/overload/internal/retry-exhausted，再由目标 fallback 压缩。fallback 压缩的 context overflow 每次只删除一个最旧项重试，成功后用 replacement 重建并真实发送该次请求。

### 权限语义

- 管理员可使用全部启用模型。
- 普通用户拥有默认权限包 + 额外绑定权限包中的启用模型。
- 用户没有任何可用模型时，`GET /api/models` 返回 403，并提示联系管理员配置模型权限。
- Session 创建、模型切换、Agent 初始化前都必须校验当前用户是否可访问目标模型。

## 5. 失败模式与错误处理

| 场景 | 行为 |
|------|------|
| DB 模型目录为空且 `models.yaml` 加载失败 | 启动 seed 失败，模型注册不可用 |
| DB 模型目录连接或查询失败 | 直接失败，不回退 YAML |
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
