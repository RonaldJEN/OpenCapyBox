import { beforeEach, describe, expect, it, vi } from 'vitest';

const { client } = vi.hoisted(() => ({
  client: {
    get: vi.fn(),
    post: vi.fn(),
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
  clearPermissionSelection,
  createAdminPermissionRule,
  createPermissionRule,
  deleteAdminPermissionRule,
  getAdminPermissionRules,
  getPermissionRules,
  getPermissionTools,
  updateAdminPermissionRule,
  updatePermissionRule,
} from '../../services/permissionApi';

describe('permissionApi', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('用户规则与工具清单使用独立权限端点', async () => {
    client.get
      .mockResolvedValueOnce({ data: { rules: [{ id: 'rule-1' }] } })
      .mockResolvedValueOnce({ data: { tools: [{ tool_ref: 'builtin:read_file' }] } });
    client.post.mockResolvedValue({ data: { id: 'rule-new' } });
    client.patch.mockResolvedValue({ data: { id: 'rule/1', effect: 'deny' } });

    await expect(getPermissionRules()).resolves.toEqual([{ id: 'rule-1' }]);
    await expect(getPermissionTools()).resolves.toEqual([{ tool_ref: 'builtin:read_file' }]);
    await createPermissionRule({ provider: 'builtin', tool_name: 'read_file', effect: 'ask' });
    await updatePermissionRule('rule/1', { effect: 'deny' });

    expect(client.get).toHaveBeenNthCalledWith(1, '/permissions/rules');
    expect(client.get).toHaveBeenNthCalledWith(2, '/permissions/tools');
    expect(client.post).toHaveBeenCalledWith('/permissions/rules', {
      provider: 'builtin',
      tool_name: 'read_file',
      effect: 'ask',
    });
    expect(client.patch).toHaveBeenCalledWith('/permissions/rules/rule%2F1', { effect: 'deny' });
  });

  it('平台规则只使用管理员端点', async () => {
    client.get.mockResolvedValue({ data: { rules: [] } });
    client.post.mockResolvedValue({ data: { id: 'managed-1' } });
    client.patch.mockResolvedValue({ data: { id: 'managed/1', enabled: false } });
    client.delete.mockResolvedValue({ data: { deleted: true } });

    await getAdminPermissionRules();
    await createAdminPermissionRule({
      provider: 'mcp',
      server_id: 'server-1',
      tool_name: '*',
      effect: 'ask',
    });
    await updateAdminPermissionRule('managed/1', { enabled: false });
    await deleteAdminPermissionRule('managed/1');

    expect(client.get).toHaveBeenCalledWith('/admin/tool-permissions');
    expect(client.post).toHaveBeenCalledWith('/admin/tool-permissions', {
      provider: 'mcp',
      server_id: 'server-1',
      tool_name: '*',
      effect: 'ask',
    });
    expect(client.patch).toHaveBeenCalledWith('/admin/tool-permissions/managed%2F1', { enabled: false });
    expect(client.delete).toHaveBeenCalledWith('/admin/tool-permissions/managed%2F1');
  });

  it('恢复默认使用单次原子清除端点，请求体携带精确工具身份', async () => {
    client.delete.mockResolvedValue({ data: { deleted: 2 } });

    await expect(
      clearPermissionSelection({ provider: 'mcp', server_id: 'server-1', tool_name: 'search' }),
    ).resolves.toBe(2);

    expect(client.delete).toHaveBeenCalledWith('/permissions/rules/selection', {
      data: { provider: 'mcp', server_id: 'server-1', tool_name: 'search' },
    });
  });
});
