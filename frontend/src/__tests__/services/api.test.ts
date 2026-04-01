import { describe, it, expect, vi, beforeEach } from 'vitest';

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
});
