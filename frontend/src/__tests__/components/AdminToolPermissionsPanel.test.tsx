import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
import AdminToolPermissionsPanel from '../../components/AdminToolPermissionsPanel';
import { getAdminMcpServers, type McpServer } from '../../services/mcpApi';
import {
  createAdminPermissionRule,
  deleteAdminPermissionRule,
  getAdminPermissionRules,
  updateAdminPermissionRule,
  type ToolPermissionRule,
} from '../../services/permissionApi';

vi.mock('../../services/mcpApi', () => ({
  getAdminMcpServers: vi.fn(),
}));

vi.mock('../../services/permissionApi', () => ({
  createAdminPermissionRule: vi.fn(),
  deleteAdminPermissionRule: vi.fn(),
  getAdminPermissionRules: vi.fn(),
  updateAdminPermissionRule: vi.fn(),
}));

function makeServer(): McpServer {
  return {
    id: 'server-1',
    name: '官方知识库',
    description: '',
    url: 'https://example.com/mcp',
    source: 'official',
    status: 'published',
    enabled: true,
    required: false,
    auth_type: 'none',
    credential_set: false,
    header_names: [],
    allow_private_network: false,
    allow_insecure_http: false,
    installation_id: null,
    tools_count: 2,
    enabled_tools_count: 2,
    enabled_tools: null,
    disabled_tools: [],
    last_tested_at: null,
    last_error: null,
    created_at: null,
    updated_at: null,
    version: 1,
  };
}

function makeRule(overrides: Partial<ToolPermissionRule> = {}): ToolPermissionRule {
  return {
    id: 'managed-1',
    scope_type: 'platform',
    scope_id: null,
    provider: 'builtin',
    server_id: null,
    tool_name: 'bash',
    tool_ref: 'builtin:bash',
    effect: 'deny',
    priority: 100,
    managed: true,
    conditions: null,
    description: '禁止危险命令',
    enabled: true,
    expires_at: null,
    created_by: 'admin',
    ...overrides,
  };
}

describe('AdminToolPermissionsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAdminMcpServers).mockResolvedValue([makeServer()]);
    vi.mocked(getAdminPermissionRules).mockResolvedValue([makeRule()]);
    vi.mocked(createAdminPermissionRule).mockResolvedValue(makeRule({ id: 'managed-new' }));
    vi.mocked(updateAdminPermissionRule).mockImplementation(async (_id, changes) => makeRule(changes));
    vi.mocked(deleteAdminPermissionRule).mockResolvedValue();
  });

  it('展示平台规则并支持原地更新策略与启用状态', async () => {
    render(<AdminToolPermissionsPanel />);

    expect(await screen.findByText('builtin:bash')).toBeInTheDocument();
    expect(screen.getByText('禁止危险命令')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('builtin:bash 策略'), { target: { value: 'ask' } });
    await waitFor(() => expect(updateAdminPermissionRule).toHaveBeenCalledWith('managed-1', { effect: 'ask' }));

    fireEvent.click(screen.getByRole('button', { name: 'builtin:bash 禁用' }));
    await waitFor(() => expect(updateAdminPermissionRule).toHaveBeenCalledWith('managed-1', { enabled: false }));
  });

  it('可为官方 MCP 创建平台规则', async () => {
    render(<AdminToolPermissionsPanel />);
    await screen.findByText('builtin:bash');

    fireEvent.change(screen.getByLabelText('规则提供方'), { target: { value: 'mcp' } });
    fireEvent.change(screen.getByLabelText('MCP 服务'), { target: { value: 'server-1' } });
    fireEvent.change(screen.getByLabelText('工具名'), { target: { value: 'search' } });
    fireEvent.change(screen.getByLabelText('规则策略'), { target: { value: 'deny' } });
    fireEvent.change(screen.getByLabelText('规则优先级'), { target: { value: '50' } });
    fireEvent.change(screen.getByLabelText('规则说明'), { target: { value: '审计要求' } });
    fireEvent.click(screen.getByRole('button', { name: '创建规则' }));

    await waitFor(() => {
      expect(createAdminPermissionRule).toHaveBeenCalledWith({
        provider: 'mcp',
        server_id: 'server-1',
        tool_name: 'search',
        effect: 'deny',
        priority: 50,
        description: '审计要求',
      });
    });
  });

  it('拒绝空白工具名而不是静默创建通配规则', async () => {
    render(<AdminToolPermissionsPanel />);
    await screen.findByText('builtin:bash');

    fireEvent.change(screen.getByLabelText('工具名'), { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: '创建规则' }));

    expect(await screen.findByText('请输入工具名，或使用 * 匹配全部工具')).toBeInTheDocument();
    expect(createAdminPermissionRule).not.toHaveBeenCalled();
  });

  it('确认后删除平台规则', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AdminToolPermissionsPanel />);
    await screen.findByText('builtin:bash');

    fireEvent.click(screen.getByRole('button', { name: '删除 builtin:bash' }));
    await waitFor(() => expect(deleteAdminPermissionRule).toHaveBeenCalledWith('managed-1'));
    expect(screen.queryByText('builtin:bash')).not.toBeInTheDocument();
  });
});
