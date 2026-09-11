import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
import { ToolItemView } from '../../components/ReasoningPanel';
import { ActivityDisclosureScope } from '../../components/useActivityDisclosure';
import type { ToolGroupItem } from '../../utils/displayBlocks';

const item: ToolGroupItem = { id: 'shell-1', toolName: 'bash', description: '运行命令', status: 'completed',
  input: { command: 'python inspect_slides.py' }, result: { content: 'done', success: true }, executionTimeMs: 2300 };
beforeEach(() => sessionStorage.clear());

describe('command tool details', () => {
  it('shows one recognizable line, retains full multiline data, and copies the exact original strings', async () => {
    const command = "python - <<'PY'\r\nprint('中文  spacing')\r\nPY\r\n";
    const output = Array.from({ length: 60 }, (_, index) => `result ${index}`).join('\r\n') + '\r\n';
    const writeText = vi.fn().mockRejectedValueOnce(new Error('clipboard denied')).mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const view = render(<ToolItemView item={{ ...item, input: { cmd: command }, result: { content: output, success: true } }} disableMotion />);
    const toggle = screen.getByRole('button', { name: "已执行 python - <<'PY' print('中文 spacing') PY" });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Shell')).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(document.getElementById(toggle.getAttribute('aria-controls')!)).toBeVisible();
    expect(view.container.querySelector('.chat-command-command code')?.textContent).toBe(command);
    expect(view.container.querySelector('.chat-command-output code')?.textContent).toBe(output);
    expect(view.container.querySelector('details')).toBeNull();
    expect(screen.queryByText('展开全部')).not.toBeInTheDocument();
    expect(screen.getByLabelText('命令与输出')).toHaveAttribute('tabindex', '0');

    fireEvent.click(screen.getByRole('button', { name: '复制命令' }));
    await screen.findByText('复制失败，请重试');
    fireEvent.click(screen.getByRole('button', { name: '复制命令' }));
    await screen.findByText('已复制');
    fireEvent.click(screen.getByRole('button', { name: '复制输出' }));
    await waitFor(() => expect(writeText.mock.calls).toEqual([[command], [command], [output]]));
  });

  it('keeps running, failure and unknown truthful as data arrives, including separate error text', () => {
    const view = render(<ToolItemView item={{ ...item, status: 'running', result: undefined }} disableMotion />);
    fireEvent.click(screen.getByRole('button', { name: '正在执行 python inspect_slides.py' }));
    expect(screen.getByText('等待命令输出…')).toBeVisible();
    view.rerender(<ToolItemView item={{ ...item, status: 'failed', result: { content: 'partial stdout', error: 'process exited 1', success: false } }} disableMotion />);
    expect(screen.getByRole('button', { name: '执行失败 python inspect_slides.py' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('partial stdout')).toBeVisible();
    expect(screen.getByText('process exited 1')).toBeVisible();
    view.rerender(<ToolItemView item={{ ...item, status: 'unknown', result: undefined }} disableMotion />);
    expect(screen.getByRole('button', { name: '结果未确认 python inspect_slides.py' })).toBeVisible();
    expect(screen.getByText('尚未收到输出')).toBeVisible();
    expect(screen.queryByText('已执行')).not.toBeInTheDocument();
  });

  it('retains output and process-control actions without inventing a command and restores the scoped disclosure', () => {
    const renderOutput = () => <ActivityDisclosureScope.Provider value="round-1"><ToolItemView item={{ ...item,
      toolName: 'bash_output', description: '读取命令输出', input: { bash_id: 'process-42' },
    }} disableMotion /></ActivityDisclosureScope.Provider>;
    const view = render(renderOutput());
    fireEvent.click(screen.getByRole('button', { name: '已执行 读取命令输出' }));
    expect(screen.getByRole('button', { name: '复制参数' })).toBeVisible();
    expect(screen.queryByRole('button', { name: '复制命令' })).not.toBeInTheDocument();
    view.unmount();
    const restored = render(renderOutput());
    expect(screen.getByRole('button', { name: '已执行 读取命令输出' })).toHaveAttribute('aria-expanded', 'true');
    restored.rerender(<ToolItemView item={{ ...item, toolName: 'bash_kill', description: '停止进程', input: { bash_id: 'process-42' } }} disableMotion />);
    expect(screen.getByRole('button', { name: '已执行 停止进程' })).toBeVisible();
  });

  it('keeps MCP tool identity on its existing path even if its real name is shell', () => {
    const view = render(<ToolItemView item={{ ...item, toolName: 'shell', description: '远程服务 · 运行检查',
      toolDisplay: { provider: 'mcp', server_name: '远程服务', tool_name: 'shell', tool_title: '运行检查' },
    }} disableMotion />);
    expect(screen.getByRole('button', { name: '远程服务 · 运行检查' })).toBeVisible();
    expect(view.container.querySelector('.chat-command-tool')).toBeNull();
  });
});
