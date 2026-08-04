import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import McpConnectionsPanel from '../../components/McpConnectionsPanel';
import {
  activateMcpServer,
  createMcpServer,
  deleteMcpServer,
  exportMcpConfig,
  getMcpServerTools,
  getMcpServers,
  importMcpConfig,
  testMcpServer,
  updateMcpConnection,
  updateMcpServer,
  updateMcpToolVisibility,
  type McpServer,
} from '../../services/mcpApi';

vi.mock('../../services/mcpApi', () => ({
  activateMcpServer: vi.fn(),
  createMcpServer: vi.fn(),
  deleteMcpServer: vi.fn(),
  exportMcpConfig: vi.fn(),
  getMcpServerTools: vi.fn(),
  getMcpServers: vi.fn(),
  importMcpConfig: vi.fn(),
  testMcpServer: vi.fn(),
  updateMcpConnection: vi.fn(),
  updateMcpServer: vi.fn(),
  updateMcpToolVisibility: vi.fn(),
}));

function makeServer(overrides: Partial<McpServer> = {}): McpServer {
  return {
    id: 'official-1',
    name: '官方知识库',
    description: '检索已审核的企业知识',
    url: 'https://official.example.com/mcp',
    source: 'official',
    status: 'published',
    enabled: true,
    required: false,
    auth_type: 'none',
    credential_set: false,
    header_names: [],
    allow_private_network: false,
    allow_insecure_http: false,
    installation_id: 'installation-1',
    tools_count: 2,
    enabled_tools_count: 2,
    enabled_tools: null,
    disabled_tools: [],
    last_tested_at: null,
    last_error: null,
    created_at: null,
    updated_at: null,
    version: 1,
    ...overrides,
  };
}

describe('McpConnectionsPanel', () => {
  const official = makeServer();
  const personal = makeServer({
    id: 'personal-1',
    name: '我的检索服务',
    source: 'personal',
    installation_id: 'installation-2',
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getMcpServers).mockResolvedValue([official, personal]);
    vi.mocked(updateMcpConnection).mockImplementation(async (_id, payload) => ({
      ...official,
      enabled: payload.enabled ?? official.enabled,
    }));
    vi.mocked(activateMcpServer).mockImplementation(async (serverId) => ({
      ...(serverId === personal.id ? personal : official),
      enabled: true,
    }));
    vi.mocked(createMcpServer).mockResolvedValue(personal);
    vi.mocked(updateMcpServer).mockResolvedValue(personal);
    vi.mocked(deleteMcpServer).mockResolvedValue();
    vi.mocked(testMcpServer).mockResolvedValue({ ok: true, tools_count: 2, latency_ms: 18, error: null });
    vi.mocked(importMcpConfig).mockResolvedValue({ imported: 0, servers: [], errors: [] });
    vi.mocked(exportMcpConfig).mockResolvedValue({ mcpServers: {} });
    const toolCatalog = {
      server_id: official.id,
      installation_id: official.installation_id,
      visibility_revision: 7,
      tools_count: 2,
      enabled_tools_count: 2,
      enabled_tools: null,
      disabled_tools: [] as string[],
      tools: [
        {
          name: 'search_documents',
          title: '搜索文档',
          description: '检索企业文档',
          schema_hash: 'schema-search',
          enabled: true,
          discovered_at: '2026-07-13T12:00:00',
        },
        {
          name: 'delete_document',
          title: '删除文档',
          description: '删除企业文档',
          schema_hash: 'schema-delete',
          enabled: true,
          discovered_at: '2026-07-13T12:00:00',
        },
      ],
    };
    vi.mocked(getMcpServerTools).mockResolvedValue(toolCatalog);
    vi.mocked(updateMcpToolVisibility).mockImplementation(async (_id, visibility) => ({
      ...toolCatalog,
      visibility_revision: visibility.expected_revision + 1,
      enabled_tools: visibility.enabled_tools,
      disabled_tools: visibility.disabled_tools,
      enabled_tools_count: toolCatalog.tools.length - visibility.disabled_tools.length,
      tools: toolCatalog.tools.map((tool) => ({
        ...tool,
        enabled: !visibility.disabled_tools.includes(tool.name),
      })),
    }));
  });

  it('按官方和个人来源分开展示连接', async () => {
    render(<McpConnectionsPanel />);

    expect(await screen.findByText('官方知识库')).toBeInTheDocument();
    expect(screen.queryByText('我的检索服务')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));

    expect(screen.getByText('我的检索服务')).toBeInTheDocument();
    expect(screen.queryByText('官方知识库')).not.toBeInTheDocument();
  });

  it('启停连接时乐观更新并写入 connection 端点', async () => {
    render(<McpConnectionsPanel />);
    const toggle = await screen.findByRole('switch', { name: '停用 官方知识库' });

    fireEvent.click(toggle);

    expect(screen.getByRole('switch', { name: '启用 官方知识库' })).toBeDisabled();
    await waitFor(() => {
      expect(updateMcpConnection).toHaveBeenCalledWith('official-1', { enabled: false });
      expect(screen.getByRole('switch', { name: '启用 官方知识库' })).not.toBeDisabled();
    });
    expect(testMcpServer).not.toHaveBeenCalled();
  });

  it('启用连接前自动发现工具，成功后再打开连接并刷新权限', async () => {
    const disabled = makeServer({
      enabled: false,
      installation_id: null,
      tools_count: 0,
      enabled_tools_count: 0,
    });
    let resolveActivation!: (server: McpServer) => void;
    vi.mocked(getMcpServers).mockResolvedValue([disabled]);
    vi.mocked(activateMcpServer).mockImplementationOnce(() => new Promise((resolve) => {
      resolveActivation = resolve;
    }));
    const onPermissionsInvalidated = vi.fn();
    render(<McpConnectionsPanel onPermissionsInvalidated={onPermissionsInvalidated} />);

    expect(await screen.findByText('未启用')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('switch', { name: '启用 官方知识库' }));

    expect(screen.getByText('正在连接并发现工具...')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: '启用 官方知识库' })).toBeDisabled();
    expect(updateMcpConnection).not.toHaveBeenCalled();

    await act(async () => resolveActivation(makeServer({
      enabled: true,
      tools_count: 2,
      enabled_tools_count: 2,
    })));
    await waitFor(() => {
      expect(activateMcpServer).toHaveBeenCalledWith('official-1');
      expect(screen.getByText('2/2 个工具已发布')).toBeInTheDocument();
      expect(onPermissionsInvalidated).toHaveBeenCalledTimes(1);
    });
    expect(testMcpServer).not.toHaveBeenCalled();
    expect(updateMcpConnection).not.toHaveBeenCalled();
  });

  it('原子激活失败时保持连接关闭且不提交普通启用', async () => {
    const disabled = makeServer({
      enabled: false,
      installation_id: null,
      tools_count: 0,
      enabled_tools_count: 0,
    });
    vi.mocked(getMcpServers).mockResolvedValue([disabled]);
    vi.mocked(activateMcpServer).mockRejectedValueOnce({
      response: { status: 409, data: { detail: '鉴权失败' } },
    });
    render(<McpConnectionsPanel />);

    fireEvent.click(await screen.findByRole('switch', { name: '启用 官方知识库' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('鉴权失败');
    expect(updateMcpConnection).not.toHaveBeenCalled();
    expect(testMcpServer).not.toHaveBeenCalled();
    expect(screen.getByRole('switch', { name: '启用 官方知识库' })).not.toBeDisabled();
    expect(screen.getByText('未启用')).toBeInTheDocument();
  });

  it('连接设置中的启用先保存为关闭，再交给原子激活端点', async () => {
    const disabled = makeServer({
      enabled: false,
      installation_id: 'installation-1',
      tools_count: 0,
      enabled_tools_count: 0,
    });
    vi.mocked(getMcpServers).mockResolvedValue([disabled]);
    vi.mocked(updateMcpConnection).mockResolvedValueOnce(disabled);
    vi.mocked(activateMcpServer).mockResolvedValueOnce(makeServer({ enabled: true }));
    render(<McpConnectionsPanel />);

    fireEvent.click(await screen.findByRole('button', { name: '配置 官方知识库' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '保存后启用此连接' }));
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    await waitFor(() => {
      expect(updateMcpConnection).toHaveBeenNthCalledWith(1, 'official-1', expect.objectContaining({
        enabled: false,
        auth_type: 'none',
      }));
      expect(activateMcpServer).toHaveBeenCalledWith('official-1');
      expect(screen.queryByRole('dialog', { name: '配置 官方知识库' })).not.toBeInTheDocument();
    });
    expect(vi.mocked(updateMcpConnection).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(activateMcpServer).mock.invocationCallOrder[0]);
    expect(testMcpServer).not.toHaveBeenCalled();
  });

  it('连接设置自动发现失败时保留已保存配置但保持关闭', async () => {
    const disabled = makeServer({ enabled: false, tools_count: 0, enabled_tools_count: 0 });
    vi.mocked(getMcpServers).mockResolvedValue([disabled]);
    vi.mocked(updateMcpConnection).mockResolvedValueOnce(disabled);
    vi.mocked(activateMcpServer).mockRejectedValueOnce({
      response: { status: 409, data: { detail: '鉴权失败' } },
    });
    render(<McpConnectionsPanel />);

    fireEvent.click(await screen.findByRole('button', { name: '配置 官方知识库' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '保存后启用此连接' }));
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('配置已保存但未启用：鉴权失败');
    expect(updateMcpConnection).toHaveBeenCalledTimes(1);
    expect(updateMcpConnection).not.toHaveBeenCalledWith('official-1', { enabled: true });
    expect(activateMcpServer).toHaveBeenCalledWith('official-1');
    expect(screen.getByRole('dialog', { name: '配置 官方知识库' })).toBeInTheDocument();
  });

  it('连接测试失败也刷新列表，避免 UI 显示旧状态', async () => {
    // 后端在测试失败时已写入 last_tested_at/last_error（甚至自动停用必需服务），
    // 因此无论 ok 与否都必须刷新，否则页面残留旧状态。
    vi.mocked(testMcpServer).mockResolvedValue({
      ok: false,
      tools_count: 0,
      latency_ms: 42,
      error: '连接被拒绝',
    });
    render(<McpConnectionsPanel />);

    await screen.findByText('官方知识库');
    expect(getMcpServers).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: '测试 官方知识库' }));

    await waitFor(() => {
      expect(testMcpServer).toHaveBeenCalledWith('official-1');
      // 失败后仍触发一次刷新（初始 1 次 + 失败刷新 1 次）。
      expect(getMcpServers).toHaveBeenCalledTimes(2);
    });
  });

  it('手动测试期间禁用同一卡片的启停开关', async () => {
    let resolveTest!: (result: {
      ok: boolean; tools_count: number; latency_ms: number; error: null;
    }) => void;
    vi.mocked(testMcpServer).mockImplementationOnce(() => new Promise((resolve) => {
      resolveTest = resolve;
    }));
    render(<McpConnectionsPanel />);
    const toggle = await screen.findByRole('switch', { name: '停用 官方知识库' });

    fireEvent.click(screen.getByRole('button', { name: '测试 官方知识库' }));

    expect(toggle).toBeDisabled();
    await act(async () => resolveTest({ ok: true, tools_count: 2, latency_ms: 10, error: null }));
    await waitFor(() => expect(toggle).not.toBeDisabled());
  });

  it('不同连接并发 mutation 丢弃旧刷新，并在全部结束后统一对账', async () => {
    const secondOfficial = makeServer({
      id: 'official-2',
      name: '第二知识库',
      installation_id: 'installation-3',
    });
    const reconciledSecond = {
      ...secondOfficial,
      name: '第二知识库已对账',
      enabled: false,
      last_tested_at: '2026-08-04T10:00:00',
    };
    let resolveTest!: (result: {
      ok: boolean; tools_count: number; latency_ms: number; error: null;
    }) => void;
    let resolveStaleLoad!: (servers: McpServer[]) => void;

    vi.mocked(getMcpServers)
      .mockReset()
      .mockResolvedValueOnce([official, secondOfficial])
      .mockImplementationOnce(() => new Promise((resolve) => { resolveStaleLoad = resolve; }))
      .mockResolvedValueOnce([official, reconciledSecond]);
    vi.mocked(testMcpServer).mockImplementationOnce(() => new Promise((resolve) => {
      resolveTest = resolve;
    }));
    vi.mocked(updateMcpConnection).mockResolvedValueOnce({
      ...secondOfficial,
      enabled: false,
    });

    render(<McpConnectionsPanel />);
    await screen.findByText('第二知识库');
    fireEvent.click(screen.getByRole('button', { name: '测试 官方知识库' }));
    await act(async () => resolveTest({ ok: true, tools_count: 2, latency_ms: 10, error: null }));
    await waitFor(() => expect(getMcpServers).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('switch', { name: '停用 第二知识库' }));
    await waitFor(() => {
      expect(updateMcpConnection).toHaveBeenCalledWith('official-2', { enabled: false });
      expect(screen.getByRole('switch', { name: '启用 第二知识库' })).not.toBeDisabled();
    });

    await act(async () => resolveStaleLoad([official, secondOfficial]));
    await waitFor(() => {
      expect(getMcpServers).toHaveBeenCalledTimes(3);
      expect(screen.getByText('第二知识库已对账')).toBeInTheDocument();
      expect(screen.getByRole('switch', { name: '启用 第二知识库已对账' })).toBeInTheDocument();
    });
  });

  it('平台必需连接在卡片和连接设置中都不能停用', async () => {
    vi.mocked(getMcpServers).mockResolvedValueOnce([
      makeServer({ required: true, enabled: true }),
    ]);
    render(<McpConnectionsPanel />);

    const toggle = await screen.findByRole('switch', { name: '停用 官方知识库' });
    expect(toggle).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '配置 官方知识库' }));

    const requiredCheckbox = screen.getByRole('checkbox', { name: '平台必需连接始终启用' });
    expect(requiredCheckbox).toBeChecked();
    expect(requiredCheckbox).toBeDisabled();
  });

  it('平台必需连接用原子激活验证并提交候选凭证', async () => {
    const requiredBearer = makeServer({
      required: true,
      enabled: true,
      auth_type: 'bearer',
      credential_set: true,
      header_names: ['Authorization'],
    });
    vi.mocked(getMcpServers).mockResolvedValueOnce([requiredBearer]);
    vi.mocked(activateMcpServer).mockResolvedValueOnce(requiredBearer);
    render(<McpConnectionsPanel />);

    fireEvent.click(await screen.findByRole('button', { name: '配置 官方知识库' }));
    fireEvent.change(screen.getByLabelText('Bearer Token'), { target: { value: 'candidate-token' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    await waitFor(() => {
      expect(activateMcpServer).toHaveBeenCalledWith('official-1', {
        auth_type: 'bearer',
        bearer_token: 'candidate-token',
      });
    });
    expect(updateMcpConnection).not.toHaveBeenCalled();
  });

  it('官方连接认证方式只读，并始终提交服务定义的 auth_type', async () => {
    const officialBearer = makeServer({
      auth_type: 'bearer',
      credential_set: true,
      header_names: ['Authorization'],
    });
    vi.mocked(getMcpServers).mockResolvedValue([officialBearer]);
    render(<McpConnectionsPanel />);

    fireEvent.click(await screen.findByRole('button', { name: '配置 官方知识库' }));

    expect(screen.queryByRole('combobox', { name: '认证方式' })).not.toBeInTheDocument();
    expect(screen.getByText('Bearer Token', { selector: '.mcp-user-readonly-url strong' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Bearer Token'), { target: { value: 'replacement-token' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    await waitFor(() => {
      expect(updateMcpConnection).toHaveBeenCalledWith('official-1', expect.objectContaining({
        auth_type: 'bearer',
        bearer_token: 'replacement-token',
      }));
    });
  });

  it('个人 MCP 表单只接受 HTTPS 并提交写入型凭证', async () => {
    const stagedPersonal = makeServer({
      ...personal,
      enabled: false,
      tools_count: 0,
      enabled_tools_count: 0,
    });
    vi.mocked(createMcpServer).mockResolvedValueOnce(stagedPersonal);
    vi.mocked(activateMcpServer).mockResolvedValueOnce(personal);
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    fireEvent.click(screen.getAllByRole('button', { name: '添加连接' })[0]);

    fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '财务数据' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'http://unsafe.example.com/mcp' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('个人 MCP 仅允许 HTTPS 地址');
    expect(createMcpServer).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'https://safe.example.com/mcp' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'bearer' } });
    fireEvent.change(screen.getByLabelText('Bearer Token'), { target: { value: 'secret-value' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    await waitFor(() => {
      expect(createMcpServer).toHaveBeenCalledWith(expect.objectContaining({
        name: '财务数据',
        url: 'https://safe.example.com/mcp',
        auth_type: 'bearer',
        bearer_token: 'secret-value',
        enabled: false,
      }));
      expect(activateMcpServer).toHaveBeenCalledWith('personal-1');
    });
    expect(testMcpServer).not.toHaveBeenCalled();
    expect(updateMcpServer).not.toHaveBeenCalled();
  });

  it('新个人 MCP 激活失败时明确保留为已保存但未启用的记录', async () => {
    const stagedPersonal = makeServer({
      ...personal,
      name: '暂存服务',
      enabled: false,
      tools_count: 0,
      enabled_tools_count: 0,
    });
    vi.mocked(createMcpServer).mockResolvedValueOnce(stagedPersonal);
    vi.mocked(activateMcpServer).mockRejectedValueOnce({
      response: { status: 409, data: { detail: '无法获取工具列表' } },
    });
    vi.mocked(getMcpServers).mockResolvedValueOnce([official]).mockResolvedValueOnce([
      official,
      stagedPersonal,
    ]);
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    fireEvent.click(screen.getAllByRole('button', { name: '添加连接' })[0]);
    fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '暂存服务' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), {
      target: { value: 'https://staged.example.com/mcp' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '配置已保存但未启用：无法获取工具列表',
    );
    expect(screen.getByRole('dialog', { name: '编辑个人 MCP' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: '保存后启用此连接' })).not.toBeChecked();
    expect(activateMcpServer).toHaveBeenCalledWith('personal-1');
    expect(testMcpServer).not.toHaveBeenCalled();
    expect(updateMcpServer).not.toHaveBeenCalledWith('personal-1', { enabled: true });
  });

  it('切换认证方式后个人 MCP 不携带残留 bearer_token', async () => {
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    fireEvent.click(screen.getByRole('button', { name: '添加连接' }));

    fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '财务数据' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'https://safe.example.com/mcp' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'bearer' } });
    fireEvent.change(screen.getByLabelText('Bearer Token'), { target: { value: 'stale-token' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'none' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    await waitFor(() => {
      expect(createMcpServer).toHaveBeenCalledTimes(1);
    });
    const payload = vi.mocked(createMcpServer).mock.calls[0][0];
    expect(payload).not.toHaveProperty('bearer_token');
    expect(payload.auth_type).toBe('none');
  });

  it('安全展示 FastAPI 422 校验错误且不渲染被拒绝的凭证输入', async () => {
    vi.mocked(createMcpServer).mockRejectedValueOnce({
      response: {
        data: {
          detail: [{
            type: 'value_error',
            loc: ['body'],
            msg: 'Value error, bearer_token 与 auth_type 不匹配',
            input: { bearer_token: 'must-not-render' },
          }],
        },
      },
    });
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    fireEvent.click(screen.getByRole('button', { name: '添加连接' }));
    fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '校验测试' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'https://safe.example.com/mcp' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('bearer_token 与 auth_type 不匹配');
    expect(alert).not.toHaveTextContent('must-not-render');
  });

  it('编辑表单变化会向设置中心上报 dirty 状态', async () => {
    const onDirtyChange = vi.fn();
    render(<McpConnectionsPanel onDirtyChange={onDirtyChange} />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    fireEvent.click(screen.getByRole('button', { name: '编辑 我的检索服务' }));

    fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '修改后的名称' } });

    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
  });

  it('origin 或认证方式变化后不承诺保留旧凭证，并要求重新输入', async () => {
    const securedPersonal = makeServer({
      id: 'personal-secured',
      name: '受保护服务',
      source: 'personal',
      auth_type: 'bearer',
      credential_set: true,
      header_names: ['Authorization'],
    });
    vi.mocked(getMcpServers).mockResolvedValue([securedPersonal]);
    render(<McpConnectionsPanel />);

    fireEvent.click(await screen.findByRole('tab', { name: /个人 MCP/ }));
    fireEvent.click(screen.getByRole('button', { name: '编辑 受保护服务' }));
    const urlInput = screen.getByLabelText('Streamable HTTP URL');
    fireEvent.change(urlInput, { target: { value: 'https://new-origin.example.com/mcp' } });

    expect(screen.getByRole('status')).toHaveTextContent('旧凭证将被清除');
    expect(screen.getByLabelText('Bearer Token')).toHaveAttribute('placeholder', '输入 Token');
    expect(screen.queryByRole('checkbox', { name: '清除已保存的凭证' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('请重新输入 Bearer Token');
    expect(updateMcpServer).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Bearer Token'), { target: { value: 'new-token' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));
    await waitFor(() => expect(updateMcpServer).toHaveBeenCalledWith('personal-secured', expect.objectContaining({
      url: 'https://new-origin.example.com/mcp',
      auth_type: 'bearer',
      bearer_token: 'new-token',
    })));
  });

  it('保存进行中禁止 X、取消和 Escape 关闭编辑器', async () => {
    let resolveUpdate: ((server: McpServer) => void) | null = null;
    vi.mocked(updateMcpServer).mockImplementationOnce(() => new Promise((resolve) => {
      resolveUpdate = resolve;
    }));
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    fireEvent.click(screen.getByRole('button', { name: '编辑 我的检索服务' }));
    fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '保存中的服务' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));

    const dialog = screen.getByRole('dialog', { name: '编辑个人 MCP' });
    expect(screen.getByRole('button', { name: '关闭 MCP 编辑' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '编辑个人 MCP' })).toBeInTheDocument();

    await act(async () => resolveUpdate?.(personal));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑个人 MCP' })).not.toBeInTheDocument());
  });

  it('最上层 MCP 弹窗独立处理 Escape 并恢复触发按钮焦点', async () => {
    render(<McpConnectionsPanel />);
    const trigger = await screen.findByRole('button', { name: '配置 官方知识库' });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: '配置 官方知识库' });

    await waitFor(() => expect(dialog).toHaveFocus());
    fireEvent.keyDown(dialog, { key: 'Escape' });

    expect(screen.queryByRole('dialog', { name: '配置 官方知识库' })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('弹窗容器获得焦点时 Shift+Tab 回绕到最后一个控件', async () => {
    render(<McpConnectionsPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '配置 官方知识库' }));
    const dialog = screen.getByRole('dialog', { name: '配置 官方知识库' });

    await waitFor(() => expect(dialog).toHaveFocus());
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });

    expect(screen.getByRole('button', { name: '保存连接' })).toHaveFocus();
  });

  it('MCP 发现或配置变化后通知权限页面失效', async () => {
    const onPermissionsInvalidated = vi.fn();
    render(<McpConnectionsPanel onPermissionsInvalidated={onPermissionsInvalidated} />);
    fireEvent.click(await screen.findByRole('button', { name: '测试 官方知识库' }));

    await waitFor(() => expect(onPermissionsInvalidated).toHaveBeenCalledTimes(1));
  });

  it('区分部分导入和全部导入失败的反馈级别', async () => {
    vi.mocked(importMcpConfig)
      .mockResolvedValueOnce({
        imported: 1,
        servers: [personal],
        errors: [{ name: '坏连接', error: 'URL 无效' }],
      })
      .mockResolvedValueOnce({
        imported: 0,
        servers: [],
        errors: [{ name: '坏连接', error: 'URL 无效' }],
      });
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    const fileInput = screen.getByLabelText('选择 mcp.json');
    const configFile = { text: vi.fn().mockResolvedValue('{"mcpServers":{}}') };

    fireEvent.change(fileInput, { target: { files: [configFile] } });
    const warning = await screen.findByRole('status');
    expect(warning).toHaveClass('warning');
    expect(warning).toHaveTextContent('已保存 1 个个人 MCP');
    expect(warning).toHaveTextContent('以下项目导入失败：坏连接: URL 无效');

    fireEvent.change(fileInput, { target: { files: [configFile] } });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('个人 MCP 导入失败');
    expect(screen.queryByText('已导入 0 个个人 MCP')).not.toBeInTheDocument();
  });

  it('导入激活失败时说明连接已保存但未启用', async () => {
    const stagedImport = makeServer({
      ...personal,
      name: '待修复连接',
      enabled: false,
      tools_count: 0,
      enabled_tools_count: 0,
    });
    vi.mocked(importMcpConfig).mockResolvedValueOnce({
      imported: 1,
      servers: [stagedImport],
      errors: [{ name: '待修复连接', error: '鉴权失败' }],
    });
    vi.mocked(getMcpServers).mockResolvedValueOnce([official]).mockResolvedValueOnce([
      official,
      stagedImport,
    ]);
    const { rerender } = render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('tab', { name: /个人 MCP/ }));
    const configFile = { text: vi.fn().mockResolvedValue('{"mcpServers":{}}') };

    fireEvent.change(screen.getByLabelText('选择 mcp.json'), { target: { files: [configFile] } });

    const warning = await screen.findByRole('status');
    expect(warning).toHaveClass('warning');
    expect(warning).toHaveTextContent('以下项目已保存但未启用：待修复连接: 鉴权失败');
    expect(await screen.findByText('待修复连接')).toBeInTheDocument();

    rerender(<McpConnectionsPanel active={false} />);
    await waitFor(() => expect(screen.queryByText(/以下项目已保存但未启用/)).not.toBeInTheDocument());
  });

  it('工具发布启停与 ALLOW/ASK/DENY 分开展示并完整替换配置', async () => {
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');

    fireEvent.click(screen.getByRole('button', { name: '管理 官方知识库 的工具' }));
    const toggle = await screen.findByRole('switch', { name: '停用工具 删除文档' });
    expect(screen.getByText(/ALLOW \/ ASK \/ DENY/)).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.getByRole('switch', { name: '启用工具 删除文档' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存工具设置' }));

    await waitFor(() => {
      expect(updateMcpToolVisibility).toHaveBeenCalledWith('official-1', {
        expected_revision: 7,
        enabled_tools: null,
        disabled_tools: ['delete_document'],
      });
    });
  });

  it('visibility revision 冲突后加载最新设置并使用新 revision 重试', async () => {
    vi.mocked(updateMcpToolVisibility).mockRejectedValueOnce({
      response: { status: 409, data: { detail: '发布设置已被其他操作修改' } },
    });
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');
    fireEvent.click(screen.getByRole('button', { name: '管理 官方知识库 的工具' }));
    const deleteToggle = await screen.findByRole('switch', { name: '停用工具 删除文档' });

    vi.mocked(getMcpServerTools).mockResolvedValueOnce({
      server_id: official.id,
      installation_id: official.installation_id,
      visibility_revision: 8,
      tools_count: 2,
      enabled_tools_count: 1,
      enabled_tools: null,
      disabled_tools: ['search_documents'],
      tools: [
        {
          name: 'search_documents', title: '搜索文档', description: '检索企业文档',
          schema_hash: 'schema-search', enabled: false, discovered_at: '2026-07-13T12:00:00',
        },
        {
          name: 'delete_document', title: '删除文档', description: '删除企业文档',
          schema_hash: 'schema-delete', enabled: true, discovered_at: '2026-07-13T12:00:00',
        },
      ],
    });
    fireEvent.click(deleteToggle);
    fireEvent.click(screen.getByRole('button', { name: '保存工具设置' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '发布设置已被其他操作修改；已加载最新设置，请重新修改',
    );
    expect(screen.getByRole('button', { name: '保存工具设置' })).toBeDisabled();

    fireEvent.click(screen.getByRole('switch', { name: '停用工具 删除文档' }));
    fireEvent.click(screen.getByRole('button', { name: '保存工具设置' }));
    await waitFor(() => {
      expect(updateMcpToolVisibility).toHaveBeenLastCalledWith('official-1', {
        expected_revision: 8,
        enabled_tools: null,
        disabled_tools: ['delete_document', 'search_documents'],
      });
    });
  });

  it('可显式切换自动发布和 allowlist 模式', async () => {
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');

    fireEvent.click(screen.getByRole('button', { name: '管理 官方知识库 的工具' }));
    const automatic = await screen.findByRole('radio', { name: /自动发布/ });
    const allowlist = screen.getByRole('radio', { name: /仅发布允许列表/ });
    expect(automatic).toBeChecked();

    fireEvent.click(allowlist);
    expect(allowlist).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: '保存工具设置' }));

    await waitFor(() => {
      expect(updateMcpToolVisibility).toHaveBeenCalledWith('official-1', {
        expected_revision: 7,
        enabled_tools: ['delete_document', 'search_documents'],
        disabled_tools: [],
      });
    });
  });

  it('编辑 allowlist 时保留未发现的发布和禁用名称', async () => {
    vi.mocked(getMcpServerTools).mockResolvedValueOnce({
      server_id: official.id,
      installation_id: official.installation_id,
      visibility_revision: 12,
      tools_count: 2,
      enabled_tools_count: 1,
      enabled_tools: ['search_documents', 'future_reader'],
      disabled_tools: ['future_writer'],
      tools: [
        {
          name: 'search_documents', title: '搜索文档', description: null,
          schema_hash: 'schema-search', enabled: true, discovered_at: null,
        },
        {
          name: 'delete_document', title: '删除文档', description: null,
          schema_hash: 'schema-delete', enabled: false, discovered_at: null,
        },
      ],
    });
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');

    fireEvent.click(screen.getByRole('button', { name: '管理 官方知识库 的工具' }));
    fireEvent.click(await screen.findByRole('switch', { name: '启用工具 删除文档' }));
    fireEvent.click(screen.getByRole('button', { name: '保存工具设置' }));

    await waitFor(() => {
      expect(updateMcpToolVisibility).toHaveBeenCalledWith('official-1', {
        expected_revision: 12,
        enabled_tools: ['delete_document', 'future_reader', 'search_documents'],
        disabled_tools: ['future_writer'],
      });
    });
  });

  it('展示并可移除当前快照未知的发布规则', async () => {
    vi.mocked(getMcpServerTools).mockResolvedValueOnce({
      server_id: official.id,
      installation_id: official.installation_id,
      visibility_revision: 18,
      tools_count: 1,
      enabled_tools_count: 1,
      enabled_tools: ['search_documents', 'future_reader'],
      disabled_tools: ['future_writer'],
      tools: [{
        name: 'search_documents', title: '搜索文档', description: null,
        schema_hash: 'schema-search', enabled: true, discovered_at: null,
      }],
    });
    render(<McpConnectionsPanel />);
    await screen.findByText('官方知识库');

    fireEvent.click(screen.getByRole('button', { name: '管理 官方知识库 的工具' }));
    expect(await screen.findByText('future_reader')).toBeInTheDocument();
    expect(screen.getByText('future_writer')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '移除未知允许规则 future_reader' }));
    fireEvent.click(screen.getByRole('button', { name: '移除未知停用规则 future_writer' }));
    fireEvent.click(screen.getByRole('button', { name: '保存工具设置' }));

    await waitFor(() => {
      expect(updateMcpToolVisibility).toHaveBeenCalledWith('official-1', {
        expected_revision: 18,
        enabled_tools: ['search_documents'],
        disabled_tools: [],
      });
    });
  });
});
