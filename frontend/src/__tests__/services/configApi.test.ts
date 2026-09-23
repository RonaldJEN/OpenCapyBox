import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { mockAxiosInstance } = vi.hoisted(() => ({
  mockAxiosInstance: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  },
}));

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => mockAxiosInstance),
  },
}));

describe('configApi', () => {
  let configApi: typeof import('../../services/configApi');
  let apiService: typeof import('../../services/api').apiService;

  beforeEach(async () => {
    vi.clearAllMocks();
    vi.resetModules();
    apiService = (await import('../../services/api')).apiService;
    configApi = await import('../../services/configApi');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('复用主客户端，不另建 axios 实例', async () => {
    const axiosModule = await import('axios');
    expect(axiosModule.default.create).toHaveBeenCalledTimes(1);
    expect(apiService.getAxiosClient()).toBe(mockAxiosInstance);

    mockAxiosInstance.get.mockResolvedValue({ data: { jobs: [] } });
    await configApi.getCronJobs();
    expect(mockAxiosInstance.get).toHaveBeenCalledWith('/cron/jobs');
  });

  it('allows the skills recovery request to wait up to 240 seconds', async () => {
    mockAxiosInstance.get.mockResolvedValue({
      data: { skills: [], sandbox_status: 'not_created' },
    });

    await configApi.getSkills();

    expect(mockAxiosInstance.get).toHaveBeenCalledWith('/config/skills', {
      timeout: 240_000,
    });
  });

  it('downloads cron artifact via authorized blob request', async () => {
    const blob = new Blob(['hello'], { type: 'text/plain' });
    mockAxiosInstance.get.mockResolvedValue({ data: blob });

    const createObjectURL = vi.fn(() => 'blob:cron-file');
    const revokeObjectURL = vi.fn();
    (window.URL.createObjectURL as unknown as typeof createObjectURL) = createObjectURL;
    (window.URL.revokeObjectURL as unknown as typeof revokeObjectURL) = revokeObjectURL;

    const anchor = document.createElement('a');
    const clickSpy = vi.fn();
    anchor.click = clickSpy;

    const originalCreateElement = document.createElement.bind(document);
    const createElementSpy = vi
      .spyOn(document, 'createElement')
      .mockImplementation((tagName: string) => (tagName === 'a' ? anchor : originalCreateElement(tagName)));

    await configApi.downloadCronRunFile('run-1', 'reports/iraq news.md', 'iraq-news.md');

    expect(mockAxiosInstance.get).toHaveBeenCalledWith('/cron/runs/run-1/files/reports/iraq%20news.md', {
      responseType: 'blob',
    });
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(anchor.download).toBe('iraq-news.md');
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:cron-file');

    createElementSpy.mockRestore();
  });
});
