# 前端 Panel Spec — 覆盖式抽屉面板

> 父级：[frontend-spec.md](./frontend-spec.md)
> 对应后端：[sandbox-spec.md](./sandbox-spec.md)（Files）、[config-spec.md](./config-spec.md)（记忆文件/Skills）、[cron-spec.md](./cron-spec.md)

覆盖组件：
- `ArtifactsPanel.tsx` — 沙箱文件浏览
- `SettingsCenter.tsx` — 设置中心居中弹窗：记忆文件编辑（MEMORY/USER）+ 能力设定（SOUL/Skills）
- `CronSchedule.tsx` / `CronMessageCenter.tsx` — Cron 看板与未读消息
- `FilePreview.tsx` — 文件内容预览（由其他面板触发）

## 1. 模块职责

所有右侧抽屉共享以下契约：
- **覆盖式打开**：`fixed right-0` + `translate-x`，不挤压主聊天区（强约束，见 frontend-spec §5.6）
- **可点击 backdrop 关闭**，但不锁定聊天滚动
- **session 切换时重置**内部状态（路径、选中项、未保存编辑等）

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

## 3. ArtifactsPanel（文件浏览）

### 3.1 数据模型

```ts
items: FileInfo[]           // 当前目录文件列表
currentPath: string         // 当前路径（'' = 根目录）
pathHistory: string[]       // 前进/后退历史
historyIndex: number
selectedFile: FileInfo|null // 面板内预览目标
targetFile?: FileInfo|null  // 外部入口传入的目标文件
targetFileNonce?: number    // 同一目标文件重复打开时用于触发 effect
```

`FileInfo.modified` 来自会话文件接口，必须按带显式时区偏移的 ISO 8601 字符串解析。

### 3.2 关键不变量

**竞态防护**：`loadDir` 必须用 `latestPathRef` 丢弃过期请求。

```ts
const latestPathRef = useRef(currentPath);

const loadDir = useCallback(async (path: string) => {
  latestPathRef.current = path;
  setLoading(true);
  const response = await apiService.getSessionFiles(sessionId, path);
  if (latestPathRef.current === path) {   // 只接受最新请求
    setItems(response.files);
  }
}, [sessionId]);
```

**session 切换重置**：

```ts
useEffect(() => {
  if (isOpen && sessionId) {
    setCurrentPath('');
    setPathHistory(['']);
    setHistoryIndex(0);
  }
}, [isOpen, sessionId]);
```

### 3.3 导航语义

| 操作 | 行为 |
|---|---|
| 点击文件夹 | `navigateTo(subPath)`：截断前进历史，追加新路径 |
| 后退 `←` | `historyIndex--` |
| 前进 `→` | `historyIndex++` |
| 上一级 `↑` | 取 `currentPath` 的父路径 `navigateTo` |
| 点击文件 | `setSelectedFile(file)` → 面板内 `FilePreview(inline)` |
| 外部目标文件 | 设置目标父目录为 `currentPath`，并直接 `setSelectedFile(targetFile)` |

### 3.4 文件操作

- **下载**：`GET /api/sessions/{sid}/files/{path}`，使用 blob 触发浏览器下载；`path` 按路径段 encode，浏览器保存名使用返回路径的 basename。
- **预览**：走 FilePreview，文本/Markdown/图片/PDF/HTML/CSV/XLS/XLSX 内联渲染；HTML 使用 `Blob URL + <iframe sandbox="allow-scripts">`，支持渲染/源码切换，不提供外部浏览器打开入口。
- **上传**：当前版本**不支持**通过面板上传，文件通过聊天附件上传到沙箱。

### 3.5 外部目标文件入口

聊天消息中的助手文件卡片通过 `targetFile` + `targetFileNonce` 打开 `ArtifactsPanel`。
`ChatV2` 必须先确认目标文件存在于当前 session 文件列表中，未命中时不得打开面板预览。

行为要求：
- 面板打开且收到 `targetFile` 时，计算目标文件父目录并写入 `currentPath` / `pathHistory`。
- 同时设置 `selectedFile`，让面板直接进入内联预览。
- 父目录文件列表加载完成后，如果列表中存在相同路径文件，必须用列表项刷新 `selectedFile` 的 `size` / `modified` / `type` 等元数据。
- session 切换或手动从 Header 打开 Files 时，外部目标状态由 `ChatV2` 清空，避免旧 session 目标污染新 session。

## 4. SettingsCenter（设置中心）

### 4.1 形态与入口

- **居中弹窗**（非右侧抽屉）：`fixed inset-0 flex items-center justify-center`，内层卡片固定宽高（`min(980px, 100vw-48px)` × `min(760px, 88vh)`），背景加 backdrop 遮罩。
- **点击卡片外遮罩关闭**：外层容器 `onClick=onClose`，内层卡片 `stopPropagation`。
- **Modal 可访问性**：设置卡片必须有 `role="dialog"` / `aria-modal="true"`，打开时背景主内容 `inert` + `aria-hidden`，Tab 焦点限制在弹窗内，Esc 走同一关闭确认逻辑。
- **未保存确认弹窗**：不得使用浏览器原生 `window.confirm`；必须使用应用内 `alertdialog`，标题「放弃未保存的修改？」，按钮「继续编辑」/「放弃并关闭」。
- **入口**：左侧栏左下角「账户菜单」下拉中的「设置」按钮触发，不再使用侧栏底部独立按钮。
- **打开不折叠左侧栏**（与 Cron 不同）。

### 4.2 功能范围

合并原 AgentConfig 与 SkillManager，分两个 section：
- **我的记忆**：`USER.md`（用户画像）、`MEMORY.md`（长期记忆）两个 tab。
- **能力设定**：`SOUL.md`（角色设定）与 Skills 启停两个 tab。

三个记忆文件（memory/user/soul）编辑共用一套加载/保存/编辑态逻辑。

### 4.3 API 契约

- `GET /api/config/agent-files/{name}`（name ∈ `memory|user|soul`）→ `{ content, version }`
- `PUT /api/config/agent-files/{name}` body `{ content }` → `{ version }`
- `GET /api/config/skills?refresh={bool}` → `{ skills: SkillInfo[], skill_issues: SkillScanIssue[], sandbox_status, inventory_state, inventory_discovered_at }`，其中 `SkillInfo.source` 为 `official | user`；缺省读取 DB 快照，显式刷新才要求远程严格扫描
- `PUT /api/config/skills/{name}` body `{ enabled }`

### 4.4 关键不变量

- **记忆是用户级**，切 session 时无需重载。
- **未保存保护**：任一记忆文件处于编辑态且内容有变更时，关闭按钮或点击遮罩关闭前必须展示应用内确认弹窗；用户取消时保持弹窗打开。
- **面板切换保护**：从 SettingsCenter 切到 Cron 或再次点击设置入口关闭时，必须复用同一未保存确认逻辑，不得直接 `setActivePanel` 绕过。
- **刷新防覆盖**：激活 `USER.md` / `MEMORY.md` / `SOUL.md` 所在 tab 或点击编辑前，必须重新拉取该文件；若该文件已有本地未保存修改，则跳过刷新以保留用户输入。
- **保存反馈**：保存成功后短暂显示「已保存」提示（约 1.8s 后自动消失）。
- **保活分区反馈清理**：数据连接与权限管控首次访问后可继续挂载以保留编辑状态，但离开分区时必须清理一次性操作提示；成功提示遵循 frontend-spec §4.5.1 自动消失，警告/错误可关闭且不得在返回分区时重现旧内容。
- **Skills 乐观更新**：toggle 后立即更新 UI，失败时回滚并提示；每个 skill 的切换请求必须独立锁定对应开关，避免并发请求乱序造成 UI 与后端状态不一致。
- **Skills 懒加载**：打开 SettingsCenter 不请求技能清单；仅进入「能力设定 · Skills」tab 时请求，之后再次进入可重新刷新。
- **Skills 快照与手动刷新**：普通加载读取用户隔离的 DB inventory 快照；标题区“刷新”及错误/降级态“重新加载”使用 `refresh=true`。刷新期间保留已有列表，防止远程恢复遮空界面；用户离开再返回 Skills tab 时必须复用尚未完成的强制刷新，不得用后发的普通快照请求覆盖刷新结果。
- **Skills 来源展示**：每项显示「官方」或「用户」来源标识；用户 Skill 不重复展示 `category=user` 标签。
- **沙箱状态展示**：`not_created` 时提示尚未创建沙箱、当前仅展示官方 Skills；`inventory_state=stale` 时提示刷新失败且正在显示上次成功清单，不得声称仅有官方 Skills；其余 `unavailable` 时提示沙箱暂不可用、当前清单为部分结果；`available` 不显示降级提示。
- **部分成功**：`GET /config/skills` 返回 200 且 `sandbox_status!=available` 时不得当作整页错误，仍应渲染返回的官方 Skills。
- **损坏项隔离**：`skill_issues` 非空时在正常 Skill 列表上方显示警示，逐项展示 path、field、message 与 suggestion；不得因为存在损坏项隐藏或禁用其他合法 Skill。刷新后问题消失时同步移除警示。
- **Skills 启停影响下一次请求**：后端在每次 LLM 请求前读取最新逻辑状态；不改变已发消息，也不删除沙箱文件。
- **默认入口状态**：从「设置」入口进入默认「我的记忆 · 用户画像」。

## 6. CronSchedule / CronMessageCenter

### 6.1 组件分工

| 组件 | 职责 |
|---|---|
| `CronSchedule` | Cron 面板根容器：顶栏 + 子视图 + 各种 overlay |
| `cron/WeekAgenda` | 周视图时间轴日历（8 列：00:00–23:00 左轴 + 7 天列，事件 absolute 按时间点位）|
| `cron/ScheduleList` | 列表视图：任务数 < 10 卡片、≥ 10 表格密排 |
| `cron/TaskFormDrawer` + `cron/SchedulePicker` | 新建/编辑表单（输出 `schedule` JSON）|
| `CronMessageCenter` | 执行记录 overlay（按日期分组、全部标已读）|

### 6.2 顶栏与导航

`CronSchedule` 顶栏由三段组成：
- 左：标题「日程」+ segmented switcher：`日历` / `列表`（不再有「消息中心」tab）
- 右：`+ 新建任务`（primary）+ `执行记录`（带未读红点 badge）+ 关闭按钮
- 「执行记录」点击 → 在 `CronSchedule` 内部以 `absolute inset-0 z-40` overlay 形式打开 `CronMessageCenter`，header 提供「← 返回日程」返回上一视图

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

## 7. FilePreview（ArtifactsPanel 内联预览 + 附件弹窗预览）

### 7.1 触发源

- `ArtifactsPanel` 点击文件
- `ChatInput` 附件点击（预览已上传的图片/文档）

> 交互约定：
> - `ArtifactsPanel` 中点击文件时，切换到面板内全幅预览视图（不再弹窗，隐藏文件列表）
> - `ChatInput` 附件点击仍使用 FilePreview 弹窗

### 7.2 类型分发

| 类型 | 渲染 |
|---|---|
| `image/*` | `<img>` |
| `.html` / `.htm` | `Blob URL` + `<iframe sandbox="allow-scripts">`（不含 `allow-same-origin`），支持 rendered/source 双视图 |
| `text/*` / `.md` | Markdown/文本渲染（`.md`/`.html` 支持源码视图） |
| `application/pdf` | `<iframe>` |
| `.csv` | 读取文本并渲染为表格 |
| `.xls` / `.xlsx` | 使用 SheetJS `xlsx` 在浏览器端解析工作簿，按 sheet 渲染表格并提供 sheet tab 切换 |
| `.zip` | 使用 JSZip 只读展示目录，不解压或执行条目内容；文件最大 10 MiB、目录最多 2000 项 |
| 其他 | 占位图 + 下载按钮 |

### 7.3 关键不变量

- **内容懒加载**：打开时才请求；ZIP 目录预览限制为 10 MiB，超过后提示下载查看。
- **普通大文件分流（待定）**：普通文件超过 5 MiB 时仅显示下载按钮的能力本期暂不实现，不作为当前验收项。
- **session 隔离**：切 session 时，ArtifactsPanel 路径与选中文件重置到根目录；附件弹窗预览自动关闭。
- **HTML 沙箱约束**：HTML 预览 iframe 仅允许 `allow-scripts`，严禁与 `allow-same-origin` 同时启用。
- **禁止外部打开**：HTML 预览仅允许在沙箱 iframe 内查看，不提供“在浏览器中打开”入口，避免离开受限上下文。
- **全幅预览优先**：ArtifactsPanel 文件点击进入全幅预览模式，返回列表后可继续浏览其他文件。
- **内联缓存复用**：ArtifactsPanel 预览优先复用缓存结果，减少重复点击同一文件时的等待。

## 8. 测试清单

- [ ] ArtifactsPanel 快速切换目录不出现"旧数据覆盖新数据"
- [ ] 抽屉打开时聊天区宽度不变（不触发 reflow）
- [ ] session 切换后 ArtifactsPanel 回到根目录
- [ ] SettingsCenter 从账户菜单「设置」入口打开，居中弹窗；无未保存修改时点击卡片外遮罩可关闭，有未保存修改时需确认
- [ ] 打开 SettingsCenter 不请求 Skills；进入「能力设定 · Skills」后才加载，并显示官方/用户来源
- [ ] Skills 接口返回 `not_created` / `unavailable` 时显示降级提示并保留官方 Skills，`available` 时合并用户 Skills
- [ ] CronSchedule 顶栏显示日历/列表 segmented switcher，无「消息中心」tab
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
- [ ] 面板打开左侧栏自动折叠（仅 Cron），SettingsCenter 居中弹窗与 ArtifactsPanel 打开均不折叠

## 9. 已知易错点

1. **用 `padding-right` 避让抽屉** → 违反 frontend-spec §5.6，出现 reflow 抖动。
2. **ArtifactsPanel 没做竞态防护** → 点击目录过快导致旧数据覆盖。
3. **FilePreview 每次打开都重新请求大文件** → "大文件仅下载"分流待定，本期未实现。
4. **抽屉 z-index 低于 Modal** → 点击抽屉里的按钮打开 FilePreview 后被抽屉遮住。
5. **关闭动画未延迟卸载** → 动画被截断，抽屉瞬间消失。
6. **Skill toggle 直接等后端响应再更新 UI** → 感觉卡顿；应乐观更新。
7. **把 sandbox_status 非 available 当成整页失败** → 会隐藏仍可用的官方 Skills；应显示状态提示并保留部分结果。
