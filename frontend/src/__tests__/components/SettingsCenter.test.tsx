import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import SettingsCenter from '../../components/SettingsCenter';
import {
  getAgentFile,
  getSkills,
  toggleSkill,
  updateAgentFile,
} from '../../services/configApi';

vi.mock('../../services/configApi', () => ({
  getAgentFile: vi.fn(),
  getSkills: vi.fn(),
  toggleSkill: vi.fn(),
  updateAgentFile: vi.fn(),
}));

describe('SettingsCenter', () => {
  let resolveSave: ((value: { version: number }) => void) | null = null;

  beforeEach(() => {
    resolveSave = null;
    vi.clearAllMocks();

    vi.mocked(getAgentFile).mockImplementation(async (name: string) => {
      const contents = {
        memory: 'old memory',
        user: 'old user',
        soul: 'old soul',
      };
      return {
        name,
        file_type: name,
        content: contents[name as keyof typeof contents],
        version: 1,
      };
    });
    vi.mocked(getSkills).mockResolvedValue([]);
  });

  it('保存期间锁定编辑区，并提交点击保存时的内容', async () => {
    vi.mocked(updateAgentFile).mockImplementation(
      () => new Promise((resolve) => {
        resolveSave = resolve;
      }),
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

    await act(async () => {
      resolveSave?.({ version: 2 });
    });

    await waitFor(() => {
      expect(screen.getByText('第一版用户画像')).toBeInTheDocument();
    });
  });

  it('编辑内容变化时上报未保存状态', async () => {
    const onUnsavedChangesChange = vi.fn();

    render(<SettingsCenter onUnsavedChangesChange={onUnsavedChangesChange} />);

    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));

    fireEvent.change(await screen.findByPlaceholderText('记录用户画像、偏好和背景信息...'), {
      target: { value: '未保存的用户画像' },
    });

    await waitFor(() => {
      expect(onUnsavedChangesChange).toHaveBeenLastCalledWith(true);
    });

    fireEvent.click(screen.getByRole('button', { name: '取消' }));

    await waitFor(() => {
      expect(onUnsavedChangesChange).toHaveBeenLastCalledWith(false);
    });
  });

  it('多个技能切换请求并发时分别锁定对应开关', async () => {
    let resolveSkillA: (() => void) | null = null;
    let resolveSkillB: (() => void) | null = null;

    vi.mocked(getSkills).mockResolvedValue([
      {
        name: 'skill-a',
        description: 'Skill A',
        category: 'general',
        enabled: true,
      },
      {
        name: 'skill-b',
        description: 'Skill B',
        category: 'general',
        enabled: false,
      },
    ]);
    vi.mocked(toggleSkill).mockImplementation(
      (skillName: string) => new Promise<void>((resolve) => {
        if (skillName === 'skill-a') {
          resolveSkillA = resolve;
        } else {
          resolveSkillB = resolve;
        }
      }),
    );

    render(<SettingsCenter initialSection="soul" initialSoulTab="skills" />);

    await screen.findByText('skill-a');

    fireEvent.click(screen.getByRole('switch', { name: '禁用 skill-a' }));

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 skill-a' })).toBeDisabled();
    });

    fireEvent.click(screen.getByRole('switch', { name: '启用 skill-b' }));

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 skill-a' })).toBeDisabled();
      expect(screen.getByRole('switch', { name: '禁用 skill-b' })).toBeDisabled();
    });

    fireEvent.click(screen.getByRole('switch', { name: '启用 skill-a' }));
    expect(toggleSkill).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveSkillA?.();
    });

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 skill-a' })).not.toBeDisabled();
      expect(screen.getByRole('switch', { name: '禁用 skill-b' })).toBeDisabled();
    });

    await act(async () => {
      resolveSkillB?.();
    });

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '禁用 skill-b' })).not.toBeDisabled();
    });
  });

  it('激活记忆文件 tab 时重新刷新该文件内容', async () => {
    let memoryCalls = 0;
    vi.mocked(getAgentFile).mockImplementation(async (name: string) => {
      const content = name === 'memory'
        ? (memoryCalls++ === 0 ? 'old memory' : 'fresh memory')
        : name === 'user'
          ? 'old user'
          : 'old soul';
      return {
        name,
        file_type: name,
        content,
        version: name === 'memory' ? memoryCalls : 1,
      };
    });

    render(<SettingsCenter />);

    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '主记忆' }));

    await waitFor(() => {
      expect(screen.getByText('fresh memory')).toBeInTheDocument();
    });
    expect(vi.mocked(getAgentFile).mock.calls.filter(([name]) => name === 'memory')).toHaveLength(2);
  });

  it('开始编辑前刷新当前文件内容', async () => {
    let userCalls = 0;
    vi.mocked(getAgentFile).mockImplementation(async (name: string) => {
      const content = name === 'user'
        ? (userCalls++ === 0 ? 'old user' : 'fresh user')
        : name === 'memory'
          ? 'old memory'
          : 'old soul';
      return {
        name,
        file_type: name,
        content,
        version: name === 'user' ? userCalls : 1,
      };
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
