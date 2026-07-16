import { beforeEach, describe, expect, it, vi } from 'vitest';

const { client } = vi.hoisted(() => ({
  client: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getAxiosClient: () => client,
  },
}));

import {
  createAdminMcpServer,
  exportMcpConfig,
  getMcpServerTools,
  getMcpServers,
  importMcpConfig,
  testMcpServer,
  updateMcpConnection,
  updateMcpToolVisibility,
} from '../../services/mcpApi';

describe('mcpApi', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('读取用户目录并规范化 Streamable HTTP 服务字段', async () => {
    client.get.mockResolvedValue({
      data: {
        config_version: 3,
        servers: [{
          id: 'srv-1',
          name: '知识库',
          description: '内部检索',
          url: 'https://mcp.example.com/mcp',
          source: 'official',
          status: 'published',
          enabled: true,
          auth_type: 'bearer',
          credential_set: true,
          header_names: ['Authorization'],
          allow_private_network: false,
          allow_insecure_http: false,
          installation_id: 'install-1',
          tools_count: 4,
          enabled_tools_count: 3,
          enabled_tools: null,
          disabled_tools: ['delete_report'],
          version: 2,
        }],
      },
    });

    await expect(getMcpServers()).resolves.toEqual([
      expect.objectContaining({
        id: 'srv-1',
        source: 'official',
        auth_type: 'bearer',
        credential_set: true,
        installation_id: 'install-1',
        tools_count: 4,
        enabled_tools_count: 3,
        enabled_tools: null,
        disabled_tools: ['delete_report'],
        version: 2,
      }),
    ]);
    expect(client.get).toHaveBeenCalledWith('/mcp/servers');
  });

  it('读取并更新每个连接独立的工具发布状态', async () => {
    const response = {
      server_id: 'srv-1',
      installation_id: 'install-1',
      visibility_revision: 7,
      tools_count: 2,
      enabled_tools_count: 1,
      enabled_tools: null,
      disabled_tools: ['delete_report'],
      tools: [{
        name: 'delete_report',
        title: '删除报告',
        description: '删除指定报告',
        schema_hash: 'schema-1',
        enabled: false,
        discovered_at: '2026-07-13T12:00:00',
      }],
    };
    client.get.mockResolvedValueOnce({ data: response });
    client.put.mockResolvedValueOnce({ data: response });

    await expect(getMcpServerTools('srv/1')).resolves.toEqual(response);
    await expect(updateMcpToolVisibility('srv/1', {
      expected_revision: 7,
      enabled_tools: null,
      disabled_tools: ['delete_report'],
    })).resolves.toEqual(response);

    expect(client.get).toHaveBeenCalledWith('/mcp/servers/srv%2F1/tools');
    expect(client.put).toHaveBeenCalledWith('/mcp/servers/srv%2F1/tools/visibility', {
      expected_revision: 7,
      enabled_tools: null,
      disabled_tools: ['delete_report'],
    });
  });

  it('缺失工具发布修订号时按初始 revision 0 规范化', async () => {
    client.get.mockResolvedValueOnce({ data: {
      server_id: 'srv-1',
      installation_id: null,
      tools: [],
    } });

    await expect(getMcpServerTools('srv-1')).resolves.toEqual(expect.objectContaining({
      visibility_revision: 0,
    }));
  });

  it('连接更新仅写入新凭证且使用 connection 端点', async () => {
    client.put.mockResolvedValue({ data: { id: 'srv-1', source: 'official', enabled: true } });

    await updateMcpConnection('srv/1', {
      enabled: true,
      auth_type: 'bearer',
      bearer_token: 'new-secret',
    });

    expect(client.put).toHaveBeenCalledWith('/mcp/servers/srv%2F1/connection', {
      enabled: true,
      auth_type: 'bearer',
      bearer_token: 'new-secret',
    });
  });

  it('测试、导入和导出使用约定端点', async () => {
    client.post
      .mockResolvedValueOnce({ data: { ok: true, tools_count: 3, latency_ms: 21, error: null } })
      .mockResolvedValueOnce({ data: { imported: 1, servers: [], errors: [] } });
    client.get.mockResolvedValueOnce({ data: { mcpServers: {} } });

    await expect(testMcpServer('srv-1')).resolves.toEqual({
      ok: true,
      tools_count: 3,
      latency_ms: 21,
      error: null,
    });
    await importMcpConfig({ mcpServers: {} });
    await expect(exportMcpConfig()).resolves.toEqual({ mcpServers: {} });

    expect(client.post).toHaveBeenNthCalledWith(1, '/mcp/servers/srv-1/test');
    expect(client.post).toHaveBeenNthCalledWith(2, '/mcp/import', { mcpServers: {} });
    expect(client.get).toHaveBeenCalledWith('/mcp/export');
  });

  it('管理员创建请求包含发布状态和网络边界', async () => {
    client.post.mockResolvedValue({ data: { id: 'official-1', name: '内部服务' } });
    const payload = {
      name: '内部服务',
      description: null,
      url: 'http://mcp.internal/mcp',
      status: 'draft' as const,
      auth_type: 'none' as const,
      allow_private_network: true,
      allow_insecure_http: true,
      required: false,
    };

    await createAdminMcpServer(payload);

    expect(client.post).toHaveBeenCalledWith('/admin/mcp/servers', payload);
  });
});
