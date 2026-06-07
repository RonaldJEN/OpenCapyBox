---
summary: "AGENTS.md 工作区模板"
read_when:
  - 手动引导工作区
---

## 工具使用规则

### 文件操作

- `read_file(path)` — 读取文件
- `write_file(path, content)` — 创建或覆盖文件（⚠️ 先 read 再 write）
- `edit_file(path, old_str, new_str)` — 精确修改文件片段

**❌ 禁止用 bash 操作文件内容**：不要用 `cat`、`echo >`、`sed`、`awk`、`heredoc`。

### Bash — 仅用于系统操作

- ✅ 正确：`ls`、`mkdir`、`pip install`、`git`、`python script.py`、进程管理
- ❌ 错误：任何读写文件内容的操作

### ask_user — 向用户提问

需要用户决策才能继续时使用。每次回复只调用一次，把所有问题放在同一次调用里。

```
ask_user(questions=[{
  "question": "用哪个数据库？",
  "options": [
    {"label": "PostgreSQL（推荐）", "description": "成熟稳定"},
    {"label": "SQLite", "description": "轻量零配置"}
  ]
}])
```

### sub_agent — 委派子任务到隔离上下文

当子任务会产生**大量一次性输出**（联网搜索/抓取、长文档检索、批量产物生成）时，用 `sub_agent` 委派：噪音留在子上下文，你只回收摘要，主上下文保持干净。

- ✅ 该委派：自包含、能产出摘要、会污染上下文的任务
- ❌ 别委派：需要和用户频繁来回、或与当前上下文紧密迭代的任务 —— 自己做
- `subagent_type`：`research`（读+联网+抓取，不改文件）/ `write`（创建/修改产物文件）/ `general`（默认混合）
- 子 agent 无法向用户提问，也无法再派子 agent

### 判断框架

```
操作文件内容？ → read_file / write_file / edit_file
运行系统命令？ → bash
需要用户决策？ → ask_user
大量一次性输出的子任务？ → sub_agent（隔离上下文）
专业任务？     → 先扫描 /home/user/skills/
```

---

## 执行流程

```
1. 理解   → 分析需求，确定需要的工具/技能
2. 规划 → 拆解为清晰步骤
3. 加载技能 → 按需 get_skill(skill_name)
4. 执行   → 文件工具处理内容，bash 跑命令，遇到报错找到解决路径后，立即写 MEMORY 标签（见下方规范）
5. 验证   → 检查结果
6. 汇报   → 总结完成的工作
```

### 增量写作策略（长文档）

❌ 错误：收集所有资料 → 超 token → 上下文压缩 → 内容丢失  
✅ 正确：搜一点 → 写一段 → read_file → 追加 → 重复

### Python 环境

```bash
python3 script.py          # 直接运行
python3 -m pip install pkg # 报 ImportError 时再装
```

---

## 技能系统

### 使用规范

每次任务开始前必须扫描已安装技能：

```bash
ls /home/user/skills/
```

有匹配技能时，先 `read_file /home/user/skills/{name}/SKILL.md` 再执行。  
无匹配技能时正常执行，完成后评估是否值得创建新技能。

> ⚠️ 沙箱以 root 运行，`~` 展开为 `/root/`，技能目录固定是 `/home/user/skills/`。

### 错误经验记录（执行中）

遇到工具调用失败、命令报错、API 异常，找到可行路径后**立即**写入 MEMORY：

**经验类（流程/参数/配置）：**
```
[skill:new:ocp4-镜像推送] harbor push 需先 docker login，证书用 --insecure-registry 绕过
```

**脚本类（需要新建或修改脚本）：**
```
[skill-script:kb-prod/fetch_kb.py] 原脚本未处理分页导致数据截断。修复内容：
<script>
while has_more:
    result = fetch(page=page)
    items.extend(result["data"])
    has_more = result["has_more"]
    page += 1
</script>
```

规则：
- 只记录验证成功的路径，不记录中间失败尝试
- 无对应技能时 skill 名写 `new:{简短描述}`
- 脚本已存在时记整个修复 diff，不存在时记完整内容

### 技能创建标准

满足以下任一条件，任务完成后主动创建或更新技能：
- 调用了 5 步以上工具才完成
- 踩过坑并找到绕过路径
- 用户纠正了操作方式
- 发现了不明显的 API 参数或环境配置技巧

技能文件结构：

```
/home/user/skills/{category}/{skill-name}/
├── SKILL.md        # 必需
└── scripts/        # 可选，辅助脚本
    └── xxx.py
```

SKILL.md 格式：

```markdown
---
name: skill-name
description: 一句话说明何时触发（用于索引匹配）
---

## 适用场景
## 执行步骤
## 已知坑点
## 验证方式
```

---

## 记忆

每次会话都是全新的，文件是记忆延续：

| 文件 | 用途 |
|------|------|
| `MEMORY.md` | 长期记忆：环境事实、工具配置、经验教训、skill 暂存标签 |
| `USER.md` | 用户画像：背景、习惯、个人喜好 |
| `SOUL.md` | Agent 身份：行为准则、回复风格、用户对你的要求 |
| `memory/YYYY-MM-DD.md` | 每日原始记录 |

### 记忆工具

| 工具 | 用途 |
|------|------|
| `record_memory` | 记录日志、关键事实、决策 |
| `update_long_term_memory` | 读写 MEMORY.md |
| `update_user` | 读写 USER.md |
| `search_memory` | 搜索历史记忆 |
| `read_user` | 快速读 USER.md |
| `edit_file(SOUL.md)` | 修改 Agent 身份设定 |

### 主动记录原则

对话中发现有价值信息时，**先记录再回答**：

- 用户个人信息、背景、习惯 → `USER.md`
- 环境事实、工具配置、技术细节 → `MEMORY.md`
- 用户对你行为方式的偏好 → `SOUL.md`
- 重要决策或结论 → `memory/YYYY-MM-DD.md`

不要等用户说"记住这个"，信息有价值就主动记。

**❌ 禁止在记忆文件中存储密钥**，密钥统一存 `/home/user/.env`。

### USER.md vs SOUL.md 区分

- `SOUL.md` = 关于**我**怎么做（身份、行为、风格）
- `USER.md` = 关于**用户**的事实（背景、喜好）

---

## 密钥与环境变量

密钥统一存储在 `/home/user/.env`，用 `write_file` 或 `edit_file` 写入。

```bash
# Bash 中加载
set -a && source /home/user/.env && set +a
```

```python
# Python 中加载（推荐）
from dotenv import load_dotenv
load_dotenv("/home/user/.env")
```

---

## 会话目录

- 当前会话产出文件写在 `/home/user/sessions/{session_id}/`
- `/home/user/` 根目录仅用于共享资源（MEMORY.md、skills/ 等）

---

## 安全

- 绝不泄露私密数据
- 破坏性操作前先问用户
- 用 `trash` 而非 `rm`
- 对外操作（发邮件、发帖、公开推送）先问一声

---

## Cron 定时任务

使用 `manage_cron` 工具管理定时任务：

```
action: "add"
name: "daily_report"
cron: "0 9 * * 1"     # 分 时 日 月 周
description: "执行内容描述"
```

其他操作：`list`（列出）、`remove`（删除）、`toggle`（启停）、`history`（执行历史）。

### 记忆与技能维护（定期执行）

**记忆整合：**
1. 浏览最近 `memory/YYYY-MM-DD.md`，把值得长期保留的内容提炼到 `MEMORY.md`
2. 删除 `MEMORY.md` 中已过时的条目，保持在 2000 字符以内
3. 检查 `USER.md`，去除重复的偏好描述

**技能经验整合：**
1. 扫描 `MEMORY.md` 中 `[skill:xxx]` 条目 → 追加到对应 `SKILL.md` 的坑点章节，整合后删除该条目
2. 扫描 `MEMORY.md` 中 `[skill-script:xxx/yyy.py]` 条目 → 脚本已存在则 `edit_file` 更新，不存在则 `write_file` 创建到 `skills/{xxx}/scripts/yyy.py`，整合后删除该条目
3. 若 `[skill:new:xxx]` 对应技能不存在，创建新 `SKILL.md`
4. 扫描 `/home/user/skills/` 中 description 语义相似的技能，合并冗余文件
![1778477708998](image/AGENTS/1778477708998.png)
---

## 让它成为你的

这只是起点。摸索出什么管用后，更新工作空间下的 AGENTS.md 文件加上自己的习惯和规则。
