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
        .mockImplementationOnce(() => ({
          promise: Promise.resolve(),
          abort: vi.fn(),
          getLatestSequence: () => 0,
        }));

      const callbacks = {
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
        onRunError: vi.fn(),
      };

      await expect(
        apiService.resumeStream('session-1', 'interrupt-1', { Q: 'A' }, callbacks),
      ).rejects.toThrow('Resume stream ended without terminal event');

      expect(callbacks.onRunError).toHaveBeenCalledTimes(1);
      expect(callbacks.onRunError).toHaveBeenCalledWith('Resume stream ended without terminal event');
    });
  });
});
