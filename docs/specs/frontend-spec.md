# 前端总 Spec

> 本文是 OpenCapyBox 前端的**单一事实源**。细分模块见：
> - [frontend-chat-spec.md](./frontend-chat-spec.md) — 聊天/SSE/推理面板
> - [frontend-session-spec.md](./frontend-session-spec.md) — 会话列表与切换
> - [frontend-panel-spec.md](./frontend-panel-spec.md) — 抽屉/弹窗类面板（Files/SettingsCenter/Cron）

## 1. 技术栈与版本

- React 18 + TypeScript 5 + Vite
- 路由：`react-router-dom`
- 样式：TailwindCSS（`claude-*` token 见 §5）
- HTTP：`axios`（集中在 `services/api.ts`）
- SSE：`EventSource`（由 `services/api.ts` 封装为 `subscribeToRound`/`sendMessage` 回调）
- 状态管理：**不引入 Redux/Zustand**，使用组件 state + ref + props 透传。复杂跨组件通信通过 `App.tsx` 顶层 state 协调。

## 2. 模块职责边界

| 模块 | 职责 | 不职责 |
|---|---|---|
| `App.tsx` | 路由、顶层 state（sessionId、model、panel）、未读计数轮询 | 业务逻辑、SSE 处理 |
| `components/ChatV2.tsx` | 会话内消息流、SSE 消费、滚动、取消 | 会话列表、面板内容 |
| `components/SessionList.tsx` | 会话 CRUD、运行中检测、入口按钮 | 消息渲染 |
| `components/Round.tsx` | 单轮（user+assistant+reasoning）视觉渲染 | 消息状态管理 |
| `components/ReasoningPanel.tsx` | `StepData[]` → Display Blocks 可视化 | 事件接收 |
| `components/ArtifactsPanel.tsx` | 沙箱文件浏览（覆盖式抽屉） | 文件内容预览（由 `FilePreview` 负责）|
| `components/SettingsCenter.tsx` | 设置中心居中弹窗：MEMORY/USER 记忆编辑 + SOUL/Skills 能力设定 | Skill 执行（后端负责）/Cron |
| `components/CronSchedule.tsx` / `CronMessageCenter.tsx` | Cron 列表与执行历史 | Cron 调度（后端 worker）|
| `components/AdminConsole.tsx` | 管理后台概览、Session 监控、用户管理、系统监控 | 业务执行、用户认证判定 |
| `services/api.ts` | 所有 HTTP/SSE 调用封装 | 业务决策 |
| `services/configApi.ts` | 记忆文件、Skills、未读计数 API | 聊天相关 |
| `utils/messageParser.ts` | AG-UI 事件 → RoundData/StepData | 渲染 |
| `utils/displayBlocks.ts` | `StepData[]` → `DisplayBlock[]`（跨 step 合并工具调用）| — |

## 3. 数据流总览

```
后端 SSE (AG-UI events)
  → services/api.ts (subscribeToRound / sendMessage)
    → StreamCallbacks (onTextMessageContent / onToolCallStart / onStateDelta ...)
      → ChatV2 setRounds/setAgentState (useState)
        → Round → ReasoningPanel → DisplayBlock 渲染
```

**关键不变量**：
- 所有后端事件必须经过 `services/api.ts` 的回调分发，不允许组件直接 `new EventSource`。
- 所有 state 更新必须经过 `boundSessionId` 校验（见 frontend-chat-spec §3）。

## 4. 全局行为契约

### 4.0 登录失败处理

- `/auth/login` 返回 401 时，登录页展示通用登录失败提示，不触发全局登出和 `window.location.href` 硬跳转。
- `/auth/login` 返回 `账户已被禁用` 时，登录页展示该明确业务提示。
- 登录页不得向普通用户展示 LDAP 内部错误细节；simple 与 ldap 都使用统一用户名密码入口。
- 非登录接口返回 401 时，`services/api.ts` 仍按会话失效处理：清理本地认证信息并跳转 `/login`。

### 4.1 会话隔离（最重要）

**任何异步回调（SSE、fetch、轮询）回调到 React state 之前，必须校验 `sessionIdRef.current === boundSessionId`。**

原因：用户可能在等待期间切换会话。不校验会导致 A 会话的消息污染 B 会话的 UI。

实现：在 `ChatV2.tsx` 的 `createStreamCallbacks({ boundSessionId })` 工厂函数中统一 `isStale()` 检查。

### 4.2 SSE 断连恢复

- 每个 `RUN_STARTED` 事件返回 `runId`，客户端记录。
- SSE 断开后，通过 `history API` 查询 round 终态（见 `_tryRecoverRoundFinished`），如已终态则触发 `onRunFinished`。
- 如仍 running，则走 `subscribeToRound` 重新订阅（携带 `lastSequence`）。

### 4.3 幂等请求

- `sendMessage` 若服务端已有对应 Round（幂等键冲突），抛 `RoundExistsError`，客户端自动切到 `subscribe` 路径。
- 切勿在组件里自行捕获后重发。

### 4.4 网络错误

- `HttpError` 携带 status，**4xx 不重试**（用户错误），**5xx 可重试一次**。
- 所有错误必须触发 `onStreamError` 回调，由 ChatV2 转为 UI 可见的错误提示。

### 4.5 轮询策略

| 场景 | 间隔 | 触发条件 |
|---|---|---|
| 会话列表刷新 | 30s | 组件挂载期间 |
| Cron 未读计数 | 60s | `App.tsx` 挂载期间 |

**禁止**在 SSE 订阅期间轮询 history（浪费资源且易产生竞态）。

### 4.6 管理后台用户管理

- `AdminConsole` 的用户页必须通过 `services/adminApi.ts` 调用 `/admin/users*` 后端接口。
- 用户页展示 `auth_type`、`enabled`、`is_admin`、周/月 token 用量与限额。
- 用户管理页采用用户目录表格布局，顶部提供本地搜索、状态/权限/认证筛选与排序控件；这些控件只筛选当前 `/admin/users` 返回的数据，不改变后端契约。
- 用户管理表格在常规桌面宽度下必须使用紧凑列宽与可收缩控件，避免出现横向滚动条。
- 用户管理页不得展示没有对应批量动作的选择框或无行为按钮。
- 用户管理页的“导出”按钮必须把当前本地搜索/筛选/排序后的可见用户 ID 按显示顺序提交给 `POST /api/admin/users/export`，并下载后端重新查询生成的 CSV；浏览器不得本地拼接敏感用户数据。
- 支持通过“新建用户”按钮打开右侧抽屉创建 `simple` 用户与 `ldap` 用户；`simple` 用户提交本地密码，`ldap` 用户不提交密码。
- 新建用户抽屉中 `simple` 账号只展示用户名与密码；`ldap` 账号才展示可选显示名。
- 支持启停用户、设置/取消管理员、更新周/月 token 限额、重置 simple 用户密码、删除用户账号；删除用户是不可恢复的硬删除，会清理该用户历史数据与 sandbox 文件，同名账号重新创建后不得继承旧数据。
- 管理员权限变更必须使用“角色选择 + 保存”的显式交互；取消管理员权限前必须二次确认。
- 当前登录管理员账号的禁用、取消管理员、删除入口必须禁用。
- 任一写操作成功后重新拉取 `/admin/users`，确保列表与后端事实源一致。
- 用户写操作成功提示必须是短暂 toast，不占用表格上方常驻空间。
- `ldap` 用户密码入口必须显示为 LDAP 认证，不提供本地密码重置控件。

### 4.7 管理后台 Session 监控

- `AdminConsole` 的 Session 监控页必须通过 `services/adminApi.ts` 调用 `/admin/rounds-tree` 获取 session 级分页聚合，再在展开单个 session 时调用 `/admin/sessions/{session_id}/rounds` 懒加载 round + 轻量 step 数据。
- `/admin/rounds-tree` 首屏只返回 session 级字段，`rounds_loaded=false` 且 `rounds=[]`；前端不得假设首屏已包含 round 树。
- Step 原始详情通过 `/admin/llm-call-records/{llm_record_id}` 按需加载。
- 筛选条件（status/search/user/page/pageSize）变化后必须折叠已展开 session，避免用旧展开状态展示新查询下的空 rounds。
- 管理后台是审计视图，可以展示 `sub_agent` child round；但每个 round 必须明确展示 `主Agent` / `子Agent`，并在子 Agent 行展示父 run 与 subagent 类型/描述。
- 主聊天历史隐藏 child round 是聊天视图契约；管理后台不得为了复用聊天历史过滤逻辑而丢失 child round 审计记录。
- Step 详情中的 LLM 请求、工具参数、工具返回如果是 JSON 字符串嵌套 JSON，前端必须递归解析并解码 `\uXXXX` 转义，避免把 `sub_agent` 的 prompt/arguments 展示成不可读的原始转义文本。

### 4.8 管理后台模型与权限

- `AdminModelAccessPanel` 负责模型目录、默认模型与模型权限包管理，通过 `services/adminApi.ts` 调用 `/admin/models*`、`/admin/model-permission-groups*` 与 `/admin/users/{user_id}/model-permission-groups`。
- 新建/编辑模型必须覆盖 provider、api_base、api_key、model_name、token 窗口、reasoning、多模态能力、启停与 tags；编辑时不填 api_key 表示保留旧密钥。
- 删除模型时若模型被默认配置或历史 Session 使用，必须要求选择启用的替换模型，并在确认弹窗中说明会迁移默认配置、历史 Session 与权限包绑定。
- 停用模型后不得留在任何模型权限包中；权限包模型选择器只允许加入启用模型。
- 模型写操作成功后必须重新拉取模型目录与权限包，确保 UI 与后端事实源一致。

### 4.9 管理后台操作日志

- `AdminConsole` 通过懒加载的独立面板展示操作日志，不继续扩大主管理组件。
- 默认查询最近 24 小时，每页 50 条；时间、动作、风险级别、目标用户、Session ID 与结果筛选均由服务端执行。
- 使用后端 `(started_at, id)` 游标进行前后翻页；`audit_log.list` 属于 L1，每次查看或刷新均留痕，本次请求自身不出现在本次结果中。
- 表格展示时间、管理员、动作、目标、结果、来源 IP、Request ID 与详情；动作使用中文主文案并保留稳定编码作为辅助信息，`started` 显示为“中断 / 结果未知”。
- 仅 `step.view` 标记为“高危 · 会话步骤原文”。其他已持久化动作按业务性质展示为“会话信息查阅”“用户信息查阅”“审计日志查阅”“账号与权限”“配置变更”“删除操作”“数据导出”“治理操作”或“外联测试”，不得使用语义不明确的“关注访问”或把所有管理动作统称为“重要变更”。
- 风险筛选支持“全部 / 高危 · 会话步骤原文 / 非高危操作”，由服务端执行；高危只匹配 `step.view`。
- 详情只展示后端返回的脱敏字段变化和补充信息（已脱敏），不提供编辑或删除入口。
- “导出当前筛选”调用 `GET /api/admin/operation-logs/export`，不得在浏览器中根据当前页数据自行生成 CSV。

## 5. 设计体系（Claude 暖色调文档流）

### 5.1 风格定位

**Warm Document Flow**：温暖、克制、清晰、专注。内容优先，零气泡，左对齐文档流。

### 5.2 布局

- 消息：左对齐文档流，**无气泡**。用户/助手通过圆形头像 + 角色标签区分。
- 间距：8px 网格系统，消息间用细分隔线（`border-b border-claude-border/50`）。
- 主内容区 `max-w-3xl`，输入框同宽。
- 左侧栏 260px 可折叠。
- 右侧面板：**覆盖式抽屉（Overlay Drawer）**，不挤压主内容（见 §5.6）。

### 5.3 色彩 Token

| Token | 色值 | 用途 |
|---|---|---|
| `claude-bg` | `#FAF9F6` | 页面背景 |
| `claude-surface` | `#F3F1EB` | 侧边栏、卡片 |
| `claude-input` | `#F5F3EE` | 输入框背景 |
| `claude-text` | `#1A1915` | 主要文字 |
| `claude-secondary` | `#6B6560` | 次要文字 |
| `claude-muted` | `#A39E95` | 占位符、辅助信息 |
| `claude-accent` | `#D4A574` | 强调色、品牌色 |
| `claude-border` | `#E8E5DE` | 边框 |
| `claude-border-strong` | `#D4D0C8` | 悬停边框 |
| `claude-hover` | `#F0EDE6` | 悬停背景 |
| `claude-success` | `#16A34A` | 成功 |
| `claude-error` | `#DC2626` | 错误 |
| `claude-warning` | `#D97706` | 警告 |

暗黑模式：**暂不支持**。

### 5.4 排版

- 正文/标题：`system-ui, -apple-system, sans-serif`
- 代码：`Fira Code, monospace`
- 字重：标题 `font-medium`（**非 bold**），正文 `font-normal`

### 5.5 组件风格

- **用户消息**：`w-7 h-7` 圆形头像 `bg-claude-text text-white`，角色标签"你"。
- **助手消息**：`w-7 h-7` 圆形头像 `bg-claude-accent/20 text-claude-accent`，角色标签"助手"。
- **按钮 Primary**：`bg-claude-text text-white rounded-xl hover:bg-claude-text/90`
- **按钮 Ghost**：`hover:bg-claude-hover text-claude-secondary`
- **发送按钮**：圆形 `w-8 h-8 rounded-full bg-claude-text text-white`
- **聊天输入**：胶囊形 `rounded-3xl`，`bg-white border border-claude-border`，聚焦 `ring-claude-accent`
- **表单输入**：`rounded-xl border-claude-border focus:border-claude-accent`
- **代码块**：`rounded-2xl border border-claude-border`，头部 `bg-claude-surface` + 语言标签，代码用 VS Code Dark Plus 主题。

### 5.6 覆盖式抽屉（Overlay Drawer）【强约束】

**禁止通过 `padding-right/width + transition-all` 挤压聊天区。**

- 右侧面板使用 `fixed right-0` + `translate-x-full ↔ translate-x-0`。
- 动效仅作用于 `transform/opacity`，避免布局属性动画（防止长列表 reflow 抖动）。
- 推荐：`transition-transform duration-300 ease-out`，backdrop `transition-opacity duration-200`。
- 打开右侧面板时，左侧栏可自动折叠释放空间；**不锁定聊天滚动**。

### 5.7 会话滚动策略

- 普通进入历史会话时，首次渲染用 `useLayoutEffect` 在浏览器绘制前定位到底部，保证用户看到最新消息。
- 从搜索结果进入且带 `scrollTarget` 时，定位到命中 round 并高亮，不执行普通底部定位。
- 新内容自动跟随**仅在用户位于底部**（距底部 < 100px）时触发。
- 不按 `sessionId` 记忆历史 `scrollTop`；切走再切回与刷新浏览器后的语义一致，都是普通进入看最新。

### 5.8 首次渲染动画禁用

历史消息首次渲染**禁用逐条动画**（`disableMotion=true`），避免大量历史消息产生瀑布式动效。仅实时新消息使用 `animate-fade-in`。

### 5.9 推理面板（Reasoning Panel）

采用 **Display Blocks** 模式（非编号列表）：
- `transformToDisplayBlocks`：`StepData[]` → `DisplayBlock[]`（ThinkingBlock / ToolGroupBlock / NarrativeBlock）。
- **跨 step 合并**：连续工具调用步骤合并为一个 ToolGroupBlock。
- **分组摘要**：`getGroupSummary()` 生成 "Edited 2 files, read a file" 风格。
- 主聊天区只显示一个思考/活动入口；工具调用和完整思考详情通过右侧覆盖式活动抽屉查看。
- 外层**无边框容器**，不使用 `rounded-xl border` 包裹整个面板。

### 5.10 文字语言约定

- 工具描述、摘要、技术术语：**英文**（"Read src/app.py"、"Edited 2 files"、"Done"）。
- UI 标签、提示、按钮：**中文**（"正在思考"、"已完成思考 3s"、"正在分析请求..."、"输入"、"输出"）。
- 例外：`sub_agent` 面向业务理解长耗时委派执行，工具摘要使用中文 `委派子任务`，并在活动抽屉中以专用胶囊展示任务标题、类型与耗时。

### 5.11 交互微动效

- Hover：颜色/透明度变化，**避免布局偏移**。
- Transition：`duration-200 ease-in-out`（**禁用 `transition-all`**）。
- Active：`active:scale-95`。
- 加载动画：3 点 `animate-dot-pulse`。

### 5.12 实时反馈（Typewriter Preview）

应用场景：推理面板、日志输出、状态栏。

**截断策略**：
- **Keep-Head**：关键信息在头部（Search/Bash/Read File），`search: "react hooks..."`
- **Keep-Tail**：追加生成型（Write/Edit File），`...import { useState } from 'react';`（字符左滚动）

## 6. 前端开发清单（Pre-Delivery Checklist）

- [ ] 图标：禁止 Emoji，统一 Heroicons 或 Lucide React
- [ ] 光标：所有可点击元素必须 `cursor-pointer`
- [ ] 反馈：所有交互元素必须有 Hover 和 Focus 状态
- [ ] 图片：`img` 必须有有意义的 `alt`
- [ ] 性能：图片用 WebP + `loading="lazy"`
- [ ] 无障碍：文本对比度 WCAG AA (4.5:1)
- [ ] 动画：尊重 `prefers-reduced-motion`
- [ ] 交互：长耗时操作必须有 loading 或实时预览
- [ ] 会话隔离：异步回调必须校验 `boundSessionId`（见 §4.1）
- [ ] 不挤压：右侧抽屉必须覆盖式（见 §5.6）

## 7. 测试约定

- 测试框架：Vitest + `@testing-library/react`（配置见 [frontend/vitest.config.ts](../../frontend/vitest.config.ts)）。
- 测试位置：`frontend/src/__tests__/`。
- 覆盖优先级：
  1. `utils/` 纯函数（messageParser、displayBlocks、fileUtils）
  2. `services/api.ts` 的 SSE 分发逻辑
  3. 组件的关键行为契约（会话切换、取消、断连恢复）
- 不要求：覆盖纯视觉/样式（交给 Storybook 或人工验证）。

## 8. 已知易错点（跨模块）

1. **useEffect 依赖漏 `sessionId`** → 跨会话污染。
2. **SSE 回调里直接 setState** 而不校验 boundSessionId → 切会话后老事件污染新会话。
3. **使用 `transition-all`** → 性能抖动，违反 §5.11。
4. **历史消息入场用 `animate-fade-in`** → 瀑布动效，违反 §5.8。
5. **右侧面板用 `padding-right`** → reflow 抖动，违反 §5.6。
6. **普通进入会话时恢复旧 `scrollTop`** → 切回会话与刷新后的语义不一致，违反 §5.7。
