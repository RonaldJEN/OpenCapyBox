# AG-UI 事件完整样例文档

本文档提供了 AG-UI 协议中所有事件类型的完整示例。

---

## 目录

1. [生命周期事件](#生命周期事件)
2. [文本消息事件](#文本消息事件)
3. [工具调用事件](#工具调用事件)
4. [状态管理事件](#状态管理事件)
5. [特殊事件](#特殊事件)
6. [完整场景示例](#完整场景示例)
7. [Human-in-the-Loop 场景示例](#human-in-the-loop-场景示例)【新增】

---

## 生命周期事件

### 1. RUN_STARTED - 运行开始事件

```json
{
  "type": "RUN_STARTED",
  "threadId": "thread_abc123",
  "runId": "run_xyz789",
  "parentRunId": "run_prev001",
  "input": {
    "threadId": "thread_abc123",
    "runId": "run_xyz789",
    "state": {},
    "messages": [],
    "tools": [],
    "context": [],
    "forwardedProps": {}
  },
  "timestamp": 1699000051000
}
```

### 2. RUN_FINISHED - 运行结束事件（正常完成）

```json
{
  "type": "RUN_FINISHED",
  "threadId": "thread_abc123",
  "runId": "run_xyz789",
  "outcome": "success",
  "result": {
    "status": "success",
    "recommendation": "buy",
    "confidence": 0.85
  },
  "timestamp": 1699000100000
}
```

### 3. RUN_FINISHED - 运行结束事件（中断等待人工）【新增】

```json
{
  "type": "RUN_FINISHED",
  "threadId": "thread_abc123",
  "runId": "run_xyz789",
  "outcome": "interrupt",
  "interrupt": {
    "id": "approval_001",
    "reason": "human_approval",
    "payload": {
      "action": "send_email",
      "description": "发送订单确认邮件给客户",
      "details": {
        "to": "customer@example.com",
        "subject": "订单确认 #12345",
        "body": "您的订单已确认，预计3天内送达..."
      },
      "riskLevel": "medium"
    }
  },
  "timestamp": 1699000100000
}
```

### 4. RUN_ERROR - 运行错误事件

```json
{
  "type": "RUN_ERROR",
  "message": "Failed to fetch stock data: API timeout",
  "code": "API_TIMEOUT",
  "timestamp": 1699000080000
}
```

### 5. STEP_STARTED - 步骤开始事件

```json
{
  "type": "STEP_STARTED",
  "stepName": "collecting_stock_data",
  "timestamp": 1699000060000
}
```

### 6. STEP_FINISHED - 步骤结束事件

```json
{
  "type": "STEP_FINISHED",
  "stepName": "collecting_stock_data",
  "timestamp": 1699000070000
}
```

---

## 文本消息事件

### 1. TEXT_MESSAGE_START - 文本消息开始事件

```json
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_assistant_001",
  "role": "assistant",
  "timestamp": 1699000055000
}
```

### 2. TEXT_MESSAGE_CONTENT - 文本消息内容事件

```json
{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_assistant_001",
  "delta": "根据您的投资偏好，",
  "timestamp": 1699000056000
}
```

```json
{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_assistant_001",
  "delta": "我建议关注 AAPL 股票。",
  "timestamp": 1699000057000
}
```

### 3. TEXT_MESSAGE_END - 文本消息结束事件

```json
{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_assistant_001",
  "timestamp": 1699000058000
}
```

### 4. TEXT_MESSAGE_CHUNK - 文本消息块事件（便捷事件）

```json
{
  "type": "TEXT_MESSAGE_CHUNK",
  "messageId": "msg_assistant_002",
  "role": "assistant",
  "delta": "这是一个完整的消息内容。",
  "timestamp": 1699000090000
}
```

---

## 工具调用事件

### 1. TOOL_CALL_START - 工具调用开始事件

```json
{
  "type": "TOOL_CALL_START",
  "toolCallId": "tool_weather_001",
  "toolCallName": "get_weather",
  "parentMessageId": "msg_assistant_003",
  "timestamp": 1699000030000
}
```

### 2. TOOL_CALL_ARGS - 工具调用参数事件

```json
{
  "type": "TOOL_CALL_ARGS",
  "toolCallId": "tool_weather_001",
  "delta": "{\"city\"",
  "timestamp": 1699000031000
}
```

```json
{
  "type": "TOOL_CALL_ARGS",
  "toolCallId": "tool_weather_001",
  "delta": ":\"Beijing\"}",
  "timestamp": 1699000032000
}
```

### 3. TOOL_CALL_END - 工具调用结束事件

```json
{
  "type": "TOOL_CALL_END",
  "toolCallId": "tool_weather_001",
  "timestamp": 1699000033000
}
```

### 4. TOOL_CALL_RESULT - 工具调用结果事件

```json
{
  "type": "TOOL_CALL_RESULT",
  "messageId": "msg_assistant_003",
  "toolCallId": "tool_weather_001",
  "content": "{\"city\":\"Beijing\",\"temperature\":22,\"condition\":\"晴朗\",\"humidity\":45,\"wind\":\"北风3级\"}",
  "role": "tool",
  "timestamp": 1699000040000
}
```

### 5. TOOL_CALL_CHUNK - 工具调用块事件（便捷事件）

```json
{
  "type": "TOOL_CALL_CHUNK",
  "toolCallId": "tool_search_001",
  "toolCallName": "web_search",
  "delta": "{\"query\":\"AG-UI protocol\",\"limit\":10}",
  "timestamp": 1699000110000
}
```

---

## 状态管理事件

### 1. STATE_SNAPSHOT - 状态快照事件

```json
{
  "type": "STATE_SNAPSHOT",
  "snapshot": {
    "portfolio": {
      "cash": 10000,
      "holdings": {
        "AAPL": 50,
        "GOOGL": 0
      },
      "totalValue": 10000
    },
    "analysis": {
      "status": "in_progress",
      "progress": 0
    },
    "preferences": {
      "riskTolerance": "medium",
      "investmentHorizon": "long"
    }
  },
  "timestamp": 1699000065000
}
```

### 2. STATE_DELTA - 状态增量事件

```json
{
  "type": "STATE_DELTA",
  "delta": [
    {"op": "replace", "path": "/portfolio/holdings/AAPL", "value": 100},
    {"op": "replace", "path": "/analysis/progress", "value": 50}
  ],
  "timestamp": 1699000075000
}
```

### 3. MESSAGES_SNAPSHOT - 消息快照事件

```json
{
  "type": "MESSAGES_SNAPSHOT",
  "messages": [
    {
      "id": "msg_user_001",
      "role": "user",
      "content": "帮我分析一下 AAPL 股票"
    },
    {
      "id": "msg_assistant_001",
      "role": "assistant",
      "content": "好的，我正在为您分析 AAPL 股票..."
    }
  ],
  "timestamp": 1699000085000
}
```

### 4. ACTIVITY_SNAPSHOT - 活动快照事件

```json
{
  "type": "ACTIVITY_SNAPSHOT",
  "messageId": "activity_001",
  "activityType": "PLAN",
  "content": {
    "title": "股票分析计划",
    "steps": [
      {"name": "收集数据", "status": "completed"},
      {"name": "分析趋势", "status": "in_progress"},
      {"name": "生成报告", "status": "pending"}
    ],
    "currentStep": 1
  },
  "replace": true,
  "timestamp": 1699000095000
}
```

### 5. ACTIVITY_DELTA - 活动增量事件

```json
{
  "type": "ACTIVITY_DELTA",
  "messageId": "activity_001",
  "activityType": "PLAN",
  "patch": [
    {"op": "replace", "path": "/steps/1/status", "value": "completed"},
    {"op": "replace", "path": "/steps/2/status", "value": "in_progress"},
    {"op": "replace", "path": "/currentStep", "value": 2}
  ],
  "timestamp": 1699000105000
}
```

---

## 特殊事件

### 1. RAW - 原始事件

```json
{
  "type": "RAW",
  "event": {
    "alert": "high_cpu",
    "value": 92,
    "threshold": 80
  },
  "source": "monitoring_system",
  "timestamp": 1699000115000
}
```

### 2. CUSTOM - 自定义事件

```json
{
  "type": "CUSTOM",
  "name": "AGENT_HANDOFF",
  "value": {
    "fromAgent": "Planner",
    "toAgent": "Executor",
    "context": {
      "taskId": "task_123",
      "status": "ready"
    }
  },
  "timestamp": 1699000125000
}
```

---

## 完整场景示例

### 场景：股票分析智能体

以下是一个完整的股票分析场景，展示所有事件的典型使用顺序：

```jsonc
// 1. 运行开始
{
  "type": "RUN_STARTED",
  "threadId": "thread_stock_001",
  "runId": "run_stock_001"
}

// 2. 发送初始状态快照
{
  "type": "STATE_SNAPSHOT",
  "snapshot": {
    "portfolio": {"cash": 10000, "holdings": {}},
    "analysis": {"status": "not_started"}
  }
}

// 3. 步骤开始：收集数据
{
  "type": "STEP_STARTED",
  "stepName": "collecting_stock_data"
}

// 4. 开始助手消息
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_001",
  "role": "assistant"
}

// 5. 流式传输消息内容
{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_001",
  "delta": "正在分析您的投资请求..."
}

// 6. 消息结束
{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_001"
}

// 7. 工具调用开始：获取股票数据
{
  "type": "TOOL_CALL_START",
  "toolCallId": "tool_001",
  "toolCallName": "fetch_stock_data"
}

// 8. 工具参数
{
  "type": "TOOL_CALL_ARGS",
  "toolCallId": "tool_001",
  "delta": "{\"ticker\":\"AAPL\"}"
}

// 9. 工具调用结束
{
  "type": "TOOL_CALL_END",
  "toolCallId": "tool_001"
}

// 10. 工具结果
{
  "type": "TOOL_CALL_RESULT",
  "messageId": "msg_002",
  "toolCallId": "tool_001",
  "content": "{\"price\":178.50,\"change\":+2.30,\"volume\":50000000}",
  "role": "tool"
}

// 11. 状态增量更新
{
  "type": "STATE_DELTA",
  "delta": [
    {"op": "add", "path": "/portfolio/holdings/AAPL", "value": {"price": 178.50}}
  ]
}

// 12. 步骤完成
{
  "type": "STEP_FINISHED",
  "stepName": "collecting_stock_data"
}

// 13. 步骤开始：分析数据
{
  "type": "STEP_STARTED",
  "stepName": "analyzing_data"
}

// 14. 继续消息流
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_003",
  "role": "assistant"
}

{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_003",
  "delta": "AAPL 当前价格为 $178.50，建议买入。"
}

{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_003"
}

// 15. 步骤完成
{
  "type": "STEP_FINISHED",
  "stepName": "analyzing_data"
}

// 16. 运行完成
{
  "type": "RUN_FINISHED",
  "threadId": "thread_stock_001",
  "runId": "run_stock_001",
  "outcome": "success",
  "result": {
    "recommendation": "buy",
    "confidence": 0.85
  }
}
```

---

## Human-in-the-Loop 场景示例【新增章节】

### 场景1：敏感操作审批

以下示例展示了 Agent 在执行敏感操作（发送邮件）前请求人工审批的完整流程：

```jsonc
// ===== 第一阶段：Agent 执行到敏感操作，请求审批 =====

// 1. 运行开始
{
  "type": "RUN_STARTED",
  "threadId": "thread_email_001",
  "runId": "run_001",
  "timestamp": 1699000001000
}

// 2. Agent 开始处理
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_001",
  "role": "assistant"
}

{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_001",
  "delta": "好的，我来帮您发送订单确认邮件。正在准备邮件内容..."
}

{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_001"
}

// 3. Agent 准备发送邮件，但这是敏感操作，需要审批
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_002",
  "role": "assistant"
}

{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_002",
  "delta": "邮件已准备好，需要您确认后才能发送。"
}

{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_002"
}

// 4. 运行结束，outcome 为 interrupt，等待人工审批
{
  "type": "RUN_FINISHED",
  "threadId": "thread_email_001",
  "runId": "run_001",
  "outcome": "interrupt",
  "interrupt": {
    "id": "approval_email_001",
    "reason": "human_approval",
    "payload": {
      "action": "send_email",
      "description": "发送订单确认邮件",
      "details": {
        "to": "customer@example.com",
        "cc": ["sales@company.com"],
        "subject": "【订单确认】您的订单 #ORD-2024-12345 已确认",
        "body": "尊敬的客户：\n\n感谢您的订购！您的订单已确认，详情如下：\n- 订单号：#ORD-2024-12345\n- 商品：iPhone 15 Pro x 1\n- 金额：¥8,999.00\n- 预计送达：3个工作日内\n\n如有疑问，请联系客服。\n\n祝您购物愉快！"
      },
      "riskLevel": "medium",
      "requiresApproval": true
    }
  },
  "timestamp": 1699000010000
}

// ===== 第二阶段：用户审批通过，恢复执行 =====

// 5. 用户发送恢复请求（这是 RunAgentInput，不是事件）
// POST /agent
// {
//   "threadId": "thread_email_001",
//   "runId": "run_002",
//   "resume": {
//     "interruptId": "approval_email_001",
//     "payload": {
//       "approved": true,
//       "comment": "确认发送，内容无误"
//     }
//   },
//   "messages": [...],
//   "tools": [...],
//   "state": {...}
// }

// 6. 新的运行开始，关联到之前被中断的运行
{
  "type": "RUN_STARTED",
  "threadId": "thread_email_001",
  "runId": "run_002",
  "parentRunId": "run_001",
  "timestamp": 1699000020000
}

// 7. Agent 继续执行，发送邮件
{
  "type": "TOOL_CALL_START",
  "toolCallId": "tool_send_email_001",
  "toolCallName": "send_email"
}

{
  "type": "TOOL_CALL_ARGS",
  "toolCallId": "tool_send_email_001",
  "delta": "{\"to\":\"customer@example.com\",\"subject\":\"【订单确认】您的订单 #ORD-2024-12345 已确认\",\"body\":\"...\"}"
}

{
  "type": "TOOL_CALL_END",
  "toolCallId": "tool_send_email_001"
}

{
  "type": "TOOL_CALL_RESULT",
  "messageId": "msg_003",
  "toolCallId": "tool_send_email_001",
  "content": "{\"success\":true,\"messageId\":\"email_abc123\",\"sentAt\":\"2024-01-15T10:30:00Z\"}",
  "role": "tool"
}

// 8. Agent 确认完成
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_004",
  "role": "assistant"
}

{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_004",
  "delta": "✅ 邮件已成功发送至 customer@example.com"
}

{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_004"
}

// 9. 运行成功完成
{
  "type": "RUN_FINISHED",
  "threadId": "thread_email_001",
  "runId": "run_002",
  "outcome": "success",
  "result": {
    "emailSent": true,
    "messageId": "email_abc123"
  },
  "timestamp": 1699000030000
}
```

---

### 场景2：需要补充信息

以下示例展示了 Agent 需要用户提供额外信息才能继续的流程：

```jsonc
// ===== Agent 需要更多信息 =====

// 1. 运行开始
{
  "type": "RUN_STARTED",
  "threadId": "thread_booking_001",
  "runId": "run_001",
  "timestamp": 1699000001000
}

// 2. Agent 分析请求
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_001",
  "role": "assistant"
}

{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_001",
  "delta": "好的，我来帮您预订酒店。但是我需要一些额外信息才能继续。"
}

{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_001"
}

// 3. 中断请求补充信息
{
  "type": "RUN_FINISHED",
  "threadId": "thread_booking_001",
  "runId": "run_001",
  "outcome": "interrupt",
  "interrupt": {
    "id": "input_booking_001",
    "reason": "input_required",
    "payload": {
      "message": "请提供以下预订信息：",
      "fields": [
        {
          "name": "checkInDate",
          "type": "date",
          "label": "入住日期",
          "required": true
        },
        {
          "name": "checkOutDate",
          "type": "date",
          "label": "退房日期",
          "required": true
        },
        {
          "name": "roomType",
          "type": "select",
          "label": "房型",
          "required": true,
          "options": ["标准间", "大床房", "豪华套房"]
        },
        {
          "name": "guestCount",
          "type": "number",
          "label": "入住人数",
          "required": true,
          "min": 1,
          "max": 4
        },
        {
          "name": "specialRequests",
          "type": "text",
          "label": "特殊要求",
          "required": false,
          "placeholder": "如需要无烟房、高层等"
        }
      ]
    }
  },
  "timestamp": 1699000010000
}

// ===== 用户填写信息后恢复 =====

// 4. 用户发送恢复请求
// POST /agent
// {
//   "threadId": "thread_booking_001",
//   "runId": "run_002",
//   "resume": {
//     "interruptId": "input_booking_001",
//     "payload": {
//       "checkInDate": "2024-02-01",
//       "checkOutDate": "2024-02-03",
//       "roomType": "大床房",
//       "guestCount": 2,
//       "specialRequests": "希望安排高层、安静的房间"
//     }
//   },
//   ...
// }

// 5. 继续执行
{
  "type": "RUN_STARTED",
  "threadId": "thread_booking_001",
  "runId": "run_002",
  "parentRunId": "run_001",
  "timestamp": 1699000020000
}

// 6. Agent 使用用户提供的信息继续处理...
```

---

### 场景3：高风险操作确认

以下示例展示了数据库删除等高风险操作的确认流程：

```jsonc
// ===== 高风险操作确认 =====

{
  "type": "RUN_FINISHED",
  "threadId": "thread_db_001",
  "runId": "run_001",
  "outcome": "interrupt",
  "interrupt": {
    "id": "confirm_delete_001",
    "reason": "human_approval",
    "payload": {
      "action": "database_delete",
      "description": "删除过期用户数据",
      "riskLevel": "high",
      "impact": {
        "affectedRows": 1523,
        "tables": ["users", "user_sessions", "user_preferences"],
        "query": "DELETE FROM users WHERE last_login < '2023-01-01'"
      },
      "warnings": [
        "此操作不可撤销",
        "将删除 1523 条用户记录及关联数据",
        "建议先执行备份"
      ],
      "rollbackPlan": "可从备份 backup_2024_01_15 恢复",
      "requiresConfirmation": true,
      "confirmationText": "我确认要删除这些数据，已了解风险"
    }
  },
  "timestamp": 1699000010000
}

// 用户确认后恢复
// resume.payload: { confirmed: true, confirmationText: "我确认要删除这些数据，已了解风险" }
```

---

### 场景4：审批被拒绝

```jsonc
// ===== 用户拒绝审批 =====

// 用户发送拒绝
// POST /agent
// {
//   "threadId": "thread_email_001",
//   "runId": "run_002",
//   "resume": {
//     "interruptId": "approval_email_001",
//     "payload": {
//       "approved": false,
//       "reason": "邮件内容需要修改，金额显示错误"
//     }
//   },
//   ...
// }

// Agent 处理拒绝情况
{
  "type": "RUN_STARTED",
  "threadId": "thread_email_001",
  "runId": "run_002",
  "parentRunId": "run_001",
  "timestamp": 1699000020000
}

{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_003",
  "role": "assistant"
}

{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_003",
  "delta": "好的，邮件暂不发送。您提到金额显示错误，请问正确的金额是多少？我来帮您修改。"
}

{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_003"
}

{
  "type": "RUN_FINISHED",
  "threadId": "thread_email_001",
  "runId": "run_002",
  "outcome": "success",
  "result": {
    "emailSent": false,
    "reason": "用户拒绝：邮件内容需要修改，金额显示错误"
  },
  "timestamp": 1699000030000
}
```

---

## 前端事件处理器示例【新增】

以下是一个完整的前端事件处理器实现，包含 Human-in-the-Loop 支持：

```javascript
// 应用状态
const state = {
  threadId: null,
  runId: null,
  messages: new Map(),
  toolCalls: new Map(),
  activeMessageId: null,
  activeToolCallId: null,
  pendingInterrupt: null,  // 新增：待处理的中断
};

class AgentEventHandler {
  // 主事件处理入口
  async handleEvent(event) {
    console.log(`📨 收到事件: ${event.type}`, event);
    
    switch (event.type) {
      // 生命周期事件
      case 'RUN_STARTED':
        this.handleRunStarted(event);
        break;
      case 'RUN_FINISHED':
        this.handleRunFinished(event);
        break;
      case 'RUN_ERROR':
        this.handleRunError(event);
        break;
      case 'STEP_STARTED':
        this.handleStepStarted(event);
        break;
      case 'STEP_FINISHED':
        this.handleStepFinished(event);
        break;
        
      // 文本消息事件
      case 'TEXT_MESSAGE_START':
        this.handleTextMessageStart(event);
        break;
      case 'TEXT_MESSAGE_CONTENT':
        this.handleTextMessageContent(event);
        break;
      case 'TEXT_MESSAGE_END':
        this.handleTextMessageEnd(event);
        break;
        
      // 工具调用事件
      case 'TOOL_CALL_START':
        this.handleToolCallStart(event);
        break;
      case 'TOOL_CALL_ARGS':
        this.handleToolCallArgs(event);
        break;
      case 'TOOL_CALL_END':
        this.handleToolCallEnd(event);
        break;
      case 'TOOL_CALL_RESULT':
        this.handleToolCallResult(event);
        break;
        
      // 状态管理事件
      case 'STATE_SNAPSHOT':
        this.handleStateSnapshot(event);
        break;
      case 'STATE_DELTA':
        this.handleStateDelta(event);
        break;
        
      default:
        console.warn('未知事件类型:', event.type);
    }
  }

  // ============ 生命周期事件处理 ============
  
  handleRunStarted(event) {
    state.threadId = event.threadId;
    state.runId = event.runId;
    state.pendingInterrupt = null;
    
    console.log('🚀 运行开始:', event.runId);
    if (event.parentRunId) {
      console.log('   ↳ 从中断恢复，父运行:', event.parentRunId);
    }
    
    this.showProgressBar();
    this.clearPendingApproval();
  }

  handleRunFinished(event) {
    console.log('✅ 运行结束:', event.runId);
    
    // 【关键】检查是否是中断
    if (event.outcome === 'interrupt' || event.interrupt) {
      this.handleInterrupt(event);
    } else {
      // 正常完成
      if (event.result) {
        console.log('   结果:', event.result);
        this.displayFinalResult(event.result);
      }
      this.hideProgressBar();
    }
  }

  // 【新增】处理中断
  handleInterrupt(event) {
    const { interrupt } = event;
    state.pendingInterrupt = interrupt;
    
    console.log('⏸️ 运行中断，等待人工介入');
    console.log('   原因:', interrupt.reason);
    console.log('   ID:', interrupt.id);
    
    switch (interrupt.reason) {
      case 'human_approval':
        this.showApprovalDialog(interrupt);
        break;
        
      case 'input_required':
        this.showInputForm(interrupt);
        break;
        
      case 'confirmation':
        this.showConfirmationDialog(interrupt);
        break;
        
      default:
        this.showGenericInterruptDialog(interrupt);
    }
  }

  handleRunError(event) {
    console.error('❌ 运行错误:', event.message);
    this.showError(event.message, event.code);
    this.hideProgressBar();
  }

  handleStepStarted(event) {
    console.log(`📍 步骤开始: ${event.stepName}`);
    this.updateStepIndicator(event.stepName, 'running');
  }

  handleStepFinished(event) {
    console.log(`✓ 步骤完成: ${event.stepName}`);
    this.updateStepIndicator(event.stepName, 'completed');
  }

  // ============ 文本消息事件处理 ============
  
  handleTextMessageStart(event) {
    state.activeMessageId = event.messageId;
    state.messages.set(event.messageId, {
      role: event.role,
      content: '',
      isComplete: false
    });
    
    console.log(`💬 消息开始 [${event.role}]:`, event.messageId);
    this.createMessageBubble(event.messageId, event.role);
  }

  handleTextMessageContent(event) {
    const message = state.messages.get(event.messageId);
    if (message) {
      message.content += event.delta;
      this.appendMessageContent(event.messageId, event.delta);
    }
  }

  handleTextMessageEnd(event) {
    const message = state.messages.get(event.messageId);
    if (message) {
      message.isComplete = true;
      console.log('✓ 消息完成:', event.messageId);
      this.finalizeMessage(event.messageId);
    }
    state.activeMessageId = null;
  }

  // ============ 工具调用事件处理 ============
  
  handleToolCallStart(event) {
    state.activeToolCallId = event.toolCallId;
    state.toolCalls.set(event.toolCallId, {
      name: event.toolCallName,
      args: '',
      result: null,
      status: 'calling',
      startTime: Date.now()
    });
    
    console.log('🔧 工具调用开始:', event.toolCallName);
    this.showToolCallCard(event.toolCallId, event.toolCallName);
  }

  handleToolCallArgs(event) {
    const toolCall = state.toolCalls.get(event.toolCallId);
    if (toolCall) {
      toolCall.args += event.delta;
      this.updateToolCallArgs(event.toolCallId, toolCall.args);
    }
  }

  handleToolCallEnd(event) {
    const toolCall = state.toolCalls.get(event.toolCallId);
    if (toolCall) {
      toolCall.status = 'executing';
      this.updateToolCallStatus(event.toolCallId, '执行中...');
    }
  }

  handleToolCallResult(event) {
    const toolCall = state.toolCalls.get(event.toolCallId);
    if (toolCall) {
      toolCall.status = 'completed';
      toolCall.result = JSON.parse(event.content);
      toolCall.endTime = Date.now();
      
      const duration = toolCall.endTime - toolCall.startTime;
      console.log('✓ 工具结果:', toolCall.result, `耗时: ${duration}ms`);
      this.displayToolCallResult(event.toolCallId, toolCall.result, duration);
    }
    state.activeToolCallId = null;
  }

  // ============ 状态管理事件处理 ============
  
  handleStateSnapshot(event) {
    console.log('📸 状态快照:', event.snapshot);
    this.updateAppState(event.snapshot);
  }

  handleStateDelta(event) {
    console.log('📝 状态增量:', event.delta);
    this.applyStatePatch(event.delta);
  }

  // ============ Human-in-the-Loop UI 方法【新增】 ============
  
  showApprovalDialog(interrupt) {
    const { payload } = interrupt;
    
    // 创建审批对话框
    const dialog = document.createElement('div');
    dialog.className = 'approval-dialog';
    dialog.innerHTML = `
      <div class="approval-overlay"></div>
      <div class="approval-content">
        <h3>🔐 需要您的审批</h3>
        <div class="approval-action">
          <strong>操作：</strong>${payload.description || payload.action}
        </div>
        ${payload.riskLevel ? `
          <div class="approval-risk risk-${payload.riskLevel}">
            风险等级：${payload.riskLevel}
          </div>
        ` : ''}
        <div class="approval-details">
          <strong>详情：</strong>
          <pre>${JSON.stringify(payload.details, null, 2)}</pre>
        </div>
        ${payload.warnings ? `
          <div class="approval-warnings">
            <strong>⚠️ 注意：</strong>
            <ul>
              ${payload.warnings.map(w => `<li>${w}</li>`).join('')}
            </ul>
          </div>
        ` : ''}
        <div class="approval-comment">
          <label>审批意见（可选）：</label>
          <textarea id="approval-comment" placeholder="请输入审批意见..."></textarea>
        </div>
        <div class="approval-buttons">
          <button class="btn-reject" onclick="handler.rejectApproval()">
            ❌ 拒绝
          </button>
          <button class="btn-approve" onclick="handler.approveAction()">
            ✅ 批准
          </button>
        </div>
      </div>
    `;
    
    document.body.appendChild(dialog);
  }

  showInputForm(interrupt) {
    const { payload } = interrupt;
    
    const formFields = payload.fields.map(field => {
      let input = '';
      switch (field.type) {
        case 'text':
          input = `<input type="text" name="${field.name}" 
                    placeholder="${field.placeholder || ''}"
                    ${field.required ? 'required' : ''}>`;
          break;
        case 'number':
          input = `<input type="number" name="${field.name}"
                    min="${field.min || ''}" max="${field.max || ''}"
                    ${field.required ? 'required' : ''}>`;
          break;
        case 'date':
          input = `<input type="date" name="${field.name}"
                    ${field.required ? 'required' : ''}>`;
          break;
        case 'select':
          input = `<select name="${field.name}" ${field.required ? 'required' : ''}>
                    <option value="">请选择...</option>
                    ${field.options.map(opt => `<option value="${opt}">${opt}</option>`).join('')}
                  </select>`;
          break;
        default:
          input = `<input type="text" name="${field.name}">`;
      }
      
      return `
        <div class="form-field">
          <label>${field.label}${field.required ? ' *' : ''}</label>
          ${input}
        </div>
      `;
    }).join('');
    
    const dialog = document.createElement('div');
    dialog.className = 'input-form-dialog';
    dialog.innerHTML = `
      <div class="dialog-overlay"></div>
      <div class="dialog-content">
        <h3>📝 请提供以下信息</h3>
        <p>${payload.message || ''}</p>
        <form id="interrupt-form">
          ${formFields}
          <div class="form-buttons">
            <button type="button" class="btn-cancel" onclick="handler.cancelInput()">
              取消
            </button>
            <button type="submit" class="btn-submit">
              提交
            </button>
          </div>
        </form>
      </div>
    `;
    
    document.body.appendChild(dialog);
    
    // 绑定表单提交
    document.getElementById('interrupt-form').onsubmit = (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      const data = Object.fromEntries(formData.entries());
      this.submitInputForm(data);
    };
  }

  showConfirmationDialog(interrupt) {
    const { payload } = interrupt;
    
    const dialog = document.createElement('div');
    dialog.className = 'confirmation-dialog';
    dialog.innerHTML = `
      <div class="dialog-overlay"></div>
      <div class="dialog-content">
        <h3>${payload.title || '确认操作'}</h3>
        <p>${payload.message}</p>
        <div class="dialog-buttons">
          <button class="btn-cancel" onclick="handler.cancelConfirmation()">
            ${payload.cancelText || '取消'}
          </button>
          <button class="btn-confirm" onclick="handler.confirmAction()">
            ${payload.confirmText || '确认'}
          </button>
        </div>
      </div>
    `;
    
    document.body.appendChild(dialog);
  }

  // ============ 恢复执行方法【新增】 ============
  
  async approveAction() {
    const comment = document.getElementById('approval-comment')?.value || '';
    await this.resumeExecution({
      approved: true,
      comment: comment
    });
    this.closeAllDialogs();
  }

  async rejectApproval() {
    const comment = document.getElementById('approval-comment')?.value || '';
    await this.resumeExecution({
      approved: false,
      reason: comment || '用户拒绝'
    });
    this.closeAllDialogs();
  }

  async submitInputForm(data) {
    await this.resumeExecution(data);
    this.closeAllDialogs();
  }

  async confirmAction() {
    await this.resumeExecution({ confirmed: true });
    this.closeAllDialogs();
  }

  async cancelConfirmation() {
    await this.resumeExecution({ confirmed: false });
    this.closeAllDialogs();
  }

  async cancelInput() {
    await this.resumeExecution({ cancelled: true });
    this.closeAllDialogs();
  }

  // 核心：发送恢复请求
  async resumeExecution(payload) {
    if (!state.pendingInterrupt) {
      console.error('没有待处理的中断');
      return;
    }

    const input = {
      threadId: state.threadId,
      runId: this.generateRunId(),
      resume: {
        interruptId: state.pendingInterrupt.id,
        payload: payload
      },
      messages: this.getMessageHistory(),
      tools: this.getAvailableTools(),
      state: this.getCurrentState(),
      context: [],
      forwardedProps: {}
    };

    console.log('📤 发送恢复请求:', input);

    try {
      const response = await fetch('/agent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input)
      });

      // 处理 SSE 流
      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const text = decoder.decode(value);
        const events = this.parseSSE(text);
        
        for (const event of events) {
          await this.handleEvent(event);
        }
      }
    } catch (error) {
      console.error('恢复执行失败:', error);
      this.showError('恢复执行失败: ' + error.message);
    }
  }

  // ============ 辅助方法 ============
  
  generateRunId() {
    return 'run_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
  }

  closeAllDialogs() {
    document.querySelectorAll('.approval-dialog, .input-form-dialog, .confirmation-dialog')
      .forEach(el => el.remove());
  }

  clearPendingApproval() {
    state.pendingInterrupt = null;
    this.closeAllDialogs();
  }

  parseSSE(text) {
    const events = [];
    const lines = text.split('\n');
    let currentData = '';
    
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        currentData += line.slice(6);
      } else if (line === '' && currentData) {
        try {
          events.push(JSON.parse(currentData));
        } catch (e) {
          console.error('解析 SSE 数据失败:', e);
        }
        currentData = '';
      }
    }
    
    return events;
  }

  // ... 其他 UI 渲染方法 ...
  showProgressBar() { /* 实现 */ }
  hideProgressBar() { /* 实现 */ }
  createMessageBubble(messageId, role) { /* 实现 */ }
  appendMessageContent(messageId, delta) { /* 实现 */ }
  finalizeMessage(messageId) { /* 实现 */ }
  showToolCallCard(toolCallId, toolName) { /* 实现 */ }
  updateToolCallArgs(toolCallId, args) { /* 实现 */ }
  updateToolCallStatus(toolCallId, status) { /* 实现 */ }
  displayToolCallResult(toolCallId, result, duration) { /* 实现 */ }
  updateStepIndicator(stepName, status) { /* 实现 */ }
  showError(message, code) { /* 实现 */ }
  displayFinalResult(result) { /* 实现 */ }
  updateAppState(snapshot) { /* 实现 */ }
  applyStatePatch(delta) { /* 实现 */ }
  getMessageHistory() { return []; /* 实现 */ }
  getAvailableTools() { return []; /* 实现 */ }
  getCurrentState() { return {}; /* 实现 */ }
}

// 使用示例
const handler = new AgentEventHandler();
```

---

## 事件类型汇总

| 事件类型 | 类别 | 描述 |
|---------|------|------|
| RUN_STARTED | 生命周期 | 运行开始 |
| RUN_FINISHED | 生命周期 | 运行完成（包含 interrupt 支持）【已升级】 |
| RUN_ERROR | 生命周期 | 运行错误 |
| STEP_STARTED | 生命周期 | 步骤开始 |
| STEP_FINISHED | 生命周期 | 步骤完成 |
| TEXT_MESSAGE_START | 文本消息 | 消息开始 |
| TEXT_MESSAGE_CONTENT | 文本消息 | 消息内容 |
| TEXT_MESSAGE_END | 文本消息 | 消息结束 |
| TEXT_MESSAGE_CHUNK | 文本消息 | 消息块（便捷） |
| TOOL_CALL_START | 工具调用 | 工具调用开始 |
| TOOL_CALL_ARGS | 工具调用 | 工具参数 |
| TOOL_CALL_END | 工具调用 | 工具调用结束 |
| TOOL_CALL_RESULT | 工具调用 | 工具结果 |
| TOOL_CALL_CHUNK | 工具调用 | 工具调用块（便捷） |
| STATE_SNAPSHOT | 状态管理 | 状态快照 |
| STATE_DELTA | 状态管理 | 状态增量 |
| MESSAGES_SNAPSHOT | 状态管理 | 消息快照 |
| ACTIVITY_SNAPSHOT | 状态管理 | 活动快照 |
| ACTIVITY_DELTA | 状态管理 | 活动增量 |
| RAW | 特殊 | 原始事件 |
| CUSTOM | 特殊 | 自定义事件 |

---

## 变更日志

### v2.0 变更【新增】

1. **RunFinishedEvent 升级**
   - 新增 `outcome` 字段，支持 `"success"` 和 `"interrupt"` 两种结果
   - 新增 `interrupt` 字段，用于携带中断详情

2. **RunAgentInput 升级**
   - 新增 `resume` 字段，用于从中断状态恢复执行

3. **新增类型**
   - `InterruptReason`：中断原因枚举
   - `InterruptDetails`：中断详情结构
   - `ResumePayload`：恢复执行负载结构
   - `RunFinishedOutcome`：运行结束结果类型

4. **新增示例**
   - Human-in-the-Loop 完整场景示例
   - 包含审批、信息补充、确认、拒绝等多种场景
   - 前端事件处理器完整实现
