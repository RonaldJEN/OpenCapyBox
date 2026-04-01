# Agent User Interaction Protocol SDK - 事件文档

## 概述

Agent User Interaction Protocol SDK 采用基于流式事件（streaming event-based）的架构。事件是 Agent 与前端之间通信的基本单位。本文档详细记录了事件类型及其属性。

---

## 事件类型枚举 (EventType)

`EventType` 枚举定义了系统中所有可能的事件类型：

```typescript
enum EventType {
  // 生命周期事件
  RUN_STARTED = "RUN_STARTED",
  RUN_FINISHED = "RUN_FINISHED",
  RUN_ERROR = "RUN_ERROR",
  STEP_STARTED = "STEP_STARTED",
  STEP_FINISHED = "STEP_FINISHED",
  
  // 文本消息事件
  TEXT_MESSAGE_START = "TEXT_MESSAGE_START",
  TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT",
  TEXT_MESSAGE_END = "TEXT_MESSAGE_END",
  
  // 工具调用事件
  TOOL_CALL_START = "TOOL_CALL_START",
  TOOL_CALL_ARGS = "TOOL_CALL_ARGS",
  TOOL_CALL_END = "TOOL_CALL_END",
  TOOL_CALL_RESULT = "TOOL_CALL_RESULT",
  
  // 状态管理事件
  STATE_SNAPSHOT = "STATE_SNAPSHOT",
  STATE_DELTA = "STATE_DELTA",
  MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT",
  
  // 活动事件
  ACTIVITY_SNAPSHOT = "ACTIVITY_SNAPSHOT",
  ACTIVITY_DELTA = "ACTIVITY_DELTA",
  
  // 特殊事件
  RAW = "RAW",
  CUSTOM = "CUSTOM",
}
```

---

## 基础事件 (BaseEvent)

所有事件都继承自 `BaseEvent` 类型，提供所有事件类型共享的通用属性。

```typescript
type BaseEvent = {
  type: EventType           // 区分字段
  timestamp?: number        // 事件创建时间戳
  rawEvent?: any            // 如果事件被转换过，则为原始事件数据
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| type | EventType | 事件类型（联合类型的区分字段） |
| timestamp | number (可选) | 事件创建时的时间戳 |
| rawEvent | any (可选) | 如果该事件经过转换，则为原始事件数据 |

---

## 生命周期事件

这些事件代表 Agent 运行的生命周期。

### RunStartedEvent - 运行开始事件

标志 Agent 运行的开始。

`RunStarted` 事件是在 Agent 开始处理请求时发出的第一个事件。它建立了一个由唯一的 `runId` 标识的新执行上下文。此事件作为前端初始化 UI 元素（如进度指示器或加载状态）的标记。它还提供了关键标识符，可用于将后续事件与此特定运行关联。

```typescript
type RunStartedEvent = BaseEvent & {
  type: EventType.RUN_STARTED
  threadId: string
  runId: string
  parentRunId?: string
  input?: RunAgentInput
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| threadId | string | 对话线程的 ID |
| runId | string | Agent 运行的 ID |
| parentRunId | string (可选) | 分支/时间旅行的血缘指针。如果存在，则指代同一线程中的先前运行，创建类似 git 的仅追加日志 |
| input | RunAgentInput (可选) | 为此次运行发送给 Agent 的确切输入负载。可能省略历史记录中已有的消息；`compactEvents()` 将对其进行规范化 |

### RunFinishedEvent - 运行结束事件【已升级】

标志 Agent 运行的成功完成或中断暂停。

`RunFinished` 事件表示 Agent 已完成当前运行的所有工作，或者需要暂停等待人工介入。

- 当 `outcome` 为 `"success"`（或省略且无 `interrupt`）时，表示正常完成
- 当 `outcome` 为 `"interrupt"` 时，表示 Agent 暂停执行，等待人工响应后恢复

```typescript
type RunFinishedEvent = BaseEvent & {
  type: EventType.RUN_FINISHED
  threadId: string
  runId: string
  result?: any
  outcome?: "success" | "interrupt"  // 【新增】运行结果类型
  interrupt?: InterruptDetails       // 【新增】中断详情
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| threadId | string | 对话线程的 ID |
| runId | string | Agent 运行的 ID |
| result | any (可选) | 运行的结果数据（当 outcome 为 success 时） |
| outcome | "success" \| "interrupt" (可选) | **【新增】** 运行结果类型。省略时为向后兼容，按旧逻辑处理 |
| interrupt | InterruptDetails (可选) | **【新增】** 当 outcome 为 "interrupt" 时，包含中断详情 |

**InterruptDetails 结构：**

```typescript
type InterruptDetails = {
  id?: string              // 中断标识符，恢复时需回传
  reason?: string          // 中断原因：human_approval, input_required, confirmation, policy_hold 等
  payload?: any            // 传给前端的数据（待审批内容、表单定义等）
}
```

**使用规则：**

1. 如果 `outcome` 省略：
   - 有 `interrupt` 字段 → 视为 interrupt
   - 无 `interrupt` 字段 → 视为 success

2. 恢复执行时：
   - 必须使用相同的 `threadId`
   - 如果 `interrupt.id` 存在，必须在 `RunAgentInput.resume.interruptId` 中回传

### RunErrorEvent - 运行错误事件

标志 Agent 运行过程中发生的错误。

`RunError` 事件表示 Agent 遇到了无法从中恢复的错误，导致运行提前终止。此事件提供了关于出错的信息，允许前端显示适当的错误消息并可能提供恢复选项。在 `RunError` 事件之后，此运行中不会发生进一步的处理。

```typescript
type RunErrorEvent = BaseEvent & {
  type: EventType.RUN_ERROR
  message: string
  code?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| message | string | 错误消息 |
| code | string (可选) | 可选的错误代码 |

### StepStartedEvent - 步骤开始事件

标志 Agent 运行中某个步骤的开始。

`StepStarted` 事件表示 Agent 正在开始其处理的特定子任务或阶段。步骤提供了对 Agent 进度的细粒度可见性，能够在 UI 中实现更精确的跟踪和反馈。步骤是可选的，但对于受益于分解为可观察阶段的复杂操作，强烈建议使用。`stepName` 可以是当前正在执行的节点或函数的名称。

```typescript
type StepStartedEvent = BaseEvent & {
  type: EventType.STEP_STARTED
  stepName: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| stepName | string | 步骤名称 |

### StepFinishedEvent - 步骤结束事件

标志 Agent 运行中某个步骤的完成。

`StepFinished` 事件表示 Agent 已完成特定的子任务或阶段。与对应的 `StepStarted` 事件配对时，它创建有界的上下文为离散的工作单元。前端可以使用这些事件来更新进度指示器、显示完成动画或显示特定于该步骤的结果。`stepName` 必须与对应的 `StepStarted` 事件匹配，以正确配对步骤的开始和结束。

```typescript
type StepFinishedEvent = BaseEvent & {
  type: EventType.STEP_FINISHED
  stepName: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| stepName | string | 步骤名称 |

---

## 文本消息事件

这些事件代表对话中文本消息的生命周期。文本消息事件遵循流式模式，其中内容是增量交付的。消息以 `TextMessageStart` 事件开始，随后是一个或多个 `TextMessageContent` 事件，当文本块可用时进行交付，最后以 `TextMessageEnd` 事件结束。

这种流式方法能够在生成时实时显示消息内容，与等待整个消息完成后再显示相比，提供了更响应的用户体验。

`TextMessageContent` 事件每个都包含一个带有文本块的 `delta` 字段。前端应按接收顺序连接这些 delta 以构建完整的消息。`messageId` 属性链接所有相关事件，允许前端将内容块与正确的消息关联。

### TextMessageStartEvent - 文本消息开始事件

标志文本消息的开始。

`TextMessageStart` 事件在对话中初始化一个新的文本消息。它建立一个唯一的 `messageId`，该 ID 将被后续的内容块和结束事件引用。此事件允许前端为传入的消息准备 UI，例如创建带有加载指示器的新消息气泡。`role` 属性标识消息是来自助手还是对话中的其他参与者。

```typescript
type TextMessageStartEvent = BaseEvent & {
  type: EventType.TEXT_MESSAGE_START
  messageId: string
  role: "developer" | "system" | "assistant" | "user" | "tool"
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 消息的唯一标识符 |
| role | string | 消息发送者的角色（"developer"、"system"、"assistant"、"user"、"tool"） |

### TextMessageContentEvent - 文本消息内容事件

表示流式文本消息中的内容块。

```typescript
type TextMessageContentEvent = BaseEvent & {
  type: EventType.TEXT_MESSAGE_CONTENT
  messageId: string
  delta: string  // 非空字符串
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 与 TextMessageStartEvent 中的 ID 匹配 |
| delta | string | 文本内容块（非空） |

### TextMessageEndEvent - 文本消息结束事件

标志文本消息的结束。

`TextMessageEnd` 事件标记流式文本消息的完成。接收到此事件后，前端知道消息已完成，不会添加更多内容。这允许 UI 完成渲染，移除任何加载指示器，并可能触发在消息完成后应发生的操作，例如启用回复控件或执行自动滚动以确保完整消息可见。

```typescript
type TextMessageEndEvent = BaseEvent & {
  type: EventType.TEXT_MESSAGE_END
  messageId: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 与 TextMessageStart 中的 ID 匹配 |

### TextMessageChunkEvent - 文本消息块事件（便捷事件）

便捷事件，自动展开为 Start → Content → End。

`TextMessageChunk` 事件允许您省略显式的 `TextMessageStart` 和 `TextMessageEnd` 事件。客户端流转换器将块展开为标准的三元组：

- 消息的第一个块必须包含 `messageId`，并将发出 `TextMessageStart`（未提供 `role` 时默认为 assistant）
- 每个包含 `delta` 的块为当前的 `messageId` 发出 `TextMessageContent`
- 当流切换到新的消息 ID 或流完成时，自动发出 `TextMessageEnd`

```typescript
type TextMessageChunkEvent = BaseEvent & {
  type: EventType.TEXT_MESSAGE_CHUNK
  messageId?: string  // 消息的第一个块必须包含
  role?: 'developer' | 'system' | 'assistant' | 'user'
  delta?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string (可选) | 消息的可选唯一标识符；消息的第一个块必须包含 |
| role | string (可选) | 发送者的可选角色（"developer"、"system"、"assistant"、"user"） |
| delta | string (可选) | 消息的可选文本内容 |

---

## 工具调用事件

这些事件代表 Agent 发起的工具调用的生命周期。工具调用遵循与文本消息类似的流式模式。当 Agent 需要使用工具时，它会发出 `ToolCallStart` 事件，随后是一个或多个 `ToolCallArgs` 事件，用于流式传输传递给工具的参数，最后以 `ToolCallEnd` 事件结束。

这种流式方法允许前端实时显示工具执行，使 Agent 的操作透明化，并提供关于正在调用哪些工具以及使用什么参数的即时反馈。

每个 `ToolCallArgs` 事件都包含一个带有参数块的 `delta` 字段。前端应按接收顺序连接这些 delta 以构建完整的参数对象。`toolCallId` 属性链接所有相关事件，允许前端将参数块与正确的工具调用关联。

### ToolCallStartEvent - 工具调用开始事件

标志工具调用的开始。

`ToolCallStart` 事件表示 Agent 正在调用工具以执行特定功能。此事件提供了被调用工具的名称，并建立了唯一的 `toolCallId`，该 ID 将被此工具调用中的后续事件引用。前端可以使用此事件向用户显示工具使用情况，例如显示特定操作正在进行的通知。可选的 `parentMessageId` 允许将工具调用与对话中的特定消息关联，为使用工具的原因提供上下文。

```typescript
type ToolCallStartEvent = BaseEvent & {
  type: EventType.TOOL_CALL_START
  toolCallId: string
  toolCallName: string
  parentMessageId?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| toolCallId | string | 工具调用的唯一标识符 |
| toolCallName | string | 被调用工具的名称 |
| parentMessageId | string (可选) | 父消息的 ID |

### ToolCallArgsEvent - 工具调用参数事件

表示工具调用的参数数据块。

`ToolCallArgs` 事件在参数可用时增量传递工具参数的各个部分。每个事件都包含 `delta` 属性中的参数数据片段。这些 delta 通常是 JSON 片段，当它们组合在一起时，会形成工具的完整参数对象。流式传输参数对于构建完整参数可能需要时间的复杂工具调用特别有价值。前端可以逐步向用户揭示这些参数，提供关于正在传递给工具的确切参数的洞察。

```typescript
type ToolCallArgsEvent = BaseEvent & {
  type: EventType.TOOL_CALL_ARGS
  toolCallId: string
  delta: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| toolCallId | string | 与 ToolCallStartEvent 中的 ID 匹配 |
| delta | string | 参数数据块 |

### ToolCallEndEvent - 工具调用结束事件

标志工具调用的结束。

`ToolCallEnd` 事件标记工具调用的完成。接收到此事件后，前端知道所有参数都已传输，工具执行正在进行或已完成。这允许 UI 完成工具调用显示并准备接收潜在结果。在工具执行结果单独返回的系统中，此事件表示 Agent 已完成指定工具及其参数，现在正在等待或已收到结果。

```typescript
type ToolCallEndEvent = BaseEvent & {
  type: EventType.TOOL_CALL_END
  toolCallId: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| toolCallId | string | 与 ToolCallStartEvent 中的 ID 匹配 |

### ToolCallResultEvent - 工具调用结果事件

提供工具调用执行的结果。

`ToolCallResult` 事件传递由 Agent 之前调用的工具的输出或结果。此事件在工具由系统执行后发送，并包含工具生成的实际输出。与工具调用规范的流式模式（开始、参数、结束）不同，结果作为完整单元传递，因为工具执行通常产生完整输出。前端可以使用此事件向用户显示工具结果，将其追加到对话历史中，或根据工具的输出触发后续操作。

```typescript
type ToolCallResultEvent = BaseEvent & {
  type: EventType.TOOL_CALL_RESULT
  messageId: string
  toolCallId: string
  content: string
  role?: "tool"
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 此结果所属的对话消息的 ID |
| toolCallId | string | 与对应的 ToolCallStartEvent 中的 ID 匹配 |
| content | string | 工具执行的实际结果/输出内容 |
| role | "tool" (可选) | 可选的角色标识符，通常为 "tool" |

### ToolCallChunkEvent - 工具调用块事件（便捷事件）

便捷事件，自动展开为 Start → Args → End。

`ToolCallChunk` 事件允许您省略显式的 `ToolCallStart` 和 `ToolCallEnd` 事件。客户端流转换器将块展开为标准的工具调用三元组：

- 工具调用的第一个块必须包含 `toolCallId` 和 `toolCallName`，并将发出 `ToolCallStart`（传播任何 `parentMessageId`）
- 每个包含 `delta` 的块为当前的 `toolCallId` 发出 `ToolCallArgs`
- 当流切换到新的 `toolCallId` 或流完成时，自动发出 `ToolCallEnd`

```typescript
type ToolCallChunkEvent = BaseEvent & {
  type: EventType.TOOL_CALL_CHUNK
  toolCallId?: string      // 后续块可选；工具调用的第一个块必须包含
  toolCallName?: string    // 后续块可选；工具调用的第一个块必须包含
  parentMessageId?: string // 可选的父消息 ID
  delta?: string           // 可选的参数数据块（通常是 JSON 片段）
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| toolCallId | string (可选) | 后续块可选；工具调用的第一个块必须包含 |
| toolCallName | string (可选) | 后续块可选；工具调用的第一个块必须包含 |
| parentMessageId | string (可选) | 可选的父消息 ID |
| delta | string (可选) | 可选的参数数据块（通常是 JSON 片段） |

---

## 状态管理事件

这些事件用于管理和同步 Agent 的状态与前端。协议中的状态管理遵循高效的快照-增量模式，其中完整的状态快照在初始时或不频繁地发送，而增量更新用于持续的变化。

这种方法在完整性和效率之间进行了优化：快照确保前端具有完整的状态上下文，而增量更新最小化了频繁更新的数据传输。它们共同使前端能够保持 Agent 状态的准确表示，而无需不必要的数据传输。

快照和增量的组合允许前端高效地跟踪 Agent 状态的变化，同时确保一致性。快照作为同步点，将状态重置为已知的基线，而增量在快照之间提供轻量级的更新。

### StateSnapshotEvent - 状态快照事件

提供 Agent 状态的完整快照。

`StateSnapshot` 事件传递 Agent 当前状态的全面表示。此事件通常在交互开始时或需要同步时发送。它包含与前端相关的所有状态变量，允许前端完全重建其内部表示。前端应该使用此快照的内容替换其现有的状态模型，而不是尝试将其与先前的状态合并。

```typescript
type StateSnapshotEvent = BaseEvent & {
  type: EventType.STATE_SNAPSHOT
  snapshot: any  // StateSchema
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| snapshot | any | 完整的状态快照 |

### StateDeltaEvent - 状态增量事件

使用 JSON Patch 提供对 Agent 状态的部分更新。

`StateDelta` 事件包含以 JSON Patch 操作形式（如 RFC 6902 中定义）的 Agent 状态增量更新。每个 delta 代表应用于当前状态模型的特定更改。这种方法在带宽上是高效的，仅发送已更改的内容而不是整个状态。前端应按顺序应用这些补丁以保持准确的状态表示。如果前端在应用补丁后检测到不一致，可以请求新的 `StateSnapshot`。

```typescript
type StateDeltaEvent = BaseEvent & {
  type: EventType.STATE_DELTA
  delta: any[]  // JSON Patch 操作 (RFC 6902)
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| delta | any[] | JSON Patch 操作数组 (RFC 6902) |

### MessagesSnapshotEvent - 消息快照事件

提供对话中所有消息的快照。

`MessagesSnapshot` 事件传递当前对话中消息的完整历史记录。与通用状态快照不同，这专门关注对话记录。此事件对于初始化聊天历史、在连接中断后同步或在用户加入正在进行的对话时提供全面视图非常有用。前端应使用此事件来建立或刷新向用户显示的对话上下文。

```typescript
type MessagesSnapshotEvent = BaseEvent & {
  type: EventType.MESSAGES_SNAPSHOT
  messages: Message[]
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messages | Message[] | 消息对象数组 |

---

## 活动事件

活动事件暴露在聊天消息之间发生的结构化、进行中的活动更新。它们遵循与状态系统相同的快照/增量模式，以便 UI 可以立即呈现完整的活动视图，然后在新信息到达时增量更新它。

### ActivitySnapshotEvent - 活动快照事件

传递活动消息的完整快照。

前端应该创建一个新的 `ActivityMessage` 或使用快照提供的负载替换现有的消息。

```typescript
type ActivitySnapshotEvent = BaseEvent & {
  type: EventType.ACTIVITY_SNAPSHOT
  messageId: string
  activityType: string
  content: Record<string, any>
  replace?: boolean
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 此事件更新的 ActivityMessage 的标识符 |
| activityType | string | 活动区分符（例如 "PLAN"、"SEARCH"） |
| content | Record<string, any> | 表示完整活动状态的结构化 JSON 负载 |
| replace | boolean (可选) | 可选。默认为 true。当为 false 时，如果消息已存在则忽略快照 |

### ActivityDeltaEvent - 活动增量事件

使用 JSON Patch 操作对现有活动应用增量更新。

活动增量应按顺序应用于之前同步的活动内容。如果应用程序检测到分歧，它可以请求或发出新的 `ActivitySnapshot` 以重新同步。

```typescript
type ActivityDeltaEvent = BaseEvent & {
  type: EventType.ACTIVITY_DELTA
  messageId: string
  activityType: string
  patch: any[]  // RFC 6902 JSON Patch 操作
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 目标活动消息的标识符 |
| activityType | string | 活动区分符（镜像来自最近快照的值） |
| patch | any[] | 应用于活动数据的 RFC 6902 JSON Patch 操作数组 |

---

## 特殊事件

特殊事件通过允许特定于系统的功能和与外部系统的集成，为协议提供了灵活性。这些事件不遵循其他事件类型的标准生命周期或流式模式，而是服务于专门的目的。

### RawEvent - 原始事件

用于从外部系统传递事件。

`Raw` 事件充当来自外部系统或不遵循 Agent UI 协议的源的事件的容器。此事件类型通过将其他事件包装在标准化格式中，实现了与其他基于事件的系统的互操作性。封装的事件数据以其原始形式保存在 `event` 属性中，而可选的 `source` 属性标识它来自的系统。前端可以使用此信息适当地处理外部事件，要么直接处理它们，要么将它们委托给特定于系统的处理程序。

```typescript
type RawEvent = BaseEvent & {
  type: EventType.RAW
  event: any
  source?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| event | any | 原始事件数据 |
| source | string (可选) | 可选的源标识符 |

### CustomEvent - 自定义事件

用于应用程序特定的自定义事件。

`Custom` 事件为实现标准事件类型未涵盖的功能提供了扩展机制。与作为透传容器的 `Raw` 事件不同，`Custom` 事件明确是协议的一部分，但具有应用程序定义的语义。`name` 属性标识特定的自定义事件类型，而 `value` 属性包含关联的数据。这种机制允许协议扩展而无需正式的规范更改。团队应该记录他们的自定义事件，以确保前端和 Agent 之间的一致实现。

```typescript
type CustomEvent = BaseEvent & {
  type: EventType.CUSTOM
  name: string
  value: any
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| name | string | 自定义事件的名称 |
| value | any | 与事件关联的值 |

---

## 事件架构 (Event Schemas)

SDK 使用 Zod 架构来验证事件：

```typescript
const EventSchemas = z.discriminatedUnion("type", [
  TextMessageStartEventSchema,
  TextMessageContentEventSchema,
  TextMessageEndEventSchema,
  ToolCallStartEventSchema,
  ToolCallArgsEventSchema,
  ToolCallEndEventSchema,
  ToolCallResultEventSchema,
  StateSnapshotEventSchema,
  StateDeltaEventSchema,
  MessagesSnapshotEventSchema,
  ActivitySnapshotEventSchema,
  ActivityDeltaEventSchema,
  RawEventSchema,
  CustomEventSchema,
  RunStartedEventSchema,
  RunFinishedEventSchema,
  RunErrorEventSchema,
  StepStartedEventSchema,
  StepFinishedEventSchema,
])
```

这允许在运行时验证事件并提供 TypeScript 类型推断。

---

## 事件流模式

协议中的事件通常遵循特定模式：

### 开始-内容-结束模式

用于流式内容（文本消息、工具调用）

- **开始事件**：启动流
- **内容事件**：传递数据块
- **结束事件**：标记完成

### 快照-增量模式

用于状态同步

- **快照**：提供完整状态
- **增量事件**：提供增量更新

### 生命周期模式

用于监控 Agent 运行

- **Started 事件**：标记开始
- **Finished/Error 事件**：标记结束

### 中断-恢复模式【新增】

用于 Human-in-the-Loop 工作流

- **RUN_FINISHED (outcome: "interrupt")**：Agent 暂停，等待人工介入
- **RunAgentInput (resume)**：用户响应后恢复执行
- **RUN_STARTED (parentRunId)**：恢复后的新运行，关联到被中断的运行

```
┌─────────────┐     RUN_FINISHED          ┌─────────────┐
│   Agent     │  ─────────────────────►   │   前端      │
│   执行中    │   outcome: "interrupt"     │   显示审批  │
└─────────────┘                           └─────────────┘
                                                 │
                                          用户操作（审批/拒绝/补充信息）
                                                 │
                                                 ▼
┌─────────────┐     RunAgentInput          ┌─────────────┐
│   Agent     │  ◄─────────────────────    │   前端      │
│   恢复执行  │   resume: {...}            │   发送响应  │
└─────────────┘                           └─────────────┘
```

---

## 草稿事件

这些事件当前处于草稿状态，在最终确定之前可能会更改。它们代表协议的提议扩展，正在积极开发和讨论中。

### 推理事件

DRAFT 视图提案

推理事件支持 LLM 推理可见性和连续性，在维护隐私的同时实现思维链推理。

#### ReasoningStartEvent - 推理开始事件

标记推理的开始。

```typescript
type ReasoningStartEvent = BaseEvent & {
  type: EventType.REASONING_START
  messageId: string
  encryptedContent?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 此推理的唯一标识符 |
| encryptedContent | string (可选) | 可选的加密内容 |

#### ReasoningMessageStartEvent - 推理消息开始事件

标记推理消息的开始。

```typescript
type ReasoningMessageStartEvent = BaseEvent & {
  type: EventType.REASONING_MESSAGE_START
  messageId: string
  role: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 消息的唯一标识符 |
| role | string | 推理消息的角色 |

#### ReasoningMessageContentEvent - 推理消息内容事件

表示流式推理消息中的内容块。

```typescript
type ReasoningMessageContentEvent = BaseEvent & {
  type: EventType.REASONING_MESSAGE_CONTENT
  messageId: string
  delta: string  // 非空字符串
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 与 ReasoningMessageStart 中的 ID 匹配 |
| delta | string | 推理内容块（非空） |

#### ReasoningMessageEndEvent - 推理消息结束事件

标记推理消息的结束。

```typescript
type ReasoningMessageEndEvent = BaseEvent & {
  type: EventType.REASONING_MESSAGE_END
  messageId: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 与 ReasoningMessageStart 中的 ID 匹配 |

#### ReasoningMessageChunkEvent - 推理消息块事件（便捷事件）

自动开始/关闭推理消息的便捷事件。

```typescript
type ReasoningMessageChunkEvent = BaseEvent & {
  type: EventType.REASONING_MESSAGE_CHUNK
  messageId?: string
  delta?: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string (可选) | 可选的消息 ID |
| delta | string (可选) | 可选的推理内容块 |

#### ReasoningEndEvent - 推理结束事件

标记推理的结束。

```typescript
type ReasoningEndEvent = BaseEvent & {
  type: EventType.REASONING_END
  messageId: string
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string | 此推理的唯一标识符 |

#### MetaEvent - 元事件

DRAFT 视图提案

元事件提供独立于 Agent 运行的注释和信号，例如用户反馈或外部系统事件。

可以在流中任何位置发生的侧带注释事件。

```typescript
type MetaEvent = BaseEvent & {
  type: EventType.META
  metaType: string
  payload: any
}
```

| 属性 | 类型 | 描述 |
|------|------|------|
| metaType | string | 应用程序定义的类型（例如 "thumbs_up"、"tag"） |
| payload | any | 应用程序定义的负载 |

---

## 实现注意事项

在实现事件处理程序时：

- 事件应按接收顺序处理
- 具有相同 ID（例如 `messageId`、`toolCallId`）的事件属于同一逻辑流
- 实现应对乱序交付具有弹性
- 自定义事件应遵循既定模式以保持一致性

### Human-in-the-Loop 实现注意事项【新增】

1. **状态保持**：中断期间，前端应保存当前会话状态，以便恢复时可以无缝继续
2. **超时处理**：长时间等待人工响应时，应考虑超时策略和状态持久化
3. **并发控制**：同一线程不应有多个等待中的中断
4. **错误恢复**：恢复请求失败时，应提供重试机制
5. **审计日志**：所有中断和恢复操作应记录日志，用于追溯和合规

---

## 总结

### 事件分类

1. **生命周期事件**：管理 Agent 运行的完整生命周期
   - `RUN_STARTED` / `RUN_FINISHED` / `RUN_ERROR`
   - `STEP_STARTED` / `STEP_FINISHED`

2. **文本消息事件**：处理流式文本消息
   - `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END`
   - `TEXT_MESSAGE_CHUNK`（便捷事件）

3. **工具调用事件**：管理工具调用的生命周期
   - `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` / `TOOL_CALL_RESULT`
   - `TOOL_CALL_CHUNK`（便捷事件）

4. **状态管理事件**：管理 Agent 状态和消息
   - `STATE_SNAPSHOT` / `STATE_DELTA`
   - `MESSAGES_SNAPSHOT`
   - `ACTIVITY_SNAPSHOT` / `ACTIVITY_DELTA`

5. **特殊事件**：扩展用途
   - `RAW`：外部系统事件透传
   - `CUSTOM`：自定义事件

### 核心特性

- **流式架构**：所有事件都支持流式传输
- **类型安全**：使用 TypeScript 和 Zod 提供完整的类型推断和运行时验证
- **便捷事件**：提供 Chunk 事件自动展开为标准事件序列
- **增量更新**：支持 JSON Patch 进行增量状态更新
- **血缘追踪**：支持 parentRunId 进行分支/时间旅行功能
- **Human-in-the-Loop**：【新增】支持中断-恢复模式，实现人机协作工作流
