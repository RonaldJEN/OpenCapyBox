import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const enabledSkillsResponse = {
  sandbox_status: 'available' as const,
  skills: [
    { key: 'pdf', name: 'pdf', display_name: 'PDF 处理', description: '读取和生成 PDF', category: 'document', source: 'official' as const, enabled: true },
    { key: 'data_analysis', name: 'data_analysis', display_name: '数据分析', description: '分析表格', category: 'general', source: 'official' as const, enabled: true },
    { key: 'disabled', name: 'disabled', display_name: '已禁用', description: '不可选', category: 'general', source: 'official' as const, enabled: false },
  ],
};

vi.mock('../../services/configApi', () => ({
  getSkills: vi.fn(),
}));

import { ChatInput } from '../../components/ChatInput';
import { getSkills } from '../../services/configApi';

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
  const [keys, setKeys] = useState<string[]>([]);
  return (
    <ChatInput
      value="hello"
      onChange={() => undefined}
      onSend={() => undefined}
      selectedSkillKeys={keys}
      onSelectedSkillKeysChange={setKeys}
    />
  );
}

describe('ChatInput preferred skills', () => {
  beforeEach(() => {
    vi.mocked(getSkills).mockReset();
    vi.mocked(getSkills).mockResolvedValue(enabledSkillsResponse);
  });

  it('lazy loads enabled skills, searches, selects and renders removable tags', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    expect(screen.queryByText('PDF 处理')).not.toBeInTheDocument();
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '选择本轮 Skill' }));
    });
    await screen.findByText('PDF 处理');
    expect(screen.queryByText('已禁用')).not.toBeInTheDocument();

    act(() => {
      fireEvent.change(screen.getByPlaceholderText('搜索名称、key 或描述'), {
        target: { value: 'data_analysis' },
      });
    });
    await waitFor(() => expect(screen.getByText('数据分析')).toBeInTheDocument());
    expect(screen.queryByText('PDF 处理')).not.toBeInTheDocument();
    const dataAnalysisButton = screen.getByText('数据分析').closest('button');
    expect(dataAnalysisButton).toHaveAttribute('aria-pressed', 'false');

    await act(async () => {
      await user.click(dataAnalysisButton!);
    });
    expect(dataAnalysisButton).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('已选择 Skill')).toHaveTextContent('数据分析');
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '移除 Skill 数据分析' }));
    });
    expect(screen.queryByLabelText('已选择 Skill')).not.toBeInTheDocument();
  });

  it('加载失败后保持错误态且不在打开期间自动重试', async () => {
    vi.mocked(getSkills).mockRejectedValue(new Error('network error'));
    const user = userEvent.setup();
    render(<Harness />);

    await act(async () => {
      await user.click(screen.getByRole('button', { name: '选择本轮 Skill' }));
    });

    expect(await screen.findByText('Skill 列表加载失败')).toBeInTheDocument();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getSkills).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Skill 列表加载失败')).toBeInTheDocument();
  });

  it('加载失败后关闭再打开会显式重试', async () => {
    vi.mocked(getSkills)
      .mockRejectedValueOnce(new Error('network error'))
      .mockResolvedValueOnce(enabledSkillsResponse);
    const user = userEvent.setup();
    render(<Harness />);
    const pickerButton = screen.getByRole('button', { name: '选择本轮 Skill' });

    await act(async () => {
      await user.click(pickerButton);
    });
    expect(await screen.findByText('Skill 列表加载失败')).toBeInTheDocument();
    expect(getSkills).toHaveBeenCalledTimes(1);

    await act(async () => {
      await user.click(pickerButton);
    });
    await act(async () => {
      await user.click(pickerButton);
    });

    expect(await screen.findByText('PDF 处理')).toBeInTheDocument();
    expect(getSkills).toHaveBeenCalledTimes(2);
  });

  it('重新打开时立即显示旧清单、后台刷新并清空上次搜索', async () => {
    const refresh = deferred<typeof enabledSkillsResponse>();
    vi.mocked(getSkills)
      .mockResolvedValueOnce(enabledSkillsResponse)
      .mockReturnValueOnce(refresh.promise);
    const user = userEvent.setup();
    render(<Harness />);
    const pickerButton = screen.getByRole('button', { name: '选择本轮 Skill' });

    await act(async () => {
      await user.click(pickerButton);
    });
    expect(await screen.findByText('PDF 处理')).toBeInTheDocument();
    act(() => {
      fireEvent.change(screen.getByPlaceholderText('搜索名称、key 或描述'), {
        target: { value: 'data_analysis' },
      });
    });
    expect(screen.queryByText('PDF 处理')).not.toBeInTheDocument();

    await act(async () => {
      await user.click(pickerButton);
    });
    await act(async () => {
      await user.click(pickerButton);
    });

    expect(screen.getByText('PDF 处理')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('搜索名称、key 或描述')).toHaveValue('');
    expect(screen.getByLabelText('正在刷新 Skill 列表')).toBeInTheDocument();
    expect(getSkills).toHaveBeenCalledTimes(2);

    await act(async () => {
      refresh.resolve(enabledSkillsResponse);
      await refresh.promise;
    });
    await waitFor(() => {
      expect(screen.queryByLabelText('正在刷新 Skill 列表')).not.toBeInTheDocument();
    });
  });

  it('关闭后在同一请求完成前重开会复用进行中的加载', async () => {
    const initial = deferred<typeof enabledSkillsResponse>();
    vi.mocked(getSkills).mockReturnValue(initial.promise);
    const user = userEvent.setup();
    render(<Harness />);
    const pickerButton = screen.getByRole('button', { name: '选择本轮 Skill' });

    await act(async () => {
      await user.click(pickerButton);
    });
    expect(screen.getByText('加载中')).toBeInTheDocument();
    await act(async () => {
      await user.click(pickerButton);
    });
    await act(async () => {
      await user.click(pickerButton);
    });

    expect(getSkills).toHaveBeenCalledTimes(1);
    await act(async () => {
      initial.resolve(enabledSkillsResponse);
      await initial.promise;
    });
    expect(await screen.findByText('PDF 处理')).toBeInTheDocument();
  });

  it('后台刷新失败时保留已加载清单并允许重试', async () => {
    vi.mocked(getSkills)
      .mockResolvedValueOnce(enabledSkillsResponse)
      .mockRejectedValueOnce(new Error('refresh failed'));
    const user = userEvent.setup();
    render(<Harness />);
    const pickerButton = screen.getByRole('button', { name: '选择本轮 Skill' });

    await act(async () => {
      await user.click(pickerButton);
    });
    expect(await screen.findByText('PDF 处理')).toBeInTheDocument();
    await act(async () => {
      await user.click(pickerButton);
    });
    await act(async () => {
      await user.click(pickerButton);
    });

    expect(await screen.findByText('Skill 列表刷新失败，已显示上次结果')).toBeInTheDocument();
    expect(screen.getByText('PDF 处理')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('服务端返回 stale 快照时保留清单并说明正在显示上次结果', async () => {
    vi.mocked(getSkills).mockResolvedValue({
      ...enabledSkillsResponse,
      inventory_state: 'stale',
    });
    const user = userEvent.setup();
    render(<Harness />);

    await act(async () => {
      await user.click(screen.getByRole('button', { name: '选择本轮 Skill' }));
    });

    expect(await screen.findByText('PDF 处理')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      '刷新失败，正在显示上次成功加载的 Skill 清单。',
    );
  });

  it('支持 Escape 和点击面板外关闭选择器', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const pickerButton = screen.getByRole('button', { name: '选择本轮 Skill' });

    await act(async () => {
      await user.click(pickerButton);
    });
    await screen.findByText('PDF 处理');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByPlaceholderText('搜索名称、key 或描述')).not.toBeInTheDocument();
    expect(pickerButton).toHaveAttribute('aria-expanded', 'false');

    await act(async () => {
      await user.click(pickerButton);
    });
    await screen.findByText('PDF 处理');
    fireEvent.mouseDown(document.body);
    expect(screen.queryByPlaceholderText('搜索名称、key 或描述')).not.toBeInTheDocument();
    expect(pickerButton).toHaveAttribute('aria-expanded', 'false');
  });

  it('展示名与 key 仅有大小写和空白差异时不重复显示 key，选择仍提交稳定 key', async () => {
    vi.mocked(getSkills).mockResolvedValue({
      sandbox_status: 'available' as const,
      skills: [
        {
          key: 'arxiv watcher',
          name: 'arxiv watcher',
          display_name: '  ARXIV    WATCHER   ',
          description: '跟踪论文',
          category: 'research',
          source: 'official' as const,
          enabled: true,
        },
      ],
    });
    const user = userEvent.setup();
    const onSelectedSkillKeysChange = vi.fn();
    render(
      <ChatInput
        value="hello"
        onChange={() => undefined}
        onSend={() => undefined}
        selectedSkillKeys={[]}
        onSelectedSkillKeysChange={onSelectedSkillKeysChange}
      />,
    );

    await act(async () => {
      await user.click(screen.getByRole('button', { name: '选择本轮 Skill' }));
    });
    const skillOption = await screen.findByRole('button', { name: /ARXIV\s+WATCHER/i });
    expect(skillOption.querySelectorAll('span.block.truncate')).toHaveLength(1);
    await act(async () => { await user.click(skillOption); });

    expect(onSelectedSkillKeysChange).toHaveBeenCalledWith(['arxiv watcher']);
  });

  it('发送和停止按钮具有明确名称、提示和 button 类型', () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    const view = render(
      <ChatInput value="hello" onChange={() => undefined} onSend={onSend} />,
    );

    const sendButton = screen.getByRole('button', { name: '发送消息' });
    expect(sendButton).toHaveAttribute('type', 'button');
    expect(sendButton).toHaveAttribute('title', '发送消息');
    fireEvent.click(sendButton);
    expect(onSend).toHaveBeenCalledOnce();

    view.rerender(
      <ChatInput
        value="hello"
        onChange={() => undefined}
        onSend={onSend}
        onStop={onStop}
        sendingLabel="生成中"
      />,
    );
    const stopButton = screen.getByRole('button', { name: '停止生成' });
    expect(stopButton).toHaveAttribute('type', 'button');
    expect(stopButton).toHaveAttribute('title', '停止生成');
    fireEvent.click(stopButton);
    expect(onStop).toHaveBeenCalledOnce();
  });

  it('卸载后重新挂载不会复用上一账号的 Skill 清单', async () => {
    const firstAccountResponse = {
      sandbox_status: 'available' as const,
      skills: [
        { key: 'private-a', name: 'private-a', display_name: '账号 A 私有 Skill', description: 'A only', category: 'user', source: 'user' as const, enabled: true },
      ],
    };
    const secondAccountResponse = {
      sandbox_status: 'available' as const,
      skills: [
        { key: 'private-b', name: 'private-b', display_name: '账号 B 私有 Skill', description: 'B only', category: 'user', source: 'user' as const, enabled: true },
      ],
    };
    vi.mocked(getSkills)
      .mockResolvedValueOnce(firstAccountResponse)
      .mockResolvedValueOnce(secondAccountResponse);
    const user = userEvent.setup();

    const firstAccount = render(<Harness />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '选择本轮 Skill' }));
    });
    expect(await screen.findByText('账号 A 私有 Skill')).toBeInTheDocument();
    firstAccount.unmount();

    render(<Harness />);
    await act(async () => {
      await user.click(screen.getByRole('button', { name: '选择本轮 Skill' }));
    });
    expect(await screen.findByText('账号 B 私有 Skill')).toBeInTheDocument();
    expect(screen.queryByText('账号 A 私有 Skill')).not.toBeInTheDocument();
    expect(getSkills).toHaveBeenCalledTimes(2);
  });
});
