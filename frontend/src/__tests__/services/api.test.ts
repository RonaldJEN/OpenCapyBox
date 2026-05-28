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

    it('getSessions 应支持搜索参数并裁剪空白', async () => {
      const axiosModule = await import('axios');
      const client = vi.mocked(axiosModule.default.create).mock.results[0].value as any;
      client.get.mockResolvedValue({ data: { sessions: [] } });

      await apiService.getSessions('  搜索词  ');

      expect(client.get).toHaveBeenCalledWith('/sessions/list', {
        params: { q: '搜索词' },
      });
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
