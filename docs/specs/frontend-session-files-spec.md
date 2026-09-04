# 前端 Session 文件工作台 Spec

> 父级：[frontend-spec.md](./frontend-spec.md)<br>
> 后端：[sessions-spec.md](./sessions-spec.md)<br>
> 本模块只拥有当前 session 目录状态；不定义“结果文件”，但可通过 WorkspaceService 显式把当前版本复制到用户工作区。

## 1. 职责与边界

Session 文件工作台负责：

- 浏览 `GET /api/sessions/{id}/files` 返回的完整 session 目录；
- 在聊天旁以分栏方式打开一个或多个文件；
- 在 `closed / split / full` 三种布局间切换；
- 按格式选择安全的预览器，并允许当前 Session 内的 Markdown 与电子表格编辑；
- 保留每个 session 自己的目录、标签、活动文件和滚动状态。
- 在所有 Session 文件预览标题栏提供“存入工作区”；dirty MD/XLSX 先保存并取得最新 opaque revision，旧历史附件缺 revision 时按完整父目录刷新 metadata，若内容已变化则拒绝复制旧预览。

明确不负责：

- 区分输入附件与 Agent 产物；
- 持有用户级工作区树和编辑状态；Session 文件标题栏仅发起“存入工作区”复制，工作区状态仍由独立 WorkspaceProvider/WorkspaceService 拥有；
- Word/PPT 在线编辑；
- 运行、暂停、恢复、取消等聊天状态机。

文件预览失败只能影响当前文件标签，不得改变 Round/Session 状态。

Session 与持久工作区右侧面板是两个独立 owner：Session 使用 `ownerSessionId + ownerEpoch`；工作区使用 `{scope:'workspace', id:'persistent', epoch}`。切换前同步触发当前 owner 的内容抓取并写入各自应用级 outbox，随后立即切换；Session outbox 持久化到 IndexedDB，Workspace outbox 只存在于当前页面内存，远端 flush 均不阻塞导航。两者不得共用标签、dirty map 或保存回执，迟到回执只按草稿 key 与 generation 更新原 owner。

## 2. 状态模型

```ts
type SessionFilesLayout = 'closed' | 'split' | 'full';

interface SessionFileTab {
  path: string;
  name: string;
  type: string;
  size: number;
  modified: string;
}

interface SessionFilesState {
  layout: SessionFilesLayout;
  chatRatio: number; // 0..100，表示聊天 pane 占容器宽度的百分比
  currentPath: string;
  pathHistory: string[];
  historyIndex: number;
  openTabs: SessionFileTab[];
  activePath: string | null;
  listScrollTop: number;
  searchQueries: Record<string, string>; // currentPath -> query
  previewScrollTops: Record<string, number>;
}
```

- 状态必须按 `sessionId` 隔离；A/B session 不得共享路径、标签、活动文件或目录搜索词。
- 每个 session 在保持 `split` 时独立保留 splitter 比例；关闭文件区再通过聊天顶栏打开时恢复标签、路径和预览，但比例固定重置为聊天 45% / 文件 55%，不得继承上次的极窄比例。
- 切回已访问 session 时恢复其文件状态。
- 经过“新建会话”（`sessionId=""`）再返回旧 session 时也必须恢复；状态 owner 必须始终挂载。
- session 删除后清理对应状态。
- session 文件状态独立于 `chatRuntimeReducer`；聊天事实仍由 `ChatRuntimeProvider` 投影。
- 左侧“会话 / 工作区”只切换浏览列表：查看会话列表不得关闭已打开的工作区右侧面板、清除 entry 深链或卸载工作区树；切回工作区时保留原展开目录、搜索和活动文件，同时重新读取根目录、当前展开目录及有效搜索结果，以服务端权威状态替换隐藏期间的旧投影。桌面侧栏和移动全屏 Sheet 必须复用同一个常驻列表/工作区 owner，不能同时挂载两套状态。只有用户真正选择 Session、新建会话或显式关闭工作区文件时才 flush 并切换/关闭 owner。
- Workspace canonical URL 只保存稳定 `entry_id`（`/workspace?entry=<id>`）；可变 path/name/breadcrumb 从统一 Workspace 投影读取。重命名和移动不得向 URL 复制 path，带旧 `path` 参数的兼容深链在成功解析 entry 后立即 replace 为 entry-only URL。
- 工作区单标签关闭先同步抓取 dirty 内容到当前页面内存 outbox，再立即卸载标签；面板级 flush 必须先快照全部 dirty handle，并在任何 `await` 前同步调用全部 `saveDirty`，随后用 `allSettled` 聚合失败，避免面板卸载清空 ref 后漏抓后续标签。网络歧义、5xx 或 mutation 尚在处理时继续使用原 key 后台重试；确定性终态失败丢弃该 Workspace 草稿并在下次打开文件时提示已恢复最近保存版本。多标签中关闭当前文件后，左侧再次点击同一 entry 必须产生新的目标事件并重新打开。
- 模型或定时任务通过统一的 Workspace invalidation 入口更新已打开的 clean 工作区文件；入口按稳定 `entry_id` 合并同批事件并丢弃重复或较旧 revision，只在命中的右侧标签内后台获取新版本。Cron invalidation 由 App 常驻通道负责：右侧存在当前 Workspace 文件或 Workspace 面板已挂载时立即读取一次最新 runs，此后仅在页面前台以 15 秒轻量周期读取最近 10 条并在恢复前台时立即补拉；不得依赖用户打开“执行记录”，面板卸载后必须停止定时器并忽略迟到响应。**只有旧内容已经 ready** 才能进入后台刷新，旧内容保留到新内容就绪后再原子替换正文与 version，面板、活动标签、聊天、焦点与滚动不得卸载或出现整面 loading。首次内容尚未 ready 时即使同一 entry 的 revision 再变化，也必须继续显示稳定 loading，禁止提前露出空内容、类型降级卡或“下载原文件”。本地标签 dirty 时保留草稿和原 base version并继续 autosave，服务端三方合并后以人的重叠行/单元格为准，再原位载入合并版本。普通界面不显示版本冲突或 change-set 决策条。
- 工作区文件重命名/移动后若 `current_version_id` 未变，右侧只更新标签、路径和 entry revision，保留已加载内容与 dirty 草稿，不进入“有新版本”冲突态。若重命名发生在首次内容请求完成前，旧请求取消后必须用新路径继续加载，Markdown/XLSX 不得留在空白画布。

## 3. 顶栏按钮

### 3.1 查看文件

- 使用 24×24 图标按钮，图标 15–16px，透明背景、4px 圆角。
- 文案恒为 `aria-label="查看文件"`；`aria-expanded/aria-pressed` 表示当前文件区是否打开。
- 点击时打开或聚焦文件区，不把一个控件同时命名为“查看/结果/工作区”。
- 文件区顶栏的“收起文件”关闭文件区；关闭后焦点返回“查看文件”。

### 3.2 展开/收起面板

- 聊天顶栏的 24×24 按钮是右侧文件工作台总开关：`closed` 时显示“展开面板”并以聊天 45% / 文件 55% 的固定距离恢复右侧 `split`，`split` 时显示“收起面板”并关闭右侧、让聊天铺满。文案描述点击后将执行的动作，禁止反向命名。
- 存在 session 或活动工作区文件目标时，该按钮在任意 viewport 的 `closed/split` 均显示；不得因窄宽隐藏。`full` 时聊天不可见，由文件侧按钮负责返回 `split`。
- 它不得折叠或展开全局 SessionList/历史侧栏；主导航栏状态仍由 App 自己管理。
- 文件区顶栏的对称按钮控制文件 `split ↔ full`，图标 14×14。
- Tooltip 文案和 `aria-label` 必须一致；不得用只有点击事件的 `span` 代替按钮。

按钮 hover/active 背景使用 `#f4f5f6`；focus-visible 必须有可见焦点环。不得复制第三方私有 SVG。

## 4. 布局

- 聊天顶栏与文件工作台第一行工具栏统一为 56px（`h-14`），使用同一 `border-claude-border` 底边；分栏时两条水平分隔线必须在同一视觉基线上，文件标签栏从该基线下方开始。
- `closed`：聊天占满主内容区；桌面仍在最右边缘保留 splitter，向左拖动即可恢复 `split`。
- `split`：聊天与文件工作台并排，中间为键盘可操作的 splitter；先按容器几何将比例 clamp 到 `0..100`。两个方向都不得设置提前吸附阈值：从 `full` 向右拖出任意正比例时立即显示聊天，从 `closed` 向左拖出任意比例时立即显示文件；仅在比例真正到达 0/100 时进入端点状态。
- splitter 连续拖动时按 `requestAnimationFrame` 合并指针事件，并直接更新容器 CSS 比例；除首次离开 `full/closed` 端点需要恢复 `split` 外，React 布局状态与持久化比例只在手势结束时提交，禁止每个 `pointermove` 重渲染整棵聊天/预览树或同步写存储。
- `full`：文件工作台占满主内容区；聊天运行态仍保持挂载，桌面在文件区最左边缘只保留一根共享 splitter。它覆盖导航/主内容的同一物理边界并按首次移动方向锁定：向左收起导航，向右立即恢复 `split`；同一手势即使反向移动也不得串到另一操作。
- splitter 的 `Home/End` 分别到达 `0/100`，方向键以小步调整并可到达两端；到达 `0` 时归一为 `full`，到达 `100` 时归一为 `closed`。两个端点都必须继续渲染可拖动、可聚焦的边缘 splitter；聊天顶栏按钮重新打开时固定恢复 45/55，边缘拖动则使用新的指针比例，不能出现按钮已高亮但文件区宽度为 0 的假打开状态。
- 窄宽仍保持聊天/文件并排与共享 splitter，不得强制切换 fixed 覆盖层、隐藏总开关或禁用聊天；用户自行通过 0..100 splitter、聊天侧总开关和文件侧 full 按钮决定空间分配。
- 切换布局保留聊天滚动、输入焦点、文件列表滚动和预览滚动。
- 聊天消息列、错误提示、交互卡与 composer 使用同一左侧 gutter，并放宽到 `max-w-5xl`；禁止 `mx-auto` 随 pane 宽度重新居中，拖动 splitter 时内容左边界必须保持稳定，聊天全宽时也不得缩回窄阅读列。
- 动画只使用明确的 width/flex/transform 属性，180–220ms，并尊重 `prefers-reduced-motion`。

Session 文件工作台是主布局的一部分，不适用普通右侧 Overlay Drawer 的“不挤压聊天区”约束。

## 5. 目录与多标签

- 目录请求使用单调递增 request id；迟到响应不得覆盖新路径。
- 支持后退、前进、上一级和根目录。
- 目录在前、文件在后，排序沿用后端结果。
- 目录视图顶部提供当前目录搜索：只在 `GET /api/sessions/{id}/files` 返回的完整条目中按 `name` 做客户端子串筛选，比较前 trim 搜索词并忽略英文大小写，不发起额外请求。搜索词按 `sessionId + currentPath` 隔离并在目录导航、文件预览和 Session 切换后恢复；空词显示全部条目，无匹配项时不得显示“空目录”。`Escape` 或清空按钮清除当前目录搜索，底部计数在筛选时显示“匹配数 / 总数”。
- 点击目录进入目录；点击文件打开或激活相同路径标签。
- 同一路径最多一个标签。
- 关闭活动标签后选择相邻标签；最后一个标签关闭后显示目录列表。
- 列表与活动标签中的元数据应以最新目录响应刷新。
- 提供显式刷新；当前 Round 数量/状态、sending/resuming 或外部目标文件变化时刷新当前目录，面板打开期间生成的新文件不得长期不可见。
- 从聊天文件卡片进入时，目标标签、活动文件和父目录必须在文件工作台首帧同步投影；不得先绘制目录列表、再由被动 effect 切换预览。父目录只发起一次刷新，禁止外部目标 effect 与通用目录 effect 重复请求同一路径。外部目标 nonce 在 ChatV2 生命周期内单调递增，关闭后重复打开同一路径也必须产生新目标事件。
- 目录刷新仅在文件版本元数据确实变化时替换标签中的 `FileInfo`；同版本对象必须保持稳定，禁止因此重新初始化活动或隐藏标签的预览请求。
- session 切换期间的迟到目录/预览响应必须丢弃。
- 每个文件标签保存独立预览滚动位置；切换 A/B 不继承彼此位置，关闭标签后焦点落到新活动标签或原目录触发项。

## 6. 预览注册表

所有预览器通过统一分发层选择，加载操作支持 `AbortSignal`。缓存身份优先使用 `content_mode/source/entry_id/version_id/snapshot_path/opaque revision/ref_id/preview URL`；`modified + size` 只用于无结构化版本的兼容来源。

聊天卡、历史附件和 composer 附件统一进入既有右侧文件工作台，不再创建全屏遮罩预览壳。Session 来源复用 `ArtifactsPanel` 的标签栏；Workspace 内容复用 `WorkspaceFilesPanel`。统一 Workspace 投影保存 deleted entry 集合；tombstone 到达时必须进入 Chat runtime reducer，清理所有已加载 Session/Round 的 Workspace 附件和助手引用，并拒绝迟到 history 重新投影。文件工作台同步关闭对应 captured/current 标签、清除 `.workspace-snapshots/<entry>/` 陈旧目录项并回到 Session 根目录、淘汰 entry/version 预览缓存；刷新后由 history 权威投影做同一过滤。Workspace 来源或 `.workspace-snapshots/<entry>/` 平台副本不得显示“存入工作区”。其余有效快照必须显式只读，只提供预览与下载；当前 Session 的 Markdown、CSV、XLSX 编辑仍只由文件工作台拥有。

| 类型 | 预览语义 |
|---|---|
| Markdown | GFM、默认关闭的可折叠 outline、平面报告排版、表格滚动；当前 Session 直接进入所见即所得编辑，外部只读来源保持渲染预览；相对图片和文件链接通过 session 鉴权 URL 解析，拒绝越界 |
| HTML | `srcDoc` iframe `sandbox="allow-scripts"`、源码、下载、安全 Blob 新标签 |
| TXT/代码/JSON | 语法高亮、行号/换行、复制 |
| 图片/SVG | 适应窗口、缩放、旋转；SVG 不注入主 DOM |
| PDF | 浏览器/PDF viewer 内联预览与下载 |
| DOC/DOCX | 首选 `render=pdf`；DOCX 转换失败可用 DOMPurify 清洗后的 Mammoth HTML，DOC 失败则下载 |
| PPT/PPTX | 首选 `render=pdf`，再由 PDF.js 提供缩略图、连续滚动、当前页同步、±2 主 Canvas 虚拟挂载和最高 2× DPR；失败降级浏览器 PDF/下载 |
| `.slides` | 当前没有可用的 schema renderer 或 PDF 转换器；显示明确降级提示并仅提供下载，不声称兼容第三方私有格式 |
| CSV/XLSX | Univer 值/公式编辑工作区（公式栏、行列头、多 sheet、撤销/重做）；当前 Session 文件可编辑，写回必须保留未修改的文件结构与元数据 |
| XLS/ET | 使用相同查看器强制只读；旧二进制 XLS 提示转换为 XLSX，ET 不尝试改写私有格式 |
| ZIP | 只读目录，≤10 MiB、≤2000 项 |
| 其他 | 元信息与下载 |

浏览器 PDF iframe 必须在自身 `load` 前保持不透明的稳定 loading 遮罩，查看器内部的空白页、工具栏和正文分阶段初始化不得直接暴露为多次闪动。文件切换的清理与新 loading 状态必须在浏览器绘制前完成。

Office 首次派生期间显示“正在生成 PDF 预览”；等待 2500ms 后可补充“首次转换可能需要几十秒”的非终止性提示，但该阈值不得 abort 请求、切换降级页或取代服务端超时。只有服务端明确返回失败，或有效 PDF 进入客户端渲染后确认不可用，才按格式降级。

### 6.1 Markdown 所见即所得编辑工作区

- 当前 Session 的 Markdown 打开后直接进入唯一的所见即所得画布，不提供阅读/源码模式切换，也不在正文上方增加“阅读视图”栏；外部 cron 等自定义 URL 来源保持只读渲染。
- 标题、引用、表格、列表和正文始终以最终排版形态直接编辑。鼠标或键盘焦点落到任意块后不得切成 IR/源码形态，不得露出 `#`、`**` 等 Markdown 标记。
- 纯 `<!-- ... -->` HTML 注释属于机器元数据：所见即所得画布必须将整个注释块从视觉和无障碍树中折叠，同时原始 Markdown 中的注释字节保持不变；普通 HTML block、代码块和正文不得被误隐藏。
- 画布使用通栏纸张结构，不套悬浮目录卡或带阴影的文章卡；正文排版、表格、引用和代码块共享一套中文报告视觉规范。
- 格式工具默认收起，通过标题栏真实按钮按需浮层展开；按钮公开 `aria-expanded`，收起后不得留下可聚焦的不可见工具。
- 普通 `Enter` 必须逐次产生新的可见段落，允许连续空段落，不得要求先输入非空字符或依赖 `Ctrl/Shift+Enter`。有意空段必须用可序列化的 Markdown/HTML 语义（如独立 `&nbsp;` 段）进入草稿，不能只保留临时 DOM；保存后重开仍须恢复相同可见段落间距，后续输入保持正确光标位置。
- 用户输入在同一 tick 内投影到父级与内存 outbox；Session 草稿继续异步写入 IndexedDB，Workspace 草稿只保留在当前页面生命周期。300ms 防抖窗口只控制远端保存，编辑器内部不得再叠加输入 debounce。
- 网络、5xx 和 `SESSION_EDIT_RETRY` 静默重试。当前 Session 内容版本变化由服务端按原始编辑基线自动合并，不因目录 revision 不同直接锁成只读。旧客户端 strict-CAS 冲突、基线确实缺失或结构无法合并时才保留草稿，持续说明实际原因并提供“下载草稿 / 丢弃草稿”。read-only/captured 预览永不读取 outbox 草稿。
- 该 300ms 是唯一的 debounce 层。编辑器组件不得在内部再叠加一层输入防抖；自定义 Enter 等手改 DOM 的按键处理必须在插入后立刻把序列化结果投影到父级，否则 dirty 标记不成立、自动保存窗口根本不会启动。
- Session 通过 `edit=true` 从同一不可变正文响应取得 `expected_revision + edit_base_token`，每代草稿绑定稳定 `save_id`；Workspace 仍使用 entry revision 与 `base_version_id`。保存期间按单文件 outbox generation 串行续写。任一来源发生自动合并，后续草稿保留原始 revision/base；按回执 token/version_id 读取固定合并正文，确认没有更新的草稿后才一起接纳正文与基线。普通未合并回执直接推进基线，不增加读取。
- Session outbox 是草稿、在途保存与成功回执的统一所有者；新打开的编辑器订阅同一回执。GET 先看到本次 PUT 的新版本、PUT 回执后到不构成冲突；视图不得单方面把 saving 草稿标记 retained。成功后清除已确认草稿并更新所有同文件视图；generation 在删除已保存记录后仍单调递增，旧回执不得清掉新一代输入。
- Workspace outbox 成功保存只推进 durable head；停止编辑 30 秒后把最新 `revision/current_version_id` 提升为 `web_idle` checkpoint，持续编辑每 5 分钟提升一次 `web_periodic` checkpoint。关闭标签/面板或切换 owner 在保存成功后提交 `web_close` checkpoint；checkpoint 失败不否认已经保存的 head，由 outbox 静默重试。
- 保存成功后以响应中的 `size + modified` 刷新标签和目录元数据；自身保存引发的目录刷新不得重新初始化编辑器或移动光标。
- 关闭 dirty 标签或文件面板时，在调用栈内先让编辑器把当前内容交给应用级 outbox，然后立即关闭；远端请求在后台完成。不得因为编辑器尚未挂载、网络失败或版本变化阻塞操作，也不得弹出放弃确认。
- 切换 Session 或新建会话遵循同一规则：同步抓取当前 owner 草稿后立即应用目标会话，远端保存静默进行。所有草稿、保存和回执携带稳定 session/path 或 workspace/entry key 与 generation；A 的迟到事务不得作用于 B。页面重载后只恢复 Session outbox；尚未远端成功的 Workspace 草稿允许丢失并从服务端 current head 重新打开。
- ChatV2 的 direct send 先按本轮 Workspace 附件 `entry_id[]` 定向调用面板保存；这些作为模型输入的 dirty 文件必须得到 `ok && !stale` 回执后才能发送，失败时保留 composer 且不创建 Round。随后 send/resume 再同步捕获其余 dirty editor 到各自 outbox，远端保存、自动合并和未决重试在后台继续；Workspace 确定性终态失败会丢弃该文件草稿，但不得清除 composer。只有非附件 `{source,path}` 进入本轮有界 `pending_file_drafts`，只在 Agent 请求上下文披露同步状态，不在前端显示全局提示，也不传草稿正文。
- 服务端灌入不得触发 PUT；只有用户在所见即所得画布中的真实内容变化才标记 dirty。
- 目录默认关闭；存在多个章节时，顶部工具栏显示真实“展开目录/收起目录”按钮，并公开 `aria-expanded` / `aria-controls`。
- 展开后目录为独立左侧 outline，使用 1px divider 与正文分隔；目录和正文各自滚动，关闭后正文占满并居中。
- 目录链接保留真实 heading 锚点并以 `aria-current="location"` 标识当前章节；窄容器下目录转为可关闭的覆盖层。
- Markdown 下载按钮直接下载服务器上的原始 Markdown 文件，不提供格式选择或 Word 转换；下载不触发编辑保存。

### 6.2 电子表格编辑工作区

- CSV/XLSX 使用嵌入式 Univer 值/公式编辑工作区，不再把数据降级成普通 HTML table；保留公式栏、行列头、工作表标签和键盘浏览语义。格式、增删行列、工作表增删/改名等当前协议不能保真写回的入口必须隐藏或拒绝，不得产生“已保存”假象。
- 从 OOXML/SheetJS 投影到 Univer 时保留数值格式用于显示，禁止把 `3.80`、百分比或负数格式退化为浮点尾数；显示格式只能作为不可编辑基线，写回仍只补丁值/公式并保留原 OOXML 样式部件。
- Univer 初始化、权限同步、工作表切换、公式缓存重算和服务端灌入不得标记 dirty 或触发 PUT；只有与当前内容基线不同的单元格值/公式 mutation 才启动自动保存。
- 用户修改单元格值或公式后立即导出当前保真文件到应用级 outbox，300ms 防抖窗口只控制远端 flush；正常过程与可重试失败均无用户决策 UI，草稿跨标签关闭、owner 切换和页面刷新恢复。
- 标题栏显示服务端 `FileInfo.modified` 对应的“最近修改”时间；自动保存成功后以响应中的新 `size + modified` 更新标签和目录元数据。
- CSV/XLSX 写回与 Markdown 共用成对读取的 `expected_revision + edit_base_token` 和 outbox 回执协议；不因 Agent 运行阻塞编辑。不同单元格合并，重叠修改沿用用户草稿优先规则，无法保真的结构变化保留草稿；禁止仅更新 metadata 强行覆盖。
- CSV/XLSX 最大在线保存 20 MiB；外部 cron 等自定义 URL 来源只能只读，不得写入当前 Session 端点。CSV 仅接受 UTF-8，保存时保留原 BOM、分隔符、CRLF/LF 与尾换行。
- XLSX 保存直接补丁原 OOXML 包中的值/公式节点，不用 SheetJS 重写整个工作簿；样式、超链接、批注、未编辑公式及其他包部件必须保留。Univer 普通输入附带的 `p/ref/custom: null` 只表示“无富文本/公式组/扩展数据”，不得误判为格式修改；这些字段非空或出现其他不可保真结构时才拒绝。损坏包或不支持的结构/格式 mutation 保守失败，原文件不得被覆盖。
- XLS 与 ET 强制只读：先关闭权限弹窗，再等待 `getWorkbookPermission().setReadOnly()` 完成后开放查看，隐藏编辑工具栏与编辑型右键菜单，并显示“只读”；XLS 明确提示转换为 XLSX。viewer 同时设置 `sheets.disableForceStringAlert=true`，无权限操作直接无效，不弹权限对话框。
- 开源 Univer 负责单元格值/公式交互，SheetJS 负责读取和 CSV 桥接；不得宣称等价于商业版协同、修订历史、完整格式编辑或 100% Excel 格式保真。

## 7. HTML Blob 新标签

- Blob URL 只表示当前浏览器本机查看，不得描述为可分享链接。
- 面板内使用 `srcDoc`，避免受限浏览器拦截 `blob:` 子框架；iframe 只能包含 `allow-scripts`，严禁同时加入 `allow-same-origin`。
- 面板内 loading 必须由 `load`、`error` 或 10 秒 deadline 确定结束；超时/失败只影响当前文件，并允许用户切换源码视图或下载。
- 新标签使用可信 wrapper Blob；待预览 HTML 仍位于无 `allow-same-origin` 的 sandbox iframe 中。
- 新标签打开后必须立即断开 `opener`；包装页自身不得运行脚本，内层 iframe 使用 `referrerpolicy="no-referrer"`。
- 包装页只包含固定结构和经过转义的标题，不注入额外 CSP 拦截预览脚本和资源；保留文件自身的策略与浏览器标准限制（例如 CORS），不得通过添加 `allow-same-origin` 解除应用登录态隔离。
- `window.open` 被拦截时立即 revoke 两个 URL 并反馈错误；成功 URL 使用 30 分钟有界 TTL，且在 `pagehide` 集中释放。

## 8. Office 派生预览

`GET /api/sessions/{id}/files/{path}?preview=true&render=pdf`：

- 仅 DOC/DOCX/PPT/PPTX；
- 在用户 OpenSandbox 单进程内完成最大 50 MiB 的有界快照、hash 与复制；API 主机不得读取或运行不可信 Office 内容；
- 临时 PDF 最大 100 MiB，shell `timeout -k` 与 SDK timeout 双重限制；
- 内容 hash + 扩展名 + renderer 版本作为缓存键；
- 相同内容使用原子目录锁；每请求唯一 scratch/profile，验证 PDF magic/大小后原子 rename，禁止 partial cache 命中；
- 缓存位于 session 隐藏 `.opencapybox-preview/`，不出现在目录列表，并随 session 删除；
- 浏览器取消、快速切换、shell/SDK 超时均必须先完成当前 `.incoming-*`、临时 LibreOffice profile 和所持缓存锁清理；下一次预览在复制源文件的同一沙箱命令内自愈清除超过 300 秒的严格命名 `.incoming-<32位小写十六进制>` 残留，不删除当前请求或已发布 hash 缓存；
- PPT/PPTX 的 ready 不以 PDF Blob 下载完成为准：PDF.js 必须成功打开文档并读取第一页后才提交新 version。已有 deck 刷新时使用双缓冲保留旧 document；新 deck ready 后原子切换并释放旧 loading task，刷新失败或较旧请求迟到时继续显示旧 deck；
- 失败不改变 Round/Session 状态，前端按格式降级。

## 9. 聊天运行时不变量

- 文件打开/关闭/展开不得停止 direct/resume transport。
- Session 文件列表、预览、下载、上传在等待 Sandbox 网络 I/O 前必须释放请求级 DB checkout。聊天/Cron 持有有效运行锁时，文件面板只能连接该轮已绑定的 Sandbox ID；缓存失效或远端 404 只让当前文件请求失败，不得由前端请求创建替代 Sandbox、改写用户绑定或让运行中的 Agent 切换容器代际。
- `waiting_interaction` 期间可以浏览文件；QuestionCard/ApprovalCard 不得消失或被文件状态覆盖。
- 切换 Skills/数据连接一级页面时 `ChatRuntimeProvider` 与 ChatV2 状态保持挂载。
- 返回聊天后恢复当前 session、草稿、滚动和文件布局。
- 文件 REST 失败不得写入聊天 runtime error 或伪造 terminal。

## 10. 验收测试

- [ ] `closed → split → full → split → closed` 状态与动态 aria-label 正确
- [ ] 查看文件、展开面板、收起面板均为可键盘操作的真实按钮
- [ ] 聊天顶栏与文件工具栏均为 56px，分栏截图中第一条水平分隔线无上下错位
- [ ] 聊天面板按钮在桌面 `closed/split` 始终存在：关闭时“展开面板”打开右侧，打开时“收起面板”关闭右侧，且不会改变全局 SessionList/历史侧栏
- [ ] splitter 到达 0/100 时归一为 full/closed；不论此前拖到什么比例，聊天顶栏重新打开均固定恢复 45/55，不出现蓝色假打开态
- [ ] full/closed 端点仍保留左/右边缘 splitter，可用指针向内拖回 split，也可用方向键和显式按钮恢复
- [ ] full 左边界只有一根 splitter；首次向左拖动只收起导航，首次向右拖动只拉出会话，手势中途反向不串操作
- [ ] splitter 指针覆盖容器 `0..100`，键盘 Home/End 可到两端；从 0 向右、从 100 向左移动一个步长就立即进入 split，不得积累到隐藏阈值后再跳出面板
- [ ] 消息列和 composer 始终锚定聊天区左侧并使用 `max-w-5xl`，文件区从窄到宽、从 split 到 closed 的过程中不重新居中漂移
- [ ] A/B session 的路径、标签和迟到响应互不污染；dirty A 内容进入持久 outbox 后立即切 B，远端失败不阻塞，切回 A 或刷新页面仍能恢复草稿
- [ ] A→新建会话→A 仍恢复目录、标签、布局与预览滚动
- [ ] 多标签打开、激活、关闭、元数据刷新正确
- [ ] 模型更新已打开的 clean 工作区文件时仅右侧命中标签原位更新且旧内容不中断；首次内容未 ready 时 revision 再变化仍保持 loading，不露出类型降级卡；dirty 标签继续保存，后端自动合并且人的重叠内容不丢，不显示冲突决策 UI
- [ ] waiting/resume/cancel/failed 期间文件工作台不破坏聊天状态
- [ ] 运行中的 Agent 与并发 Session/Workspace 文件预览共存；文件请求不持有 DB 连接等待 Sandbox，不重建绑定，连接池无 overflow/blocked lock
- [ ] Markdown 直接进入单一所见即所得画布；当前编辑与生成时快照共享右侧工作台壳但 content identity 隔离，revision conflict 停止自动重试且旧草稿不覆盖当前/只读内容
- [ ] Markdown 下载按钮可键盘操作，直接下载原文件，不显示格式菜单或触发派生转换
- [ ] Markdown 目录默认关闭，展开/收起、当前章节、独立滚动和窄屏覆盖层语义正确
- [ ] Markdown 相对资源、HTML/DOCX/PDF/XLSX/PPTX/图片/代码/ZIP 均有真实预览或明确降级
- [ ] CSV/XLSX 修改值/公式后保真自动保存并刷新最近修改时间；链接/批注/数字格式与 CSV 编码换行 round-trip 不丢失；XLS/ET 强制只读且不发写请求
- [ ] 100 页 PPTX 主区只挂载当前页±2 Canvas，缩略图懒渲染且定位/滚动同步；后台刷新在新 PDF.js 文档和第一页 ready 前保留旧 deck，失败或迟到响应不清空/回切
- [ ] HTML 内嵌使用无 `allow-same-origin` 的 sandbox `srcDoc`，load/error/deadline 均能结束 loading；新标签为 sandbox wrapper Blob
- [ ] HTML popup blocked、TTL/pagehide 回收、内嵌 timeout 与 Office 并发锁/partial cache/尺寸/超时/失败有测试
- [ ] 窄宽仍可用总开关和 splitter 往返聊天/文件，且不丢草稿
- [ ] 少量关键故障契约、TypeScript/build 通过；最终验收固定包含真实浏览器高频编辑/切换/关闭、PostgreSQL entry/version/blob/reference 事实、沙箱物理 SHA/inode/容量与 Markdown/XLSX/Office 真实解析，以及 Terra 对真实沙箱的独立只读扫描
