import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import SkillsPanel from '../../components/SkillsPanel';
import { getSkills, toggleSkill, type SkillsResponse } from '../../services/configApi';

vi.mock('../../services/configApi', () => ({
  getSkills: vi.fn(),
  toggleSkill: vi.fn(),
}));

const skillsResponse: SkillsResponse = {
  skills: [
    {
      key: 'pdf-stable-key',
      name: 'internal-pdf',
      display_name: 'PDF 处理',
      description: '读取和生成 PDF 文档',
      category: 'document',
      source: 'official',
      enabled: true,
    },
    {
      name: 'my-data-skill',
      description: '分析本地数据',
      category: 'user',
      source: 'user',
      enabled: false,
    },
  ],
  sandbox_status: 'available',
  inventory_state: 'current',
};

describe('SkillsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getSkills).mockResolvedValue(skillsResponse);
    vi.mocked(toggleSkill).mockResolvedValue();
  });

  it('初次进入读取快照，用户刷新时才要求远程重扫', async () => {
    render(<SkillsPanel />);

    expect(await screen.findByText('PDF 处理')).toBeInTheDocument();
    expect(getSkills).toHaveBeenNthCalledWith(1, { refresh: undefined });

    fireEvent.click(screen.getByRole('button', { name: '刷新 Skill 清单' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));
    expect(getSkills).toHaveBeenLastCalledWith({ refresh: true });
  });

  it('以 display_name 展示并使用稳定 key 乐观切换', async () => {
    let resolveToggle: (() => void) | null = null;
    vi.mocked(toggleSkill).mockImplementation(() => new Promise<void>((resolve) => {
      resolveToggle = resolve;
    }));

    render(<SkillsPanel />);
    expect(await screen.findByText('PDF 处理')).toBeInTheDocument();
    expect(screen.queryByText('internal-pdf')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('switch', { name: '禁用 PDF 处理' }));
    expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).toBeDisabled();
    expect(toggleSkill).toHaveBeenCalledWith('pdf-stable-key', false);

    await act(async () => { resolveToggle?.(); });
    await waitFor(() => expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).not.toBeDisabled());
  });

  it('多个切换请求并发时分别锁定对应开关', async () => {
    let resolvePdf: (() => void) | null = null;
    let resolveData: (() => void) | null = null;
    vi.mocked(toggleSkill).mockImplementation((key: string) => new Promise<void>((resolve) => {
      if (key === 'pdf-stable-key') resolvePdf = resolve;
      else resolveData = resolve;
    }));

    render(<SkillsPanel />);
    await screen.findByText('PDF 处理');
    fireEvent.click(screen.getByRole('switch', { name: '禁用 PDF 处理' }));
    fireEvent.click(screen.getByRole('switch', { name: '启用 my-data-skill' }));

    expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).toBeDisabled();
    expect(screen.getByRole('switch', { name: '禁用 my-data-skill' })).toBeDisabled();

    await act(async () => { resolvePdf?.(); });
    await waitFor(() => expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).not.toBeDisabled());
    expect(screen.getByRole('switch', { name: '禁用 my-data-skill' })).toBeDisabled();

    await act(async () => { resolveData?.(); });
    await waitFor(() => expect(screen.getByRole('switch', { name: '禁用 my-data-skill' })).not.toBeDisabled());
  });

  it('搜索、状态与来源筛选可以组合使用', async () => {
    render(<SkillsPanel />);
    await screen.findByText('PDF 处理');

    fireEvent.change(screen.getByRole('searchbox', { name: '搜索 Skills' }), {
      target: { value: '数据' },
    });
    expect(screen.queryByText('PDF 处理')).not.toBeInTheDocument();
    expect(screen.getByText('my-data-skill')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '筛选状态：已启用' }));
    expect(screen.getByText('没有匹配的 Skill')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '清空 Skill 搜索' }));
    fireEvent.click(screen.getByRole('button', { name: '筛选来源：官方' }));
    expect(screen.getByText('PDF 处理')).toBeInTheDocument();
    expect(screen.queryByText('my-data-skill')).not.toBeInTheDocument();
  });

  it('使用响应式卡片目录展示真实来源与分类', async () => {
    render(<SkillsPanel />);
    expect(await screen.findByRole('article', { name: 'Skill：PDF 处理' })).toBeInTheDocument();
    expect(screen.getByTestId('skills-card-grid')).toHaveStyle({
      gridTemplateColumns: 'repeat(auto-fill, minmax(min(300px, 100%), 1fr))',
    });
    expect(screen.getByText('文档')).toBeInTheDocument();
    expect(screen.getAllByText('官方').length).toBeGreaterThan(0);
    expect(screen.getAllByText('我的').length).toBeGreaterThan(0);
  });

  it('严格刷新失败时显示缓存清单与 stale 提示', async () => {
    vi.mocked(getSkills).mockResolvedValue({
      ...skillsResponse,
      sandbox_status: 'unavailable',
      inventory_state: 'stale',
    });

    render(<SkillsPanel />);
    expect(await screen.findByText('my-data-skill')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('刷新失败，正在显示上次成功加载的 Skill 清单。');
    expect(screen.queryByText('工作沙箱暂时不可用，目前仅显示官方技能。')).not.toBeInTheDocument();
  });

  it('当前刷新不会覆盖仍在提交的乐观切换', async () => {
    let resolveToggle: (() => void) | null = null;
    vi.mocked(toggleSkill).mockImplementation(() => new Promise<void>((resolve) => {
      resolveToggle = resolve;
    }));
    vi.mocked(getSkills)
      .mockResolvedValueOnce(skillsResponse)
      .mockResolvedValueOnce(skillsResponse);

    render(<SkillsPanel />);
    fireEvent.click(await screen.findByRole('switch', { name: '禁用 PDF 处理' }));
    fireEvent.click(screen.getByRole('button', { name: '刷新 Skill 清单' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));

    expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).toBeDisabled();
    await act(async () => { resolveToggle?.(); });
    await waitFor(() => expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).not.toBeDisabled());
  });

  it('成功切换后迟到的旧刷新快照不得回滚已确认状态', async () => {
    let resolveToggle: (() => void) | null = null;
    let resolveRefresh!: (value: SkillsResponse) => void;
    vi.mocked(toggleSkill).mockImplementation(() => new Promise<void>((resolve) => {
      resolveToggle = resolve;
    }));
    vi.mocked(getSkills)
      .mockResolvedValueOnce(skillsResponse)
      .mockImplementationOnce(() => new Promise<SkillsResponse>((resolve) => {
        resolveRefresh = resolve;
      }));

    render(<SkillsPanel />);
    fireEvent.click(await screen.findByRole('switch', { name: '禁用 PDF 处理' }));
    fireEvent.click(screen.getByRole('button', { name: '刷新 Skill 清单' }));
    await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));

    await act(async () => { resolveToggle?.(); });
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).not.toBeDisabled();
    });

    await act(async () => { resolveRefresh(skillsResponse); });
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: '启用 PDF 处理' })).toHaveAttribute('aria-checked', 'false');
      expect(screen.queryByRole('switch', { name: '禁用 PDF 处理' })).not.toBeInTheDocument();
    });
  });
});
