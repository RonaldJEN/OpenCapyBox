import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { flushSync } from 'react-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../services/configApi', () => ({ getSkills: vi.fn() }));
vi.mock('../../services/mcpApi', () => ({ getMcpServers: vi.fn() }));

import { ChatInput } from '../../components/ChatInput';
import { getMcpServers } from '../../services/mcpApi';

const server = (overrides: Record<string, unknown> = {}) => ({
  id: 'server-a',
  name: '东方财富数据',
  description: '查询金融市场实时数据',
  url: 'https://example.com/mcp',
  source: 'official' as const,
  status: 'published' as const,
  enabled: true,
  required: false,
  auth_type: 'none' as const,
  credential_set: false,
  header_names: [],
  allow_private_network: false,
  allow_insecure_http: false,
  installation_id: 'installation-a',
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
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function Harness() {
  const [connections, setConnections] = useState<Array<{
    server_id: string;
    display_name: string;
  }>>([]);
  return (
    <ChatInput
      value="hello"
      onChange={() => undefined}
      onSend={() => undefined}
      onFileUpload={() => undefined}
      selectedSkillKeys={[]}
      onSelectedSkillKeysChange={() => undefined}
      selectedMcpConnections={connections}
      onSelectedMcpConnectionsChange={setConnections}
    />
  );
}

async function openMcpPicker(user: ReturnType<typeof userEvent.setup>) {
  void user;
  flushSync(() => {
    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
  });
  fireEvent.click(screen.getByRole('menuitem', { name: /数据连接/ }));
  await act(async () => Promise.resolve());
}

describe('ChatInput MCP connection preferences', () => {
  beforeEach(() => {
    vi.mocked(getMcpServers).mockReset();
    vi.mocked(getMcpServers).mockResolvedValue([
      server(),
      server({ id: 'disabled', name: '已停用', enabled: false }),
      server({ id: 'empty', name: '无工具', enabled_tools_count: 0 }),
      server({ id: 'not-installed', name: '未安装', installation_id: null }),
      server({ id: 'personal', name: '内部数据', source: 'personal' }),
    ]);
  });

  it('groups upload, Skills and data connections under one add menu', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    expect(getMcpServers).not.toHaveBeenCalled();
    const trigger = screen.getByRole('button', { name: '添加内容' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    await act(async () => { await user.click(trigger); });
    expect(screen.getByRole('menuitem', { name: '上传文件' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /专家 Skills/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /数据连接/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: '上传文件' })).toHaveFocus();
    await act(async () => { await user.keyboard('{ArrowDown}'); });
    expect(screen.getByRole('menuitem', { name: /专家 Skills/ })).toHaveFocus();
    await act(async () => { await user.keyboard('{End}'); });
    expect(screen.getByRole('menuitem', { name: /数据连接/ })).toHaveFocus();
    await act(async () => { await user.tab(); });
    await waitFor(() => expect(trigger).toHaveAttribute('aria-expanded', 'false'));
    expect(getMcpServers).not.toHaveBeenCalled();
  });

  it('lazy loads usable connections, searches, selects and removes a stable server id', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await openMcpPicker(user);
    expect(screen.getByRole('dialog', { name: '本轮优先数据连接' })).toBeInTheDocument();
    expect(screen.getByLabelText('搜索数据连接')).toBeInTheDocument();
    expect(await screen.findByText('东方财富数据')).toBeInTheDocument();
    expect(screen.queryByText('已停用')).not.toBeInTheDocument();
    expect(screen.queryByText('无工具')).not.toBeInTheDocument();
    expect(screen.queryByText('未安装')).not.toBeInTheDocument();
    expect(getMcpServers).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByPlaceholderText('搜索连接名称或说明'), {
      target: { value: '内部' },
    });
    expect(await screen.findByText('内部数据')).toBeInTheDocument();
    expect(screen.queryByText('东方财富数据')).not.toBeInTheDocument();

    await act(async () => {
      await user.click(screen.getByRole('button', { name: /内部数据/ }));
    });
    expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('内部数据');
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '移除数据连接 内部数据' }));
    });
    expect(screen.queryByLabelText('已选择本轮偏好')).not.toBeInTheDocument();
  });

  it('Escape closes the picker and restores focus to the add trigger', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByRole('button', { name: '添加内容' });

    await openMcpPicker(user);
    await screen.findByText('东方财富数据');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByPlaceholderText('搜索连接名称或说明')).not.toBeInTheDocument();
  });

  it('keeps an explicit initial error until retry succeeds', async () => {
    vi.mocked(getMcpServers)
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce([server()]);
    const user = userEvent.setup();
    render(<Harness />);

    await openMcpPicker(user);
    expect(await screen.findByText('数据连接加载失败')).toBeInTheDocument();
    expect(getMcpServers).toHaveBeenCalledTimes(1);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '重新加载' }));
    });

    expect(await screen.findByText('东方财富数据')).toBeInTheDocument();
    expect(getMcpServers).toHaveBeenCalledTimes(2);
  });

  it('closes an open picker when the composer becomes disabled', async () => {
    const props = {
      value: 'hello',
      onChange: () => undefined,
      onSend: () => undefined,
      selectedMcpConnections: [],
      onSelectedMcpConnectionsChange: () => undefined,
    };
    const user = userEvent.setup();
    const view = render(<ChatInput {...props} />);

    await openMcpPicker(user);
    expect(await screen.findByRole('dialog', { name: '本轮优先数据连接' })).toBeInTheDocument();
    view.rerender(<ChatInput {...props} disabled />);

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '本轮优先数据连接' }))
        .not.toBeInTheDocument();
    });
  });

  it('disables new MCP selections at the 20-item limit', async () => {
    const user = userEvent.setup();
    render(
      <ChatInput
        value="hello"
        onChange={() => undefined}
        onSend={() => undefined}
        selectedMcpConnections={Array.from({ length: 20 }, (_, index) => ({
          server_id: `selected-${index}`,
          display_name: `已选连接 ${index}`,
        }))}
        onSelectedMcpConnectionsChange={() => undefined}
      />,
    );

    await openMcpPicker(user);
    const option = await screen.findByRole('button', { name: /东方财富数据/ });
    expect(option).toBeDisabled();
  });

  it('keeps the loaded list when a background refresh fails', async () => {
    vi.mocked(getMcpServers)
      .mockResolvedValueOnce([server()])
      .mockRejectedValueOnce(new Error('refresh failed'));
    const user = userEvent.setup();
    render(<Harness />);

    await openMcpPicker(user);
    expect(await screen.findByText('东方财富数据')).toBeInTheDocument();
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '添加内容' }));
    });
    await openMcpPicker(user);

    expect(await screen.findByText('数据连接刷新失败，已显示上次结果')).toBeInTheDocument();
    expect(screen.getByText('东方财富数据')).toBeInTheDocument();
  });

  it('reuses an in-flight request when the picker is reopened', async () => {
    const initial = deferred<ReturnType<typeof server>[]>();
    vi.mocked(getMcpServers).mockReturnValue(initial.promise);
    const user = userEvent.setup();
    render(<Harness />);

    await openMcpPicker(user);
    expect(screen.getByText('加载中')).toBeInTheDocument();
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '添加内容' }));
    });
    await openMcpPicker(user);
    expect(getMcpServers).toHaveBeenCalledTimes(1);

    await act(async () => {
      initial.resolve([server()]);
      await initial.promise;
    });
    expect(await screen.findByText('东方财富数据')).toBeInTheDocument();
  });
});
