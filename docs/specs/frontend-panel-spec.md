# 前端 Panel 与一级能力页 Spec

> 父级：[frontend-spec.md](./frontend-spec.md)
> 对应后端：[config-spec.md](./config-spec.md)（记忆文件/Skills）、[mcp-spec.md](./mcp-spec.md)、[cron-spec.md](./cron-spec.md)
> Session 文件工作台已独立到 [frontend-session-files-spec.md](./frontend-session-files-spec.md)。

覆盖组件：
- `SettingsCenter.tsx` — 设置中心居中弹窗：MEMORY/USER/SOUL 与权限管控
- `SchedulePage.tsx` / `CronSchedule.tsx` — “日程管理”一级页面
- `SkillsPage.tsx` / `SkillsPanel.tsx` — Skills 一级页面
- `ConnectionsPage.tsx` / `McpConnectionsPanel.tsx` — “数据”一级页面（底层 MCP 连接）
- `CronSchedule.tsx` / `CronMessageCenter.tsx` — Cron 看板与未读消息

## 1. 模块职责

普通右侧抽屉共享以下契约：
- **覆盖式打开**：`fixed right-0` + `translate-x`，不挤压主聊天区（强约束，见 frontend-spec §5.6）
- **可点击 backdrop 关闭**，但不锁定聊天滚动
- **session 切换时重置**内部状态（路径、选中项、未保存编辑等）

日程/Skills/数据是保活的一级页面，SettingsCenter 是居中 modal，Session 文件是主布局分栏；这些主 surface 均不套用 Overlay Drawer 结构。新建/编辑任务等二级操作仍可使用组件内 Drawer。

**不职责**：抽屉自身不应发起 SSE 订阅；面板内的数据通过 REST 拉取。

## 2. 统一交互契约

### 2.1 打开 / 关闭

```tsx
<div className={`fixed right-0 top-0 h-full w-[420px] bg-claude-bg shadow-2xl z-40
                 transition-transform duration-300 ease-out
                 ${isOpen ? 'translate-x-0' : 'translate-x-full'}`}>
  ...
</div>

{isOpen && (
  <div className="fixed inset-0 bg-black/20 z-30 transition-opacity duration-200"
       onClick={onClose} />
)}
```

### 2.2 延迟卸载（避免关闭动画被切断）

```ts
const [isMounted, setIsMounted] = useState(isOpen);

useEffect(() => {
  if (isOpen) setIsMounted(true);
}, [isOpen]);

// 动画结束后才真正卸载
const handleTransitionEnd = () => {
  if (!isOpen) setIsMounted(false);
};
```

### 2.3 z-index 规范

| 层 | z-index |
|---|---|
| 抽屉 panel | `z-40` |
| Backdrop | `z-30` |
| Modal（FilePreview 弹窗）| `z-50` |
| 顶部 Toast | `z-60` |

## 3. Session 文件工作台

文件目录、多标签、`closed/split/full`、双图标按钮、格式分发、HTML Blob、Office/PPT 预览和移动端隔离全部以 [frontend-session-files-spec.md](./frontend-session-files-spec.md) 为唯一事实源。

本文件不再把 `ArtifactsPanel` 描述为 Overlay Drawer，也不定义“结果文件”或用户工作区。

## 4. SettingsCenter（设置中心）

### 4.1 形态与入口

- **居中弹窗**（非右侧抽屉）：`fixed inset-0 flex items-center justify-center`，内层卡片固定宽高（`min(980px, 100vw-48px)` × `min(760px, 88vh)`），背景加 backdrop 遮罩。
- **点击卡片外遮罩关闭**：外层容器 `onClick=onClose`，内层卡片 `stopPropagation`。
- **Modal 可访问性**：设置卡片必须有 `role="dialog"` / `aria-modal="true"`，打开时背景主内容 `inert` + `aria-hidden`，Tab 焦点限制在弹窗内，Esc 走同一关闭确认逻辑。
- **未保存确认弹窗**：不得使用浏览器原生 `window.confirm`；必须使用应用内 `alertdialog`，标题「放弃未保存的修改？」，按钮「继续编辑」/「放弃并关闭」。
- **入口**：左侧栏左下角「账户菜单」下拉中的「设置」按钮触发，不再使用侧栏底部独立按钮。
- **打开不折叠左侧栏**。

### 4.2 功能范围

设置中心分三个 section：
- **我的记忆**：`USER.md`（用户画像）、`MEMORY.md`（长期记忆）两个 tab。`MEMORY.md` 是可编辑、可检索的持久化数据，不会整份自动拼入 system prompt，也不会因保存而触发模型自动提炼。
- **角色设定**：`SOUL.md`。
- **权限管控**：独立懒加载权限页。

三个记忆文件（memory/user/soul）编辑共用一套加载/保存/编辑态逻辑。
设置导航只展示上述三个设置分区；Skills 与数据只能从应用左侧一级导航（移动端为一级主导航）进入，不在设置中提供重复捷径。

### 4.3 API 契约

- `GET /api/config/agent-files/{name}`（name ∈ `memory|user|soul`）→ `{ content, version }`
- `PUT /api/config/agent-files/{name}` body `{ content }` → `{ version }`

### 4.4 关键不变量

- **记忆是用户级**，切 session 时无需重载。
- **未保存保护**：任一记忆文件处于编辑态且内容有变更时，关闭按钮或点击遮罩关闭前必须展示应用内确认弹窗；用户取消时保持弹窗打开。
- **面板切换保护**：从 SettingsCenter 切到日程一级页或再次点击设置入口关闭时，必须复用同一未保存确认逻辑，不得直接 `setActivePanel` 绕过。
- **刷新防覆盖**：激活 `USER.md` / `MEMORY.md` / `SOUL.md` 所在 tab 或点击编辑前，必须重新拉取该文件；若该文件已有本地未保存修改，则跳过刷新以保留用户输入。
- **保存反馈**：保存成功后短暂显示「已保存」提示（约 1.8s 后自动消失）。
- **权限保活**：权限管控首次访问后保活；MCP 工具发布变化通过 App refresh token 触发已访问权限页重新读取。
- **默认入口状态**：从「设置」入口进入默认「我的记忆 · 用户画像」。

## 5. 日程、Skills 与数据一级页面

- 路由为 `/schedule`、`/skills`、`/connections`，尾斜杠等价；左侧栏和移动端主导航均提供入口和 `aria-current="page"` 活动态。
- `ChatRuntimeProvider`、真实 `ChatV2`、草稿、waiting Interaction 与 transport 在一级页面切换时保持挂载，不得 abort 或重复订阅。
- 日程页由 `SchedulePage` 提供与 Skills/数据一致的图标、eyebrow、H1、说明和 1120px 内容基线；`CronSchedule variant="page"` 只负责日历/列表工具栏和业务内容，不再渲染顶层关闭按钮或页面遮罩。
- Skills 为唯一状态 owner：缺省读 inventory 快照，显式刷新才严格扫描；页面以真实 inventory 组成响应式技能卡片目录，提供搜索、带数量的状态/来源分段筛选、逐项乐观启停与明确的保存中状态，不伪造推荐/热度/作者等市场数据；同时保留迟到刷新 mutation epoch、防损坏项折叠提示，以及 `not_created/unavailable/stale` 部分成功语义。
- “数据”是 MCP 连接的用户侧产品命名，复用 `McpConnectionsPanel`；dirty editor/tool manager 对 PUSH/REPLACE/Back/Forward/刷新统一阻塞。应用内 alertdialog 的按钮保留原生 Enter/Space 语义。
- Settings 不得提供 Skills/数据的重复入口，也不得创建第二套 Skills/MCP 状态。

## 6. CronSchedule / CronMessageCenter

### 6.1 组件分工

| 组件 | 职责 |
|---|---|
| `SchedulePage` | 日程一级页面壳：标题、说明、宽度与响应式基线 |
| `CronSchedule` | 页面内日历/列表工具栏、业务子视图与二级操作 |
| `cron/WeekAgenda` | 周视图时间轴日历（8 列：00:00–23:00 左轴 + 7 天列，事件 absolute 按时间点位）|
| `cron/ScheduleList` | 列表视图：任务数 < 10 卡片、≥ 10 表格密排 |
| `cron/TaskFormDrawer` + `cron/SchedulePicker` | 新建/编辑表单（输出 `schedule` JSON）|
| `CronMessageCenter` | 执行记录页面子视图（按日期分组、全部标已读）|

### 6.2 顶栏与导航

`SchedulePage` 顶部标题为「日程管理」，视觉结构与 Skills/数据一致。`CronSchedule` 页面内工具栏由两段组成：
- 左：segmented switcher：`日历` / `列表`（不再有「消息中心」tab）
- 右：`+ 新建任务`（primary）+ `执行记录`（带未读红点 badge）；一级页不提供关闭按钮
- 「执行记录」点击 → 在 `CronSchedule` 的正常 flex 文档流内切换到 `CronMessageCenter` 子视图，不得使用 `absolute/fixed` 覆盖层；顶部提供「返回日程」，未读统计与「全部标已读」保持完整横向布局

### 6.3 未读消息轮询与红点

- `App.tsx` 每 60s 调 `getUnreadCount()` → `cronUnreadCount` → `SessionList` 入口按钮 + `CronSchedule` 顶栏「执行记录」按钮均显示同源红点
- `CronMessageCenter` 内部维护本地 `unreadCount`（从 `runs` 派生）；进入面板**不**自动 mark-read

### 6.4 关键不变量

- **进入「执行记录」不做全量隐式标已读**：仅打开面板时不触发 mark-read
- **仅终态未读 run 展开即标记该条已读（running 除外）**：点击未读终态 run 卡片展开时调用 `POST /api/cron/runs/mark-read?run_id=...`，红点即时消失并刷新未读计数；running 记录不触发标记
- **保留显式全量入口**：`CronMessageCenter` 顶部「全部标已读」按钮 → `POST /api/cron/runs/mark-read`（不传 `run_id` = 全量）；按钮在 `unreadCount === 0` 时 disabled
- **未读红点判定**：`!run.is_read`（含 success/failed/cancelled 等所有终态），不再排除 failed
- **执行记录排序**：按日期分组（`今天`/`昨天`/`M月D日`），组内 `failed && unread` 优先，其次 `started_at desc`
- **日期分组时区口径一致**：分组 key 与 `今天/昨天` 判断都按浏览器本地时区计算，避免 UTC 基准导致错标
- **日期分组标题不吸顶**：日期行作为普通分组标题展示，随列表自然滚动
- **WeekAgenda 是时间轴日历**：左侧固定 56px 时间轴（00:00–23:00，每小时 40px）；任务按 `HH:MM` absolute 定位到对应小时格
- **WeekAgenda 任务色统一**：启用态 = 柔和米橘色块（`bg-amber-100/70` + `border-amber-400`）；disabled = `bg-claude-surface` 灰调；不再用 6 色区分任务
- **WeekAgenda 全周仅一个选中**：点击任意事件块则选中，不同事件互斥；hover 事件块也会临时浮出工具条
- **WeekAgenda 点击外部自动收起**：选中后，点击事件块/浮出工具条以外任意区域会自动取消选中
- **WeekAgenda 事件条信息结构**：每条固定展示「时间 + 标题」两行，状态反馈由运行按钮与执行记录承载
- **WeekAgenda 展示与管理分层**：周视图展示启用任务，暂停任务在列表视图提供启用/编辑/删除管理入口
- **WeekAgenda 浮出工具条仅一个图标**：只保留 `运行任务`；暂停/启用统一在列表视图管理，事件块保持安静
- **ScheduleList 密排阈值**：按当前筛选/搜索结果数判定；`filteredTasks.length >= 10` 切表格，< 10 保持卡片
- **ScheduleList 默认排序**：按 `cronTime(HH:MM)` 升序；无法提取固定时间（如 interval）的任务排在最后，再按任务名升序兜底
- **ScheduleList 顶栏能力**：提供 `全部/启用中/已暂停/最近失败` 筛选、任务名/cron 搜索与总览统计（总数/启用/暂停/失败）
- **ScheduleList 空态语义**：仅在 `tasks.length === 0` 时显示「暂无日程」；筛选/搜索无匹配时显示「未找到匹配任务」并提供「清空筛选与搜索」
- **ScheduleList 仅就地操作**：卡片与表格行不支持点击进入详情页；操作统一在行内按钮与菜单完成
- **ScheduleList 主次操作分离**：主操作为 `执行` + `启用/暂停 Switch`；次操作仅保留（编辑/删除）并收纳至 `更多操作` 菜单
- **ScheduleList 菜单收起规则**：点击菜单外任意区域时，`更多操作` 菜单自动收起
- **ScheduleList 信息层级压缩**：meta 行只展示频率与状态，不展示上次/下次执行时间（避免预览时间抖动）
- **启停切换必须可点击**：仅 `ScheduleList` 状态按钮可触发 `PUT /api/cron/jobs/{name}`（仅传 `enabled`），成功后本地状态即时更新；`WeekAgenda` 不提供启停入口
- **启停切换默认静默成功**：不弹成功提示条（避免频繁操作噪音）；失败时才展示错误提示
- **手动执行反馈策略**：点击执行后通过行内按钮状态反馈进度与结果，失败时展示错误提示
- **表单保存失败**：仅在任务抽屉内展示可修正的内联错误，不得再调用 `window.alert` 形成重复、阻塞式提示
- **WeekAgenda 运行反馈需动态可见**：点击「运行任务」后，运行按钮进入 `aria-busy=true` 状态并显示旋转加载图标；该动画不依赖 hover，鼠标移开后仍持续，直至任务退出 running
- **Cron 任务新增/编辑**走 `TaskFormDrawer`：表单产出 `schedule` JSON，后端 `schedule_to_cron()` 双写 `cron_expr`；前端**不展示 cron 表达式原文**给用户编辑
- **TaskFormDrawer 字段最小化**：仅保留「任务名 / 任务内容 / 执行时间 / 启用」；不再区分显示名与执行内容，不显示摘要与未来执行预览
- **TaskFormDrawer 编辑回填**：编辑时「任务内容」优先回填 `content`；若 `content` 为空（兼容老数据）则回填 `description`，确保用户可直接在原内容上修改
- **编辑老数据**（`schedule == null` 而 `cron_expr != null`）：表单中时间区域只读展示 `cron_expr`，需要点「重新选择」才能进入 `SchedulePicker`
- **手动触发**：`POST /api/cron/jobs/{job_name}/run`，立即返回 `run_id`；执行结果不注入聊天 Session，由「执行记录」展示

## 7. FilePreview

文件格式注册表、Markdown 相对资源、HTML sandbox/Blob 新标签、Office→PDF、PPT Canvas 虚拟浏览、每标签滚动和降级链全部以 [frontend-session-files-spec.md](./frontend-session-files-spec.md) 为唯一事实源。聊天附件复用 `FilePreview` modal 时强制只读，只提供预览与下载；Markdown、CSV、XLSX 编辑仅允许在 Session 文件工作台发生。

## 8. 测试清单

- [ ] Session 文件工作台满足 `frontend-session-files-spec` 全部验收项
- [ ] SettingsCenter 从账户菜单「设置」入口打开，居中弹窗；无未保存修改时点击卡片外遮罩可关闭，有未保存修改时需确认
- [ ] `/schedule`、`/skills`、`/connections` 深链与尾斜杠刷新恢复；ChatV2/transport/draft/waiting 保活
- [ ] 数据页 dirty 状态能阻止按钮导航及 Back/Forward；取消/确认只执行一次
- [ ] Skills 迟到刷新不覆盖已成功的乐观切换，且降级状态仍展示合法清单
- [ ] CronSchedule 顶栏显示日历/列表 segmented switcher，无「消息中心」tab
- [ ] 执行记录作为页面内子视图替换日历/列表内容，返回与全部标已读不重叠、不挤成竖栏
- [ ] WeekAgenda 呈现为时间轴日历：左侧 00–23 时间轴 + 7 列，任务按时间 absolute 定位；任务色块统一柔和米橘色，disabled 灰调
- [ ] WeekAgenda 点击事件块或 hover 才浮出工具条（仅运行图标）
- [ ] WeekAgenda 事件条固定展示时间与标题两行，状态反馈由运行按钮/执行记录承载
- [ ] WeekAgenda 选中后点击事件块/工具条外区域会自动取消选中
- [ ] WeekAgenda 浮出工具条仅保留运行图标，aria-label 语义正确
- [ ] 暂停任务不出现在周视图日历列；切到列表视图仍可看到并执行启用/编辑/删除
- [ ] ScheduleList 当前筛选结果数 < 10 为卡片、≥ 10 为表格（`data-testid=schedule-list-table` 可检测）
- [ ] ScheduleList 顶栏包含筛选、搜索、统计，筛选与搜索可叠加生效
- [ ] ScheduleList 筛选/搜索无匹配时显示「未找到匹配任务」+「清空筛选与搜索」；仅真正无任务时显示「暂无日程」
- [ ] ScheduleList 行点击不会进入详情页；点击按钮区仅触发行内操作
- [ ] ScheduleList 主操作为「执行 + Switch」，次操作在「更多操作」菜单可访问（编辑/删除）且点击菜单外区域会自动收起
- [ ] ScheduleList meta 行不展示上次/下次执行时间，仅保留频率与状态信息
- [ ] 在列表视图点击「暂停/启用」会触发 `PUT /api/cron/jobs/{name}`，并立即反映新状态（周视图无启停入口）
- [ ] 点击执行后通过行内按钮状态反馈执行进度与结果；失败时展示提示
- [ ] 周视图点击「运行任务」后，按钮进入 `aria-busy=true` 且图标旋转；鼠标移开后动画持续直到任务结束
- [ ] TaskFormDrawer 仅展示「任务名 / 任务内容 / 执行时间 / 启用」，不展示「显示名 / 摘要 / 未来 5 次执行」
- [ ] 进入「执行记录」面板不自动清未读；展开未读终态 run 卡片触发单条标已读，running 记录不触发
- [ ] 「全部标已读」按钮在有未读时可点击、点击后红点归零；无未读时 disabled
- [ ] failed 状态的未读 run 卡片显示红点
- [ ] 日程一级页保持左侧栏及其当前宽度；SettingsCenter 居中弹窗不折叠；Session 文件按独立分栏语义运行

## 9. 已知易错点

1. **用 `padding-right` 避让普通抽屉** → 违反 frontend-spec §5.6，出现 reflow 抖动；Session 文件例外必须使用专属 flex/grid 分栏。
2. **POP 导航绕过 MCP dirty guard** → 浏览器后退会静默丢编辑，必须由统一 router blocker 处理。
3. **Settings 再提供 Skills/MCP 入口或持有其状态** → 与一级导航重复并形成双 owner，禁止。
4. **Skills 只在 toggle 开始记录 mutation epoch** → 成功后迟到刷新可回滚，开始/完成都必须形成边界。
5. **关闭动画未延迟卸载** → 动画被截断，抽屉瞬间消失。
