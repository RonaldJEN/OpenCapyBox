# 视觉挂件（Vision Fallback Model）— Plan

状态：已实施（2026-10-18），实际行为以 `models-spec`、`chat-spec`、`frontend-chat-spec`、`admin-spec` 为准；与本计划的差异见第 13 节。

## 1. 目标

让不支持图片输入的主模型（当前开发库中为 `glm`，其他环境按各自 `llm_models` 配置）也能处理图片：由管理员在后台指定一个支持图片的模型作为"视觉挂件"，在主模型不能看图时代为看图，把结果以文字交回主模型。主模型、推理风格、会话上下文都不变。

覆盖两个入口：

- 用户在 Composer 上传的对话图片。
- Agent 通过 `read_image_file` 读取沙箱内图片（截图、生成的图表、PDF 转出的页面图等）。子 Agent 与 Cron 任务通过同一工具工厂创建工具，自动受益。

不在本次范围：视频；按用户单独配置挂件；挂件调用失败时的静默兜底。

未配置挂件时，所有行为与现状完全一致。

## 2. 现状

模型能力以数据库 `llm_models` 为准，`models.yaml` 只在首次初始化时写入。开发数据库当前状态（2026-10-18 查询）：

| model_id | 显示名 | 启用 | 图片 | max_images | 系统角色 |
| --- | --- | --- | --- | --- | --- |
| `deepseek-flash` | DeepSeek V4 Flash | 是 | 是 | 33 | 默认模型 |
| `qwen3.6-plus` | 通义千问 qwen3.6-plus | 是 | 是 | 20 | （拟作挂件） |
| `qwen3.8-flash` | Qwen3.8-Flash | 是 | 是 | 30 | Cron 默认、Subagent 默认 |
| `glm` | 智谱 GLM-5 | 是 | 否 | 0 | — |
| `kimi-2.5` | Kimi 2.5 | 否 | 是 | 20 | — |
| `deepseek-pro` | DeepSeek V4 pro | 否 | 否 | 0 | — |
| `minimax-m2` / `minimax-m2-openai` / `minimax-text-01` | MiniMax | 否 | 否 | 0 | — |

启用的 4 个模型里只有 `glm` 不支持图片；默认、Cron、Subagent 三个系统角色模型都支持图片。因此在当前开发库里，挂件只在用户选到 `glm` 时生效；禁用模型（`deepseek-pro`、MiniMax 系列）一旦启用也会落入挂件范围。以上是本库快照，不代表其他环境。

主模型不支持图片时，三处直接失败：

| 位置 | 行为 |
| --- | --- |
| `frontend/src/utils/chatImageInput.ts` | 上传与发送时拦截，提示切换模型 |
| `src/api/services/agent_service.py` `_validate_multimodal_blocks` | 后端拒绝，报"不支持图片输入" |
| `src/agent/tools/sandbox_file_tools.py` `SandboxReadImageTool.execute` | 工具返回错误"请切换模型"；任务进行到一半时用户无法补救 |

可复用的既有机制：

- `project_message_media_capabilities`（`src/agent/context_compaction.py`）：持久化与内存历史保留原图块，仅在发给供应商的请求副本里把模型不支持的媒体替换成文本标记。挂件只需替换该标记内容，不改写历史，与 `models-spec` 既有约定一致。
- `llm_model_settings` 表已有 `default_model_id` / `cron_default_model_id` / `subagent_default_model_id` 三个系统角色模型字段，管理后台设置区、删除模型引用检查、启动校验均已成型，挂件照此扩展。
- `llm_call_records` 已记录每次模型调用的请求、响应与用量，`call_kind` 可区分调用类型。

## 3. 配置模型

### 3.1 字段

- `llm_model_settings.vision_fallback_model_id`：`String(100)`，可空。空表示关闭挂件。
- `models.yaml` 新增顶层 `vision_fallback_model`，仅用于首次初始化数据库，与 `cron_default_model` 同级。
- `ModelRegistry` 新增 `vision_fallback_model_id` 与 `get_vision_fallback()`。

### 3.2 校验

- 启动与保存时：指向的模型必须存在、`enabled=true`、`supports_image=true` 且 `max_images > 0`，否则直接报错，不降级。
- 管理后台删除模型时，若被 `vision_fallback_model_id` 引用，与现有三个默认字段同样处理（阻止或按替换模型重分配）。

### 3.3 范围与权限

- 全局一份，不按用户配置。
- 挂件不受用户模型权限组限制：它是系统能力，用户无法直接选择它，只在后台配置。用户可用模型列表不包含挂件本身（除非该模型本来就在其权限组内）。

### 3.4 对外暴露

- `GET /api/models` 响应新增 `vision_fallback`：`null` 或 `{ "id", "name", "max_images" }`。前端据此决定是否放行图片以及提示文案。
- 管理后台模型设置接口（`AdminModelSettingsPayload` 与 `_settings_payload`）新增 `vision_fallback_model_id`；下拉框只列出支持图片的已启用模型，含"关闭"选项。

## 4. 视觉服务

新增 `src/api/services/vision_fallback_service.py`，对两个入口提供统一能力：

- `describe(images, focus=None) -> list[str]`：为每张图生成完整描述。
- `answer(images, question) -> str`：针对具体问题回答。

实现要点：

- 用挂件模型的 `ModelConfig` 构造 `LLMClient`，单次非流式调用，不带工具。
- 系统基座 prompt 固定在代码中，规定挂件的身份与回答规则：只回答与图片有关的内容；图中文字逐字转录；表格转为 Markdown；图表写明坐标轴、系列与可读出的数值；看不清或无法确定的地方明确标注"不确定"，不猜测、不发挥；使用中文。
- 描述长度设上限（初定每张 2,000 字符），超出时让挂件自行压缩，不在代码里截断。
- 每次调用写入 `llm_call_records`，`call_kind="vision_fallback"`，`session_id` / `round_id` 关联当前轮次，便于审计用量。
- 调用失败直接抛出异常，由调用方决定如何暴露；不吞错、不重试。

## 5. 入口 A：用户上传图片

### 5.1 放行条件

主模型 `supports_image=false` 且挂件已配置时放行；图片数量上限取挂件模型的 `max_images`。前端与后端各自校验，后端权威。

- 前端 `getImageInputError`：改为同时接收 `vision_fallback`，放行时附件区提示"当前模型不支持图片，将由视觉助手 X 识别后交给模型"。切换模型后的兼容性派生逻辑同步更新。
- 后端 `_validate_multimodal_blocks`：相同规则。

### 5.2 描述生成时机

在本轮消息落库之前、主模型首次调用之前，对该轮所有对话图片并行调用 `describe`，`focus` 为用户本条消息的文本（无文本则不传）。结果写入图片块：

```json
{
  "type": "image_url",
  "image_url": {"url": "data:..."},
  "file": {...},
  "vision_description": {
    "model_id": "qwen3.6-plus",
    "text": "..."
  }
}
```

原图块完整保留。描述只在写入时生成一次，后续轮次复用，不重复调用。

需核对 `src/api/routes/chat.py` 中落库与首次模型调用的先后顺序，确保描述在落库前完成；若顺序不满足，改为落库后、首次调用前回写该字段。

### 5.3 请求投影

`project_message_media_capabilities` 在主模型不支持图片时：

- 图片块含 `vision_description` → 替换为文本：`[图片 <name>（<path>）由视觉助手 <model> 代读：<text>。如需细节，可用 read_image_file 带 question 追问。]`
- 不含 → 保持现有标记"历史图片未随本次请求发送"（对应挂件后来才开启、或旧历史）。

主模型支持图片时投影不变，直接发原图。切换模型来回时历史不被改写。

### 5.4 失败语义与重试

描述生成发生在 `_validate_multimodal_blocks` 之后、`create_round` 之前，此时 Round 尚未创建、消息尚未落库。任一图片描述失败：

- 后端抛出 `VisionFallbackError`，按既有 `ValueError → HTTP 400` 路径返回确定性发送失败；错误信息指明失败的图片名与原因（超时 / 供应商错误 / 响应为空）。不带着缺失信息继续执行，不落入半成品历史。
- 前端沿用 `frontend-chat-spec` 第 334 行的既有规则："服务端返回 400 时按普通确定性发送失败处理并恢复草稿"。正文、附件、模型选择全部回到 Composer，已上传的草稿附件不需要重传。
- 在恢复后的草稿里，把失败的图片卡片标为"识别失败"，附件区提示"视觉助手识别 <name> 失败：<原因>，可重新发送或移除该图片"。重试即再次点击发送（或 Enter），不另设单独的图片级重试按钮：本轮所有描述都在发送时生成，没有可单独保留的中间结果，单图重试与整体重发的成本相同。
- 实施时需核对：失败发生时请求仍处于 HTTP 阶段（未进入 SSE 流）。若实现中此步已进入流内，则改为发出 `RUN_ERROR(code="VISION_FALLBACK_FAILED")` 且不创建 Round，并让前端对该 code 同样执行草稿恢复。

每次发送都会对本轮图片重新生成描述（包括上次已成功的图片）。首版不做跨次发送的描述缓存。

### 5.5 历史与回放

`_blocks_to_plain_text`、`_extract_user_attachments`、历史 v2 读取均不受影响：原图块与附件元数据不变。`vision_description` 是附加字段，前端不展示（后续如需展示再另行设计）。

## 6. 入口 B：`read_image_file`

### 6.1 三种模式

| 主模型支持图片 | 挂件已配置 | 工具行为 |
| --- | --- | --- |
| 是 | 任意 | 现状不变：读图并作为 `image_url` 内容块送入下一轮 |
| 否 | 是 | 挂件模式：读图后调用 `answer` / `describe`，返回文字 |
| 否 | 否 | 现状不变：返回"当前模型不支持图片输入" |

### 6.2 问题由主模型提出

不使用单一固定 prompt。工具新增可选参数 `question`：

- 有 `question` → 调 `answer(images, question)`，针对性回答。
- 无 `question` → 调 `describe(images)`，返回完整描述。

主模型可对同一组图片带不同问题反复调用。工具描述在挂件模式下改为："当前模型不能直接查看图片，图片将由视觉助手代读。请在 `question` 中写明你想从图中获得什么（数值、文字、布局、状态等），越具体越好；不填则返回完整描述。" 主模型支持图片时，工具描述与参数保持现状，`question` 不出现。

### 6.3 返回格式

挂件模式下返回固定结构，便于主模型衔接：

```
[视觉助手 <model> 读取了 N 张图片：a.png, b.png]
[问题] <question 或 "（完整描述）">
[回答]
<text>
```

不产生 `content_blocks`，不向下一轮注入图片。

### 6.4 数量与大小限制

挂件模式下一次最多读取的图片数取挂件模型的 `max_images`；单张 / 总量字节上限沿用现有常量。读取沙箱图片的方式（`_sandbox_read_image_bytes`）不变。

### 6.5 工具工厂

`create_agent_tools` 新增参数 `vision_fallback: ModelConfig | None`，由 `AgentService` 在构造工具时从 registry 读取并传入；`SandboxReadImageTool` 据此与 `supports_image` 共同决定模式。子 Agent 与 Cron 走同一入口。

### 6.6 失败语义

挂件调用失败不终止本轮，按工具失败处理，由主模型决定下一步：

- 视觉服务的异常直接从 `execute` 抛出；`Agent._execute_tool_call_for_record` 既有逻辑把工具异常转为 `ToolResult(success=False, error="<ExcType>: <msg>")` 返回给主模型，超时则按 `tool_timeout` 既有规则返回 `Tool execution timed out`。
- 错误文本以"视觉助手 <model> 调用失败："开头并带原因，主模型据此可以重试一次（`repeat_policy="read_only"` 允许重复调用）或向用户说明图片无法读取。工具内部不做自动重试。
- 读取沙箱图片本身的失败（路径不存在、格式不支持、超出大小）沿用现有错误返回，不走挂件。

## 7. 改动清单

| 层 | 文件 | 改动 |
| --- | --- | --- |
| 数据模型 | `src/api/models/llm_model.py` | `LLMModelSettings` 新增字段 |
| 数据模型 | `src/api/models/database.py` | 启动补列清单新增 `("llm_model_settings", "vision_fallback_model_id")` |
| 注册表 | `src/api/model_registry.py` | 解析 `vision_fallback_model`、校验、`get_vision_fallback()` |
| 注册表 | `src/api/services/model_access_service.py` | seed / DB 加载 / 响应新增字段 |
| 配置 | `models.yaml`、`.env.example`（如需） | 新增 `vision_fallback_model` 示例（默认留空） |
| 服务 | `src/api/services/vision_fallback_service.py`（新） | `describe` / `answer`、基座 prompt、调用记录 |
| 入口 A | `src/api/services/agent_service.py` | 放行校验、描述生成、`create_agent_tools` 传参 |
| 入口 A | `src/agent/context_compaction.py` | 投影时使用 `vision_description` |
| 入口 A | `src/api/routes/chat.py` | 描述生成时机接线（视核对结果） |
| 入口 B | `src/agent/tools/sandbox_file_tools.py` | `question` 参数、挂件模式、返回格式、动态描述 |
| 入口 B | `src/api/services/tool_factory.py` | 透传 `vision_fallback` |
| 用户接口 | `src/api/routes/models.py` | 响应新增 `vision_fallback` |
| 管理后台 | `src/api/routes/admin.py` | 设置读写、删除引用检查 |
| 管理后台 | `frontend/src/components/AdminModelAccessPanel.tsx`、`frontend/src/services/adminApi.ts` | 设置下拉框与类型 |
| 前端聊天 | `frontend/src/utils/chatImageInput.ts`、`frontend/src/types/index.ts`、调用处 | 放行规则、提示文案、类型 |

## 8. 测试

后端放在 `tests/`，前端放在 `frontend/src/__tests__/`。

- 注册表：挂件模型不存在 / 未启用 / 不支持图片 / `max_images=0` 时启动与保存均报错；留空时正常。
- 投影：含描述、不含描述、主模型支持图片三种情况的输出；历史原图块不被修改。
- 入口 A：主模型不支持图片且挂件开启时放行并生成描述；数量超过挂件 `max_images` 拒绝；描述失败时返回 400、不创建 Round、不落库；挂件关闭时维持现有拒绝。
- 入口 A 前端：400 后草稿恢复，失败图片卡片标记与提示文案；再次发送可成功。
- 入口 B：三种模式的行为；带 / 不带 `question` 的分支；返回格式；工具描述随模式变化；数量上限取挂件值；挂件调用抛异常时主模型收到 `success=False` 的工具结果而非本轮终止。
- 视觉服务：调用记录写入 `llm_call_records` 且 `call_kind="vision_fallback"`；异常直接抛出。
- 管理后台：设置读写；删除被引用模型的处理；下拉框候选仅含支持图片的模型。
- 前端：`getImageInputError` 新规则；`/api/models` 新字段的类型与消费。

## 9. Spec 同步

实施时同步以下文件：

- `docs/specs/models-spec.md`：`vision_fallback_model_id` 字段、校验规则、`/api/models` 响应、投影规则补充。
- `docs/specs/chat-spec.md`：上传图片的放行条件、描述生成时机、失败语义、`vision_description` 字段。
- `docs/specs/frontend-chat-spec.md`：第 8 节附件校验规则与提示文案。
- `docs/specs/admin-spec.md`：模型设置新增项与删除引用规则。
- `docs/specs/tool-permission-spec.md` 或工具相关 spec：`read_image_file` 的 `question` 参数与模式说明。

## 10. 实施步骤

每步完成后给出验证结果，再进入下一步。

1. 配置与注册表：字段、seed、校验、管理后台接口；跑单元测试。
2. 视觉服务：实现并跑通一次真实调用；核对 `llm_call_records` 落库。基座 prompt 与描述长度上限在此步按真实输出调整。
3. 入口 B（`read_image_file`）：改动最小、收益最大，先做。用不支持图片的模型让 Agent 读沙箱内图片，确认得到文字结果且能带问题追问；人为让挂件失败（如临时指向无效 api_base）确认主模型收到工具错误而非本轮终止。
4. 入口 A（上传与投影）：浏览器实测——用 `glm` 上传截图提问，确认回答正确；刷新后历史回放正常；切到 `qwen3.6-plus` 再切回，图片块与描述均保持；人为让挂件失败确认 400、草稿恢复与失败标记。
5. 管理后台与前端提示：浏览器实测设置保存、下拉候选、提示文案。
6. 收尾：同步 spec，全量 `pytest tests/` 与前端测试通过；改动停在本地工作区，不提交、不推送，等待审阅。

第 2–4 步的验证会用现有 DashScope 密钥真实调用 `qwen3.6-plus`，产生少量费用。不另做实施前的独立模型实测。

## 11. 代价与取舍

- 主模型看到的是文字描述而非原图，复杂图表与版面细节会有损失；入口 B 的带问题追问可补回一部分。
- 每张图多一次模型调用，增加耗时与费用；具体数值以第 10 节第 2–4 步的真实调用为准。
- 未采用"遇到图片自动整轮切换到支持图片的模型"：会替换用户选择的模型与推理风格，上下文也会断。

## 12. 已决事项（2026-10-18）

1. 挂件模型：使用已配置的 `qwen3.6-plus`；`models.yaml` 的 `vision_fallback_model` 示例值指向它；开发库已完成初始化，不再走 seed，实施后由管理员在后台设置为它。`qwen3-vl-flash` 作为后续可选项，管理员可在后台切换。
2. 入口 A 的 `focus`：带入用户本条消息作为关注点，同时要求描述保持完整，以便后续轮次复用。
3. 不做实施前的独立模型实测；管理员在后台配置并实际使用时即可看到效果。实施过程中的验证步骤仍会真实调用模型。
4. 入口 A 失败：400 + 草稿恢复 + 失败图片标记，重试即重新发送，不设图片级单独重试按钮（见 5.4）。入口 B 失败：作为工具错误交给主模型处理（见 6.6）。

## 13. 实施差异（2026-10-18）

1. 挂件调用不写 `llm_call_records`：用户上传图片时 Round 尚未创建，而该表要求 `round_id` 外键且 `(round_id, step_index)` 唯一。改为每次调用写 INFO 日志（模型、图片数、token、耗时）。
2. 入口 A 的失败不走 HTTP 400：描述生成在 SSE 流内的 `prepare_chat_round` 中执行，按 5.4 的备选方案发出 `RUN_ERROR(code="VISION_FALLBACK_FAILED")`，不创建 Round。
3. 失败提示与重试（按用户反馈调整）：用户只看到"图片识别失败，点一下图片即可重试"，约 3 秒消失；技术原因只进后端日志。本轮图片卡片标为"识别失败"并带"点击重试"遮罩，点击即重新发送整条消息。因为一次发送是一批识别，任一张失败整批作废，所以标记的是本轮全部图片，而不是只标失败的那一张。
4. 前端发送块：主模型不支持图片但配置了挂件时，图片以 `image_url` 发送（原逻辑只对支持图片的模型这样做，其余以文件引用发送）。
5. 挂件失效统一由 registry 启动/重载校验处理：模型不存在、停用或不支持图片时记录 WARNING 并关闭挂件。管理端只在保存设置时校验一次，不再对停用、删除模型单独设守卫，YAML 加载也不重复校验。修改挂件设置会失效 AgentPool 缓存。
6. 重试只重新识别失败的图片（2026-10-19，替代 5.4 末段"首版不做跨次发送的描述缓存"）：一张失败不中断其他图片，成功的描述按"正文 + 图片"缓存在 AgentService 实例上，重试时复用（改了正文则重新识别）；Round 创建后清空。前端标记仍按第 3 条标记本轮全部图片。识别期间发送流随 SSE heartbeat 续期运行锁，防止识别超过锁陈旧阈值后被新请求回收。
