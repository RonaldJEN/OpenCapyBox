import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import SettingsCenter from '../../components/SettingsCenter';
import { getAgentFile, updateAgentFile } from '../../services/configApi';

vi.mock('../../services/configApi', () => ({
  getAgentFile: vi.fn(),
  updateAgentFile: vi.fn(),
}));

vi.mock('../../components/ToolPermissionsPanel', () => ({
  default: ({ refreshToken = 0 }: { refreshToken?: number }) => (
    <><div>独立权限策略面板</div><output aria-label="权限刷新版本">{refreshToken}</output></>
  ),
}));

describe('SettingsCenter', () => {
  let resolveSave: ((value: { version: number }) => void) | null = null;

  beforeEach(() => {
    resolveSave = null;
    vi.clearAllMocks();
    vi.mocked(getAgentFile).mockImplementation(async (name: string) => {
      const contents = { memory: 'old memory', user: 'old user', soul: 'old soul' };
      return {
        name,
        file_type: name,
        content: contents[name as keyof typeof contents],
        version: 1,
      };
    });
  });

  it('保存期间锁定编辑区，并提交点击保存时的内容', async () => {
    vi.mocked(updateAgentFile).mockImplementation(
      () => new Promise((resolve) => { resolveSave = resolve; }),
    );
    render(<SettingsCenter />);

    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    const textarea = await screen.findByPlaceholderText('记录用户画像、偏好和背景信息...');
    fireEvent.change(textarea, { target: { value: '第一版用户画像' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(updateAgentFile).toHaveBeenCalledWith('user', '第一版用户画像');
    expect(textarea).toBeDisabled();
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    await act(async () => { resolveSave?.({ version: 2 }); });
    await waitFor(() => expect(screen.getByText('第一版用户画像')).toBeInTheDocument());
  });

  it('编辑内容变化时上报未保存状态', async () => {
    const onUnsavedChangesChange = vi.fn();
    render(<SettingsCenter onUnsavedChangesChange={onUnsavedChangesChange} />);

    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.change(await screen.findByPlaceholderText('记录用户画像、偏好和背景信息...'), {
      target: { value: '未保存的用户画像' },
    });
    await waitFor(() => expect(onUnsavedChangesChange).toHaveBeenLastCalledWith(true));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(onUnsavedChangesChange).toHaveBeenLastCalledWith(false));
  });

  it('不在设置中重复展示 Skills 与数据连接一级入口', async () => {
    render(<SettingsCenter />);
    await screen.findByText('old user');

    expect(screen.queryByRole('button', { name: '打开 Skills' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开数据连接' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '数据连接' })).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });

  it('权限管控保留独立分区并接收外部刷新版本', async () => {
    render(<SettingsCenter permissionsRefreshToken={4} />);
    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '权限管控' }));
    expect(await screen.findByText('独立权限策略面板')).toBeInTheDocument();
    expect(screen.getByRole('status', { name: '权限刷新版本' })).toHaveTextContent('4');
  });

  it('为窄屏提供顶部横向设置导航并收紧内容留白', async () => {
    render(<SettingsCenter />);
    await screen.findByText('old user');
    const navigation = screen.getByRole('navigation', { name: '设置分区' });
    const content = screen.getByRole('main');
    expect(navigation).toHaveClass('w-full', 'flex-row', 'overflow-x-auto');
    expect(navigation).toHaveClass('sm:w-[180px]', 'sm:flex-col');
    expect(content).toHaveClass('px-4', 'pt-5', 'sm:px-8', 'sm:pt-8');
  });

  it('激活记忆文件 tab 时重新刷新该文件内容', async () => {
    let memoryCalls = 0;
    vi.mocked(getAgentFile).mockImplementation(async (name: string) => {
      const content = name === 'memory'
        ? (memoryCalls++ === 0 ? 'old memory' : 'fresh memory')
        : name === 'user' ? 'old user' : 'old soul';
      return { name, file_type: name, content, version: name === 'memory' ? memoryCalls : 1 };
    });

    render(<SettingsCenter />);
    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '主记忆' }));
    await waitFor(() => expect(screen.getByText('fresh memory')).toBeInTheDocument());
    expect(vi.mocked(getAgentFile).mock.calls.filter(([name]) => name === 'memory')).toHaveLength(2);
  });

  it('开始编辑前刷新当前文件内容', async () => {
    let userCalls = 0;
    vi.mocked(getAgentFile).mockImplementation(async (name: string) => {
      const content = name === 'user'
        ? (userCalls++ === 0 ? 'old user' : 'fresh user')
        : name === 'memory' ? 'old memory' : 'old soul';
      return { name, file_type: name, content, version: name === 'user' ? userCalls : 1 };
    });

    render(<SettingsCenter />);
    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    await waitFor(() => {
      expect(screen.getByPlaceholderText('记录用户画像、偏好和背景信息...')).toHaveValue('fresh user');
    });
    expect(vi.mocked(getAgentFile).mock.calls.filter(([name]) => name === 'user')).toHaveLength(2);
  });
});
