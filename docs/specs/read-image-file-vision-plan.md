# `read_image_file` 视觉读图策略 — Plan

状态：已实施（2026-10-20），已复审与实测（2026-10-21）。实际行为以 `chat-spec`、`models-spec` 为准，与本计划的差异记录在第 9 节。

本计划是 `vision-fallback-plan.md` 第 6 节（入口 B）的后续，起因是用不支持图片的主模型转写 10 页图片型 PDF 的实测（见第 2 节）。

## 1. 目标

1. 主模型在读图之前就知道规则：一次最多几张、页面要渲染到多少像素、密集区域要先裁剪。不再靠撞报错学习。
2. 视觉挂件对每张图都有完整的输出预算，不再因为多图一批而被提示词授权"省略"。
3. 图表数据以"类别—系列—数值"的表格返回，错配能被核对。
4. 保留跨图联看能力，由主模型按意图显式选择。

两种主模型都受益于第 1 条（描述层）；第 2–4 条只影响主模型不支持图片、由挂件代读的情况。

不在本次范围：

- 转写结果的关键字段校验（由独立审核 skill 负责）。
- 供应商侧的图片分辨率参数（挂件为内部部署，不适用）。
- 工具对图片像素的强制检查（先靠描述引导，不做兜底）。
- 主模型原生支持图片时也可选"以文字返回"的读图方式；原生模式下累计上限与工具成功结果的矛盾（见第 8 节）。
- 更换视觉模型。

## 2. 现状

实测样本：《博时裕荣纯债》10 页宣传材料，图片型 PDF，逐页转 PNG 后由主模型以每次 5 张调用 `read_image_file`，挂件按问题转写。两版对比（第二版把挂件思考强度提到 `xhigh`）：正文基本准确；柱状图数值从 1/10 正确提升到全对；业绩图表仍有 4 处数值归属错位；第 9 页基金名称、人名错字；第 10 页风险披露出现术语替换和内容遗漏。

代码层面能对上的事实：

| 位置 | 现状 | 后果 |
| --- | --- | --- |
| `src/agent/tools/sandbox_file_tools.py` `description` | 不含数量上限，不含分辨率要求 | 主模型不知道规则 |
| 同文件 `parameters.max_images` | 写死 `"default": 10`，描述"default 10" | 挂件上限为 5 时，主模型按 10 张请求 |
| 同文件 `execute` 预检查 | `allowed_count = min(requested_max, 上限)`，超限返回"一次最多读取 N 张图片，当前请求 M 张" | 第一次必失败，视觉模型未被调用；主模型从报错学到上限后第二次成功。每个会话浪费一步主模型 |
| `_sandbox_read_image_bytes` | 原字节 base64 直传，不缩放、不重编码 | 像素完全由主模型转图命令决定。PyMuPDF `get_pixmap()` 默认 72 DPI（A4 为 595×842），pdftoppm 默认 150，pdf2image 默认 200 |
| `src/api/services/vision_fallback_service.py` `answer()` | N 张图加一段指令一次提交 | 多页共享一次回答 |
| 同文件 `VISION_SYSTEM_PROMPT` 第 8 条 | "回答不超过 2000 字；内容过多时优先保留文字、数值和结构，省略装饰性细节" | 为单图描述写的；5 张图一批时 5 页挤 2000 字，提示词本身授权省略。报告中的"内容遗漏""术语压缩"有相当部分来自这里 |
| 同文件第 4 条 | 图表"写明类型、标题、坐标轴与刻度、图例或系列，以及能读出的数据点数值" | 不要求类别—系列—数值的对应关系，错配无法核对 |
| `src/api/services/agent_service.py` `_describe_conversation_images` | 用户上传入口已是每张图一次并发调用 | 两个入口对挂件的用法不一致 |
| `src/agent/agent.py` `_tool_multimodal_limit_error` | 原生模式下按历史累计图片数校验，超限整批不注入，只留一句说明；工具结果已报成功 | 本计划不改，记录为后续问题 |
| `src/agent/agent.py` `_bound_tool_result_content` | 工具输出按 42,667 字节 × 1.2 中间截断 | 逐图拼接输出需在此预算内，见 3.5 |

## 3. 设计

### 3.1 工具描述：两种模式共用部分

构造时算出有效上限 `limit`：原生模式取主模型 `max_images`，挂件模式取挂件 `config.max_images` 与主模型输出预算允许张数的较小者（见 3.5，复审修正）。两种模式的描述都追加以下内容（英文，与现有描述一致）：

- 数量："At most {limit} images per call; split larger sets into several calls."（已被 9.8 取代：原生模式改为按对话累计的上限、不写分批指引，只有挂件模式保留本句。）
- 分辨率与裁剪："Images are read at their stored resolution and cannot be zoomed afterwards. Make text legible before reading: render PDF pages at 200 DPI or more (A4 ≈ 1650×2340 px), take full-size screenshots, and crop dense regions (small print, tables, chart axes) into separate images."

`max_images` 参数保留以兼容现有调用，`default` 与 `maximum` 都改为 `limit`，描述改为 "Maximum number of images to read from paths (at most {limit})."。`execute` 中的 `min()` 夹取逻辑不变。

原生模式下的描述和参数除以上两点外不变：没有 `question`，没有 `joint`，行为不变。

### 3.2 挂件模式：默认逐图，显式 `joint` 联看

挂件模式新增布尔参数 `joint`，默认 `false`，与 `question` 一样只在挂件模式出现。

| `joint` | 调用方式 | 适用 |
| --- | --- | --- |
| `false`（默认） | 对每张图各调一次 `vision_fallback.answer([image], question)`，`asyncio.gather` 并发；无 `question` 时每张图返回完整描述 | 逐页转写、读多张独立图表、筛选"哪张有 X" |
| `true` | 维持现状，`vision_fallback.answer(images, question)` 一次提交 | 改前改后差异、多图一致性判断、内容跨页延续、裁剪局部回填整页结构 |

挂件模式描述改为说明两种模式及适用场景，大意："By default each image is read separately with your question and answered per file. Set joint=true to have the assistant view all images together, for comparing or relating them: before/after differences, consistency between images, content continuing across pages, or placing a cropped region back into the full page."

并发在工具内完成，挂件服务不加新方法：服务只负责单次调用，与用户上传入口的做法一致。

### 3.3 输出格式

单张图或 `joint=true` 时格式不变：

```
[视觉助手 <model> 读取了 N 张图片：a.png, b.png]
[问题] <question 或（完整描述）>
[回答]
<text>
```

多张图且 `joint=false` 时按文件分段，顺序与 `paths` 一致：

```
[视觉助手 <model> 读取了 N 张图片：a.png, b.png]
[问题] <question 或（完整描述）>
[回答 a.png]
<text>
[回答 b.png]
<text>
```

### 3.4 逐图模式的失败语义

全部结束后统一处理。全部成功返回 `success=True`。任一张失败时返回 `success=False`：`error` 为失败那几张的 `VisionFallbackError` 文本（已含模型名、文件名、原因），`content` 为成功部分按 3.3 拼好的正文。Agent 既有的 `_tool_result_content` 会把两者一起交给主模型（"Error: …\n\nOutput: …"），主模型只需重读失败的文件，成功的转写不浪费。全部失败时 `content` 为空。

`joint=true` 的失败语义不变：异常从工具抛出，按通用工具异常路径处理。

### 3.5 输出预算

逐图拼接后的正文仍受 Agent 的通用工具输出截断约束（42667 字节 × 1.2 ≈ 51 KB，中间截断）。中文 3 字节一字，每张 2000 字上限时 8 张约 48 KB 放得下。挂件实际上限是 20（`models.yaml` 与开发库的 qwen3.6-plus），按 20 张逐图会有一半页面在中间被截掉（截断处有标记，但结果仍报成功）；而且预算按主模型配置而定，开发库的文本主模型 `glm` 只有 10000 字节，连 8 张都放不下。因此工具按主模型实际预算算张数：`max(1, tool_output_truncation_bytes × 1.2 ÷ 6000)`，再与挂件 `max_images` 取较小者（复审修正，见 9.7）。

### 3.6 系统提示（`VISION_SYSTEM_PROMPT`）

只改两条，其余不变。改动同时作用于用户上传入口的 `describe`，这是预期的。

- 第 4 条改为：图表写明类型、标题、坐标轴与刻度、图例或系列；数据点用 Markdown 表格列出，三列为类别（年份、项目等）、系列（图例名）、数值，一行一个数据点；读不出的数值写"（不确定）"，不得按趋势推算或补齐。
- 第 8 条改为：每张图片的回答不超过 2000 字；内容过多时优先保留文字、数值和结构，省略装饰性细节。

### 3.7 并发度

逐图模式一次最多 `limit` 个并发请求（挂件模式按主模型输出预算而定，默认预算下为 8）。用户上传入口同样是无界 `gather`，保持一致，不另设信号量。出现供应商限流再加。

## 4. 改动清单

| 层 | 文件 | 改动 |
| --- | --- | --- |
| 工具 | `src/agent/tools/sandbox_file_tools.py` | `description` 共用段（上限、分辨率）；`max_images` 的 default/maximum；挂件模式 `joint` 参数与描述；`execute` 逐图并发、部分失败、分段输出 |
| 服务 | `src/api/services/vision_fallback_service.py` | `VISION_SYSTEM_PROMPT` 第 4、8 条 |
| 测试 | `tests/test_sandbox_tools.py` | 见第 5 节 |
| 测试 | `tests/test_vision_fallback_service.py` | 见第 5 节（复审后作废，见 9.7） |
| 工厂 | `src/api/services/tool_factory.py` | 文本模型无挂件时排除工具；透传主模型 `tool_output_truncation_bytes`（9.7） |
| 服务 | `src/api/services/agent_service.py` | 构造工具时传主模型截断预算（9.7） |
| 测试 | `tests/test_agent_service_extended.py` | 工具注册条件与预算透传用例（9.7） |
| Spec | `docs/specs/chat-spec.md` `read_image_file` 小节 | 描述内容、`joint`、分段格式、逐图失败语义、输出预算 |
| Spec | `docs/specs/models-spec.md` 视觉挂件一节 | 基座 prompt 摘要：图表三列表格、每张 2000 字 |
| Spec | `docs/specs/vision-fallback-plan.md` 第 13 节 | 追加一条指向本计划 |

不涉及前端、数据库、配置项。

## 5. 测试

`tests/test_sandbox_tools.py`：

- 原生模式（已被 9.8 取代，现断言 "at most 5 images in the conversation in total" 与 step 整批不送达文案）：`max_images=5` 时描述含 "At most 5 images" 与 "200 DPI"；`parameters.max_images` 的 default 与 maximum 为 5；无 `question`、无 `joint`。
- 挂件模式：描述含挂件上限数字与 `joint` 说明；`parameters.joint` 存在且默认 `False`。
- 两张图、默认模式：`answer` 被调用两次，每次一张图、同一 `question`；输出含 `[回答 a.png]`、`[回答 b.png]` 且顺序与 `paths` 一致（mock 按文件名返回不同文本）。
- 两张图、`joint=true`：`answer` 调用一次、两张图；输出为 `[回答]` 单段。
- 单张图：现有两个用例不改，格式不变。
- 部分失败：两张图中一张抛 `VisionFallbackError`，结果 `success=False`，`error` 含失败文件名，`content` 含成功那张的分段。
- 超限：现有 `test_read_image_rejects_too_many` 不改。

`tests/test_vision_fallback_service.py`：

- 系统提示含"每张图片"与"类别""系列""数值"三列要求各一次断言；现有 4 个用例不改。

受影响测试一次跑全（引用了 `SandboxReadImageTool` / `read_image_file` 的文件）：

```bash
python -m pytest tests/test_sandbox_tools.py tests/test_vision_fallback_service.py \
  tests/test_agent.py tests/test_agent_service_extended.py \
  tests/test_codex_context_compaction.py tests/test_conversation_message_persistence.py -q
```

## 6. Spec 同步

- `chat-spec.md` `read_image_file` 小节：工具描述包含数量上限与分辨率、裁剪指引（两种模式）；挂件模式新增 `joint` 参数及默认逐图语义；分段输出格式；逐图模式部分失败返回 `success=false` 且 `content` 保留成功部分；输出受通用截断约束。
- `models-spec.md` 视觉挂件一节：基座 prompt 摘要改为"图表数据以类别/系列/数值三列表格给出""每张图片不超过 2000 字"。
- `vision-fallback-plan.md` 第 13 节追加："2026-10-20：入口 B 改为默认逐图、`joint` 联看，见 `read-image-file-vision-plan.md`"。

## 7. 实施步骤

每步完成后给出验证结果，再进入下一步。

1. 系统提示第 4、8 条 + 服务测试。验证：`python -m pytest tests/test_vision_fallback_service.py -q`。
2. 工具描述与 `max_images` 参数（两种模式）+ 对应测试。验证：`python -m pytest tests/test_sandbox_tools.py -k ReadImage -q`。
3. 挂件模式 `joint`、逐图并发、部分失败、分段格式 + 对应测试。验证同上。
4. 三处 spec 同步。
5. 一次跑全第 5 节列出的 6 个测试文件；暂存后按惯例交 Opus 复审，逐条核实后向用户汇报。改动停在本地工作区，不提交、不推送。
6. 实测由用户用同一份 10 页样本进行，用字段级准确率比较：逐图 vs `joint`；200 DPI vs 原 DPI；裁剪二次识别有无。挂件每次调用的 INFO 日志（图片数、token、耗时）用于成本对比。

## 8. 后续问题（记录，不在本次）

1. 原生模式下 Agent 按历史累计图片数校验，超限时整批不注入并只留一句说明，而工具结果已报"已读取并加入下一轮视觉上下文"，主模型收到两条矛盾信息。工具预检查看不到历史，无法在工具层解决。2026-10-22 复审：原生模式描述不再写"分几次调用"，改写成"整个对话累计最多 N 张；同一 step 内所有调用读到的图片会使累计超限时，这些图片整批不送达、调用仍报成功"，和 Agent 按 step 攒块、step 末一次校验并整批丢弃的行为一致，避免把主模型引向分批或误以为前几张/前一次调用的图已送达；根治需要 Agent 在注入新一批工具图片时把更早的工具图片块换成文字占位（滚动窗口），另开一轮。
2. 原生模式下每张图作为真实图片块留在历史里，后续每步重发；长文档转写场景即使主模型能看图，用挂件转文字也可能更省。需要产品层面决定是否允许为支持图片的模型配置挂件并提供"以文字返回"选项。
3. 挂件并发请求当前无界（两个入口一致），出现限流时再加信号量。
4. 挂件 `max_images` 超过预算允许张数时由工具封顶（见 3.5），不再撞通用截断。张数是估算值（每张按 2000 字纯中文 6 KB 算，标题与分段标签不计入），每张都写满时末尾几十字会被中间截断并带标记；不为此扣开销，否则预算 10000 的文本主模型每次只能读 1 张。
5. `agent_service.py` 构造 `Agent` 时 `tool_output_truncation_bytes` 仍用 `getattr(model_config, ..., 42667)` 兜底，而同一方法给 `create_agent_tools` 的透传已改为严格读取；旧代码，待清理。

## 9. 实施差异（2026-10-20）

1. `execute` 的 `max_images` 形参默认值由 10 改为 `None`，缺省时取有效上限，与 schema 的 `default` 一致；原先 `int(max_images or 10)` 外层的 `try/except` 一并去掉，非法值直接抛出，由 Agent 的通用工具异常路径处理；0 不再视为缺省，返回 "max_images must be greater than 0"。
2. 单张图不论 `joint` 取值都走一次联看调用，输出格式与改动前完全一致，现有用例未改。
3. 主模型不支持图片且未配置挂件时，`tool_factory` 不再注册 `read_image_file`（复审修正，原先描述会写 "At most 0 images per call" 与 `execute` 拒绝 0 自相矛盾）；`max_images` schema 加 `minimum: 1`。
4. `tests/test_agent.py` 中 `test_agent_initialization`、`test_workspace_creation` 两个用例在未改动的 HEAD 上同样失败（工作区目录创建的旧断言），与本次无关，未处理。
5. 逐图并发只把 `VisionFallbackError` 收进 `error`，其他异常（含子任务的 `CancelledError`）原样抛出，抛出前把同时出现的其余异常记 ERROR 日志；用户上传入口 `_describe_conversation_images` 只抛不记日志，两处在这一点上不同（Opus 复审 P1）。
6. 实测（2026-10-21，开发库挂件 qwen3.6-plus，开思考）：带标签柱状图 20 个数值全对；密集折线图模型声明无法读出而不逐点猜；同一密集小字页 72 DPI 时名称、姓名错 11 处且改写一句，200 DPI 时全部逐字一致，总 token 反而少三成；两图联看与逐图同样全对。样本与原始输出未随仓库保存（实测时的 `outputs/vision-probe-20261021/` 目录不在仓库和服务器上，以上数字只有实测者的记录）。
7. 复审修正（2026-10-21）：逐图分段标题、失败归属与 `VisionImage.name` 改用请求路径而非 basename；多个非挂件异常只抛第一个、其余记 ERROR 日志；挂件模式工具上限改为按主模型 `tool_output_truncation_bytes` 算出的张数与挂件 `max_images` 的较小者，由 `tool_factory` 传入预算（第 3.5 节原按挂件上限 5、默认预算估算，实际挂件为 20、开发库文本主模型预算为 10000）；`max_images=0` 报错补回归用例；补"全部失败"与"同名文件按路径区分"用例；删除对提示词字面的形状断言，第 4、5 节中对 `tests/test_vision_fallback_service.py` 的修改条目作废（描述文案断言在 9.8 中恢复为按对话累计的上限与 "200 DPI"）。
8. 复审修正（2026-10-22，/code-review）：原生模式描述改为按对话累计的上限、去掉分批指引（见第 8 节第 1 条）；预算公式抽为 `context_compaction.tool_output_byte_budget()` 供 `truncate_tool_output` 与 `_image_limit` 共用；`tests/helpers.py` 的 `MockModelConfig` 补 `tool_output_truncation_bytes`；补 `AgentService.initialize_agent` 向 `create_agent_tools` 透传预算的用例；注释与 chat-spec 把"放得下"改为"估算"。未采纳：张数扣标题开销、`tool_factory` 与工具类的读图判定合并、逐图 `gather` 改写、完成顺序用例。
9. 复审修正（2026-10-22，同事第二轮）：原生模式描述改为"同一 step 内读到的图片会使累计超限时整批不送达、调用仍报成功"，与 `Agent._flush_pending_tool_content_blocks` 按 step 整批丢弃一致（原文案"超出的不会送达"暗示前几张已送达；丢弃单位是 step 而非单次调用，同一 step 并行的两次调用会一起丢）；每张回答字数软上限抽为 `vision_fallback_service.VISION_ANSWER_MAX_CHARS`，提示词第 8 条与 `_image_limit` 的字节估算共用；删除工具内已不可达的"当前模型不支持图片输入，请切换 qwen3.6-plus/kimi-2.5"分支（含硬编码模型名）及其用例，该情形由 `tool_factory` 不注册工具覆盖。未采纳：逐图 `gather` 加 `Semaphore`（用户上传入口同样无限制并发，且现有预算配置下宽度至多 8，见第 8 节第 3 条）、部分失败时标题按成功数计数并把标题/错误字节计入预算（失败文件已按路径列在 `error`，开销属已记录的估算余量）。
