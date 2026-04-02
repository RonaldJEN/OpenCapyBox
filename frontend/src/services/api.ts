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

    // 状态追踪（用于断线重连）
    let currentThreadId: string | null = null;
    let currentRunId: string | null = null;
    let runCompleted = false;
    let retryCount = 0;
    const maxRetries = 3;

    const doRequest = async (): Promise<void> => {
      return new Promise((resolve, reject) => {
        fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...this.getAuthHeaders(),
          },
          body: JSON.stringify({ content }),
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
                    this.handleAGUIEvent(event, callbacks, (tid, rid) => {
                      currentThreadId = tid;
                      currentRunId = rid;
                    }, () => {
                      runCompleted = true;
                    });
                  } catch (e) {
                    console.error('Failed to parse SSE data:', e, 'Line:', data);
                  }
                }
              }
            }
          })
          .catch((error) => {
            reject(error);
          });
      });
    };

    // 主执行逻辑
    try {
      await doRequest();
    } catch (error: any) {
      let lastError: any = error;

      while (!runCompleted && currentThreadId && currentRunId && retryCount < maxRetries) {
        retryCount++;
        console.log(`⚠️ 连接断开，尝试重连 (${retryCount}/${maxRetries})...`);

        await new Promise((r) => setTimeout(r, 1000 * retryCount));

        try {
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
          console.log('✅ 重连成功');
          return;
        } catch (retryError: any) {
          lastError = retryError;
          console.error('❌ 重连失败:', retryError);
        }
      }

      callbacks.onRunError?.(lastError?.message || '连接失败');
      throw lastError;
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

            if (done) {
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
                      callbacks.onRunFinished?.(event.threadId, event.runId, event.result, event.outcome || 'success', event.interrupt);
                      resolve();
                      return;

                    case 'RUN_ERROR':
                      callbacks.onRunError?.(event.message, event.code);
                      // 不 reject —— 等待后续 RUN_FINISHED 作为终态事件收敛
                      break;

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
            console.log('订阅已取消:', runId);
            resolve();
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
