import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '../utils/test-utils';
import ToolPermissionsPanel from '../../components/ToolPermissionsPanel';
import {
  clearPermissionSelection,
  getPermissionRules,
  getPermissionTools,
  setPermissionSelection,
  setPermissionSelectionBatch,
  type PermissionTool,
  type ToolPermissionRule,
} from '../../services/permissionApi';

vi.mock('../../services/permissionApi', () => ({
  clearPermissionSelection: vi.fn(),
  getPermissionRules: vi.fn(),
  getPermissionTools: vi.fn(),
  setPermissionSelection: vi.fn(),
  setPermissionSelectionBatch: vi.fn(),
}));

function makeRule(overrides: Partial<ToolPermissionRule> = {}): ToolPermissionRule {
  return {
    id: 'rule-user',
    scope_type: 'user',
    scope_id: 'demo',
    provider: 'builtin',
    server_id: null,
    tool_name: 'read_file',
    tool_ref: 'builtin:read_file',
    effect: 'allow',
    priority: 0,
    managed: false,
    conditions: null,
    description: null,
    enabled: true,
    expires_at: null,
    created_by: 'demo',
    ...overrides,
  };
}

function makeTool(overrides: Partial<PermissionTool> = {}): PermissionTool {
  return {
    tool_ref: 'builtin:read_file',
    provider: 'builtin',
    server_id: null,
    server_name: null,
    source_type: 'builtin',
    tool_name: 'read_file',
    title: 'read_file',
    description: '读取工作区文件',
    effect: 'allow',
    matched_rule_id: null,
    ...overrides,
  };
}

describe('ToolPermissionsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getPermissionTools).mockResolvedValue([
      makeTool(),
      makeTool({
        tool_ref: 'builtin:write_file',
        tool_name: 'write_file',
        title: 'write_file',
        description: '写入工作区文件',
        effect: 'allow',
      }),
      makeTool({
        tool_ref: 'mcp:server-1:search',
        provider: 'mcp',
        server_id: 'server-1',
        server_name: '企业检索',
        source_type: 'official',
        tool_name: 'search',
        title: '知识检索',
        effect: 'ask',
        matched_rule_id: 'rule-managed',
      }),
    ]);
    vi.mocked(getPermissionRules).mockResolvedValue([
      makeRule({
        id: 'rule-managed',
        scope_type: 'platform',
        scope_id: null,
        provider: 'mcp',
        server_id: 'server-1',
        tool_name: 'search',
        tool_ref: 'mcp:server-1:search',
        effect: 'ask',
        managed: true,
        created_by: 'admin',
      }),
    ]);
    vi.mocked(setPermissionSelection).mockResolvedValue(makeRule({ effect: 'deny' }));
    vi.mocked(setPermissionSelectionBatch).mockResolvedValue([makeRule({ effect: 'deny' })]);
    vi.mocked(clearPermissionSelection).mockResolvedValue(1);
  });

  it('按来源分 Tab 展示，并支持 Tab 内二级筛选', async () => {
    render(<ToolPermissionsPanel />);

    // 默认系统工具 Tab：仅内置工具可见
    expect(await screen.findByText('builtin:read_file')).toBeInTheDocument();
    expect(screen.getByText('builtin:write_file')).toBeInTheDocument();
    expect(screen.queryByText('mcp:server-1:search')).not.toBeInTheDocument();

    // 切到官方 MCP Tab
    fireEvent.click(screen.getByRole('button', { name: '来源 官方 MCP' }));
    expect(screen.getByText('mcp:server-1:search')).toBeInTheDocument();
    expect(screen.queryByText('builtin:read_file')).not.toBeInTheDocument();
    expect(screen.getByText('平台策略')).toBeInTheDocument();

    // 回到系统工具，二级筛选 DENY 隐藏 ALLOW 工具
    fireEvent.click(screen.getByRole('button', { name: '来源 系统工具' }));
    fireEvent.click(screen.getByRole('button', { name: '筛选 DENY' }));
    expect(screen.queryByText('builtin:read_file')).not.toBeInTheDocument();
    expect(screen.queryByText('builtin:write_file')).not.toBeInTheDocument();
  });

  it('显式设置策略统一调用 selection 接口', async () => {
    vi.mocked(getPermissionRules).mockResolvedValue([makeRule()]);
    render(<ToolPermissionsPanel />);

    const builtinRow = (await screen.findByText('builtin:read_file')).closest('section');
    expect(builtinRow).not.toBeNull();

    fireEvent.click(within(builtinRow as HTMLElement).getByRole('button', { name: 'DENY' }));
    await waitFor(() => expect(setPermissionSelection).toHaveBeenCalledWith({
      provider: 'builtin',
      server_id: null,
      tool_name: 'read_file',
      effect: 'deny',
    }));
  });

  it('多选后批量调用 selection/batch 接口', async () => {
    render(<ToolPermissionsPanel />);

    const readRow = (await screen.findByText('builtin:read_file')).closest('section');
    const writeRow = screen.getByText('builtin:write_file').closest('section');
    fireEvent.click(within(readRow as HTMLElement).getByRole('button', { name: '选择 builtin:read_file' }));
    fireEvent.click(within(writeRow as HTMLElement).getByRole('button', { name: '选择 builtin:write_file' }));

    expect(screen.getByText(/已选 2 项/)).toBeInTheDocument();
    const batchBar = screen.getByText('批量设为').closest('div');
    fireEvent.click(within(batchBar as HTMLElement).getByRole('button', { name: 'DENY' }));

    await waitFor(() => expect(setPermissionSelectionBatch).toHaveBeenCalledWith({
      effect: 'deny',
      items: [
        { provider: 'builtin', server_id: null, tool_name: 'read_file' },
        { provider: 'builtin', server_id: null, tool_name: 'write_file' },
      ],
    }));
  });

  it('批量遇到平台策略天花板时跳过并提示', async () => {
    const { rerender } = render(<ToolPermissionsPanel />);

    fireEvent.click(await screen.findByRole('button', { name: '来源 官方 MCP' }));
    const mcpRow = (await screen.findByText('mcp:server-1:search')).closest('section');
    fireEvent.click(within(mcpRow as HTMLElement).getByRole('button', { name: '选择 mcp:server-1:search' }));

    const batchBar = screen.getByText('批量设为').closest('div');
    fireEvent.click(within(batchBar as HTMLElement).getByRole('button', { name: 'ALLOW' }));

    await waitFor(() => expect(screen.getByText(/均被平台策略限制/)).toBeInTheDocument());
    expect(setPermissionSelectionBatch).not.toHaveBeenCalled();

    rerender(<ToolPermissionsPanel active={false} />);
    await waitFor(() => expect(screen.queryByText(/均被平台策略限制/)).not.toBeInTheDocument());
  });

  it('版本绑定标签仅在命中的用户条件规则时显示', async () => {
    vi.mocked(getPermissionTools).mockResolvedValue([
      makeTool({ matched_rule_id: 'schema-grant' }),
    ]);
    vi.mocked(getPermissionRules).mockResolvedValue([
      makeRule({
        id: 'schema-grant',
        conditions: { version: 1, schema_hash: 'schema-v1' },
      }),
    ]);
    render(<ToolPermissionsPanel />);

    const row = (await screen.findByText('builtin:read_file')).closest('section');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText('审批授权 · 绑定工具版本')).toBeInTheDocument();
  });

  it('条件规则未命中时不显示标签，即使旧规则仍存在', async () => {
    vi.mocked(getPermissionTools).mockResolvedValue([
      makeTool({ matched_rule_id: null }),
    ]);
    vi.mocked(getPermissionRules).mockResolvedValue([
      makeRule({
        id: 'stale-grant',
        conditions: { version: 1, schema_hash: 'schema-old' },
      }),
    ]);
    render(<ToolPermissionsPanel />);

    const row = (await screen.findByText('builtin:read_file')).closest('section');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).queryByText('审批授权 · 绑定工具版本')).not.toBeInTheDocument();
  });

  it('命中无条件规则时不显示标签', async () => {
    vi.mocked(getPermissionTools).mockResolvedValue([
      makeTool({ matched_rule_id: 'plain-rule', effect: 'deny' }),
    ]);
    vi.mocked(getPermissionRules).mockResolvedValue([
      makeRule({ id: 'plain-rule', effect: 'deny', conditions: null }),
    ]);
    render(<ToolPermissionsPanel />);

    const row = (await screen.findByText('builtin:read_file')).closest('section');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).queryByText('审批授权 · 绑定工具版本')).not.toBeInTheDocument();
  });

  it('恢复默认清理该工具的全部用户规则', async () => {
    vi.mocked(getPermissionRules).mockResolvedValue([
      makeRule({
        id: 'schema-grant',
        conditions: { schema_hash: 'schema-v1', connection_fingerprint: 'connection-v1' },
      }),
      makeRule({
        id: 'legacy-conditional-grant',
        conditions: { legacy_flag: true },
      }),
      makeRule({
        id: 'expired-unconditional-grant',
        conditions: null,
        expires_at: '2000-01-01T00:00:00Z',
      }),
    ]);
    render(<ToolPermissionsPanel />);

    const builtinRow = (await screen.findByText('builtin:read_file')).closest('section');
    expect(builtinRow).not.toBeNull();

    await waitFor(() => expect(within(builtinRow as HTMLElement).getByTitle('恢复默认策略')).not.toBeDisabled());
    fireEvent.click(within(builtinRow as HTMLElement).getByTitle('恢复默认策略'));
    // 单次原子清除接口替代多个独立 DELETE，避免部分删除脏状态。
    await waitFor(() => {
      expect(clearPermissionSelection).toHaveBeenCalledTimes(1);
      expect(clearPermissionSelection).toHaveBeenCalledWith({
        provider: 'builtin',
        server_id: null,
        tool_name: 'read_file',
      });
    });
  });

  it('refreshToken 变化时重新加载权限工具目录', async () => {
    const { rerender } = render(<ToolPermissionsPanel refreshToken={0} />);
    await screen.findByText('builtin:read_file');
    expect(getPermissionTools).toHaveBeenCalledTimes(1);

    vi.mocked(getPermissionTools).mockResolvedValue([
      makeTool({
        tool_ref: 'builtin:new_tool',
        tool_name: 'new_tool',
        title: 'new_tool',
      }),
    ]);
    rerender(<ToolPermissionsPanel refreshToken={1} />);

    expect(await screen.findByText('builtin:new_tool')).toBeInTheDocument();
    expect(getPermissionTools).toHaveBeenCalledTimes(2);
  });

  it('连接关闭刷新目录时清理已消失 MCP 工具的多选状态', async () => {
    const { rerender } = render(<ToolPermissionsPanel refreshToken={0} />);
    fireEvent.click(await screen.findByRole('button', { name: '来源 官方 MCP' }));
    const mcpRow = (await screen.findByText('mcp:server-1:search')).closest('section');
    fireEvent.click(within(mcpRow as HTMLElement).getByRole('button', {
      name: '选择 mcp:server-1:search',
    }));
    expect(screen.getByText(/已选 1 项/)).toBeInTheDocument();

    vi.mocked(getPermissionTools).mockResolvedValue([
      makeTool(),
      makeTool({
        tool_ref: 'builtin:write_file',
        tool_name: 'write_file',
        title: 'write_file',
      }),
    ]);
    rerender(<ToolPermissionsPanel refreshToken={1} />);

    await waitFor(() => {
      expect(screen.queryByText('mcp:server-1:search')).not.toBeInTheDocument();
      expect(screen.queryByText(/已选 1 项/)).not.toBeInTheDocument();
    });
  });
});
