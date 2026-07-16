import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import AdminMcpCatalogPanel from '../../components/AdminMcpCatalogPanel';
import {
  createAdminMcpServer,
  deleteAdminMcpServer,
  getAdminMcpServers,
  testAdminMcpServer,
  updateAdminMcpServer,
  type McpServer,
} from '../../services/mcpApi';

vi.mock('../../services/mcpApi', () => ({
  createAdminMcpServer: vi.fn(),
  deleteAdminMcpServer: vi.fn(),
  getAdminMcpServers: vi.fn(),
  testAdminMcpServer: vi.fn(),
  updateAdminMcpServer: vi.fn(),
}));

const server: McpServer = {
  id: 'official-1',
  name: '内部知识库',
  description: '公司知识检索',
  url: 'https://mcp.internal.example/mcp',
  source: 'official',
  status: 'published',
  enabled: true,
  required: false,
  auth_type: 'bearer',
  credential_set: true,
  header_names: ['Authorization'],
  allow_private_network: false,
  allow_insecure_http: false,
  installation_id: null,
  tools_count: 6,
  enabled_tools_count: 6,
  enabled_tools: null,
  disabled_tools: [],
  last_tested_at: '2026-07-13T12:00:00',
  last_error: null,
  created_at: null,
  updated_at: null,
  version: 1,
};

describe('AdminMcpCatalogPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAdminMcpServers).mockResolvedValue([server]);
    vi.mocked(createAdminMcpServer).mockResolvedValue(server);
    vi.mocked(updateAdminMcpServer).mockResolvedValue(server);
    vi.mocked(deleteAdminMcpServer).mockResolvedValue();
    vi.mocked(testAdminMcpServer).mockResolvedValue({
      ok: true,
      tools_count: 6,
      latency_ms: 25,
      error: null,
    });
  });

  it('展示官方目录并可测试连接', async () => {
    render(<AdminMcpCatalogPanel />);

    expect(await screen.findByText('内部知识库')).toBeInTheDocument();
    expect(screen.getByText('6 个工具')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '测试 内部知识库' }));

    await waitFor(() => {
      expect(testAdminMcpServer).toHaveBeenCalledWith('official-1');
      expect(screen.getByRole('status')).toHaveTextContent('连接成功 · 6 个工具 · 25 ms');
    });
  });

  it('并发操作刷新时只接收最后一次列表响应', async () => {
    const secondServer = { ...server, id: 'official-2', name: '第二知识库' };
    let resolveFirstTest!: (result: {
      ok: boolean; tools_count: number; latency_ms: number; error: null;
    }) => void;
    let resolveSecondTest!: (result: {
      ok: boolean; tools_count: number; latency_ms: number; error: null;
    }) => void;
    let resolveStaleLoad!: (servers: McpServer[]) => void;
    let resolveLatestLoad!: (servers: McpServer[]) => void;

    vi.mocked(getAdminMcpServers)
      .mockReset()
      .mockResolvedValueOnce([server, secondServer])
      .mockImplementationOnce(() => new Promise((resolve) => { resolveStaleLoad = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveLatestLoad = resolve; }));
    vi.mocked(testAdminMcpServer).mockImplementation((serverId) => new Promise((resolve) => {
      if (serverId === server.id) resolveFirstTest = resolve;
      else resolveSecondTest = resolve;
    }));

    render(<AdminMcpCatalogPanel />);
    await screen.findByText('第二知识库');
    fireEvent.click(screen.getByRole('button', { name: '测试 内部知识库' }));
    fireEvent.click(screen.getByRole('button', { name: '测试 第二知识库' }));

    await act(async () => resolveFirstTest({ ok: true, tools_count: 6, latency_ms: 10, error: null }));
    await waitFor(() => expect(getAdminMcpServers).toHaveBeenCalledTimes(2));
    await act(async () => resolveSecondTest({ ok: true, tools_count: 6, latency_ms: 11, error: null }));
    await waitFor(() => expect(getAdminMcpServers).toHaveBeenCalledTimes(3));

    await act(async () => resolveLatestLoad([{ ...server, name: '最新目录' }]));
    expect(await screen.findByText('最新目录')).toBeInTheDocument();

    await act(async () => resolveStaleLoad([{ ...server, name: '过期目录' }]));
    expect(screen.getByText('最新目录')).toBeInTheDocument();
    expect(screen.queryByText('过期目录')).not.toBeInTheDocument();
  });

  it('平台必需 MCP 必须先保存草稿，且提交认证和网络边界', async () => {
    render(<AdminMcpCatalogPanel />);
    await screen.findByText('内部知识库');
    fireEvent.click(screen.getByRole('button', { name: '新增官方 MCP' }));

    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '行情服务' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'https://market.example.com/mcp' } });
    fireEvent.change(screen.getByLabelText('发布状态'), { target: { value: 'published' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'headers' } });
    fireEvent.change(screen.getByLabelText('请求头 JSON'), { target: { value: '{"X-API-Key":"secret"}' } });
    fireEvent.click(screen.getByLabelText('平台必需（用户不可停用）'));
    fireEvent.click(screen.getByLabelText('允许访问私网地址'));
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('先保存为草稿');
    expect(createAdminMcpServer).not.toHaveBeenCalled();
    expect(screen.getByText(/修改 URL、认证、凭证或网络边界后需重新测试/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('发布状态'), { target: { value: 'draft' } });
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));

    await waitFor(() => {
      expect(createAdminMcpServer).toHaveBeenCalledWith({
        name: '行情服务',
        description: null,
        url: 'https://market.example.com/mcp',
        status: 'draft',
        auth_type: 'headers',
        headers: { 'X-API-Key': 'secret' },
        allow_private_network: true,
        allow_insecure_http: false,
        required: true,
      });
    });
  });

  it('HTTP 官方地址必须显式开启不安全 HTTP', async () => {
    render(<AdminMcpCatalogPanel />);
    await screen.findByText('内部知识库');
    fireEvent.click(screen.getByRole('button', { name: '新增官方 MCP' }));
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '内部服务' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'http://mcp.internal/mcp' } });
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('允许不安全 HTTP');
    expect(createAdminMcpServer).not.toHaveBeenCalled();
  });

  it('安全展示 FastAPI 422 校验错误且不渲染被拒绝的凭证输入', async () => {
    vi.mocked(createAdminMcpServer).mockRejectedValueOnce({
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
    render(<AdminMcpCatalogPanel />);
    await screen.findByText('内部知识库');
    fireEvent.click(screen.getByRole('button', { name: '新增官方 MCP' }));
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '校验测试' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'https://safe.example.com/mcp' } });
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('bearer_token 与 auth_type 不匹配');
    expect(alert).not.toHaveTextContent('must-not-render');
  });

  it('切换认证方式后不携带残留 bearer_token', async () => {
    render(<AdminMcpCatalogPanel />);
    await screen.findByText('内部知识库');
    fireEvent.click(screen.getByRole('button', { name: '新增官方 MCP' }));

    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '切换服务' } });
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), { target: { value: 'https://switch.example.com/mcp' } });
    fireEvent.change(screen.getByLabelText('发布状态'), { target: { value: 'draft' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'bearer' } });
    fireEvent.change(screen.getByLabelText('Bearer Token'), { target: { value: 'stale-token' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'headers' } });
    fireEvent.change(screen.getByLabelText('请求头 JSON'), { target: { value: '{"X-API-Key":"secret"}' } });
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));

    await waitFor(() => {
      expect(createAdminMcpServer).toHaveBeenCalledTimes(1);
    });
    const payload = vi.mocked(createAdminMcpServer).mock.calls[0][0];
    expect(payload).not.toHaveProperty('bearer_token');
    expect(payload.auth_type).toBe('headers');
    expect(payload.headers).toEqual({ 'X-API-Key': 'secret' });
  });

  it('编辑脏表单时关闭需要确认，并向 AdminConsole 上报 dirty', async () => {
    const onDirtyChange = vi.fn();
    render(<AdminMcpCatalogPanel onDirtyChange={onDirtyChange} />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑 内部知识库' }));
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '修改中的知识库' } });
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));

    fireEvent.click(screen.getByRole('button', { name: '关闭官方 MCP 编辑' }));
    expect(screen.getByRole('alertdialog', { name: '放弃未保存的官方 MCP 修改？' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(screen.getByRole('dialog', { name: '编辑服务定义' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '关闭官方 MCP 编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '放弃修改' }));
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
  });

  it('连接 origin 或认证变化时要求重新输入凭证', async () => {
    render(<AdminMcpCatalogPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑 内部知识库' }));
    fireEvent.change(screen.getByLabelText('Streamable HTTP URL'), {
      target: { value: 'https://new-origin.example.com/mcp' },
    });

    expect(screen.getByRole('status')).toHaveTextContent('旧凭证将被清除');
    expect(screen.getByLabelText('Bearer Token')).toHaveAttribute('placeholder', '输入 Token');
    expect(screen.queryByRole('checkbox', { name: '清除已保存凭证' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('请重新输入 Bearer Token');
    expect(updateAdminMcpServer).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Bearer Token'), { target: { value: 'replacement' } });
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));
    await waitFor(() => expect(updateAdminMcpServer).toHaveBeenCalledWith('official-1', expect.objectContaining({
      url: 'https://new-origin.example.com/mcp',
      auth_type: 'bearer',
      bearer_token: 'replacement',
    })));
  });

  it('同一服务操作互斥，先结束的请求不会提前解除 busy', async () => {
    let resolveTest: ((result: { ok: boolean; tools_count: number; latency_ms: number; error: null }) => void) | null = null;
    vi.mocked(testAdminMcpServer).mockImplementationOnce(() => new Promise((resolve) => {
      resolveTest = resolve;
    }));
    render(<AdminMcpCatalogPanel />);
    const testButton = await screen.findByRole('button', { name: '测试 内部知识库' });
    fireEvent.click(testButton);

    expect(testButton).toBeDisabled();
    expect(screen.getByRole('button', { name: '编辑 内部知识库' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '删除 内部知识库' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '停用' })).toBeDisabled();
    fireEvent.click(testButton);
    expect(testAdminMcpServer).toHaveBeenCalledTimes(1);

    await act(async () => resolveTest?.({ ok: true, tools_count: 6, latency_ms: 20, error: null }));
    await waitFor(() => expect(testButton).not.toBeDisabled());
  });

  it('保存进行中禁止关闭或放弃 Admin MCP 编辑器', async () => {
    let resolveUpdate: ((value: McpServer) => void) | null = null;
    vi.mocked(updateAdminMcpServer).mockImplementationOnce(() => new Promise((resolve) => {
      resolveUpdate = resolve;
    }));
    render(<AdminMcpCatalogPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑 内部知识库' }));
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '保存中的服务' } });
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }));

    const dialog = screen.getByRole('dialog', { name: '编辑服务定义' });
    expect(screen.getByRole('button', { name: '关闭官方 MCP 编辑' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '编辑服务定义' })).toBeInTheDocument();

    await act(async () => resolveUpdate?.(server));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑服务定义' })).not.toBeInTheDocument());
  });

  it('抽屉容器获得焦点时 Shift+Tab 回绕到最后一个控件', async () => {
    render(<AdminMcpCatalogPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑 内部知识库' }));
    const dialog = screen.getByRole('dialog', { name: '编辑服务定义' });

    await waitFor(() => expect(dialog).toHaveFocus());
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });

    expect(screen.getByRole('button', { name: '保存服务' })).toHaveFocus();
  });
});
