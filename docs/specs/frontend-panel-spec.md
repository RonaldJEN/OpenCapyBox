# 前端 Panel Spec — 覆盖式抽屉面板

> 父级：[frontend-spec.md](./frontend-spec.md)
> 对应后端：[sandbox-spec.md](./sandbox-spec.md)（Files）、[config-spec.md](./config-spec.md)（AgentConfig/Skills）、[cron-spec.md](./cron-spec.md)

覆盖组件：
- `ArtifactsPanel.tsx` — 沙箱文件浏览
- `AgentConfig.tsx` — SOUL/USER/MEMORY 记忆文件编辑
- `SkillManager.tsx` — Skills 启停
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
```

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
| 点击文件 | `onFilePreview(file)` → 弹 FilePreview Modal |

### 3.4 文件操作

- **下载**：`GET /api/sessions/{sid}/files/download?path={p}`，使用 blob 触发浏览器下载。
- **预览**：走 FilePreview，文本/Markdown/图片内联渲染，其他类型提示"不支持预览"。
- **上传**：当前版本**不支持**通过面板上传，文件通过聊天附件上传到沙箱。

## 4. AgentConfig（记忆文件编辑）

### 4.1 功能范围

编辑三个沙箱内记忆文件：
- `SOUL.md` — Agent 人格
- `USER.md` — 用户画像
- `MEMORY.md` — 长期记忆

### 4.2 API 契约

- `GET /api/config/memory?type={soul|user|memory}` → `{ content: string }`
- `PUT /api/config/memory` body `{ type, content }` → 保存

### 4.3 关键不变量

- **未保存提示**：内容有变更且未保存时关闭抽屉前必须 `confirm('有未保存的修改...')`。
- **并发冲突**：后端若返回 409，提示"文件已被其他端修改，请刷新"，不自动覆盖。
- **切会话不影响**：记忆是用户级，非会话级。切 session 时无需重载。

## 5. SkillManager（Skills 启停）

### 5.1 功能范围

- 显示官方 Skills（分类过滤）
- 每个 Skill 可启停（toggle）
- 批量启停同类别

### 5.2 API 契约

- `GET /api/config/skills` → `{ skills: SkillInfo[] }`
- `PUT /api/config/skills/{name}` body `{ enabled: boolean }`

### 5.3 关键不变量

- **乐观更新**：toggle 后立即更新 UI，失败时回滚并提示。
- **启停不立即生效于已发消息**：Skill 启停影响**下一次** Agent 请求的工具集。

## 6. CronSchedule / CronMessageCenter

### 6.1 两个组件分工

| 组件 | 职责 |
|---|---|
| `CronSchedule` | Cron 任务列表 CRUD |
| `CronMessageCenter` | Cron 执行历史 + 未读消息 |

### 6.2 未读消息轮询

`App.tsx` 每 60s 调 `getUnreadCount()` → 更新 `cronUnreadCount` → `SessionList` 上的入口按钮显示红点。

### 6.3 关键不变量

- **打开 CronMessageCenter 时标记已读**：`POST /api/cron/messages/mark_read`，未读计数归零。
- **Cron 任务新增/编辑**走表单（不允许直接写 cron 表达式原文，必须经前端校验）。
- **手动触发**：`POST /api/cron/{id}/trigger`，返回的新 round 会通过 `pollSession` 被 ChatV2 检测到（见 frontend-chat-spec §6）。

## 7. FilePreview（模态弹窗，非抽屉）

### 7.1 触发源

- `ArtifactsPanel` 点击文件
- `ChatInput` 附件点击（预览已上传的图片/文档）

### 7.2 类型分发

| 类型 | 渲染 |
|---|---|
| `image/*` | `<img>` |
| `text/*` / `.md` | Markdown 渲染（同 Round 内的 markdown 组件）|
| `application/pdf` | `<iframe>` |
| 其他 | 占位图 + 下载按钮 |

### 7.3 关键不变量

- **内容懒加载**：打开时才请求；大文件（> 5MB）默认仅显示下载按钮。
- **session 隔离**：弹窗属于当前 session，切 session 时自动关闭。

## 8. 测试清单

- [ ] ArtifactsPanel 快速切换目录不出现"旧数据覆盖新数据"
- [ ] 抽屉打开时聊天区宽度不变（不触发 reflow）
- [ ] session 切换后 ArtifactsPanel 回到根目录
- [ ] AgentConfig 未保存修改时关闭面板有确认提示
- [ ] CronMessageCenter 打开后未读红点消失
- [ ] 面板打开左侧栏自动折叠（AgentConfig/Skills/Cron），ArtifactsPanel 打开不折叠

## 9. 已知易错点

1. **用 `padding-right` 避让抽屉** → 违反 frontend-spec §5.6，出现 reflow 抖动。
2. **ArtifactsPanel 没做竞态防护** → 点击目录过快导致旧数据覆盖。
3. **FilePreview 每次打开都重新请求大文件** → 未做"大文件仅下载"分流。
4. **抽屉 z-index 低于 Modal** → 点击抽屉里的按钮打开 FilePreview 后被抽屉遮住。
5. **关闭动画未延迟卸载** → 动画被截断，抽屉瞬间消失。
6. **Skill toggle 直接等后端响应再更新 UI** → 感觉卡顿；应乐观更新。
