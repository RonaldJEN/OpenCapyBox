# Agent User Interaction Protocol SDK - 核心类型文档

## 概述

Agent User Interaction Protocol SDK 建立在一组核心类型之上，这些类型代表了整个系统中使用的基础结构。本文档记录了这些类型及其属性。

---

## 核心类型

### RunAgentInput - 运行 Agent 输入

运行 Agent 的输入参数。在 HTTP API 中，这是 POST 请求的请求体。

```typescript
type RunAgentInput = {
  threadId: string
  runId: string
  parentRunId?: string
  state: any
  messages: Message[]
  tools: Tool[]
  context: Context[]
  forwardedProps: any
  resume?: ResumePayload  // 【新增】Human-in-the-Loop 恢复执行
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| threadId | string | 对话线程的 ID |
| runId | string | 当前运行的 ID |
| parentRunId | string (可选) | 产生此运行的运行 ID |
| state | any | Agent 的当前状态 |
| messages | Message[] | 对话中的消息数组 |
| tools | Tool[] | Agent 可用的工具数组 |
| context | Context[] | 提供给 Agent 的上下文对象数组 |
| forwardedProps | any | 传递给 Agent 的附加属性 |
| resume | ResumePayload (可选) | **【新增】** 用于从中断状态恢复执行，包含用户的响应数据 |

---

## Human-in-the-Loop 类型【新增章节】

本节定义了支持人机协作工作流（Human-in-the-Loop）所需的类型。这些类型使 Agent 能够在执行敏感操作前请求人工审批、收集额外输入或确认潜在风险操作。

### InterruptReason - 中断原因

表示 Agent 请求中断的原因类型。

```typescript
type InterruptReason =
  | "human_approval"     // 需要人工审批（如敏感操作确认）
  | "input_required"     // 需要用户补充信息
  | "confirmation"       // 需要用户确认（如二次确认）
  | "policy_hold"        // 组织策略或合规要求导致的暂停
  | "error_recovery"     // 遇到错误，需要用户指导
  | string               // 允许自定义原因
```

| 值 | 描述 |
|------|------|
| human_approval | 敏感操作（如发送邮件、资金转账、删除数据）需要人工批准 |
| input_required | Agent 需要用户提供额外的信息或文件才能继续 |
| confirmation | 需要用户对某个决策或结果进行确认 |
| policy_hold | 由于组织政策或合规要求自动触发的暂停 |
| error_recovery | Agent 遇到无法自动处理的错误，需要人工指导 |

---

### InterruptDetails - 中断详情

代表 Agent 发起中断时携带的详细信息。

```typescript
type InterruptDetails = {
  id?: string
  reason?: InterruptReason
  payload?: any
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string (可选) | 中断的唯一标识符。如果提供，恢复时必须回传此 ID |
| reason | InterruptReason (可选) | 中断的原因，用于前端选择合适的 UI 组件展示 |
| payload | any (可选) | 传递给前端的任意 JSON 数据，如待审批的内容、表单字段定义、风险说明等 |

**常见 payload 结构示例：**

```typescript
// human_approval 场景
{
  action: string           // 待执行的操作名称
  description: string      // 操作描述
  details: any             // 操作详细参数
  riskLevel: "low" | "medium" | "high"  // 风险等级
}

// input_required 场景
{
  fields: Array<{
    name: string
    type: "text" | "number" | "file" | "select"
    label: string
    required: boolean
    options?: string[]     // select 类型时的选项
  }>
  message: string          // 提示信息
}

// confirmation 场景
{
  title: string
  message: string
  confirmText?: string     // 确认按钮文本
  cancelText?: string      // 取消按钮文本
}
```

---

### ResumePayload - 恢复执行负载

代表用户响应中断后，用于恢复 Agent 执行的数据。

```typescript
type ResumePayload = {
  interruptId?: string
  payload?: any
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| interruptId | string (可选) | 如果中断时提供了 id，则必须回传此字段 |
| payload | any (可选) | 用户的响应数据，如审批结果、表单填写内容、确认决定等 |

**常见 payload 结构示例：**

```typescript
// 审批响应
{
  approved: boolean        // 是否批准
  comment?: string         // 审批意见
  modifications?: any      // 对原操作的修改建议
}

// 信息补充响应
{
  [fieldName: string]: any // 表单字段值
}

// 确认响应
{
  confirmed: boolean       // 是否确认
}
```

---

### RunFinishedOutcome - 运行结束结果

表示 Agent 运行结束时的结果类型。

```typescript
type RunFinishedOutcome = "success" | "interrupt"
```

| 值 | 描述 |
|------|------|
| success | Agent 成功完成所有工作 |
| interrupt | Agent 暂停执行，等待人工介入后恢复 |

---

## 消息类型

SDK 包含多种消息类型，代表系统中不同种类的消息。

### Role - 角色类型

表示消息发送者可能具有的角色。

```typescript
type Role = "developer" | "system" | "assistant" | "user" | "tool" | "activity"
```

---

### DeveloperMessage - 开发者消息

代表来自开发者的消息。

```typescript
type DeveloperMessage = {
  id: string
  role: "developer"
  content: string
  name?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string | 消息的唯一标识符 |
| role | "developer" | 消息发送者的角色，固定为 "developer" |
| content | string | 消息的文本内容（必需） |
| name | string (可选) | 发送者的名称 |

---

### SystemMessage - 系统消息

代表系统消息。

```typescript
type SystemMessage = {
  id: string
  role: "system"
  content: string
  name?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string | 消息的唯一标识符 |
| role | "system" | 消息发送者的角色，固定为 "system" |
| content | string | 消息的文本内容（必需） |
| name | string (可选) | 发送者的名称 |

---

### AssistantMessage - 助手消息

代表来自助手（Assistant）的消息。

```typescript
type AssistantMessage = {
  id: string
  role: "assistant"
  content?: string
  name?: string
  toolCalls?: ToolCall[]
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string | 消息的唯一标识符 |
| role | "assistant" | 消息发送者的角色，固定为 "assistant" |
| content | string (可选) | 消息的文本内容 |
| name | string (可选) | 发送者的名称 |
| toolCalls | ToolCall[] (可选) | 在此消息中进行的工具调用 |

---

### UserMessage - 用户消息

代表来自用户的消息。

```typescript
type UserMessage = {
  id: string
  role: "user"
  content: string | InputContent[]
  name?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string | 消息的唯一标识符 |
| role | "user" | 消息发送者的角色，固定为 "user" |
| content | string \| InputContent[] | 纯文本或多模态内容片段的有序数组 |
| name | string (可选) | 发送者的名称 |

---

#### InputContent - 输入内容

支持的多模态片段的联合类型。

```typescript
type InputContent = TextInputContent | BinaryInputContent
```

##### TextInputContent - 文本输入内容

```typescript
type TextInputContent = {
  type: "text"
  text: string
}
```

##### BinaryInputContent - 二进制输入内容

```typescript
type BinaryInputContent = {
  type: "binary"
  mimeType: string
  id?: string
  url?: string
  data?: string
  filename?: string
}
```

**注意**：必须至少提供 id、url 或 data 中的一个。

| 属性 | 类型 | 描述 |
|------|------|------|
| type | "binary" | 内容类型，固定为 "binary" |
| mimeType | string | MIME 类型 |
| id | string (可选) | 内容 ID |
| url | string (可选) | 内容 URL |
| data | string (可选) | 内容数据（Base64 编码） |
| filename | string (可选) | 文件名 |

---

### ToolMessage - 工具消息

代表来自工具的消息。

```typescript
type ToolMessage = {
  id: string
  content: string
  role: "tool"
  toolCallId: string
  error?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string | 消息的唯一标识符 |
| content | string | 消息的文本内容 |
| role | "tool" | 消息发送者的角色，固定为 "tool" |
| toolCallId | string | 此消息响应的工具调用 ID |
| error | string (可选) | 如果工具调用失败，则为错误消息 |

---

### ActivityMessage - 活动消息

代表在聊天消息之间发出的结构化活动进度。

```typescript
type ActivityMessage = {
  id: string
  role: "activity"
  activityType: string
  content: Record<string, any>
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string | 活动消息的唯一标识符 |
| role | "activity" | 固定的区分字段，将消息标识为活动 |
| activityType | string | 用于渲染器选择的活动区分符 |
| content | Record<string, any> | 表示活动状态的结构化负载 |

---

### Message - 消息联合类型

代表系统中任何类型的消息的联合类型。

```typescript
type Message =
  | DeveloperMessage
  | SystemMessage
  | AssistantMessage
  | UserMessage
  | ToolMessage
  | ActivityMessage
```

---

## 工具调用类型

### ToolCall - 工具调用

代表由 Agent 进行的工具调用。

```typescript
type ToolCall = {
  id: string
  type: "function"
  function: FunctionCall
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| id | string | 工具调用的唯一标识符 |
| type | "function" | 工具调用的类型，始终为 "function" |
| function | FunctionCall | 关于被调用函数的详细信息 |

---

### FunctionCall - 函数调用

表示工具调用中的函数名称和参数。

```typescript
type FunctionCall = {
  name: string
  arguments: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| name | string | 要调用的函数名称 |
| arguments | string | 函数参数的 JSON 编码字符串 |

---

## 上下文类型

### Context - 上下文

代表提供给 Agent 的一段上下文信息。

```typescript
type Context = {
  description: string
  value: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| description | string | 此上下文所代表内容的描述 |
| value | string | 实际的上下文值 |

---

## 工具类型

### Tool - 工具

定义可由 Agent 调用的工具。

```typescript
type Tool = {
  name: string
  description: string
  parameters: any  // JSON Schema
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| name | string | 工具的名称 |
| description | string | 工具功能的描述 |
| parameters | any | 定义工具参数的 JSON Schema |

---

## 状态类型

### State - 状态

代表 Agent 在执行期间的状态。

```typescript
type State = any
```

状态类型是灵活的，可以保存 Agent 实现所需的任何数据结构。

---

## 类型关系图

```
RunAgentInput
├── threadId: string
├── runId: string
├── parentRunId?: string
├── state: State (any)
├── messages: Message[]
│   ├── DeveloperMessage
│   ├── SystemMessage
│   ├── AssistantMessage
│   │   └── toolCalls?: ToolCall[]
│   │       └── function: FunctionCall
│   ├── UserMessage
│   │   └── content: string | InputContent[]
│   │       ├── TextInputContent
│   │       └── BinaryInputContent
│   ├── ToolMessage
│   └── ActivityMessage
├── tools: Tool[]
├── context: Context[]
├── forwardedProps: any
└── resume?: ResumePayload          【新增】
    ├── interruptId?: string
    └── payload?: any

InterruptDetails                     【新增】
├── id?: string
├── reason?: InterruptReason
└── payload?: any
```

---

## 总结

### 核心类型分类

1. **输入类型**
   - `RunAgentInput`：运行 Agent 的完整输入参数

2. **消息类型** (6种)
   - `DeveloperMessage`：开发者消息
   - `SystemMessage`：系统消息
   - `AssistantMessage`：助手消息（可包含工具调用）
   - `UserMessage`：用户消息（支持多模态内容）
   - `ToolMessage`：工具消息
   - `ActivityMessage`：活动消息

3. **工具调用类型**
   - `ToolCall`：工具调用
   - `FunctionCall`：函数调用详情

4. **支持类型**
   - `Context`：上下文信息
   - `Tool`：工具定义
   - `State`：Agent 状态（灵活类型）

5. **内容类型**
   - `InputContent`：多模态输入内容
   - `TextInputContent`：文本内容
   - `BinaryInputContent`：二进制内容

6. **Human-in-the-Loop 类型**【新增】
   - `InterruptReason`：中断原因枚举
   - `InterruptDetails`：中断详情
   - `ResumePayload`：恢复执行负载
   - `RunFinishedOutcome`：运行结束结果类型

### 关键特性

- **类型安全**：所有类型都有明确的 TypeScript 定义
- **多模态支持**：用户消息支持文本和二进制内容
- **灵活状态**：State 类型为 `any`，支持任意数据结构
- **工具集成**：完整的工具调用和结果返回机制
- **活动追踪**：ActivityMessage 用于结构化活动进度展示
- **上下文管理**：Context 类型支持向 Agent 提供额外上下文信息
- **Human-in-the-Loop**：【新增】支持人工审批、信息补充、确认等人机协作场景
