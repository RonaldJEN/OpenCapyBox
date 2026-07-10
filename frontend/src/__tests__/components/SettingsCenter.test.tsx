import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import SettingsCenter from '../../components/SettingsCenter';
import {
  getAgentFile,
  getSkills,
  toggleSkill,
  updateAgentFile,
  type SkillsResponse,
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
    vi.mocked(getSkills).mockResolvedValue({
      skills: [],
      sandbox_status: 'not_created',
    });
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

  it('进入技能 tab 时才加载技能清单', async () => {
    render(<SettingsCenter />);

    await screen.findByText('old user');
    expect(getSkills).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '能力设定' }));
    fireEvent.click(screen.getByRole('button', { name: '技能' }));

    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(1));
  });

  it('多个技能切换请求并发时分别锁定对应开关', async () => {
    let resolveSkillA: (() => void) | null = null;
    let resolveSkillB: (() => void) | null = null;

    vi.mocked(getSkills).mockResolvedValue({
      skills: [
        {
          name: 'skill-a',
          description: 'Skill A',
          category: 'general',
          source: 'official',
          enabled: true,
        },
        {
          name: 'skill-b',
          description: 'Skill B',
          category: 'general',
          source: 'user',
          enabled: false,
        },
      ],
      sandbox_status: 'available',
    });
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

  it('沙箱不可用时提示用户，同时保留官方技能', async () => {
    vi.mocked(getSkills).mockResolvedValue({
      skills: [
        {
          name: 'pdf',
          description: 'PDF documents',
          category: 'document',
          source: 'official',
          enabled: true,
        },
      ],
      sandbox_status: 'unavailable',
    });

    render(<SettingsCenter initialSection="soul" initialSoulTab="skills" />);

    expect(await screen.findByText('pdf')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      '用户技能暂时无法读取，以下仅显示官方技能。',
    );
    expect(screen.getByText('官方')).toBeInTheDocument();
  });

  it('尚未创建沙箱时说明当前仅展示官方技能', async () => {
    vi.mocked(getSkills).mockResolvedValue({
      skills: [{
        name: 'docx',
        description: 'Word documents',
        category: 'document',
        source: 'official',
        enabled: true,
      }],
      sandbox_status: 'not_created',
    });

    render(<SettingsCenter initialSection="soul" initialSoulTab="skills" />);

    expect(await screen.findByText('docx')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      '尚未创建工作沙箱，以下仅显示官方技能。',
    );
  });

  it('旧技能请求先完成时不会提前结束最新请求的加载态', async () => {
    let resolveFirst: ((value: SkillsResponse) => void) | null = null;
    let resolveSecond: ((value: SkillsResponse) => void) | null = null;
    vi.mocked(getSkills)
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveFirst = resolve;
      }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveSecond = resolve;
      }));

    render(<SettingsCenter />);
    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '能力设定' }));
    fireEvent.click(screen.getByRole('button', { name: '技能' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: '角色设定' }));
    fireEvent.click(screen.getByRole('button', { name: '技能' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));

    await act(async () => {
      resolveFirst?.({
        skills: [{
          name: 'stale-skill',
          description: 'Stale',
          category: 'general',
          source: 'official',
          enabled: true,
        }],
        sandbox_status: 'available',
      });
    });

    expect(screen.getByText('加载中...')).toBeInTheDocument();
    expect(screen.queryByText('stale-skill')).not.toBeInTheDocument();

    await act(async () => {
      resolveSecond?.({
        skills: [{
          name: 'current-skill',
          description: 'Current',
          category: 'general',
          source: 'official',
          enabled: true,
        }],
        sandbox_status: 'available',
      });
    });

    expect(await screen.findByText('current-skill')).toBeInTheDocument();
  });

  it('旧技能请求后完成时不会覆盖最新列表或乐观切换', async () => {
    let resolveFirst: ((value: SkillsResponse) => void) | null = null;
    let resolveSecond: ((value: SkillsResponse) => void) | null = null;
    vi.mocked(getSkills)
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveFirst = resolve;
      }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveSecond = resolve;
      }));
    vi.mocked(toggleSkill).mockResolvedValue();

    render(<SettingsCenter />);
    await screen.findByText('old user');
    fireEvent.click(screen.getByRole('button', { name: '能力设定' }));
    fireEvent.click(screen.getByRole('button', { name: '技能' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: '角色设定' }));
    fireEvent.click(screen.getByRole('button', { name: '技能' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));

    await act(async () => {
      resolveSecond?.({
        skills: [{
          name: 'skill-a',
          description: 'Current',
          category: 'general',
          source: 'official',
          enabled: true,
        }],
        sandbox_status: 'available',
      });
    });

    fireEvent.click(await screen.findByRole('switch', { name: '禁用 skill-a' }));
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 skill-a' })).not.toBeDisabled();
    });

    await act(async () => {
      resolveFirst?.({
        skills: [{
          name: 'skill-a',
          description: 'Stale',
          category: 'general',
          source: 'official',
          enabled: true,
        }],
        sandbox_status: 'available',
      });
    });

    expect(screen.getByRole('switch', { name: '启用 skill-a' })).toBeInTheDocument();
    expect(screen.getByText('Current')).toBeInTheDocument();
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
  });

  it('当前刷新也不会覆盖仍在提交的乐观切换', async () => {
    const enabledSkill: SkillsResponse = {
      skills: [{
        name: 'skill-a',
        description: 'Current',
        category: 'general',
        source: 'official',
        enabled: true,
      }],
      sandbox_status: 'available',
    };
    vi.mocked(getSkills)
      .mockResolvedValueOnce(enabledSkill)
      .mockResolvedValueOnce(enabledSkill);

    let resolveToggle: (() => void) | null = null;
    vi.mocked(toggleSkill).mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveToggle = resolve;
      }),
    );

    render(<SettingsCenter initialSection="soul" initialSoulTab="skills" />);

    fireEvent.click(await screen.findByRole('switch', { name: '禁用 skill-a' }));
    expect(screen.getByRole('switch', { name: '启用 skill-a' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '角色设定' }));
    fireEvent.click(screen.getByRole('button', { name: '技能' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));

    expect(await screen.findByRole('switch', { name: '启用 skill-a' })).toBeDisabled();

    await act(async () => {
      resolveToggle?.();
    });

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 skill-a' })).not.toBeDisabled();
    });
  });

  it('切换成功后才返回的重叠刷新也不会回写旧值', async () => {
    const enabledSkill: SkillsResponse = {
      skills: [{
        name: 'skill-a',
        description: 'Current',
        category: 'general',
        source: 'official',
        enabled: true,
      }],
      sandbox_status: 'available',
    };
    let resolveRefresh: ((value: SkillsResponse) => void) | null = null;
    vi.mocked(getSkills)
      .mockResolvedValueOnce(enabledSkill)
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveRefresh = resolve;
      }));

    let resolveToggle: (() => void) | null = null;
    vi.mocked(toggleSkill).mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveToggle = resolve;
      }),
    );

    render(<SettingsCenter initialSection="soul" initialSoulTab="skills" />);
    fireEvent.click(await screen.findByRole('switch', { name: '禁用 skill-a' }));

    fireEvent.click(screen.getByRole('button', { name: '角色设定' }));
    fireEvent.click(screen.getByRole('button', { name: '技能' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));

    await act(async () => {
      resolveToggle?.();
    });
    await act(async () => {
      resolveRefresh?.(enabledSkill);
    });

    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 skill-a' })).not.toBeDisabled();
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
