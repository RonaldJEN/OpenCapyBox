# Session / Cron 执行隔离规划（按当前代码重订）

状态：规划，未实施。基线：`ac251fabad39b407292e396ce26e712970665acb`。日期：2026-09-15。

本版替代前一版方案。用户已明确：**个人 Skill 及其中的配置、用户自行配置的 .env 必须跨会话共用；配置一次，新会话直接沿用。隔离的是 Session/Cron 私有文件、临时目录和进程。** 保留 OpenSandbox。

本文中“当前”表示已读代码确认；“拟改”表示本次需要实现；“部署验证”表示尚未在实际 OpenSandbox 后端证明。只修改本文，不变更运行代码或现行规格。

## 1. 这次究竟共享什么、隔离什么

| 对象 | 归属与目标行为 |
| --- | --- |
| Chat Session A 的输入、产物、会话笔记 | A 私有；B/Cron 不能直接读写 |
| Chat Session B 的输入、产物、会话笔记 | B 私有 |
| 一次 Cron run 的输入、产物 | 该 run 私有；下一次 run 另建执行实例 |
| /tmp、进程、容器系统可写层 | 各执行 Sandbox 独立 |
| 用户 Skill 目录，包括其配置文件、node_modules、.venv | **同一用户共享读写**，保持现有安装和修改方式 |
| 用户自行维护的根 .env | **同一用户共享**，保持旧脚本读取兼容 |
| USER.md / MEMORY.md / SOUL.md | 用户级内容，继续与现有数据库和配置工具联动 |
| 官方 Skill 来源 | 仍由应用服务端提供，按需部署到用户共享 Skill 目录 |
| 正式 Workspace、内容对象、未发送草稿、其他会话目录 | 不挂给普通执行 Sandbox；由平台服务按现有工具/API授权访问 |
| OpenSandbox 管理密钥、数据库凭据、后端项目 .env | 不属于用户共享运行配置，不进入执行环境 |

共享区中的文件变化可以被其他会话看到，这是明确需求。不能再写“A 修改 Skill 不影响 B”或“所有跨会话数据交换都被禁止”。隔离承诺是：A 不能直接读取/修改 B 的私有目录或操作 B 的进程；共享 Skill/config 是约定的公共区域。

## 2. 修正前版中的具体问题

| 前版不准确或未说明的地方 | 本版处理 |
| --- | --- |
| 把 Skill 和配置默认做成每 Session 副本 | 取消；改为精确挂载同一份用户 Skill/config |
| 只说“Skill 发布入口”，但仓库没有该能力 | 不新增发布工具、内容版本平台或上传编辑 UI；保留当前 Bash/文件工具安装修改共享 Skill 的方式 |
| 宣称等模型第一次用工具再创建 Sandbox | 一期保留当前时序：第一次 send/resume 准备 Agent 时创建/连接，先于模型调用 |
| 所有缓存都从 user 改成 Session | 执行缓存按 owner；Skill 推送、扫描、用户配置仍按用户共享存储身份 |
| Skill 在 /home/user/skills，却声称会自动找到 Session/node_modules | 明确依赖安装位置；共享 Skill 使用自身/共享父级 node_modules 或已验证镜像环境 |
| 建议把资料挂载根整体改成 /data/user | 保留当前挂载视图，避免改坏 Workspace.root_path、引用 key 和 prepared journal |
| 将配置同步描述成每轮无条件回写 | 当前轮末只在 dirty 标记下回写；保留该条件，不依据不存在的私有副本去删除整套同步机制 |
| 认为 Chat/Cron 已共用用户并发槽 | 当前 Chat 有 slot，Cron 没有接入同一 slot；本期不偷偷改变调度语义 |
| 把持续续租、独立转换环境、资源配置当现有能力 | 分别注明新增工作或后续加固，先验证实际后端 |

## 3. 当前代码证据索引

实施前重新核对这些位置，以下行号对应本次基线。

| 编号 | 当前实现 | 代码 |
| --- | --- | --- |
| C01 | user_id 唯一绑定一个 Sandbox | `src/api/models/user_sandbox.py:10` |
| C02 | H(user) 整体 RW 挂到 M；默认 M=/home/user | `src/api/services/sandbox_service.py:309`、`:321` |
| C03 | 创建 Session 只写 DB，send/resume 初始化先取得 Sandbox | `src/api/routes/sessions.py:664`；`src/api/routes/chat.py:730`、`:757`、`:1044` |
| C04 | AgentPool 已按 Session 缓存 Agent，但执行实例、续租和失效判据仍按用户 | `src/api/services/agent_pool_service.py:51`、`:323`、`:593`、`:713`、`:824` |
| C05 | Bash 将原命令交给 commands.run，只设 cwd/timeout；通用文件工具接受绝对路径 | `src/agent/tools/sandbox_bash_tool.py:525`、`:624`；`sandbox_file_tools.py:51` |
| C06 | get_skill 先 ensure_ready，再返回正文；用户 Skill 假定已在共享 Sandbox | `src/agent/tools/skill_tool.py:50`；`src/api/services/tool_factory.py:370`、`:385`、`:451` |
| C07 | 官方 Skill 推送完整子树，排除依赖目录，成功后按用户/名称记 pushed | `src/api/services/sandbox_service.py:231`、`:1342`、`:1359` |
| C08 | 官方路径改写为 /home/user/skills；用户正文按 sandbox_skill_dir 改写 | `src/agent/tools/skill_loader.py:221`、`:447` |
| C09 | Skill UI/API只有列表、重扫、启停，无独立发布/安装 API | `src/api/routes/config.py:157`、`:453`；`frontend/src/components/SkillsPanel.tsx` |
| C10 | 用户 inventory 的 revision 是元数据快照版本，不是脚本内容版本 | `src/api/models/user_skill_inventory.py:9`；`src/api/services/skill_inventory_service.py:307` |
| C11 | PPTX require playwright/sharp，DOCX require docx；两者有 package.json/lock | `src/agent/skills/pptx/scripts/html2pptx.js:28`；`docx/scripts/create_docx.js:24`；对应 package.json |
| C12 | WorkspaceStore优先用注入的 sandbox；源和目标在同一视图复制 | `src/api/services/workspace_service.py:2602`、`:1166`、`:1291` |
| C13 | 非空 Workspace 的 mount/root/profile 变化会被拒绝 | `src/api/services/workspace_service.py:2702` |
| C14 | 草稿上传绑定用户实例，claim要求仍等于当前用户实例 | `src/api/services/composer_draft_attachment_service.py:127`、`:209`、`:236` |
| C15 | Session文件接口、Cron结果下载仍通过用户Sandbox定位文件 | `src/api/routes/sessions.py:475`、`:1036`、`:1557`；`src/api/routes/cron.py:305`、`:368` |
| C16 | 普通present_files是live路径；Workspace引用固定版本；既有capture另有分支 | `src/api/services/agent_service.py:1969`、`:2017`、`:2078` |
| C17 | Cron先建run/claim、取并冻结Sandbox，后建内部Session | `src/api/services/cron_service.py:746`、`:811`、`:819`、`:874` |
| C18 | 子Agent继承sandbox/session，但没有显式传workspace_dir | `src/api/services/agent_service.py:679` |
| C19 | 配置即时同步、AgentPool独立初始化同步、MemoryService同步、dirty轮末同步均存在 | `src/api/services/tool_factory.py:70`；`agent_pool_service.py:436`、`:754`；`memory_service.py:642`；`agent_service.py:3355`、`:4176` |
| C20 | 数据cleanup冻结旧sandbox_id；现有kill先清用户mount | `src/api/services/sandbox_cleanup_service.py:193`；`sandbox_service.py:1067` |
| C21 | Office预览是平台固定soffice命令，不是模型自定义命令 | `src/api/services/file_preview_service.py:148`、`:175` |
| C22 | 全局Sandbox镜像/TTL；创建未传CPU/内存/PID/网络参数 | `src/api/config.py:198`；`src/api/services/sandbox_service.py:508` |
| C23 | 新模型import + create_all + 手写幂等列/约束迁移；无Alembic | `src/api/models/database.py:14`、`:151`、`:322`、`:449` |
| C24 | 用户purge是手写顺序，包含删除cleanup与UserSandbox | `src/api/services/auth_service.py:422` |

## 4. 具体挂载方案

记号：H 是当前 `_user_storage_host_path(user_id)` 产生的宿主持久根；M 是配置的容器挂载根，默认 `/home/user`。两者不能混为同一路径。

### 4.1 Chat A、Chat B、Cron C

| Sandbox | 宿主源路径 | 容器目标 | 模式 |
| --- | --- | --- | --- |
| Chat A | H/sessions/A | M/sessions/A | RW，A私有 |
| Chat B | H/sessions/B | M/sessions/B | RW，B私有 |
| Cron C | H/cron/runs/C | M/cron/runs/C | RW，C私有 |
| 上述所有执行实例 | H/skills | M/skills | RW，用户共享 |
| 上述所有执行实例 | H/.user-config | M/.user-config | RW，用户共享 |

**执行实例不挂 H → M。** M本身及其sessions/cron父目录是各容器自己的路径结构，因此不能通过 `../B` 访问宿主上的B。M/skills与M/.user-config则明确映射到同一批共享文件。

```text
Sandbox A                              Sandbox B
M/sessions/A → H/sessions/A             M/sessions/B → H/sessions/B
M/skills     ───────────────┬─────────── M/skills
                           └→ H/skills
M/.user-config ────────────┬─────────── M/.user-config
                           └→ H/.user-config
/tmp、进程独立                          /tmp、进程独立
```

首次与重建后的A都挂同一个H/sessions/A；B始终挂H/sessions/B。配置不随执行容器的创建、重建而复制或重置。

### 4.2 平台文件访问实例

复用现有用户级UserSandbox作为平台文件访问实例，挂载仍是H→M，保留完整用户数据视图。它承担Workspace、草稿、历史文件、Skill推送/扫描、清理等现有固定I/O。

它不交给Agent的Bash或通用文件工具，也不接受模型提供的任意命令。用新的干净实例承接这一角色；不把旧的任意代码执行容器原地改名。

保留M数据路径，单独固定平台命令的HOME、cwd、PATH和解释器，使平台I/O不加载共享Skill、用户.env或用户可写的Python启动模块。是否需要独立I/O镜像在阶段0验证；当前仓库只有应用后端Dockerfile，没有证明它等于Sandbox镜像。

### 4.3 同盘是上线前提

当前Host挂载不是跨节点存储系统。一期固定用户资料实例与执行实例到同一个真实存储后端；同一节点本地盘可直接复用。如果后端会跨节点调度，必须证明共享文件系统/节点亲和有效，否则阻断这条部署路径。

不通过“相同绝对路径字符串”判断同盘，不因目录不存在自动创建空目录替代旧数据。

## 5. Skill怎样继续驱动：沿用当前调用链

### 5.1 官方Skill

```text
服务端 src/agent/skills 或配置目录
  → SkillLoader发现，提示词只含元数据
  → 模型get_skill(name)
  → 最新启停检查
  → 平台资料实例push_skill到共享M/skills
  → 当前执行Sandbox经共享挂载立即可见
  → 返回正文，read_file/Bash执行原路径脚本
```

拟改的核心是推送目标角色，不是重新设计Skill协议：

- `push_skill`明确取得用户资料句柄，不能从“最近使用的执行Sandbox”缓存选目标。
- `_collect_skill_files`继续按真实源目录结构推送，保留package.json、lock、脚本和参考资源；不只传SKILL.md。
- `_pushed_skills`和推送锁按用户存储身份/Skill管理，不按每个Session重复部署。资料实例重建后若保留成功标记，必须依据同一实际存储而非任意新sandbox_id。
- 推送失败不返回成功正文；只在现有成功条件满足后记ready。保持SDK write_files正常返回None的语义。
- 不借此次隔离新增Skill版本历史；inventory revision仍是元数据快照版本。
- 官方相对目录名与frontmatter.name可能不同，不能拼成“skills/<name>”代替真实目录。
- 当前SkillLoader硬编码`/home/user/skills`，而推送路径支持配置M。给SkillLoader和`_process_skill_paths`传入SharedUserContext的实际`M/skills`，让正文、推送和用户扫描的根一致；默认路径保持原值，不同时保留任意M与硬编码根的矛盾。

### 5.2 用户Skill和配置

用户Skill仍在M/skills下，由普通Bash/文件工具安装、修改、配置，之后重新扫描。新会话挂到同一目录，所以不用再次安装或输入配置。

当前用户Skill readiness直接True的前提仍然成立，但前提变成“执行实例已确认挂到该用户的共享Skill根”。扫描和marker读取使用用户资料视图；脚本在执行实例中运行。

保留：

- 官方同名优先及禁用后不回退同名用户Skill。
- 加载前后启停复核、失败/stale展示、扫描失败保留最后成功元数据。
- 用户Skill异常逐项隔离，元数据刷新替换正文缓存。
- Skills页面重扫只触达资料实例，不挑选一个聊天执行实例。
- 用户明确要求安装/修改Skill的自然语言流程。**不新增SkillPublish、不要求手动发布一次副本才能给新会话使用。**

### 5.3 共享依赖的位置必须明确

| 依赖 | 规划位置/使用方式 |
| --- | --- |
| Skill自己的npm依赖 | M/skills/<真实目录>/node_modules；按该目录package.json/lock安装 |
| 多个Skill共用npm依赖 | M/skills/node_modules，作为脚本的共享父级依赖目录 |
| Skill自己的Python虚拟环境 | M/skills/<目录>/.venv；脚本显式使用其bin/python或在命令中激活 |
| 基础Python/Node/soffice/字体/浏览器 | 已验证的固定Sandbox镜像或明确部署的运行资源 |
| 普通会话项目依赖 | 当前Session目录，只服务该项目 |

当前PPTX脚本直接require playwright/sharp。Node按脚本位置查找依赖；在M/sessions/A执行npm install，不会自动让M/skills里的脚本找到那些包。不能把cwd与模块查找混淆。[Node模块查找规则](https://nodejs.org/api/modules.html#loading-from-node_modules-folders)

当前官方推送排除node_modules/.venv，不代表这些依赖不存在，也不负责安装它们。当前代码默认镜像是code-interpreter-agent:v1.1.0，实际镜像内容未知；部署验证必须区分“镜像原有依赖”和“旧容器运行时临时安装”。

存量Skill内部的.env、node_modules、.venv保持原位置。位于旧容器系统层、M/.local或其他目录的依赖另做盘点，迁入共享Skill环境或固定镜像；不能销毁旧容器后才发现依赖丢失。共享虚拟环境要求所有执行实例使用兼容的基础镜像。

### 5.4 已发现的源代码路径差异

仓库实际目录有 `src/agent/skills/docx`、`pptx`、`xlsx`，部分正文仍引用 `skills/document-skills/...`。当前路径改写只在源文件存在时替换，实际部署是否有兼容目录本次未验证。

这属于已有兼容点，不能把所有旧路径失败都归因于新隔离。PoC必须跑真实get_skill→引用资料→脚本执行，按真实source目录定位；只修确实阻断该链路的路径，不全库机械替换文案。

## 6. 用户配置一次、新会话直接沿用

### 6.1 Skill内部配置

例如M/skills/my-skill/.env或config.json直接处于共享目录，无须迁移、复制或新增凭据分发。A配置后B/Cron下一次启动脚本可读取新内容。已经启动的进程不会自动重启或重新加载环境变量。

### 6.2 根.env与用户配置文件

新增共享目录H/.user-config，保存原用户根.env及USER/MEMORY/SOUL文件；在每个容器内挂M/.user-config，并为旧路径建立固定兼容入口：

```text
M/.env       → M/.user-config/.env
M/USER.md    → M/.user-config/USER.md
M/MEMORY.md  → M/.user-config/MEMORY.md
M/SOUL.md    → M/.user-config/SOUL.md
```

这是一次性路径整理，不是每会话重新配置。Skill内部配置完全保持原路径。只迁移明确的用户运行文件，不扫描或复制应用服务端的项目.env。

选择目录挂载的原因：直接bind单个.env文件时，“临时文件+rename”的保存方式可能使其他容器仍指向旧inode。仅创建软链也不够：原子替换旧入口可能覆盖软链本身。因此拟改：

1. 增加固定用户配置路径解析器，只识别上述已知根入口与真实共享路径。
2. 当前注册的read_file、apply_patch和配置工具共用解析器；apply_patch的源路径、移动目标分别解析到真实共享目标再原子安装，回调以逻辑文件类型同步DB。代码里保留的旧write_file/edit_file类也复用底层解析，但当前工厂未注册它们，本期不重新开放或把它们当成已暴露能力。
3. AgentPool._sync_memory_to_sandbox、MemoryService、配置页面PUT和dirty轮末同步都使用同一真实共享文件路径。共享挂载与兼容入口必须先建立，再进入AgentPool初始化同步；只修改MemoryService会漏掉当前主初始化链。
4. 平台给模型的配置写入指引使用M/.user-config/...；旧入口保留给已有脚本读取。使用原子保存的用户脚本需在一次性迁移中适配到真实共享路径。
5. 对旧入口读取、普通写入、apply_patch原子保存、重建后读取进行双容器实测；不能只测cat就宣布兼容。

当前没有通用Skill凭据仓库，本方案也不新增按次选key的机制。共享.env文件不等于把文件当shell脚本自动source，更不等于自动更新所有进程的os.environ；保留各Skill当前读取配置的方式。

### 6.3 保留已有记忆语义，去掉角色混用

当前主初始化由AgentPool._sync_memory_to_sandbox直接读写并在非force模式回写DB，并非委托MemoryService.sync_to_sandbox；后者是另一个同步入口。文件/配置工具还有即时回调；轮末只有dirty标记时触发回写。这些路径均有代码，不应笼统说成“每轮自动覆盖”。

本版没有Session配置副本，因此不再依据“旧副本覆盖新配置”的假设删除整套同步。拟改为：

- 共享文件与DB仍保留现有用户级关系；读取/写入路径统一解析。
- 同步函数明确取得用户共享资料/配置句柄，不在某个Session根下寻找副本。
- `get_agent_config_file_type_for_path`识别兼容入口和真实路径；不能把Session私有目录里的同名MEMORY.md当用户配置。
- 保持MEMORY.md当前不注入system prompt的既有行为，不因隔离重新开启。
- .agent_memory.json移到当前Session的私有持久目录；短期笔记不共享。
- 平台AGENTS仍由平台模板控制，可分别初始化在执行实例，不开放成用户共享可写配置。

## 7. 数据模型和缓存的准确拆分

### 7.1 用户资料身份

一期复用现有UserSandbox记录承接用户资料实例，明确其稳定记录ID与可替换的sandbox_id不是同一概念。补充冻结的宿主数据根、容器mount和后端身份；数据消费者记录storage_binding_id。

用户资料实例重建后，只要真实存储未变，草稿、Skill inventory和cleanup不应因为计算实例ID变化失效。跨后端搬数据本期不自动实施。

### 7.2 新增ExecutionSandboxBinding

最低字段：

| 字段 | 作用 |
| --- | --- |
| user_id + owner_kind + owner_id | 所属用户；chat_session或cron_run；组合唯一 |
| storage_binding_id | 持久数据及共享Skill/config所在后端 |
| sandbox_id + sandbox_generation | 当前执行实例及代际 |
| profile_id/profile_version | 该实例实际连接身份 |
| execution_root | 冻结的M/sessions/id或M/cron/runs/id |
| state、last_activity_at | 实例生命周期，不更新聊天最近消息时间 |

Cron绑定不能强依赖已存在的Session外键：当前run/claim先存在，内部Cron Session后创建。owner=cron_run:run_id；内部Session id虽然等于run_id，也不能另分配一套chat_session环境。

### 7.3 缓存不能全部按同一种键修改

| 状态 | 新归属 |
| --- | --- |
| Agent实例、执行Sandbox、执行续租、取消、创建锁 | Session/Cron owner及实例代际 |
| 用户资料Sandbox、Skill推送与扫描、共享配置同步 | 用户storage binding |
| Skill metadata inventory | 用户共享存储身份及现有扫描顺序 |
| Chat用户并发slot、Skill启停配置 | 保留用户级 |
| Workspace版本、内容对象、配额、claim | 保留现有用户/entry/blob/mutation身份 |

执行工具收到冻结的ExecutionContext；平台数据/Skill服务收到SharedUserContext。工具工厂明确分派，禁止同一个含义模糊的sandbox变量同时承担两种角色。

## 8. Chat、子Agent、Cron的调用点

### Chat

保留 `chat.send/resume → _start_agent_init → AgentPool → initialize_agent → prepare_chat_round → 模型` 的顺序，以及SSE初始化心跳、超时、取消和受理规则。

- chat.py预读绑定从UserSandbox改为目标owner绑定。
- AgentPool._do_create_agent改为acquire_execution(chat_session,id)，构建三类精确挂载。
- 删除“同用户其他Agent的sandbox_id不同就失效”的假设。
- 模型/MCP变化重建Agent对象时继续复用该Session执行实例。
- 打开页面/创建空Session不创建执行实例；**首次send/resume初始化时会创建，不实施工具级lazy**。
- 已有Session上传及无Session草稿上传在数据面切换后由资料实例写目录，不再触发聊天执行实例。

### 子Agent

当前传父sandbox/session_id但重新推默认workspace。拟改为继承父完整ExecutionContext，包括精确execution_root和generation；共享Skill/config保持同一用户。当前Cron不开放SubAgentTool，保留该行为。

### Cron

保留run记录和claim先行、冻结sandbox_id后执行、内部Session随后创建的顺序，仅把获取用户执行Sandbox改为获取cron_run owner。

已冻结实例只精确重连；preparing的受控恢复保留，executing/publishing失联仍按现有unknown语义收敛，不重放副作用。Cron结果列表的旧数据扫描和下载API也必须改为storage binding + run目录；不能只改worker。

#### 当前已经具备的Skill/Workspace链路

`cron_service.py:936`构建AgentService，经`agent_service.py:353`进入同一个create_agent_tools。Cron排除的工具不含GetSkillTool或Workspace工具，因此本期要保留这条现有能力，不另造简化的Cron工具工厂。

- 已传`workspace_dir=run_workspace`、`workspace_access="manage"`、`workspace_actor="cron"`。
- 有效claim下传入`workspace_fence=assert_cron_workspace_lease`和`workspace_change_recorder=append_cron_workspace_change`。
- `workspace_context`包含cron_job_id、cron_run_id；WorkspaceTool再叠加round_id、tool_call_id并生成原有幂等键。
- `_WorkspaceTool._call`先执行fence，当前再用执行sandbox创建WorkspaceService；这里改为资料句柄，其余actor/fence/context/recorder继续传递。
- Cron保持无人值守；不新增ask_user、子Agent或任务级Workspace权限字段。已有完整Workspace工具是否使用、操作哪些文件由保存的任务prompt决定。

#### 改造后的完整执行例子

任务示例：使用已配置的个人Skill更新Workspace中的一份报告。

```text
Cron run C取得独立执行Sandbox
  ├─ 挂C私有run目录
  ├─ 挂与Chat同一份Skill/config（不重新配置）
  └─ 工具同时持有C执行上下文及用户共享资料上下文
        ↓
get_skill → 返回共享Skill正文/路径
workspace_list / workspace_stage → 平台把获授正式版本放入C/.workspace-snapshots
bash → 在C内运行共享Skill脚本，读取上述副本，输出到C的run目录
workspace_publish → 带base_version和当前run身份，通过平台提交现有change set
        ↓
正式结果记workspace_changes；运行文件记artifacts；发布审计记workspace_change_sets
        ↓
落定结果并回收C执行实例；共享配置、正式Workspace和C历史产物继续保留
```

创建目录、移动、重命名、删除继续走相应workspace工具；Skill脚本不能通过绝对路径直接写M/workdir。若某个用户Skill当前硬编码正式Workspace路径，需一次性将它的输入/输出接到stage副本与publish流程，不能为了兼容重新挂整个Workspace。

还要保持两类不同失败：claim失效时禁止后续工具入口继续写；正式WorkspaceMutation已成功而change recorder失败时，保持真实成功事实，由现有reconciliation补齐摘要，不能重跑整个Skill/Cron制造重复副作用。

## 9. Workspace、附件、历史文件：保留事务协议

### 9.1 固定I/O走资料实例

Workspace工具构造中的 `sandbox=self.sandbox`、Agent附件stage/引用保护的同类构造要逐处替换。WorkspaceStore取得SharedUserContext，其M/workdir路径保持不变。

- stage：在资料实例内复制正式版本到当前owner的.workspace-snapshots；执行实例经自身挂载看到副本。
- publish：工具提供当前owner下的相对路径，服务端生成合法source_root；在资料实例内读取并提交现有proposal/change-set。
- 服务端验证精确owner，不能只通过sessions/或cron/runs/前缀校验。
- 保留现有base_version_id、revision、SHA、claim、配额、journal、幂等和原子安装；不重写Workspace协议。
- 不给WorkspaceContentObject增加execution Sandbox ID，数据对象仍按用户/blob/version归属。

### 9.2 草稿claim

上传、claim、删除、到期cleanup一起改：

- ComposerDraftAttachment增加稳定storage_binding_id；legacy sandbox_id用于明确迁移，不与执行实例比较。
- 仍在资料实例内将草稿复制到Session专属attachments目录。
- 保留逐附件ready→claiming→claimed/attached、generation CAS、大小/SHA、失败恢复和late-copy re-arm。
- 只在真实Round绑定后认定attached；不能把拷贝成功当发送已被接受。

### 9.3 文件API与展示

Session列表/上传/编辑/下载/ZIP、Cron产物下载都按目标owner查存储目录。释放执行容器后仍能操作持久文件。

保留三种已有身份：普通present_files的live路径；既有.assistant-artifacts capture；Workspace entry/version固定引用。不得借此次隔离把所有历史文件改成不可变快照。

会话短期笔记及运行环境目录应继续从普通交付列表/ZIP按现有规则排除；共享Skill目录本就不在Session根，不会被整个打包进去。

### 9.4 预览范围

当前UI预览使用平台固定的soffice命令。本期先保持同一套缓存key、锁、输入快照、超时、原子发布及清理，通过资料实例执行固定预览流程，不暴露其任意命令接口给模型。

平台HOME/cwd/解释器与用户共享目录隔开，预览临时目录和LibreOffice profile按请求独立。另建单文件转换Sandbox可作为解析器加固后续项；本期不把它暗中升级成必须新增的第三套运行平台。AI使用Skill生成/修改Office文件仍在其Session/Cron执行实例中。

## 10. 续租、取消、删除与Profile

### 续租和回收

当前AgentPool TTL默认86400秒，Sandbox timeout配置默认60分钟，后台命令默认上限21600秒；续租主要在acquire及热命中时按用户节流300秒，没有活动命令持续续租循环。

拟新增owner活动登记和续租任务，覆盖主轮次、子任务、受管理后台命令、等待用户回答/工具审批及有效continuation claim。不能只依据AgentService.is_running的活动流计数判断等待中的Session空闲。后台登记从Agent对象移到执行owner后，才允许回收Agent对象而保留远端后台任务。

同owner生命周期锁与DB CAS负责创建竞争；只有胜出候选能启动用户命令。确认旧执行者终止前，不把同一私有目录交给新代际。未知状态、超时、5xx不触发重建。

一期先保留按实际后端状态connect/resume/rebuild的模式，不承诺pause一定释放资源或恢复进程内存。空闲策略在PoC测清后定；容器释放与持久目录删除是两个独立操作。

### 取消与并发

取消按root round/child round记录精确SDK command ID；取消一个轮次不停止另一会话，也不误杀同Session之前已完成受理的独立后台任务。删除整个Session才终止该owner全部进程。

Cron丢claim时必须收敛远端命令，不能只cancel Python task。新旧代际回调不能操作另一实例。

Cron正常进入终态后，先落定产物引用、结果与Workspace发布事实，再停止该run遗留的后台命令并释放其精确执行实例；保留run目录及用户共享Skill/config。回收失败保留可重试实例清理记录，这属于新增收尾工作，不能假设当前代码已经完成。

保持Chat现有slot和同Session串行，Cron保持当前worker/claim调度。将Chat/Cron统一计入用户总slot属于额外调度变更，不在本期顺带实现。部署容量要同时计入两者及平台资料实例。

### 删除与Profile

- 不把当前“清空用户mount后kill”用于执行容器回收。
- Session删除先停止其写入者，再排入该Session持久目录cleanup；共享Skill/config不随Session删除。
- 数据cleanup保存storage_binding_id；实例cleanup保存owner、准确sandbox_id/generation和后端身份，现有cleanup generation仍表示清理claim。
- 用户删除枚举全部执行实例；全部执行者停止、用户资料物理清理成功后，才执行auth_service手写purge。保留admin.py:3047现有“清理失败则409、用户未删除”规则，失败时保留账号与清理身份，防止同名账号继承残留数据。
- 当前Profile guard只检查非空Workspace，需要扩展到实际Session/Cron/草稿/共享配置存储。
- 用户数据迁移前，新旧执行owner均使用与storage binding兼容的后端，不直接跟随新默认Profile创建空目录。
- 保留并明确现有单worker约束；不因新增容器数量宣称应用已支持多worker。

## 11. 网络和平台命令边界

三类挂载隔离私有文件，独立容器隔离进程和/tmp；执行实例还不能绕到其他Sandbox execd或OpenSandbox生命周期API。

当前Sandbox.create没有传网络/CPU/内存/PID限制。实际网络、权限、execd token隔离及支持的SDK字段均列入PoC；禁止仅以SDK版本或工具schema证明部署已隔离。保留任务需要的外网、MCP和依赖下载。

用户共享.env属于用户配置；平台连接凭据仍留在服务端。当前已安装SDK0.1.6提供RunCommandOpts.envs/uid等字段，但远端是否支持要实测；uv.lock当前为0.1.5，两者不能混为同一部署版本。

## 12. 按代码依赖拆成七个工作包

| 工作包 | 改动 | 验收出口 |
| --- | --- | --- |
| P0：可丢弃环境PoC | 同盘三类挂载、共享Skill配置、真实PPTX/DOCX依赖、根.env原子保存、网络、暂停/回收 | A配置一次，新建B/Cron直接使用；私有文件/tmp/进程不互通；确认实际镜像和后端约束 |
| P1：身份与数据库 | UserSandbox资料角色、执行绑定模型、stable storage字段、手写迁移和role创建接口 | 同owner唯一绑定；旧库升级不创建容器、不移动文件；候选竞争/旧代际安全 |
| P2：共享资源 | Skill推送/扫描改资料句柄、用户共享配置路径解析、DB同步、依赖定位 | 现有get_skill/安装修改/重扫/启停保持可用；不新增发布平台；新会话不重复配置 |
| P3：资料I/O | Workspace构造、Session/Cron文件API、draft claim、cleanup、固定预览环境 | 旧相对路径和事务语义保留；执行容器停后历史仍可读；不会读到错误owner |
| P4：执行链 | Chat首条消息初始化、AgentPool、Bash/文件工具、子Agent、Cron精确owner和完整Skill→Workspace链 | Chat A/B及Cron运行在不同实例；Cron沿用已配置Skill，通过stage/publish操作正式数据，actor/fence/recorder无丢失 |
| P5：生命周期/运维 | 活动续租、命令登记、取消、删除、Profile guard、用户purge和管理投影 | 长任务不因只热命中续租而丢环境；目标清理准确；共享配置不被删 |
| P6：用户级切换 | 存量数据盘点、配置兼容整理、旧执行者收敛、切换标记、四类真实验收 | 隔离与共享均符合用户要求；不存在旧共享总根执行入口 |

P2/P3可在P1后并行开发，P4不能先于它们单独切用户流量。新增模型在database._import_models注册；create_all、_PENDING_COLUMNS和约束/索引迁移覆盖新旧数据库。物理迁移不放进init_db。

本期不包含：迁移CubeSandbox、工具级lazy、Skill版本平台/发布工具、新Skill编辑UI、统一Chat/Cron调度、跨节点自动搬盘、全面Office解析器隔离或多worker改造。

## 13. 存量用户切换步骤

1. 只读盘点：真实Profile/存储根、活动Chat/Cron/后台任务、草稿与cleanup、共享Skill及依赖所在位置。对配置只记录路径/大小/校验结果，不打印秘密内容。
2. 新增数据库身份与用户迁移状态；未切换用户保持当前模式，已切换用户不得在报错时自动退回共享总根执行。
3. 在用户安全窗口停止新执行，等运行及文件mutation收敛；保留未完成任务事实，不自动重放。
4. 配置目录做一次性整理并校验旧入口兼容；Skill目录及内部配置/依赖保持原位。旧rootfs独有依赖先准备可复用环境，再销毁旧容器。
5. 检查私有owner彼此之间，以及私有目录与共享Skill/config、workdir、草稿、内容对象目录之间的跨边界硬链接；若存在，独立复制解除私有文件别名并核对内容和revision语义。合法共享区内部链接保留；本次未确认存量存在这些跨边界链接。
6. 只释放旧执行容器，保留H数据；新建干净平台资料实例，校验同盘、文件引用和存储身份。
7. 启用三类精确挂载与owner执行。历史Chat环境首次继续时再创建；不预启动全部会话。
8. 验证A配置一次、B和Cron直接使用、A/B私有数据互不可见；核对旧草稿、Workspace、Cron历史和cleanup后完成切换。

回退以保留数据、停止新执行和修复目标版本为主。没有上一隔离版本的首次上线，不虚构“回滚到上一隔离版本”；是否恢复旧运行模式必须明确其边界变化，不能静默降级。

## 14. 验收矩阵：共享与隔离同时证明

| 编号 | 场景 | 期望结果 |
| --- | --- | --- |
| T01 | A安装个人Skill并配置测试.env/内部config，再新建B/Cron | 不再次配置即可运行；读取同一共享文件 |
| T02 | 官方get_skill(pptx/docx)→引用md→执行真实脚本 | marker、脚本、Node/Python依赖和输出格式均可用，不只测目录存在 |
| T03 | A修改共享Skill或配置，B重新加载/启动脚本 | B看到新内容；已运行进程不被错误宣称自动热更新 |
| T04 | 根.env用普通写、apply_patch原子写，再重建A | A/B读到同一当前配置，不替换成私有兼容文件或旧inode |
| T05 | A/B/C各写/tmp/same-name并启动长进程 | 内容和进程独立；停止A不影响B/C |
| T06 | Bash/Python/文件工具访问其他owner、workdir、草稿、其他execd | 私有资源不可达；共享Skill/config仍正常可读写 |
| T07 | 刷新/切换模型/下一轮，或确认A实例丢失 | 正常复用；必要重建挂回A文件及共同配置，不借错用户缓存 |
| T08 | create竞争、网络超时、旧回调、删除后晚写 | 不出现两个私有目录写入者，不复活删除数据，不操作新代际 |
| T09 | 草稿发送取消重试、Session上传编辑、释放后历史下载/ZIP | claim、SHA、revision、live/capture/Workspace版本语义不变 |
| T10 | Chat/Cron并行publish同一Workspace文件 | 现有base/claim/CAS/配额与物理结果一致 |
| T11 | Skill禁用、重扫失败、推送中断、资料实例重建 | 现有失败/stale行为保持；不因新Session换ID重装或丢用户Skill |
| T12 | 长后台任务超出一轮对话，Agent对象过期 | 按新owner登记续租；取消和结束可精确回收 |
| T13 | 删除A、Cron完成、修改Profile、删除用户 | Session清理不删共享Skill/config；后端/物理身份准确，用户purge无失联残留 |
| T14 | 平台I/O旁放用户可写python模块/启动文件 | 固定平台流程不加载用户代码；AI执行只在自己的执行实例 |
| T15 | Chat先配置个人Skill；Cron按任务prompt加载Skill、stage正式文件、生成真实Office产物并publish | 不重复配置；读写只经Workspace工具；文件格式、base/version、cron actor/run/round、workspace_changes与artifacts归属正确 |
| T16 | Cron在stage/publish前丢claim，或publish提交成功后摘要记录失败 | 前者被fence拒绝；后者保留已提交正式结果、reconciliation补摘要，不自动重跑任务 |

沿用现有相关test文件，只保留必要的归属、竞争、claim/幂等、删除和格式回归。最终分别提供真实浏览器/自然Agent、只读数据库、真实Sandbox及格式解析、独立只读残留/账本四类证据。纯HTTP/mock/pytest不能证明容器挂载隔离。

## 15. 当前完成度

已完成：逐段重读代码并重订方案，纳入用户确认的Skill/config共享要求。

未完成：PoC、环境盘点、代码实现、数据库变更、数据迁移与部署。没有读取用户秘密文件，也没有把服务端最新文档能力当成现网事实。

本版最重要的实施顺序是：**先证明“一次配置、所有会话可用”与“私有文件/进程隔离”同时成立，再改身份与调用链。**
