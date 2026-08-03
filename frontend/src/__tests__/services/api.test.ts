import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// 在 import apiService 之前 mock axios
vi.mock('axios', () => {
  const mockAxiosInstance = {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  };
  return {
    default: {
      create: vi.fn(() => mockAxiosInstance),
    },
  };
});

describe('APIService', () => {
  let apiService: any;

  beforeEach(async () => {
    vi.clearAllMocks();

    // 動態 import 以獲取新實例
    vi.resetModules();
    const module = await import('../../services/api');
    apiService = module.apiService;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  describe('Session 管理', () => {
    it('setUserId 應該保存到 localStorage', () => {
      apiService.setUserId('test-session-123');
      
      expect(localStorage.setItem).toHaveBeenCalledWith(
        'userId',
        'test-session-123'
      );
    });

    it('getUserId 應該返回當前 user', () => {
      apiService.setUserId('test-session');
      expect(apiService.getUserId()).toBe('test-session');
    });

    it('logout 應該清除 user', () => {
      apiService.setUserId('test-session');
      apiService.logout();
      
      expect(localStorage.removeItem).toHaveBeenCalledWith('userId');
      expect(apiService.getUserId()).toBeNull();
    });

    it('setUserId 應該保存角色並可識別管理員', () => {
      apiService.setUserId('admin', 'token-1', 'admin');

      expect(localStorage.setItem).toHaveBeenCalledWith('userRole', 'admin');
      expect(apiService.getUserRole()).toBe('admin');
      expect(apiService.isAdminUser()).toBe(true);
    });

    it('非管理員角色應返回 false', () => {
      apiService.setUserId('demo', 'token-2', 'user');
      expect(apiService.isAdminUser()).toBe(false);
    });

    it('登录接口返回 401 时不应触发全局登出跳转', async () => {
      const axiosModule = await import('axios');
      const client = vi.mocked(axiosModule.default.create).mock.results[0].value as any;
      const responseRejected = client.interceptors.response.use.mock.calls[0][1];
      const logoutSpy = vi.spyOn(apiService, 'logout');
      const error = {
        config: { url: '/auth/login' },
        response: { status: 401 },
      };

      await expect(responseRejected(error)).rejects.toBe(error);

      expect(logoutSpy).not.toHaveBeenCalled();
    });

    it('管理员入口不应保存非管理员登录结果', async () => {
      const axiosModule = await import('axios');
      const client = vi.mocked(axiosModule.default.create).mock.results[0].value as any;
      client.post.mockResolvedValue({
        data: {
          user_id: 'ldap-user',
          access_token: 'user-token',
          role: 'user',
          is_admin: false,
        },
      });

      await expect(
        apiService.login('ldap-user', 'correct-password', { requireAdmin: true }),
      ).rejects.toThrow('ADMIN_LOGIN_REJECTED');

      expect(apiService.isAuthenticated()).toBe(false);
      expect(localStorage.setItem).not.toHaveBeenCalledWith('userId', 'ldap-user');
      expect(localStorage.setItem).not.toHaveBeenCalledWith('accessToken', 'user-token');
    });

    it('getSessions 应支持搜索参数并裁剪空白', async () => {
      const axiosModule = await import('axios');
      const client = vi.mocked(axiosModule.default.create).mock.results[0].value as any;
      client.get.mockResolvedValue({ data: { sessions: [] } });

      await apiService.getSessions('  搜索词  ');

      expect(client.get).toHaveBeenCalledWith('/sessions/list', {
        params: { q: '搜索词' },
      });
    });

    it('getSessionHistoryV2 preserves the legacy call and forwards an optional AbortSignal', async () => {
      const axiosModule = await import('axios');
      const client = vi.mocked(axiosModule.default.create).mock.results[0].value as unknown as {
        get: ReturnType<typeof vi.fn>;
      };
      const payload = { session_id: 'session-1', rounds: [], total: 0 };
      client.get.mockResolvedValue({ data: payload });

      await expect(apiService.getSessionHistoryV2('session-1')).resolves.toEqual(payload);
      expect(client.get).toHaveBeenNthCalledWith(1, '/sessions/session-1/history/v2');

      const controller = new AbortController();
      await expect(
        apiService.getSessionHistoryV2('session-1', controller.signal),
      ).resolves.toEqual(payload);
      expect(client.get).toHaveBeenNthCalledWith(
        2,
        '/sessions/session-1/history/v2',
        { signal: controller.signal },
      );
    });

    it('abortChat 应返回远端副作用不确定警告', async () => {
      const axiosModule = await import('axios');
      const client = vi.mocked(axiosModule.default.create).mock.results[0].value as unknown as {
        post: ReturnType<typeof vi.fn>;
      };
      const payload = {
        status: 'cancelled',
        request_id: 'cancel-1',
        reason: 'force_aborted',
        outcome_warning: '远端副作用可能已经发生',
      };
      client.post.mockResolvedValue({ data: payload });

      await expect(apiService.abortChat('session-1')).resolves.toEqual(payload);
      expect(client.post).toHaveBeenCalledWith('/chat/session-1/abort');
    });
  });

  describe('流式连接可靠性', () => {
    it('sendMessageStreamV2 应按 maxRetries 连续重试', async () => {
      vi.useFakeTimers();

      const encoder = new TextEncoder();
      const reader = {
        read: vi
          .fn()
          .mockResolvedValueOnce({
            done: false,
            value: encoder.encode('data: {"type":"RUN_STARTED","threadId":"thread-1","runId":"run-1"}\n\n'),
          })
          .mockRejectedValueOnce(new Error('stream dropped')),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      const subscribeSpy = vi.spyOn(apiService, 'subscribeToRound');
      subscribeSpy
        .mockImplementationOnce(() => ({
          promise: Promise.reject(new Error('retry-1-failed')),
          abort: vi.fn(),
          getLatestSequence: () => 0,
        }))
        .mockImplementationOnce(() => ({
          promise: Promise.reject(new Error('retry-2-failed')),
          abort: vi.fn(),
          getLatestSequence: () => 0,
        }))
        .mockImplementationOnce((...args: unknown[]) => {
          const cbs = args[2] as {
            onRunFinished?: (threadId: string, runId: string, result: any, outcome: string) => void;
          };
          // 模拟成功的 subscribe：先触发 onRunFinished 再 resolve
          const promise = new Promise<void>((resolve) => {
            cbs.onRunFinished?.('thread-1', 'run-1', {}, 'success');
            resolve();
          });
          return { promise, abort: vi.fn(), getLatestSequence: () => 0 };
        });

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunError: vi.fn(),
      };

      const requestPromise = apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      await vi.runAllTimersAsync();
      await requestPromise;

      expect(subscribeSpy).toHaveBeenCalledTimes(3);
      expect(callbacks.onRunError).not.toHaveBeenCalled();
    });

    it('sendMessageStreamV2 断线订阅恢复应从已消费 sequence 后续接', async () => {
      vi.useFakeTimers();

      const encoder = new TextEncoder();
      const reader = {
        read: vi
          .fn()
          .mockResolvedValueOnce({
            done: false,
            value: encoder.encode([
              'data: {"type":"RUN_STARTED","threadId":"thread-1","runId":"run-1","sequence":1}',
              'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg-1","delta":"hello","sequence":2}',
              '',
            ].join('\n')),
          })
          .mockRejectedValueOnce(new Error('stream dropped')),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      const subscribeSpy = vi.spyOn(apiService, 'subscribeToRound');
      subscribeSpy.mockImplementationOnce((...args: unknown[]) => {
        const cbs = args[2] as {
          onRunFinished?: (threadId: string, runId: string, result: any, outcome: string) => void;
        };
        cbs.onRunFinished?.('thread-1', 'run-1', { finalResponse: 'done' }, 'success');
        return { promise: Promise.resolve(), abort: vi.fn(), getLatestSequence: () => 3 };
      });

      const callbacks = {
        onTextMessageContent: vi.fn(),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      const requestPromise = apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      await vi.runAllTimersAsync();
      await requestPromise;

      expect(callbacks.onTextMessageContent).toHaveBeenCalledOnce();
      expect(callbacks.onTextMessageContent.mock.calls[0][2]).toBeUndefined();
      expect(subscribeSpy).toHaveBeenCalledWith(
        'session-1',
        'run-1',
        expect.any(Object),
        2,
      );
      expect(callbacks.onRunFinished).toHaveBeenCalledWith(
        'thread-1',
        'run-1',
        { finalResponse: 'done' },
        'success',
        undefined,
      );
    });

    it('sendMessageStreamV2 重连聚合内容不应重复拼接已显示 raw delta', async () => {
      vi.useFakeTimers();

      const encoder = new TextEncoder();
      const reader = {
        read: vi
          .fn()
          .mockResolvedValueOnce({
            done: false,
            value: encoder.encode([
              'data: {"type":"RUN_STARTED","threadId":"thread-1","runId":"run-1","sequence":1}',
              'data: {"type":"TEXT_MESSAGE_START","messageId":"msg-1","role":"assistant","sequence":2}',
              'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg-1","delta":"hel"}',
              '',
            ].join('\n')),
          })
          .mockRejectedValueOnce(new Error('stream dropped')),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      const subscribeSpy = vi.spyOn(apiService, 'subscribeToRound');
      subscribeSpy.mockImplementationOnce((...args: unknown[]) => {
        const cbs = args[2] as {
          onTextMessageContent?: (messageId: string, delta: string, meta?: { isAggregate?: boolean; sequence?: number }) => void;
          onRunFinished?: (threadId: string, runId: string, result: any, outcome: string) => void;
        };
        cbs.onTextMessageContent?.('msg-1', 'hello', { sequence: 3, isAggregate: true });
        cbs.onRunFinished?.('thread-1', 'run-1', { finalResponse: 'hello' }, 'success');
        return { promise: Promise.resolve(), abort: vi.fn(), getLatestSequence: () => 4 };
      });

      let renderedText = '';
      const callbacks = {
        onTextMessageContent: vi.fn((_messageId: string, delta: string, meta?: { isAggregate?: boolean }) => {
          renderedText = meta?.isAggregate ? delta : renderedText + delta;
        }),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      const requestPromise = apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      await vi.runAllTimersAsync();
      await requestPromise;

      expect(renderedText).toBe('hello');
      expect(callbacks.onTextMessageContent).toHaveBeenCalledTimes(2);
      expect(callbacks.onTextMessageContent.mock.calls[0]).toEqual(['msg-1', 'hel']);
      expect(callbacks.onTextMessageContent).toHaveBeenNthCalledWith(
        2,
        'msg-1',
        'hello',
        { sequence: 3, isAggregate: true },
      );
      expect(subscribeSpy).toHaveBeenCalledWith(
        'session-1',
        'run-1',
        expect.any(Object),
        2,
      );
    });

    it('subscribeToRound 应将带 sequence 的内容 delta 标记为聚合 replay 视图', async () => {
      const encoder = new TextEncoder();
      const reader = {
        read: vi
          .fn()
          .mockResolvedValueOnce({
            done: false,
            value: encoder.encode([
              'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg-1","delta":"hello","sequence":7}',
              'data: {"type":"RUN_FINISHED","threadId":"thread-1","runId":"run-1","result":{"finalResponse":"hello"},"outcome":"success","sequence":8}',
              '',
            ].join('\n')),
          })
          .mockResolvedValueOnce({ done: true, value: undefined }),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      const callbacks = {
        onTextMessageContent: vi.fn(),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      const subscription = apiService.subscribeToRound(
        'session-1',
        'run-1',
        callbacks,
        6,
      );

      await subscription.promise;

      expect(callbacks.onTextMessageContent).toHaveBeenCalledWith(
        'msg-1',
        'hello',
        { sequence: 7, isAggregate: true },
      );
      expect(subscription.getLatestSequence()).toBe(8);
    });

    it('sendMessageStreamV2 在响应通过后应触发 onStreamAccepted', async () => {
      const encoder = new TextEncoder();
      const reader = {
        read: vi
          .fn()
          .mockResolvedValueOnce({
            done: false,
            value: encoder.encode('data: {"type":"RUN_FINISHED","threadId":"session-1","runId":"run-1","result":{},"outcome":"success"}\n\n'),
          })
          .mockResolvedValueOnce({ done: true, value: undefined }),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      await apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      expect(callbacks.onStreamAccepted).toHaveBeenCalledOnce();
      expect(callbacks.onRunFinished).toHaveBeenCalledOnce();
      expect(callbacks.onRunError).not.toHaveBeenCalled();
    });

    it('sendMessageStreamV2 应处理关闭前未换行的最终 SSE 事件', async () => {
      const encoder = new TextEncoder();
      const reader = {
        read: vi
          .fn()
          .mockResolvedValueOnce({
            done: false,
            value: encoder.encode('data: {"type":"RUN_FINISHED","threadId":"session-1","runId":"run-1","result":{"finalResponse":"Done"},"outcome":"success"}'),
          })
          .mockResolvedValueOnce({ done: true, value: undefined }),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      await apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      expect(callbacks.onRunFinished).toHaveBeenCalledWith(
        'session-1',
        'run-1',
        { finalResponse: 'Done' },
        'success',
        undefined,
      );
      expect(callbacks.onRunError).not.toHaveBeenCalled();
    });

    it('sendMessageStreamV2 已被接受但未收到 runId 时应从最新完成 round 恢复', async () => {
      vi.useFakeTimers();
      vi.stubGlobal('crypto', {
        randomUUID: vi.fn(() => 'idempotency-restored'),
      });

      const reader = {
        read: vi.fn().mockResolvedValueOnce({ done: true, value: undefined }),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      vi.spyOn(apiService, 'getSessionHistoryV2').mockResolvedValue({
        session_id: 'session-1',
        total: 1,
        rounds: [
          {
            round_id: 'run-restored',
            idempotency_key: 'idempotency-restored',
            user_message: 'hello',
            final_response: 'Recovered final',
            steps: [],
            step_count: 0,
            status: 'completed',
            created_at: new Date().toISOString(),
          },
        ],
      });

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      const requestPromise = apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      await vi.runAllTimersAsync();
      await requestPromise;

      expect(callbacks.onStreamAccepted).toHaveBeenCalledOnce();
      expect(callbacks.onRunFinished).toHaveBeenCalledWith(
        'session-1',
        'run-restored',
        { finalResponse: 'Recovered final', stepCount: 0 },
        'success',
        undefined,
      );
      expect(callbacks.onRunError).not.toHaveBeenCalled();
    });

    it('sendMessageStreamV2 已完成恢复 max_steps_reached 时应保留 interrupt reason', async () => {
      vi.useFakeTimers();
      vi.stubGlobal('crypto', {
        randomUUID: vi.fn(() => 'idempotency-max-steps'),
      });

      const reader = {
        read: vi.fn().mockResolvedValueOnce({ done: true, value: undefined }),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      vi.spyOn(apiService, 'getSessionHistoryV2').mockResolvedValue({
        session_id: 'session-1',
        total: 1,
        rounds: [
          {
            round_id: 'run-max-steps',
            idempotency_key: 'idempotency-max-steps',
            user_message: 'loop forever',
            final_response: '已达到最大步数限制',
            steps: [],
            step_count: 3,
            status: 'max_steps_reached',
            created_at: new Date().toISOString(),
          },
        ],
      });

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      const requestPromise = apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'loop forever' }],
        callbacks,
      );

      await vi.runAllTimersAsync();
      await requestPromise;

      expect(callbacks.onStreamAccepted).toHaveBeenCalledOnce();
      expect(callbacks.onRunFinished).toHaveBeenCalledWith(
        'session-1',
        'run-max-steps',
        {
          finalResponse: '已达到最大步数限制',
          stepCount: 3,
          reason: 'max_steps_reached',
        },
        'interrupt',
        undefined,
      );
      expect(callbacks.onRunError).not.toHaveBeenCalled();
    });

    it('sendMessageStreamV2 accepted 后不应按时间窗误恢复旧 round', async () => {
      vi.useFakeTimers();
      vi.stubGlobal('crypto', {
        randomUUID: vi.fn(() => 'idempotency-current'),
      });

      const reader = {
        read: vi.fn().mockResolvedValueOnce({ done: true, value: undefined }),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      vi.spyOn(apiService, 'getSessionHistoryV2').mockResolvedValue({
        session_id: 'session-1',
        total: 1,
        rounds: [
          {
            round_id: 'run-old',
            idempotency_key: 'idempotency-old',
            user_message: 'old message',
            final_response: 'Old final',
            steps: [],
            step_count: 0,
            status: 'completed',
            created_at: new Date().toISOString(),
          },
        ],
      });

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunFinished: vi.fn(),
        onRunError: vi.fn(),
      };

      const requestPromise = apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      await vi.runAllTimersAsync();
      await requestPromise;

      expect(callbacks.onStreamAccepted).toHaveBeenCalledOnce();
      expect(callbacks.onRunFinished).not.toHaveBeenCalled();
      expect(callbacks.onRunError).toHaveBeenCalledWith(
        '网络中断，请检查连接后重试',
        'REQUEST_FAILED',
      );
    });

    it('sendMessageStreamV2 被 429 拒绝时不应触发 onStreamAccepted', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        text: vi.fn().mockResolvedValue('{"detail":"当前有正在运行的任务"}'),
      }));

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunError: vi.fn(),
      };

      await apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      expect(callbacks.onStreamAccepted).not.toHaveBeenCalled();
      expect(callbacks.onRunError).toHaveBeenCalledWith('当前有正在运行的任务', 'USER_BUSY');
    });

    it('sendMessageStreamV2 应把 FastAPI 422 detail 数组转成明确长文本提示', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        text: vi.fn().mockResolvedValue(JSON.stringify({
          detail: [
            {
              type: 'string_too_long',
              loc: ['body', 'content', 0, 'text'],
              msg: 'String should have at most 10000 characters',
              ctx: { max_length: 10000 },
            },
          ],
        })),
      }));

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunError: vi.fn(),
      };

      await apiService.sendMessageStreamV2(
        'session-1',
        [{ type: 'text', text: 'hello' }],
        callbacks,
      );

      expect(callbacks.onStreamAccepted).not.toHaveBeenCalled();
      expect(callbacks.onRunError).toHaveBeenCalledWith(
        '消息太长，当前最多支持 10000 字。请拆成多条发送，或保存为文件后上传。',
        'HTTP_CLIENT_ERROR',
      );
    });

    it('subscribeToRound HTTP 错误应解析 JSON detail', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        text: vi.fn().mockResolvedValue('{"detail":"轮次不存在"}'),
      }));
      vi.spyOn(apiService, 'getSessionHistoryV2').mockResolvedValue({
        session_id: 'session-1',
        total: 0,
        rounds: [],
      });

      const callbacks = {
        onRunError: vi.fn(),
      };

      const subscription = apiService.subscribeToRound('session-1', 'missing-run', callbacks);
      await expect(subscription.promise).rejects.toThrow('轮次不存在');

      expect(callbacks.onRunError).toHaveBeenCalledWith('轮次不存在');
    });

    it('downloadFile HTTP 错误应解析 JSON detail', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        text: vi.fn().mockResolvedValue('{"detail":"文件不存在或尚未生成"}'),
      }));

      await expect(apiService.downloadFile('session-1', 'missing.txt')).rejects.toThrow(
        '文件不存在或尚未生成',
      );
    });

    it('resumeStream 在未收到终态事件时应 reject', async () => {
      const encoder = new TextEncoder();
      const reader = {
        read: vi
          .fn()
          .mockResolvedValueOnce({
            done: false,
            value: encoder.encode('data: {"type":"CUSTOM","name":"heartbeat","value":{"ts":1}}\n\n'),
          })
          .mockResolvedValueOnce({ done: true, value: undefined }),
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        body: {
          getReader: () => reader,
        },
      }));

      const callbacks = {
        onStreamAccepted: vi.fn(),
        onRunError: vi.fn(),
      };

      await expect(
        apiService.resumeStream('session-1', 'interrupt-1', { Q: 'A' }, callbacks),
      ).rejects.toThrow('Resume stream ended without terminal event');

      expect(callbacks.onStreamAccepted).toHaveBeenCalledOnce();
      expect(callbacks.onRunError).toHaveBeenCalledTimes(1);
      expect(callbacks.onRunError).toHaveBeenCalledWith('Resume stream ended without terminal event');
    });
  });
});
