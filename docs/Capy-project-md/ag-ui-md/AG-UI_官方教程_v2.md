# 掌握 AG-UI 事件类型，正确构建智能体

智能体-用户交互协议（AG-UI）正迅速成为连接智能体与用户界面的标准。

它提供了一个清晰的事件流，使 UI 与智能体的操作保持同步。所有通信都被分解为类型化的事件。

我一直在深入研究该协议，特别是围绕那些核心事件类型，以理解它们如何协同工作。以下是我学到的内容以及为什么它很重要。

## 1. 智能体协议（AG-UI）

AG-UI（智能体-用户交互协议）是一个开放的、轻量级的、基于事件的协议，用于标准化 AI 智能体与面向用户的应用程序之间的实时通信。

在智能体应用中，前端（假设是 React UI）和智能体后端通过 WebSockets、SSE 或 HTTP 交换 JSON 事件流（如消息、工具调用、状态更新、生命周期信号）。

这使得 UI 能够与智能体的进度保持完美同步，在生成 token 时进行流式传输，显示工具执行进度，并反映实时状态变化。

与为每个智能体使用自定义 WebSocket 或临时 JSON 不同，AG-UI 提供了事件的通用词汇表，因此任何兼容 AG-UI 的智能体（如 LangGraph、CrewAI、Mastra、LlamaIndex、Pydantic AI、Agno）都可以插入到任何支持 AG-UI 的前端，而无需重写集成。查看所有支持的框架列表。

例如，下图显示了 UI 中的用户操作如何通过 AG-UI 发送到任何智能体后端，以及响应如何作为标准化事件流回：

protocol
图片来源：dailydoseofds.com

您可以使用以下命令通过 CLI 创建新应用。

```bash
npx create-ag-ui-app@latest
```

## 2. 什么是 AG-UI 事件类型，为什么要关注它们？

AG-UI 定义了 17 种核心事件类型（包括特殊情况），涵盖了智能体在其生命周期中可能做的所有事情。可以将事件视为智能体和前端之间的基本通信单元。

每个事件都是一个 JSON 对象，具有一个类型（如 "TextMessageContent"、"ToolCallStart"）和一个有效载荷。

ag-ui event diagram
图片来源：dailydoseofds.com

因为这些事件是标准和自描述的，前端确切地知道如何解释它们。例如：

- TEXT_MESSAGE_CONTENT：事件流传输 LLM token
- TOOL_CALL_START/END：传达函数调用进度
- STATE_DELTA：携带 JSON Patch 增量以同步状态

将这些标准化——将 UI 与智能体逻辑解耦，反之亦然。UI 不需要自定义胶水代码来理解智能体的行为。

任何智能体后端都可以发出 AG-UI 事件。
任何兼容 AG-UI 的 UI 都可以消费它们。

该协议将所有事件分为五个高级别类别：

✅ 生命周期事件：跟踪智能体运行的进度（开始、完成、错误、子步骤）。

✅ 文本消息事件：流式传输聊天或其他文本内容（逐个 token）。

✅ 工具调用事件：报告对外部工具或 API 的调用及其结果。

✅ 状态管理事件：在智能体和 UI 之间同步共享应用程序状态。

✅ 特殊事件：用于高级用例的通用传递或自定义事件。

在下一节中，让我们通过实际示例了解所有这些内容。

## 3. 用实际示例拆解所有事件

如果您有兴趣自己探索，请阅读官方文档，其中包括概述、生命周期事件、流模式等。

所有事件都继承自 BaseEvent 类型，它提供了所有事件类型共享的公共属性：

- type：事件类型
- timestamp（可选）：事件创建时间
- rawEvent（可选）：如果事件被转换，则为原始事件数据

```typescript
type BaseEvent = {
  type: EventType // 判别字段
  timestamp?: number
  rawEvent?: any
}
```

还有其他属性（如 runId、threadId）是特定于事件类型的。

### 事件编码

智能体用户交互协议使用流式方法将事件从智能体发送到客户端。EventEncoder 类提供了将事件编码为可以通过 HTTP 发送的格式的功能。

我们将在所有事件类别的示例中使用它，所以这里有一个简单的示例：

```python
from ag_ui.core import BaseEvent
from ag_ui.encoder import EventEncoder

# 初始化编码器
encoder = EventEncoder()

# 编码事件
encoded_event = encoder.encode(event)
```

一旦编码器设置完毕，智能体就可以实时发出事件，前端可以立即监听并做出反应。在文档中阅读更多内容。

让我们通过示例深入介绍每个类别。

### ✅ 生命周期事件

生命周期事件有助于监控整体运行及其子步骤。它们告诉 UI 何时运行开始、进行、成功或失败。

五个生命周期事件是：

1) RunStarted：信号智能体运行的开始
2) RunFinished：信号运行的成功完成**或中断暂停**【已升级】
3) RunError：信号运行期间的失败。
4) StepStarted（可选）：运行中子任务的开始。
5) StepFinished：标记子任务的完成。

在单个运行中可能有多对 StepStarted / StepFinished，代表通过中间子任务的进度。

示例流程：

```javascript
// 正常完成流程
RunStarted → (StepStarted → StepFinished …) → RunFinished

// 中断流程（Human-in-the-Loop）【新增】
RunStarted → ... → RunFinished(interrupt) → [用户响应] → RunStarted(resume) → ... → RunFinished(success)
```

如果出现故障，RunError 将替换 RunFinished。

以下是在智能体端发出事件的简单示例：

```python
# 当智能体开始运行时
yield encoder.encode(RunStartedEvent(
    type=EventType.RUN_STARTED,
    thread_id=thread_id,
    run_id=run_id
))
# ... 智能体执行工作（例如发送消息、调用工具等）...
# 当运行完成时
yield encoder.encode(RunFinishedEvent(
    type=EventType.RUN_FINISHED,
    thread_id=thread_id,
    run_id=run_id
))
```

以下是股票分析智能体的简单示例代码（前端侧）。

```javascript
async function handleLifecycleEvents(event) {
  switch(event.type) {
    case 'RUN_STARTED':
      // event.thread_id, event.run_id 在真实的 AG-UI 事件中可用
      setAgentStatus('processing');
      showProgressBar();
      break;
    case 'STEP_STARTED':
      updateStepIndicator(event.step_name);
      // 例如："收集股票数据"、"分析趋势"、"生成见解"
      break;
    case 'STEP_FINISHED':
      clearStepIndicator(event.step_name);
      break;
    case 'RUN_FINISHED':
      // 【新增】检查是否是中断
      if (event.outcome === 'interrupt') {
        handleInterrupt(event.interrupt);
      } else {
        setAgentStatus('completed');
        hideProgressBar();
      }
      break;
    case 'RUN_ERROR':
      showErrorUI(event.error);
      offerRetryOption();
      logErrorForDebugging(event);
      break;
  }
}
```

在这里，UI 会监听这些事件，以了解何时显示加载指示器以及何时显示最终结果。如果出现故障，智能体会发出 RunError，UI 可以捕获它以显示错误消息。

#### Human-in-the-Loop 支持【新增章节】

AG-UI 协议原生支持 Human-in-the-Loop（人机协作）工作流。当智能体需要人工审批、用户输入或确认时，可以通过 `RUN_FINISHED` 事件的 `interrupt` 机制来实现。

**中断类型（InterruptReason）：**

| 类型 | 说明 |
|------|------|
| human_approval | 敏感操作需要人工批准（如发送邮件、资金转账） |
| input_required | 需要用户提供额外信息 |
| confirmation | 需要用户确认决策 |
| policy_hold | 组织策略或合规要求导致的暂停 |
| error_recovery | 遇到错误，需要人工指导 |

**智能体端发出中断：**

```python
# 当需要人工审批时
yield encoder.encode(RunFinishedEvent(
    type=EventType.RUN_FINISHED,
    thread_id=thread_id,
    run_id=run_id,
    outcome="interrupt",
    interrupt={
        "id": "approval_001",
        "reason": "human_approval",
        "payload": {
            "action": "send_email",
            "description": "发送订单确认邮件",
            "details": {
                "to": "customer@example.com",
                "subject": "订单确认"
            },
            "riskLevel": "medium"
        }
    }
))
```

**前端处理中断：**

```javascript
function handleInterrupt(interrupt) {
  switch (interrupt.reason) {
    case 'human_approval':
      showApprovalDialog({
        title: '需要您的审批',
        content: interrupt.payload,
        onApprove: () => resumeExecution(interrupt.id, { approved: true }),
        onReject: () => resumeExecution(interrupt.id, { approved: false })
      });
      break;
    case 'input_required':
      showInputForm({
        fields: interrupt.payload.fields,
        onSubmit: (data) => resumeExecution(interrupt.id, data)
      });
      break;
    // ... 其他中断类型
  }
}
```

**恢复执行：**

```javascript
async function resumeExecution(interruptId, payload) {
  const input = {
    threadId: currentThreadId,
    runId: generateNewRunId(),
    resume: {
      interruptId: interruptId,
      payload: payload
    },
    messages: [...],
    tools: [...],
    state: {...}
  };
  
  // 发送恢复请求
  await fetch('/agent', {
    method: 'POST',
    body: JSON.stringify(input)
  });
}
```

### ✅ 文本消息事件

文本事件携带人类或助手消息，通常逐个 token 流式传输内容。在此类别中定义了三个事件：

1) TEXT_MESSAGE_START：信号新消息的开始。包含 messageId 和 role（如 "developer"、"system"、"assistant"、"user"、"tool"）作为属性。

2) TEXT_MESSAGE_CONTENT：携带文本块（增量），因为它被生成，允许 UI 实时显示文本。

3) TEXT_MESSAGE_END：信号消息的结束。

#### TEXT_MESSAGE_CHUNK 便捷事件

`TEXT_MESSAGE_CHUNK` 是一个便捷事件，可以简化后端代码，无需手动管理消息的开始和结束。

**工作原理：**

客户端的流转换器会将 `TEXT_MESSAGE_CHUNK` 自动展开为标准的 `START → CONTENT → END` 三事件序列。这意味着：

- **后端简化**：只需发送 CHUNK 事件，无需手动管理 START/END
- **前端统一**：始终处理标准的三事件序列，无需特殊逻辑
- **流式/非流式一致**：无论是流式还是非流式场景，客户端处理逻辑完全相同

**属性说明：**

| 属性 | 类型 | 描述 |
|------|------|------|
| messageId | string (可选) | 消息的唯一标识符；消息的第一个块必须包含 |
| role | string (可选) | 发送者的角色（"developer"、"system"、"assistant"、"user"） |
| delta | string (可选) | 消息的文本内容 |

**后端（智能体端）示例：**

```python
# 非流式场景：一次发送完整内容
yield encoder.encode(TextMessageChunkEvent(
    type=EventType.TEXT_MESSAGE_CHUNK,
    messageId="msg_001",
    role="assistant",
    delta="这是一个完整的消息。"
))

# 流式场景：分块发送内容
yield encoder.encode(TextMessageChunkEvent(
    type=EventType.TEXT_MESSAGE_CHUNK,
    messageId="msg_002",
    role="assistant",
    delta="这是第一个"
))
yield encoder.encode(TextMessageChunkEvent(
    type=EventType.TEXT_MESSAGE_CHUNK,
    messageId="msg_002",
    delta="块的内容。"
))
```

**前端（UI 端）自动展开：**

```javascript
// 客户端流转换器会自动将 TEXT_MESSAGE_CHUNK 展开为标准事件
// 前端只需要处理标准的三事件序列，无需关心后端使用的是 CHUNK 还是标准事件

async function handleTextEvents(event) {
  switch(event.type) {
    case 'TEXT_MESSAGE_START':
      createMessageContainer(event.messageId, event.role);
      break;
    case 'TEXT_MESSAGE_CONTENT':
      appendToMessage(event.messageId, event.delta);
      break;
    case 'TEXT_MESSAGE_END':
      finalizeMessage(event.messageId);
      enableUserInput();
      break;
    // 不需要处理 TEXT_MESSAGE_CHUNK，它已被自动展开
  }
}
```

**自动展开示例：**

```javascript
// 后端发送的 TEXT_MESSAGE_CHUNK 事件流（非流式场景）：
[
  { type: "TEXT_MESSAGE_CHUNK", messageId: "msg_1", role: "assistant", delta: "完整消息" }
]

// 客户端自动展开后的标准事件流：
[
  { type: "TEXT_MESSAGE_START", messageId: "msg_1", role: "assistant" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "msg_1", delta: "完整消息" },
  { type: "TEXT_MESSAGE_END", messageId: "msg_1" }  // 流结束时自动插入
]

// 后端发送的 TEXT_MESSAGE_CHUNK 事件流（流式场景）：
[
  { type: "TEXT_MESSAGE_CHUNK", messageId: "msg_2", role: "assistant", delta: "Hello " },
  { type: "TEXT_MESSAGE_CHUNK", messageId: "msg_2", delta: "World!" }
]

// 客户端自动展开后的标准事件流：
[
  { type: "TEXT_MESSAGE_START", messageId: "msg_2", role: "assistant" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "msg_2", delta: "Hello " },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "msg_2", delta: "World!" },
  { type: "TEXT_MESSAGE_END", messageId: "msg_2" }  // 流结束时自动插入
]
```

**使用场景：**

1. **简化后端代码**：无需手动管理消息的开始和结束
2. **统一处理逻辑**：流式和非流式场景使用相同的事件类型
3. **减少代码量**：对于简单场景，用单个事件发送完整内容

**注意事项：**

- 消息的**第一个块必须包含 `messageId`**
- 当省略 `role` 时，默认为 `"assistant"`
- 当流切换到新的 `messageId` 时，会自动为前一个消息发送 `TEXT_MESSAGE_END`
- 流完成时，会自动为最后一个消息发送 `TEXT_MESSAGE_END`
- 客户端始终收到标准的三事件序列，处理逻辑保持一致

示例流程：

```javascript
TEXT_MESSAGE_START → (TEXT_MESSAGE_CONTENT → TEXT_MESSAGE_CONTENT ...) → TEXT_MESSAGE_END
```

每条消息都由 TEXT_MESSAGE_START 和 TEXT_MESSAGE_END 框架，中间有一个或多个 TEXT_MESSAGE_CONTENT 事件。

例如，助手回复 "Hello" 可能会被发送为（智能体端）：

```python
yield encoder.encode(TextMessageStartEvent(
    type=EventType.TEXT_MESSAGE_START,
    message_id=msg_id,
    role="assistant"
))
yield encoder.encode(TextMessageContentEvent(
    type=EventType.TEXT_MESSAGE_CONTENT,
    message_id=msg_id,
    delta="Hello"
))
yield encoder.encode(TextMessageEndEvent(
    type=EventType.TEXT_MESSAGE_END,
    message_id=msg_id
))
```

UI 可能会这样处理：

```javascript
async function handleTextEvents(event) {
  switch(event.type) {
    case 'TEXT_MESSAGE_START':
      createMessageContainer(event.message_id, event.role);
      break;
    case 'TEXT_MESSAGE_CONTENT':
      // 用于自然对话的实时文本流式传输
      appendToMessage(event.message_id, event.delta);
      break;
    case 'TEXT_MESSAGE_END':
      finalizeMessage(event.message_id);
      enableUserInput();
      break;
  }
}
```

### ✅ 工具调用事件

工具调用事件代表智能体发起的工具调用的生命周期。它们遵循与文本消息类似的流式模式。有五个事件：

1) TOOL_CALL_START：工具调用开始，包含 toolCallId 和 toolCallName
2) TOOL_CALL_ARGS：携带工具参数数据块（delta）
3) TOOL_CALL_END：标志工具调用结束
4) TOOL_CALL_RESULT：提供工具执行结果
5) TOOL_CALL_CHUNK（便捷事件）：自动展开为标准的四事件序列

**智能体端示例：**

```python
# 工具调用开始
yield encoder.encode(ToolCallStartEvent(
    type=EventType.TOOL_CALL_START,
    tool_call_id="tool_001",
    tool_call_name="get_weather"
))

# 工具参数（流式）
yield encoder.encode(ToolCallArgsEvent(
    type=EventType.TOOL_CALL_ARGS,
    tool_call_id="tool_001",
    delta='{"city": "Beijing"}'
))

# 工具调用结束
yield encoder.encode(ToolCallEndEvent(
    type=EventType.TOOL_CALL_END,
    tool_call_id="tool_001"
))

# 工具结果
yield encoder.encode(ToolCallResultEvent(
    type=EventType.TOOL_CALL_RESULT,
    message_id="msg_002",
    tool_call_id="tool_001",
    content='{"temperature": 22, "condition": "晴朗"}'
))
```

**前端处理：**

```javascript
async function handleToolCallEvents(event) {
  switch(event.type) {
    case 'TOOL_CALL_START':
      showToolCallCard(event.toolCallId, event.toolCallName);
      break;
    case 'TOOL_CALL_ARGS':
      updateToolCallArgs(event.toolCallId, event.delta);
      break;
    case 'TOOL_CALL_END':
      updateToolCallStatus(event.toolCallId, '执行中...');
      break;
    case 'TOOL_CALL_RESULT':
      displayToolCallResult(event.toolCallId, event.content);
      break;
  }
}
```

### ✅ 状态管理事件

这些事件用于管理和同步智能体的状态与前端。智能体遵循高效的快照-增量模式，而不是每次都重新发送大数据块：

1) StateSnapshot：发送当前状态的完整 JSON 快照。用于初始同步或偶尔的完整刷新。

2) StateDelta：作为 JSON Patch 差异（RFC6902）发送增量更改。减少频繁更新的数据传输。

3) MessagesSnapshot（可选）：如果需要重新同步 UI，则发送完整的对话历史。

4) ActivitySnapshot：传递活动消息的完整快照。

5) ActivityDelta：使用 JSON Patch 提供对活动的增量更新。

> **特别说明：MESSAGES_SNAPSHOT**
> 
> MESSAGES_SNAPSHOT 是一个可选但重要的事件类型，用于在以下场景中同步完整的对话历史：
> 
> - **新用户连接**：当新用户加入会话时，可以发送完整的历史记录
> - **页面刷新/重载**：用户刷新页面后，需要恢复之前的对话上下文
> - **离线同步**：在设备重新连接后同步对话历史
> - **跨设备同步**：在不同设备间同步对话状态

示例流程：

```javascript
StateSnapshot → (StateDelta → StateDelta …) → StateSnapshot → (StateDelta ...)
```

智能体从 StateSnapshot 开始初始化前端，然后随着更改的发生流式传输增量 StateDelta 事件。如果需要，偶尔发送 StateSnapshot 事件以重新同步。

以下是在智能体端的样子（发出事件）：

```python
# 发送完整状态快照（初始或大型更新）
yield encoder.encode(StateSnapshotEvent(
    type=EventType.STATE_SNAPSHOT,
    snapshot={
        "score": 0,
        "tasks_completed": 0,
        "current_step": "fetching_data"
    }
))

# 稍后，仅发送更改（JSON Patch 格式）
yield encoder.encode(StateDeltaEvent(
    type=EventType.STATE_DELTA,
    delta=[
        {"op": "replace", "path": "/score", "value": 42},
        {"op": "replace", "path": "/current_step", "value": "analyzing_data"}
    ]
))

# 可选：同步整个对话历史
yield encoder.encode(MessagesSnapshotEvent(
    type=EventType.MESSAGES_SNAPSHOT,
    messages=[...]
))
```

以下是如何在前端端处理这些事件：

```javascript
async function handleStateEvents(event) {
  switch(event.type) {
    case 'STATE_SNAPSHOT':
      setAppState(event.snapshot);  
      restoreUIFromState(event.snapshot); // 从快照恢复 UI
      break;
    case 'STATE_DELTA':
      applyStateDelta(event.delta);  // 应用增量实时更新
      // 示例：event.delta = [{"op": "replace", "path": "/portfolio/AAPL", "value": 1250}]
      break;
    case 'MESSAGES_SNAPSHOT':
      setMessageHistory(event.messages);  // 如果需要，替换对话历史
      break;
  }
}
```

假设智能体正在更新 UI 表格或购物车：它可以通过状态增量添加或修改条目，而不是重新发送整个表格。

通过使用状态事件，UI 可以合并小更新而无需从头开始。

### ✅ 特殊事件

特殊事件是 AG-UI 中的"全能"事件。它们用于交互不适合通常类别的情况。这些事件不遵循其他事件类型的标准生命周期或流式模式。

简单来说：如果您需要智能体和前端做一些标准事件未涵盖的独特或自定义的事情，您可以使用特殊事件。

**1) RawEvent：**

- 用于传递来自外部系统的事件。
- 充当来自 AG-UI 外部的事件的容器，保留原始数据。
- 可选的 source 属性可以识别外部系统。
- 前端可以直接处理这些事件或将它们委托给系统特定的处理程序。
- 属性：event 包含原始事件数据 & source（可选）识别外部系统。

**2) CustomEvent：**

- 用于标准类型未涵盖的应用程序特定事件。
- 显式是协议的一部分（与 Raw 不同）但完全由应用程序定义。
- 无需更改规范即可启用协议扩展。
- 属性：name 识别自定义事件 & value 包含关联的数据。

假设您想实现一个多智能体工作流，其中控制从一个智能体传递到另一个智能体，您可以定义一个自定义事件，如：

```json
{
  "type": "Custom",
  "name": "AGENT_HANDOFF",
  "value": {
    "from_agent": "Planner",
    "to_agent": "Executor"
  }
}
```

AG-UI 本身不知道"handoff"是什么，这取决于您的应用程序代码来执行它。所以 Custom 启用了这种模式，但它完全是应用程序定义的。

示例流程：

```
RawEvent → CustomEvent → RawEvent → CustomEvent …
```

这些事件没有固定的顺序：它们根据外部触发器和应用程序特定逻辑在事件流中按需出现。

以下是智能体端的简单示例：

```python
# 来自外部监控系统的原始事件
yield encoder.encode(RawEvent(
    type=EventType.RAW,
    event={"alert": "high_cpu", "value": 92},
    source="monitoring_system"
))

# 在智能体之间传递控制的自定义事件（我们之前讨论过）
yield encoder.encode(CustomEvent(
    type=EventType.CUSTOM,
    name="AGENT_HANDOFF",
    value={"from_agent": "Planner", "to_agent": "Executor"}
))
```

以下是在前端端如何处理它：

```javascript
async function handleSpecialEvents(event) {
  switch(event.type) {
    case 'RAW':
      forwardToExternalSystem(event.event, event.source);
      console.log("External system event:", event.source, event.event);
      break;
    case 'CUSTOM':
      if(event.name === "AGENT_HANDOFF") {
        switchActiveAgent(event.value.from_agent, event.value.to_agent);
      }
      break;
  }
}
```

简而言之，当您需要在核心 AG-UI 模式之外获得"额外的东西"时，特殊事件提供了一个扩展点。

### ✅ 草稿事件

还有更多事件目前处于草稿状态，可能在最终确定之前发生变化。以下是一些类型：

- **Activity Events**：将表示智能体在消息之间的进度，让 UI 按顺序显示细粒度的更新。
- **Reasoning Events**：将支持 LLM 推理可见性和连续性，启用思维链推理。
- **Meta Events**：将提供注释或独立于智能体运行的信号，如用户反馈或外部事件。

在官方文档中查看完整列表。

在下一节中，您将找到一个实时交互流程，以了解所有事件如何在实践中协同工作。

## 实时交互流程（显示事件流）

这是一个结合多种事件类型的实时示例，说明智能体交互的完整生命周期。您可以在以下位置尝试。

https://www.copilotkit.ai/blog/introducing-the-ag-ui-dojo

以下是 LangGraph AG-UI 演示中显示股票分析智能体的完整事件序列：

```javascript
RUN_STARTED → 智能体开始处理用户投资查询
STATE_SNAPSHOT → 使用可用现金初始化投资组合状态
TEXT_MESSAGE_START → 开始问候消息
TEXT_MESSAGE_CONTENT → 流式传输"正在分析您的投资请求..."
TEXT_MESSAGE_END → 完成问候消息
TOOL_CALL_START → 开始股票数据提取
TOOL_CALL_ARGS → 显示参数：{"tickers": ["AAPL"], "amount": [10000]}
TOOL_CALL_END → 股票数据获取完成
STATE_DELTA → 更新工具日志："收集股票数据" → "已完成"
TOOL_CALL_START → 开始现金分配计算
STATE_DELTA → 实时更新投资组合持仓
TEXT_MESSAGE_START → 开始分析响应
TEXT_MESSAGE_CONTENT → 流式传输投资分析结果
TEXT_MESSAGE_END → 完成分析消息
TOOL_CALL_START → 生成看涨/看跌见解
TOOL_CALL_RESULT → 显示投资见解
RUN_FINISHED → 智能体任务完成
```

一旦您掌握了 AG-UI 事件，您就会意识到交互式智能体变得更加简单和可预测。

这是那些悄悄地为任何构建严肃智能体应用程序的人解决大问题的规范之一。

## Human-in-the-Loop 完整示例【新增章节】

以下是一个包含人工审批流程的完整事件序列：

```javascript
// === 第一阶段：执行到敏感操作 ===
RUN_STARTED → 智能体开始处理发送邮件请求
TEXT_MESSAGE_START → 开始消息
TEXT_MESSAGE_CONTENT → "好的，我来帮您发送邮件..."
TEXT_MESSAGE_END → 消息完成
TEXT_MESSAGE_START → 开始消息
TEXT_MESSAGE_CONTENT → "邮件已准备好，需要您确认后才能发送。"
TEXT_MESSAGE_END → 消息完成
RUN_FINISHED (outcome: "interrupt") → 智能体暂停，等待审批
  └─ interrupt: {
       id: "approval_001",
       reason: "human_approval",
       payload: { action: "send_email", to: "...", subject: "..." }
     }

// === 用户审批 ===
[前端显示审批对话框]
[用户点击"批准"]
[前端发送恢复请求: resume: { interruptId: "approval_001", payload: { approved: true } }]

// === 第二阶段：恢复执行 ===
RUN_STARTED (parentRunId: "run_001") → 从中断恢复
TOOL_CALL_START → 开始发送邮件
TOOL_CALL_ARGS → {"to": "...", "subject": "..."}
TOOL_CALL_END → 工具调用结束
TOOL_CALL_RESULT → {"success": true, "messageId": "..."}
TEXT_MESSAGE_START → 开始消息
TEXT_MESSAGE_CONTENT → "✅ 邮件已成功发送"
TEXT_MESSAGE_END → 消息完成
RUN_FINISHED (outcome: "success") → 智能体任务完成
```

---

## 总结

AG-UI（智能体-用户交互协议）通过定义 19 种核心事件类型（包括便捷事件），为智能体与前端之间的通信提供了一套标准化、可扩展的解决方案。本文深入介绍了以下内容：

### 核心概念

- **事件驱动架构**：所有通信都通过类型化事件进行，确保前后端解耦
- **实时同步**：UI 能够实时反映智能体的操作进度和状态变化
- **框架无关**：任何兼容 AG-UI 的智能体都可以插入任何支持 AG-UI 的前端
- **类型安全**：使用 TypeScript 和 Zod 提供完整的类型推断和运行时验证
- **血缘追踪**：支持 parentRunId 进行分支/时间旅行功能
- **Human-in-the-Loop**：【新增】原生支持人工审批、信息补充、确认等人机协作场景

### 五大事件类别

| 类别 | 事件数量 | 主要用途 |
|------|---------|---------|
| 生命周期事件 | 5 种 | 跟踪智能体运行进度（开始、完成、错误、子步骤、**中断**） |
| 文本消息事件 | 4 种 | 流式传输聊天或文本内容（含便捷事件） |
| 工具调用事件 | 5 种 | 报告工具调用及其结果（含便捷事件） |
| 状态管理事件 | 5 种 | 同步共享应用程序状态和活动 |
| 特殊事件 | 2 种 | 处理自定义或外部系统事件 |

### 事件类型详解

#### 1. 生命周期事件（5 种）
- **RUN_STARTED**：标志 Agent 运行的开始，包含 threadId、runId、parentRunId 和 input
- **RUN_FINISHED**：标志运行的成功完成**或中断暂停**，包含 result、**outcome、interrupt**【已升级】
- **RUN_ERROR**：标志运行过程中的错误，包含 message 和 code
- **STEP_STARTED**：标志子任务的开始，包含 stepName
- **STEP_FINISHED**：标志子任务的完成，包含 stepName

#### 2. 文本消息事件（4 种）
- **TEXT_MESSAGE_START**：标志文本消息的开始，包含 messageId 和 role
- **TEXT_MESSAGE_CONTENT**：携带文本内容块（delta），非空字符串
- **TEXT_MESSAGE_END**：标志文本消息的结束
- **TEXT_MESSAGE_CHUNK**（便捷事件）：自动展开为标准的三事件序列

#### 3. 工具调用事件（5 种）
- **TOOL_CALL_START**：标志工具调用的开始，包含 toolCallId、toolCallName
- **TOOL_CALL_ARGS**：携带工具参数数据块（delta）
- **TOOL_CALL_END**：标志工具调用的结束
- **TOOL_CALL_RESULT**：提供工具执行结果，包含 messageId、toolCallId、content
- **TOOL_CALL_CHUNK**（便捷事件）：自动展开为标准的四事件序列

#### 4. 状态管理事件（5 种）
- **STATE_SNAPSHOT**：提供完整的状态快照
- **STATE_DELTA**：使用 JSON Patch 提供增量更新
- **MESSAGES_SNAPSHOT**：提供完整的对话历史快照
- **ACTIVITY_SNAPSHOT**：传递活动消息的完整快照
- **ACTIVITY_DELTA**：使用 JSON Patch 提供对活动的增量更新

#### 5. 特殊事件（2 种）
- **RAW**：用于从外部系统传递事件，包含 event 和 source
- **CUSTOM**：用于应用程序特定的自定义事件，包含 name 和 value

### 核心类型

#### RunAgentInput - 运行 Agent 输入【已升级】
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

#### ResumePayload - 恢复执行负载【新增】
```typescript
type ResumePayload = {
  interruptId?: string    // 对应 interrupt.id
  payload?: any           // 用户的响应数据
}
```

#### InterruptDetails - 中断详情【新增】
```typescript
type InterruptDetails = {
  id?: string             // 中断标识符
  reason?: string         // 中断原因
  payload?: any           // 传给前端的数据
}
```

#### Role - 角色类型
```typescript
type Role = "developer" | "system" | "assistant" | "user" | "tool" | "activity"
```

#### 消息类型
- **DeveloperMessage**：来自开发者的消息
- **SystemMessage**：系统消息
- **AssistantMessage**：助手消息，可选包含 toolCalls
- **UserMessage**：用户消息，支持纯文本或多模态内容
- **ToolMessage**：工具消息，包含 toolCallId 和可选的 error

### 关键优势

1. **标准化**：统一的协议减少了集成复杂度
2. **可扩展**：支持自定义事件和草稿事件
3. **高效**：使用快照-增量模式减少数据传输
4. **实时性**：支持流式传输和实时更新
5. **解耦**：前后端可以独立开发和演进
6. **类型安全**：完整的 TypeScript 类型支持和 Zod 运行时验证
7. **便捷性**：提供 Chunk 事件自动展开为标准事件序列
8. **血缘追踪**：支持 parentRunId 进行分支和时间旅行功能
9. **Human-in-the-Loop**：【新增】原生支持中断-恢复模式，实现人机协作工作流

### 实践建议

- 使用 `EventEncoder` 对事件进行编码
- 生命周期事件是必需的，用于监控整体运行状态
- 文本消息事件支持流式和非流式两种模式
- 工具调用事件可以显示参数和结果的实时进度
- 状态管理事件优先使用 `StateDelta` 而非完整快照
- 特殊事件用于处理协议未覆盖的自定义场景
- 活动事件（ACTIVITY_SNAPSHOT/ACTIVITY_DELTA）用于显示智能体在消息之间的进度
- MESSAGES_SNAPSHOT 用于新用户连接、页面刷新、离线同步和跨设备同步等场景
- **Human-in-the-Loop**：【新增】敏感操作使用 interrupt 机制请求人工审批

### 未来展望

AG-UI 协议仍在不断发展中，Human-in-the-Loop 机制的正式引入进一步完善了智能体的交互能力，让智能体能够在关键决策点暂停并请求人工介入。对于任何希望构建严肃智能体应用程序的开发者来说，AG-UI 都是一个值得深入学习和应用的规范。
