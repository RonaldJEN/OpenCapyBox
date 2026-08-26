# 前端 Session 文件工作台 Spec

> 父级：[frontend-spec.md](./frontend-spec.md)<br>
> 后端：[sessions-spec.md](./sessions-spec.md)<br>
> 本模块只浏览当前 session 目录；不定义“结果文件”，也不负责用户级工作区。

## 1. 职责与边界

Session 文件工作台负责：

- 浏览 `GET /api/sessions/{id}/files` 返回的完整 session 目录；
- 在聊天旁以分栏方式打开一个或多个文件；
- 在 `closed / split / full` 三种布局间切换；
- 按格式选择安全的预览器，并允许当前 Session 内的 Markdown 与电子表格编辑；
- 保留每个 session 自己的目录、标签、活动文件和滚动状态。

明确不负责：

- 区分输入附件与 Agent 产物；
- 用户级工作区、保存到工作区或从工作区选择；
- Word/PPT 在线编辑；
- 运行、暂停、恢复、取消等聊天状态机。

文件预览失败只能影响当前文件标签，不得改变 Round/Session 状态。

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
  previewScrollTops: Record<string, number>;
}
```

- 状态必须按 `sessionId` 隔离；A/B session 不得共享路径、标签或活动文件。
- 每个 session 在保持 `split` 时独立保留 splitter 比例；关闭文件区再通过聊天顶栏打开时恢复标签、路径和预览，但比例固定重置为聊天 45% / 文件 55%，不得继承上次的极窄比例。
- 切回已访问 session 时恢复其文件状态。
- 经过“新建会话”（`sessionId=""`）再返回旧 session 时也必须恢复；状态 owner 必须始终挂载。
- session 删除后清理对应状态。
- session 文件状态独立于 `chatRuntimeReducer`；聊天事实仍由 `ChatRuntimeProvider` 投影。

## 3. 顶栏按钮

### 3.1 查看文件

- 使用 24×24 图标按钮，图标 15–16px，透明背景、4px 圆角。
- 文案恒为 `aria-label="查看文件"`；`aria-expanded/aria-pressed` 表示当前文件区是否打开。
- 点击时打开或聚焦文件区，不把一个控件同时命名为“查看/结果/工作区”。
- 文件区顶栏的“收起文件”关闭文件区；关闭后焦点返回“查看文件”。

### 3.2 展开/收起面板

- 聊天顶栏的 24×24 按钮是右侧文件工作台总开关：`closed` 时显示“展开面板”并以聊天 45% / 文件 55% 的固定距离恢复右侧 `split`，`split` 时显示“收起面板”并关闭右侧、让聊天铺满。文案描述点击后将执行的动作，禁止反向命名。
- 桌面且存在 session 时该按钮在 `closed/split` 均显示；移动端不显示，`full` 时聊天不可见，由文件侧按钮负责返回 `split`。
- 它不得折叠或展开全局 SessionList/历史侧栏；主导航栏状态仍由 App 自己管理。
- 文件区顶栏的对称按钮控制文件 `split ↔ full`，图标 14×14。
- Tooltip 文案和 `aria-label` 必须一致；不得用只有点击事件的 `span` 代替按钮。

按钮 hover/active 背景使用 `#f4f5f6`；focus-visible 必须有可见焦点环。不得复制第三方私有 SVG。

## 4. 布局

- 聊天顶栏与文件工作台第一行工具栏统一为 56px（`h-14`），使用同一 `border-claude-border` 底边；分栏时两条水平分隔线必须在同一视觉基线上，文件标签栏从该基线下方开始。
- `closed`：聊天占满主内容区；桌面仍在最右边缘保留 splitter，向左拖动即可恢复 `split`。
- `split`：聊天与文件工作台并排，中间为键盘可操作的 splitter；先按容器几何将比例 clamp 到 `0..100`。两个方向都不得设置提前吸附阈值：从 `full` 向右拖出任意正比例时立即显示聊天，从 `closed` 向左拖出任意比例时立即显示文件；仅在比例真正到达 0/100 时进入端点状态。
- `full`：文件工作台占满主内容区；聊天运行态仍保持挂载，桌面在文件区最左边缘只保留一根共享 splitter。它覆盖导航/主内容的同一物理边界并按首次移动方向锁定：向左收起导航，向右立即恢复 `split`；同一手势即使反向移动也不得串到另一操作。
- splitter 的 `Home/End` 分别到达 `0/100`，方向键以小步调整并可到达两端；到达 `0` 时归一为 `full`，到达 `100` 时归一为 `closed`。两个端点都必须继续渲染可拖动、可聚焦的边缘 splitter；聊天顶栏按钮重新打开时固定恢复 45/55，边缘拖动则使用新的指针比例，不能出现按钮已高亮但文件区宽度为 0 的假打开状态。
- 小屏不并排，文件视图覆盖主内容；被遮住的聊天必须同时 `inert + aria-hidden`，焦点进入文件区，关闭后回到触发按钮。
- 切换布局保留聊天滚动、输入焦点、文件列表滚动和预览滚动。
- 聊天消息列、错误提示、交互卡与 composer 使用同一左侧 gutter，并放宽到 `max-w-5xl`；禁止 `mx-auto` 随 pane 宽度重新居中，拖动 splitter 时内容左边界必须保持稳定，聊天全宽时也不得缩回窄阅读列。
- 动画只使用明确的 width/flex/transform 属性，180–220ms，并尊重 `prefers-reduced-motion`。

Session 文件工作台是主布局的一部分，不适用普通右侧 Overlay Drawer 的“不挤压聊天区”约束。

## 5. 目录与多标签

- 目录请求使用单调递增 request id；迟到响应不得覆盖新路径。
- 支持后退、前进、上一级和根目录。
- 目录在前、文件在后，排序沿用后端结果。
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

所有预览器通过统一分发层选择，加载操作支持 `AbortSignal`，并使用 `(preview URL, modified, size, renderer version)` 作为缓存身份。

聊天消息和 composer 的附件弹窗可以复用同一预览分发层，但必须显式只读，只提供预览与下载；当前 Session 的 Markdown、CSV、XLSX 编辑仅由文件工作台拥有。

| 类型 | 预览语义 |
|---|---|
| Markdown | GFM、默认关闭的可折叠 outline、平面报告排版、表格滚动、渲染/源码切换；相对图片和文件链接通过 session 鉴权 URL 解析，拒绝越界 |
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

### 6.1 Markdown 阅读工作区

- 当前 Session 的 Markdown 直接进入单一所见即所得编辑器；外部 cron 等自定义 URL 来源保持只读渲染，不并存第二套编辑方案。
- 编辑画布使用通栏纸张结构，不套悬浮目录卡或带阴影的文章卡；正文排版、表格、引用和代码块共享一套中文报告视觉规范。
- 格式工具默认收起，通过标题栏真实按钮按需展开；按钮公开 `aria-expanded`，收起后不得留下可聚焦的不可见工具。
- 用户输入后等待 900ms 静默窗口自动保存；保存中、成功和失败必须有可见状态，失败时保留 dirty 并提供显式重试。
- 自动保存使用编辑开始时的 `size + modified` 做乐观并发检查；保存期间出现新输入时，先完成当前写入，再使用响应的新版本串行保存最新草稿，不得丢写或退回旧内容。
- 保存成功后以响应中的 `size + modified` 刷新标签和目录元数据；自身保存引发的目录刷新不得重新初始化编辑器或移动光标。
- 用户首次关闭 dirty 标签或文件面板时不显示“放弃修改”确认；关闭动作立即触发保存并保持界面挂载，只有全部目标文件保存成功后才真正关闭。保存失败时取消本次关闭、保留编辑内容并显示重试；失败后再次关闭必须提供“继续编辑 / 放弃修改并关闭”的应用内确认，用户不得被永久困在标签或面板中。
- 切换 Session 或新建会话前也必须 flush 当前 owner 的全部 dirty 文件；保存到最新 revision 后才允许切换，任一保存失败则留在原 Session 并保留草稿。所有保存、关闭和外部目标事件携带 `ownerSessionId + ownerEpoch`，A 的迟到事务不得作用于 B。
- WYSIWYG 初始化和服务端灌入不得触发 PUT。用户真实编辑后由 Vditor 重序列化 Markdown，保证内容语义与 Unicode，不承诺保留 BOM、CRLF 或非语义空白的字节级拼写；要求源码字节保真时不得使用当前 WYSIWYG 保存路径。
- 目录默认关闭；存在多个章节时，顶部工具栏显示真实“展开目录/收起目录”按钮，并公开 `aria-expanded` / `aria-controls`。
- 展开后目录为独立左侧 outline，使用 1px divider 与正文分隔；目录和正文各自滚动，关闭后正文占满并居中。
- 目录链接保留真实 heading 锚点并以 `aria-current="location"` 标识当前章节；窄容器下目录转为可关闭的覆盖层。

### 6.2 电子表格编辑工作区

- CSV/XLSX 使用嵌入式 Univer 值/公式编辑工作区，不再把数据降级成普通 HTML table；保留公式栏、行列头、工作表标签和键盘浏览语义。格式、增删行列、工作表增删/改名等当前协议不能保真写回的入口必须隐藏或拒绝，不得产生“已保存”假象。
- 用户修改单元格值或公式后等待 1.2 秒静默窗口自动保存；保存期间、成功和失败都必须有可见状态，失败后保留 dirty 状态并提供显式重试。
- 标题栏显示服务端 `FileInfo.modified` 对应的“最近修改”时间；自动保存成功后以响应中的新 `size + modified` 更新标签和目录元数据。
- 写回使用编辑开始时的 `size + modified` 做乐观并发检查；Agent 正在占用 Session 或文件版本已变化时拒绝覆盖，不得把最后写入者胜出当作正常行为。
- CSV/XLSX 最大在线保存 20 MiB；外部 cron 等自定义 URL 来源只能只读，不得写入当前 Session 端点。CSV 仅接受 UTF-8，保存时保留原 BOM、分隔符、CRLF/LF 与尾换行。
- XLSX 保存直接补丁原 OOXML 包中的值/公式节点，不用 SheetJS 重写整个工作簿；样式、超链接、批注、未编辑公式及其他包部件必须保留。损坏包或不支持的结构/格式 mutation 保守失败，原文件不得被覆盖。
- XLS 与 ET 强制只读：先关闭权限弹窗，再等待 `getWorkbookPermission().setReadOnly()` 完成后开放查看，隐藏编辑工具栏与编辑型右键菜单，并显示“只读”；XLS 明确提示转换为 XLSX。viewer 同时设置 `sheets.disableForceStringAlert=true`，无权限操作直接无效，不弹权限对话框。
- 开源 Univer 负责单元格值/公式交互，SheetJS 负责读取和 CSV 桥接；不得宣称等价于商业版协同、修订历史、完整格式编辑或 100% Excel 格式保真。

## 7. HTML Blob 新标签

- Blob URL 只表示当前浏览器本机查看，不得描述为可分享链接。
- 面板内使用 `srcDoc`，避免受限浏览器拦截 `blob:` 子框架；iframe 只能包含 `allow-scripts`，严禁同时加入 `allow-same-origin`。
- 面板内 loading 必须由 `load`、`error` 或 10 秒 deadline 确定结束；超时/失败只影响当前文件，并允许用户切换源码视图或下载。
- 新标签使用可信 wrapper Blob；待预览 HTML 仍位于无 `allow-same-origin` 的 sandbox iframe 中。
- 新标签打开后必须立即断开 `opener`；包装页自身不得运行脚本，内层 iframe 使用 `referrerpolicy="no-referrer"`。
- `window.open` 被拦截时立即 revoke 两个 URL 并反馈错误；成功 URL 使用 30 分钟有界 TTL，且在 `pagehide` 集中释放。

## 8. Office 派生预览

`GET /api/sessions/{id}/files/{path}?preview=true&render=pdf`：

- 仅 DOC/DOCX/PPT/PPTX；
- 在用户 OpenSandbox 单进程内完成最大 50 MiB 的有界快照、hash 与复制；API 主机不得读取或运行不可信 Office 内容；
- 临时 PDF 最大 100 MiB，shell `timeout -k` 与 SDK timeout 双重限制；
- 内容 hash + 扩展名 + renderer 版本作为缓存键；
- 相同内容使用原子目录锁；每请求唯一 scratch/profile，验证 PDF magic/大小后原子 rename，禁止 partial cache 命中；
- 缓存位于 session 隐藏 `.opencapybox-preview/`，不出现在目录列表，并随 session 删除；
- 失败不改变 Round/Session 状态，前端按格式降级。

## 9. 聊天运行时不变量

- 文件打开/关闭/展开不得停止 direct/resume transport。
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
- [ ] A/B session 的路径、标签和迟到响应互不污染；dirty A 保存到最新 revision 后才切 B，失败仍停留 A
- [ ] A→新建会话→A 仍恢复目录、标签、布局与预览滚动
- [ ] 多标签打开、激活、关闭、元数据刷新正确
- [ ] waiting/resume/cancel/failed 期间文件工作台不破坏聊天状态
- [ ] Markdown 使用单一所见即所得编辑器；格式工具按需展开，900ms 自动保存、串行续写、版本冲突和失败重试不丢草稿
- [ ] Markdown 目录默认关闭，展开/收起、当前章节、独立滚动和窄屏覆盖层语义正确
- [ ] Markdown 相对资源、HTML/DOCX/PDF/XLSX/PPTX/图片/代码/ZIP 均有真实预览或明确降级
- [ ] CSV/XLSX 修改值/公式后保真自动保存并刷新最近修改时间；链接/批注/数字格式与 CSV 编码换行 round-trip 不丢失；XLS/ET 强制只读且不发写请求
- [ ] 100 页 PPTX 主区只挂载当前页±2 Canvas，缩略图懒渲染且定位/滚动同步
- [ ] HTML 内嵌使用无 `allow-same-origin` 的 sandbox `srcDoc`，load/error/deadline 均能结束 loading；新标签为 sandbox wrapper Blob
- [ ] HTML popup blocked、TTL/pagehide 回收、内嵌 timeout 与 Office 并发锁/partial cache/尺寸/超时/失败有测试
- [ ] 移动端文件视图能返回聊天且不丢草稿
- [ ] 前端全量测试、TypeScript、build、lint 与后端全量 pytest 通过
