import axios, { AxiosInstance } from 'axios';
import type {
  AuthResponse,
  CreateSessionResponse,
  SessionListResponse,
  ModelsResponse,
  HistoryResponseV2,
  FileListResponse,
  FileInfo,
  RunningSessionResponse,
  ChatContentBlock,
  StreamCallbacks,
  SubscribeCallbacks,
  SubscriptionResult,
} from '../types';

/**
 * 幂等衝突：服務端已有對應 Round，客戶端應走 subscribe 路徑
 */
class RoundExistsError extends Error {
  constructor(public readonly roundId: string, public readonly roundStatus: string) {
    super('SSE_ROUND_EXISTS');
    this.name = 'RoundExistsError';
  }
}

class APIService {
  private client: AxiosInstance;
  private userId: string | null = null;
  private accessToken: string | null = null;

  constructor() {
    this.client = axios.create({
      baseURL: '/api',
      timeout: 60000, // 60 seconds for agent responses
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // 从 localStorage 恢复认证信息
    this.userId = localStorage.getItem('userId');
    this.accessToken = localStorage.getItem('accessToken');

    // 请求拦截器 - 添加 Authorization Bearer Token
    this.client.interceptors.request.use((config) => {
      if (this.accessToken && config.url !== '/auth/login') {
        config.headers.set('Authorization', `Bearer ${this.accessToken}`);
      }
      return config;
    });

    // 响应拦截器 - 处理错误
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401) {
          this.logout();
          window.location.href = '/login';
        }
        return Promise.reject(error);
      }
    );
  }

  // 设置当前登录信息
  setUserId(userId: string, accessToken?: string) {
    this.userId = userId;
    localStorage.setItem('userId', userId);
    if (accessToken) {
      this.accessToken = accessToken;
      localStorage.setItem('accessToken', accessToken);
    }
  }

  // 获取 user ID
  getUserId(): string | null {
    return this.userId;
  }

  getAccessToken(): string | null {
    return this.accessToken;
  }

  isAuthenticated(): boolean {
    return Boolean(this.userId && this.accessToken);
  }

  getAuthHeaders(): Record<string, string> {
    if (!this.accessToken) {
      return {};
    }
    return { Authorization: `Bearer ${this.accessToken}` };
  }

  // 登出
  logout() {
    this.userId = null;
    this.accessToken = null;
    localStorage.removeItem('userId');
    localStorage.removeItem('accessToken');
  }

  // ========== 认证 API ==========

  /**
   * 用户登录
   */
  async login(username: string, password: string): Promise<AuthResponse> {
    const formData = new FormData();
    formData.append('username', username);
    formData.append('password', password);

    const response = await this.client.post<AuthResponse>('/auth/login', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });

    this.setUserId(response.data.user_id, response.data.access_token);
    return response.data;
  }

  // ========== 会话 API ==========

  /**
   * 获取可用模型列表
   */
  async getModels(): Promise<ModelsResponse> {
    const response = await this.client.get<ModelsResponse>('/models');
    return response.data;
  }

  /**
   * 创建新会话
   */
  async createSession(modelId?: string): Promise<CreateSessionResponse> {
    const params: Record<string, string> = {};
    if (modelId) {
      params.model_id = modelId;
    }
    const response = await this.client.post<CreateSessionResponse>('/sessions/create', null, { params });
    return response.data;
  }

  /**
   * 获取用户的所有会话
   */
  async getSessions(): Promise<SessionListResponse> {
    const response = await this.client.get<SessionListResponse>('/sessions/list');
    return response.data;
  }


  /**
   * 删除会话
   */
  async deleteSession(chatSessionId: string): Promise<void> {
    await this.client.delete(`/sessions/${chatSessionId}`);
  }


  /**
   * 🆕 获取会话的轮次历史（V2）
   */
  async getSessionHistoryV2(chatSessionId: string): Promise<HistoryResponseV2> {
    const response = await this.client.get<HistoryResponseV2>(
      `/sessions/${chatSessionId}/history/v2`
    );
    return response.data;
  }

  /**
   * 检查用户是否有运行中的会话（单次 API 调用）
   */
  async getRunningSession(): Promise<RunningSessionResponse> {
    const response = await this.client.get<RunningSessionResponse>('/sessions/running-session');
    return response.data;
  }

  /**
   * 轻量级轮询：获取会话的轮次数量，用于检测新消息
   */
  async pollSession(chatSessionId: string): Promise<{ round_count: number }> {
    const response = await this.client.get<{ round_count: number }>(
      `/sessions/${chatSessionId}/poll`
    );
    return response.data;
  }

  /**
   * 中止正在进行的 Agent 执行
   * 后端会设置 cancel_token，Agent 通过 SSE 正常推送 RUN_FINISHED(interrupt)
   */
  async abortChat(chatSessionId: string): Promise<void> {
    await this.client.post(`/chat/${chatSessionId}/abort`);
  }

  /**
   * 流式发送消息并接收 AG-UI 事件（Server-Sent Events）
   * 支持自动断线重连
   */
  async sendMessageStreamV2(
    chatSessionId: string,
    content: ChatContentBlock[],
    callbacks: StreamCallbacks,
  ): Promise<void> {
    const url = `/api/chat/${chatSessionId}/message/stream`;

    // 幂等键：防止多 Worker 重复处理同一请求
    // crypto.randomUUID 需要安全上下文(HTTPS)，不可用时回退到 getRandomValues
    const idempotencyKey = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : Array.from(crypto.getRandomValues(new Uint8Array(16)),
          (b, i) => ((i === 6 ? (b & 0x0f) | 0x40 : i === 8 ? (b & 0x3f) | 0x80 : b))
            .toString(16).padStart(2, '0') + ([4, 6, 8, 10].includes(i) ? '-' : '')
        ).join('');

    // 状态追踪（用于断线重连）
    let currentThreadId: string | null = null;
    let currentRunId: string | null = null;
    let runCompleted = false;
    let retryCount = 0;
    const maxRetries = 3;

    const doRequest = async (): Promise<void> => {
      const abortController = new AbortController();
      let lastDataTime = Date.now();
      const STALE_TIMEOUT_MS = 45_000; // 3x heartbeat interval (15s)

      // Staleness timer: if no data received for 45s, abort the fetch
      const staleTimer = setInterval(() => {
        if (Date.now() - lastDataTime > STALE_TIMEOUT_MS) {
          console.warn(`⚠️ SSE 连接超过 ${STALE_TIMEOUT_MS / 1000}s 无数据，主动断开`);
          abortController.abort();
        }
      }, 10_000);

      return new Promise((resolve, reject) => {
        fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...this.getAuthHeaders(),
          },
          body: JSON.stringify({ content, idempotency_key: idempotencyKey }),
          signal: abortController.signal,
        })
          .then(async (response) => {
            if (!response.ok) {
              const errorText = await response.text();
              throw new Error(`HTTP ${response.status}: ${errorText}`);
            }

            const reader = response.body?.getReader();
            if (!reader) {
              throw new Error('Response body is null');
            }

            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
              const { done, value } = await reader.read();
              lastDataTime = Date.now();

              if (done) {
                if (runCompleted) {
                  resolve();
                } else {
                  // SSE 流被異常關閉（如 nginx proxy_read_timeout），但 Agent 可能仍在運行
                  reject(new Error('SSE_STREAM_CLOSED'));
                }
                break;
              }

              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() || '';

              for (const line of lines) {
                if (line.startsWith('data: ')) {
                  const data = line.slice(6);

                  try {
                    const event = JSON.parse(data);
                    this.handleAGUIEvent(event, callbacks, (tid, rid) => {
                      currentThreadId = tid;
                      currentRunId = rid;
                    }, () => {
                      runCompleted = true;
                    });
                  } catch (e) {
                    // RoundExistsError: 更新 currentRunId 為已有的 round_id，讓重連循環 subscribe 到正確目標
                    if (e instanceof RoundExistsError) {
                      currentRunId = e.roundId;
                      currentThreadId = chatSessionId;
                      throw e;
                    }
                    console.error('Failed to parse SSE data:', e, 'Line:', data);
                  }
                }
              }
            }
          })
          .catch((error) => {
            if (error.name === 'AbortError') {
              reject(new Error('SSE_STALE_TIMEOUT'));
            } else {
              reject(error);
            }
          })
          .finally(() => {
            clearInterval(staleTimer);
          });
      });
    };

    // 主执行逻辑
    try {
      await doRequest();
    } catch (error: any) {
      // 4xx 确定性错误不重试（参数错误、鉴权失败等）
      const is4xx = /^HTTP 4\d{2}:/.test(error?.message || '');
      if (is4xx) {
        callbacks.onRunError?.(error.message, 'HTTP_CLIENT_ERROR');
        return;
      }

      // 重连循环：有 runId 时用 subscribe 恢复，没有时重试整个请求
      while (!runCompleted && retryCount < maxRetries) {
        retryCount++;
        console.log(`⚠️ 连接断开，尝试重连 (${retryCount}/${maxRetries})...`);
        await new Promise((r) => setTimeout(r, 1000 * retryCount));

        try {
          if (currentThreadId && currentRunId) {
            // 已有 runId → subscribe 断点续传
            const subscription = this.subscribeToRound(chatSessionId, currentRunId, {
              onMessagesSnapshot: callbacks.onMessagesSnapshot,
              onStateSnapshot: callbacks.onStateSnapshot,
              onStateDelta: callbacks.onStateDelta,
              onRunFinished: (tid, rid, result, outcome, interrupt) => {
                runCompleted = true;
                callbacks.onRunFinished?.(tid, rid, result, outcome, interrupt);
              },
              onRunError: callbacks.onRunError,
              onCustomEvent: callbacks.onCustomEvent,
            });
            await subscription.promise;
            if (runCompleted) {
              console.log('✅ 重连成功');
              return;
            }
            // subscription resolved 但 runCompleted 仍为 false（不应发生，但防御性处理）
            throw new Error('SSE_STREAM_CLOSED');
          } else {
            // 尚无 runId → 先检查后端是否已创建 running 轮次（避免重复 POST）
            try {
              const history = await this.getSessionHistoryV2(chatSessionId);
              const runningRound = history.rounds
                ?.filter((r: any) => r.status === 'running')
                .sort((a: any, b: any) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())[0];

              if (runningRound) {
                // 后端已受理请求，改为 subscribe
                console.log(`🔍 检测到后端已有 running 轮次 ${runningRound.round_id}，改用 subscribe`);
                currentRunId = runningRound.round_id;
                currentThreadId = chatSessionId;
                continue; // 进入下一轮循环，走 subscribe 分支
              }
            } catch (checkError) {
              console.error('检查后端轮次失败:', checkError);
            }
            // 后端无 running 轮次 → 安全重试 POST
            await doRequest();
            console.log('✅ 重试请求成功');
            return;
          }
        } catch (retryError: any) {
          console.error(`❌ 重连/重试失败 (${retryCount}/${maxRetries}):`, retryError);
        }
      }

      // 所有重试耗尽：检查后端轮次实际状态进行恢复
      if (!runCompleted) {
        if (currentRunId) {
          try {
            console.log(`🔍 重试耗尽，检查轮次 ${currentRunId} 实际状态...`);
            const history = await this.getSessionHistoryV2(chatSessionId);
            const round = history.rounds.find((r: any) => r.round_id === currentRunId);

            if (round?.status === 'completed' || round?.status === 'failed' || round?.status === 'interrupted') {
              const outcome = round.status === 'completed'
                ? 'success'
                : round.status === 'interrupted'
                  ? 'interrupt'
                  : 'error';
              console.log(`✅ 轮次 ${currentRunId} 实际状态: ${round.status}，恢复 UI`);
              callbacks.onRunFinished?.(currentThreadId || chatSessionId, currentRunId, {
                finalResponse: round.final_response || '',
                stepCount: round.step_count || 0,
              }, outcome, round.interrupt);
              return;
            }
          } catch (checkError) {
            console.error('检查轮次状态失败:', checkError);
          }
          // 轮次仍在运行或状态检查失败，通知前端断连
          callbacks.onRunError?.('连接已断开，Agent 可能仍在运行。请刷新页面查看结果', 'SSE_DISCONNECTED');
        } else {
          // 连 RUN_STARTED 都没收到，通过 onRunError 重置状态让用户可重新发送
          callbacks.onRunError?.('连接已断开，请重新发送', 'SSE_DISCONNECTED');
        }
      }
    }
  }

  /**
   * 处理 AG-UI 事件
   */
  private handleAGUIEvent(
    event: any,
    callbacks: StreamCallbacks,
    onRunStart: (threadId: string, runId: string) => void,
    onComplete: () => void
  ): void {
    switch (event.type) {
      // 生命周期事件
      case 'RUN_STARTED':
        onRunStart(event.threadId, event.runId);
        callbacks.onRunStarted?.(event.threadId, event.runId);
        break;

      case 'RUN_FINISHED':
        onComplete();
        callbacks.onRunFinished?.(event.threadId, event.runId, event.result, event.outcome || 'success', event.interrupt);
        break;

      case 'RUN_ERROR':
        // ROUND_IN_PROGRESS: 幂等冲突，message 中携带已有 round_id，抛错让重连循环走 subscribe
        if (event.code === 'ROUND_IN_PROGRESS') {
          throw new RoundExistsError(event.message, 'running');
        }
        onComplete();
        callbacks.onRunError?.(event.message, event.code);
        break;

      case 'STEP_STARTED':
        callbacks.onStepStarted?.(event.stepName, event.timestamp);
        break;

      case 'STEP_FINISHED':
        callbacks.onStepFinished?.(event.stepName, event.timestamp);
        break;

      // 文本消息事件
      case 'TEXT_MESSAGE_START':
        callbacks.onTextMessageStart?.(event.messageId, event.role);
        break;

      case 'TEXT_MESSAGE_CONTENT':
        callbacks.onTextMessageContent?.(event.messageId, event.delta);
        break;

      case 'TEXT_MESSAGE_END':
        callbacks.onTextMessageEnd?.(event.messageId);
        break;

      // 思考过程事件
      case 'THINKING_TEXT_MESSAGE_START':
        callbacks.onThinkingStart?.(event.messageId, event.timestamp);
        break;

      case 'THINKING_TEXT_MESSAGE_CONTENT':
        callbacks.onThinkingContent?.(event.messageId, event.delta);
        break;

      case 'THINKING_TEXT_MESSAGE_END':
        callbacks.onThinkingEnd?.(event.messageId, event.timestamp);
        break;

      // 工具调用事件
      case 'TOOL_CALL_START':
        callbacks.onToolCallStart?.(event.toolCallId, event.toolCallName, event.parentMessageId, event.timestamp);
        break;

      case 'TOOL_CALL_ARGS':
        callbacks.onToolCallArgs?.(event.toolCallId, event.delta);
        break;

      case 'TOOL_CALL_END':
        callbacks.onToolCallEnd?.(event.toolCallId, event.timestamp);
        break;

      case 'TOOL_CALL_RESULT':
        callbacks.onToolCallResult?.(event.messageId, event.toolCallId, event.content, event.timestamp, event.executionTimeMs);
        break;

      // 状态管理事件
      case 'STATE_SNAPSHOT':
        callbacks.onStateSnapshot?.(event.snapshot);
        break;

      case 'STATE_DELTA':
        callbacks.onStateDelta?.(event.delta);
        break;

      case 'MESSAGES_SNAPSHOT':
        callbacks.onMessagesSnapshot?.(event.messages);
        break;

      // 活动事件
      case 'ACTIVITY_SNAPSHOT':
        callbacks.onActivitySnapshot?.(event.messageId, event.activityType, event.content);
        break;

      case 'ACTIVITY_DELTA':
        callbacks.onActivityDelta?.(event.messageId, event.activityType, event.patch);
        break;

      // 自定义事件
      case 'CUSTOM':
        callbacks.onCustomEvent?.(event.name, event.value);
        break;

      default:
        console.debug('Unknown AG-UI event type:', event.type);
    }
  }

  /**
   * 订阅轮次更新（用于断线恢复）- AG-UI 协议
   * 
   * AG-UI 重連機制：
   * 1. 通過 lastSequence 參數告知服務端最後收到的事件序列號
   * 2. 服務端會重放 lastSequence 之後的所有事件
   * 3. 然後繼續推送後續的實時事件
   * 
   * @param chatSessionId 會話 ID
   * @param runId 輪次 ID（AG-UI runId）
   * @param callbacks 事件回調
   * @param lastSequence 最後收到的事件序列號（默認 0，表示從頭重放）
   * @returns 返回一个对象，包含 promise 和 abort 方法用于取消订阅
   */
  subscribeToRound(
    chatSessionId: string,
    runId: string,
    callbacks: SubscribeCallbacks,
    lastSequence: number = 0
  ): SubscriptionResult {
    const url = `/api/chat/${chatSessionId}/round/${runId}/subscribe?last_sequence=${lastSequence}`;
    const abortController = new AbortController();
    let latestSequence = lastSequence;
    let lastDataTime = Date.now();
    const STALE_TIMEOUT_MS = 45_000;

    // 区分 staleness 超时 vs 用户主动 abort
    let isStaleAbort = false;

    // Staleness timer: abort if no data for 45s
    const staleTimer = setInterval(() => {
      if (Date.now() - lastDataTime > STALE_TIMEOUT_MS) {
        console.warn(`⚠️ 订阅连接超过 ${STALE_TIMEOUT_MS / 1000}s 无数据，主动断开`);
        isStaleAbort = true;
        abortController.abort();
      }
    }, 10_000);

    // 追踪是否收到了终态事件（RUN_FINISHED）
    let runFinishedReceived = false;

    const promise = new Promise<void>((resolve, reject) => {
      fetch(url, {
        method: 'GET',
        headers: {
          'Accept': 'text/event-stream',
          ...this.getAuthHeaders(),
        },
        signal: abortController.signal,
      })
        .then(async (response) => {
          if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errorText}`);
          }

          const reader = response.body?.getReader();
          if (!reader) {
            throw new Error('Response body is null');
          }

          const decoder = new TextDecoder();
          let buffer = '';

          while (true) {
            const { done, value } = await reader.read();
            lastDataTime = Date.now();

            if (done) {
              if (runFinishedReceived) {
                resolve();
              } else {
                // SSE 流关闭但未收到 RUN_FINISHED，可能是 nginx 断连
                reject(new Error('SSE_STREAM_CLOSED'));
              }
              break;
            }

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
              if (line.startsWith('data: ')) {
                const data = line.slice(6);

                try {
                  const event = JSON.parse(data);
                  if (typeof event.sequence === 'number') {
                    latestSequence = event.sequence;
                  } else if (typeof event._sequence === 'number') {
                    latestSequence = event._sequence;
                  }

                  switch (event.type) {
                    case 'MESSAGES_SNAPSHOT':
                      callbacks.onMessagesSnapshot?.(event.messages);
                      break;

                    case 'STATE_SNAPSHOT':
                      callbacks.onStateSnapshot?.(event.snapshot);
                      break;

                    case 'STATE_DELTA':
                      callbacks.onStateDelta?.(event.delta);
                      break;

                    case 'RUN_FINISHED':
                      runFinishedReceived = true;
                      callbacks.onRunFinished?.(event.threadId, event.runId, event.result, event.outcome || 'success', event.interrupt);
                      resolve();
                      return;

                    case 'RUN_ERROR':
                      runFinishedReceived = true;
                      callbacks.onRunError?.(event.message, event.code);
                      resolve();
                      return;

                    case 'CUSTOM':
                      callbacks.onCustomEvent?.(event.name, event.value);
                      // 心跳事件
                      if (event.name === 'heartbeat') {
                        console.debug('订阅心跳:', event.value?.timestamp);
                      }
                      break;

                    // 🆕 流式消息事件
                    case 'TEXT_MESSAGE_START':
                      callbacks.onTextMessageStart?.(event.messageId, event.role);
                      break;

                    case 'TEXT_MESSAGE_CONTENT':
                      callbacks.onTextMessageContent?.(event.messageId, event.delta);
                      break;

                    case 'TEXT_MESSAGE_END':
                      callbacks.onTextMessageEnd?.(event.messageId);
                      break;

                    // 🆕 思维链事件
                    case 'THINKING_TEXT_MESSAGE_START':
                      callbacks.onThinkingStart?.(event.messageId, event.timestamp);
                      break;

                    case 'THINKING_TEXT_MESSAGE_CONTENT':
                      callbacks.onThinkingContent?.(event.messageId, event.delta);
                      break;

                    case 'THINKING_TEXT_MESSAGE_END':
                      callbacks.onThinkingEnd?.(event.messageId, event.timestamp);
                      break;

                    // 🆕 工具调用事件
                    case 'TOOL_CALL_START':
                      callbacks.onToolCallStart?.(event.toolCallId, event.toolCallName, event.parentMessageId, event.timestamp);
                      break;

                    case 'TOOL_CALL_ARGS':
                      callbacks.onToolCallArgs?.(event.toolCallId, event.delta);
                      break;

                    case 'TOOL_CALL_END':
                      callbacks.onToolCallEnd?.(event.toolCallId, event.timestamp);
                      break;

                    case 'TOOL_CALL_RESULT':
                      callbacks.onToolCallResult?.(event.messageId, event.toolCallId, event.content, event.timestamp, event.executionTimeMs);
                      break;

                    // 🆕 步骤事件
                    case 'STEP_STARTED':
                      callbacks.onStepStarted?.(event.stepName, event.timestamp);
                      break;

                    case 'STEP_FINISHED':
                      callbacks.onStepFinished?.(event.stepName, event.timestamp);
                      break;

                    default:
                      console.debug('Subscribe: Unknown event type:', event.type);
                  }
                } catch (e) {
                  console.error('Failed to parse subscribe SSE data:', e, 'Line:', data);
                }
              }
            }
          }
        })
        .catch(async (error) => {
          if (error.name === 'AbortError') {
            if (!isStaleAbort) {
              // 用户主动取消：直接静默 resolve
              console.log('订阅已取消:', runId);
              resolve();
              return;
            }
            // staleness timeout：检查轮次实际状态进行恢复
            console.warn(`⚠️ 订阅连接超时中断 (${runId})，尝试检查状态...`);
            try {
              const history = await this.getSessionHistoryV2(chatSessionId);
              const round = history.rounds.find((r: any) => r.round_id === runId);
              
              if (round?.status === 'completed' || round?.status === 'failed' || round?.status === 'interrupted') {
                console.log(`✅ 检测到轮次 ${runId} 已完成 (status=${round.status})，恢复状态`);
                const outcome = round.status === 'completed'
                  ? 'success'
                  : round.status === 'interrupted'
                    ? 'interrupt'
                    : 'error';
                callbacks.onRunFinished?.(chatSessionId, runId, {
                  finalResponse: round.final_response || '',
                  stepCount: round.step_count || 0,
                }, outcome, round.interrupt);
                resolve();
                return;
              }
            } catch (checkError) {
              console.error('检查轮次状态失败:', checkError);
            }
            // staleness 但轮次仍在运行，reject 让上层重连
            reject(new Error('SSE_STALE_TIMEOUT'));
            return;
          }
          
          // 🆕 訂閱異常斷開時，檢查輪次狀態進行恢復
          console.warn(`⚠️ 订阅异常断开 (${runId})，尝试检查状态...`);
          try {
            const history = await this.getSessionHistoryV2(chatSessionId);
            const round = history.rounds.find((r: any) => r.round_id === runId);
            
            if (round?.status === 'completed' || round?.status === 'failed' || round?.status === 'interrupted') {
              // 輪次已完成，補發 onRunFinished 回調
              console.log(`✅ 检测到轮次 ${runId} 已完成 (status=${round.status})，恢复状态`);
              const outcome = round.status === 'completed'
                ? 'success'
                : round.status === 'interrupted'
                  ? 'interrupt'
                  : 'error';
              callbacks.onRunFinished?.(chatSessionId, runId, {
                finalResponse: round.final_response || '',
                stepCount: round.step_count || 0,
              }, outcome, round.interrupt);
              resolve();
              return;
            }
          } catch (checkError) {
            console.error('检查轮次状态失败:', checkError);
          }
          
          callbacks.onRunError?.(error.message);
          reject(error);
        })
        .finally(() => {
          clearInterval(staleTimer);
        });
    });

    return {
      promise,
      abort: () => abortController.abort(),
      getLatestSequence: () => latestSequence,
    };
  }

  // ========== Resume API (Human-in-the-Loop) ==========

  /**
   * 恢复被中断的 Agent 执行（SSE 流）
   * 与 sendMessageStreamV2 共享同一套 AG-UI 事件处理逻辑
   */
  async resumeStream(
    chatSessionId: string,
    interruptId: string,
    answers: Record<string, string>,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    const url = `/api/chat/${chatSessionId}/resume`;
    let receivedTerminalEvent = false;

    return new Promise((resolve, reject) => {
      fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...this.getAuthHeaders(),
        },
        body: JSON.stringify({ interrupt_id: interruptId, answers }),
      })
        .then(async (response) => {
          if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errorText}`);
          }

          const reader = response.body?.getReader();
          if (!reader) {
            throw new Error('Response body is null');
          }

          const decoder = new TextDecoder();
          let buffer = '';

          while (true) {
            const { done, value } = await reader.read();

            if (done) {
              if (!receivedTerminalEvent) {
                const terminalError = new Error('Resume stream ended without terminal event');
                callbacks.onRunError?.(terminalError.message);
                reject(terminalError);
                return;
              }
              resolve();
              break;
            }

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
              if (line.startsWith('data: ')) {
                const data = line.slice(6);

                try {
                  const event = JSON.parse(data);
                  if (event.type === 'RUN_FINISHED' || event.type === 'RUN_ERROR') {
                    receivedTerminalEvent = true;
                  }
                  this.handleAGUIEvent(event, callbacks, () => {}, () => {});
                } catch (e) {
                  console.error('Failed to parse resume SSE data:', e, 'Line:', data);
                }
              }
            }
          }
        })
        .catch((error) => {
          callbacks.onRunError?.(error.message);
          reject(error);
        });
    });
  }

  // ========== 文件管理 API ==========

  /**
   * 🆕 获取会话的文件列表
   */
  async getSessionFiles(chatSessionId: string): Promise<FileListResponse> {
    const response = await this.client.get<FileListResponse>(
      `/sessions/${chatSessionId}/files`
    );
    return response.data;
  }

  /**
   * 🆕 上传文件到会话工作空间
   */
  async uploadFile(chatSessionId: string, file: File): Promise<FileInfo> {
    const formData = new FormData();
    formData.append('file', file);

    const response = await this.client.post<FileInfo>(
      `/sessions/${chatSessionId}/upload`,
      formData,
      {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      }
    );
    return response.data;
  }

  /**
   * 🆕 下载会话中的文件
   */
  async downloadFile(chatSessionId: string, filePath: string): Promise<void> {
    const url = `/api/sessions/${chatSessionId}/files/${filePath}`;
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        ...this.getAuthHeaders(),
      },
    });

    if (!response.ok) {
      throw new Error(`下载失败: HTTP ${response.status}`);
    }

    const blob = await response.blob();
    const objectUrl = window.URL.createObjectURL(blob);

    try {
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = filePath.split('/').pop() || 'download';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    } finally {
      window.URL.revokeObjectURL(objectUrl);
    }
  }

}

// 导出单例
export const apiService = new APIService();
