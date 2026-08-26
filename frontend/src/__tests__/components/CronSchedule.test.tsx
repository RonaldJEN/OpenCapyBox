import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { StrictMode } from 'react';
import CronSchedule, { cronToReadable, taskVisibleOnDate } from '../../components/CronSchedule';

// Mock configApi
vi.mock('../../services/configApi', () => ({
  getCronJobs: vi.fn().mockResolvedValue([
    { name: 'daily_report', cron_expr: '0 9 * * *', schedule: null, content: '', description: '每天9点日报', enabled: true },
    { name: 'weekday_check', cron_expr: '0 10 * * 1-5', schedule: null, content: '', description: '工作日检查', enabled: true },
    { name: 'disabled_task', cron_expr: '*/30 * * * *', schedule: null, content: '', description: '已暂停', enabled: false },
  ]),
  getCronRuns: vi.fn().mockResolvedValue({
    runs: [{ id: 'run-1', job_name: 'daily_report', cron_expr: '0 9 * * *', started_at: '2026-04-14T09:00:00Z', completed_at: '2026-04-14T09:01:00Z', status: 'success', output: 'done', is_read: true, artifacts: null, run_workspace: null }],
    total: 1, offset: 0, limit: 20,
  }),
  getUnreadCount: vi.fn().mockResolvedValue({ count: 1 }),
  markCronRunsRead: vi.fn().mockResolvedValue({ marked: 1 }),
  getCronRunFiles: vi.fn().mockResolvedValue({ files: [] }),
  downloadCronRunFile: vi.fn().mockResolvedValue(undefined),
  previewSchedule: vi.fn().mockResolvedValue({
    cron_expr: '0 9 * * *',
    next_fires: ['2026-04-22T09:00:00Z'],
  }),
  updateCronJob: vi.fn().mockImplementation(async (name: string, payload: { enabled?: boolean }) => ({
    id: 1,
    name,
    cron_expr: '0 9 * * *',
    schedule: null,
    description: '每天9点日报',
    content: '',
    enabled: payload.enabled ?? true,
  })),
  triggerCronJob: vi.fn().mockResolvedValue({ job_name: 'daily_report', run_id: 'fake-run-id', status: 'accepted', message: '后台任务已执行' }),
  getCronRunStatus: vi.fn().mockResolvedValue({ id: 'fake-run-id', job_name: 'daily_report', status: 'success', output: 'ok' }),
  deleteCronJob: vi.fn().mockResolvedValue(undefined),
}));

describe('CronSchedule', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('渲染日历视图并显示任务卡片', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('日程')).toBeInTheDocument();
    });

    // 视图切换器：日历 / 列表
    expect(screen.getByText('日历')).toBeInTheDocument();
    expect(screen.getByText('列表')).toBeInTheDocument();

    // 任务卡片应出现在日历中（每天9点的任务每天都显示）
    await waitFor(() => {
      expect(screen.getAllByText('每天9点日报').length).toBeGreaterThan(0);
    });
  });

  it('周视图时间轴覆盖全天 00:00 到 23:00', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('00:00')).toBeInTheDocument();
      expect(screen.getByText('23:00')).toBeInTheDocument();
    });
  });

  it('工作日 cron 表达式 (1-5) 的任务在工作日显示', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      // weekday_check 使用 1-5，应该在工作日列中显示
      const cards = screen.getAllByText('工作日检查');
      expect(cards.length).toBeGreaterThan(0);
    });
  });

  it('暂停任务不显示在日历视图，但会在列表视图保留用于管理', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getAllByText('每天9点日报').length).toBeGreaterThan(0);
    });

    // 日历只展示 enabled=true 的任务
    expect(screen.queryByText('已暂停')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('列表'));
    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    // 列表仍保留暂停任务，便于启用/编辑/删除
    const list = screen.getByTestId('schedule-list-cards');
    const pausedRow = list.querySelector('[data-task-name="disabled_task"]') as HTMLElement;
    expect(within(pausedRow).getByText('已暂停')).toBeInTheDocument();
  });

  it('列表筛选最近失败无匹配时，显示“未找到匹配任务”提示', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '最近失败' }));

    await waitFor(() => {
      expect(screen.getByText('未找到匹配任务')).toBeInTheDocument();
    });
    expect(screen.queryByText('暂无日程')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '清空筛选与搜索' })).toBeInTheDocument();
  });

  it('列表搜索无匹配时显示无结果提示，清空后恢复列表', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    fireEvent.change(screen.getByPlaceholderText('搜索任务名 / cron 表达式'), {
      target: { value: 'no-match-keyword' },
    });

    await waitFor(() => {
      expect(screen.getByText('未找到匹配任务')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '清空筛选与搜索' }));

    await waitFor(() => {
      expect(screen.queryByText('未找到匹配任务')).not.toBeInTheDocument();
      expect(screen.getByText('每天9点日报')).toBeInTheDocument();
    });
  });

  it('列表视图不展示上次/下次执行时间，且不会请求 previewSchedule', async () => {
    const { previewSchedule } = await import('../../services/configApi');
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    expect(within(targetRow).queryByText(/^下次\s/)).not.toBeInTheDocument();
    expect(within(targetRow).queryByText(/^上次\s/)).not.toBeInTheDocument();
    expect(previewSchedule).not.toHaveBeenCalled();
  });

  it('关闭按钮触发 onClose', async () => {
    const onClose = vi.fn();
    render(<CronSchedule onClose={onClose} />);

    await waitFor(() => {
      expect(screen.getByText('日程')).toBeInTheDocument();
    });

    const closeButton = screen.getByRole('button', { name: '关闭日程' });
    expect(closeButton).toHaveAttribute('type', 'button');
    expect(closeButton).toHaveAttribute('title', '关闭日程');
    closeButton.click();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('page 形态只渲染页面内工具栏，不提供顶层关闭按钮', async () => {
    render(<CronSchedule variant="page" />);

    const schedule = await screen.findByTestId('cron-schedule');
    expect(schedule).toHaveAttribute('data-variant', 'page');
    expect(screen.queryByRole('heading', { name: '日程' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '关闭日程' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '日历' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '列表' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('执行记录在页面流内替换日程内容，不再使用 absolute 覆盖层', async () => {
    render(<CronSchedule variant="page" unreadCount={1} />);

    fireEvent.click(await screen.findByRole('button', { name: /^执行记录/ }));

    const historyView = await screen.findByTestId('schedule-history-view');
    expect(historyView).not.toHaveClass('absolute', 'inset-0');
    expect(screen.getByRole('button', { name: '返回日程' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '全部标已读' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '+ 新建任务' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '返回日程' }));
    expect(screen.queryByTestId('schedule-history-view')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+ 新建任务' })).toBeInTheDocument();
  });

  it('日历导航和任务表单关闭图标具有明确可访问名称', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('日程')).toBeInTheDocument();
    });

    const previousWeek = screen.getByRole('button', { name: '上一周' });
    const nextWeek = screen.getByRole('button', { name: '下一周' });
    expect(previousWeek).toHaveAttribute('type', 'button');
    expect(previousWeek).toHaveAttribute('title', '上一周');
    expect(nextWeek).toHaveAttribute('type', 'button');
    expect(nextWeek).toHaveAttribute('title', '下一周');

    fireEvent.click(screen.getByRole('button', { name: '+ 新建任务' }));
    const closeForm = screen.getByRole('button', { name: '关闭任务表单' });
    expect(closeForm).toHaveAttribute('type', 'button');
    expect(closeForm).toHaveAttribute('title', '关闭任务表单');
    fireEvent.click(closeForm);
    expect(screen.queryByRole('button', { name: '关闭任务表单' })).not.toBeInTheDocument();
  });

  it('周视图 inline 只保留运行，不提供启停和详情入口', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getAllByText('每天9点日报').length).toBeGreaterThan(0);
    });

    const cards = screen.getAllByText('每天9点日报');
    fireEvent.click(cards[0]);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: '运行任务' }).length).toBeGreaterThan(0);
    });
    expect(screen.queryByRole('button', { name: /暂停任务|启用任务/ })).not.toBeInTheDocument();
    expect(document.querySelector('.bg-green-500')).not.toBeInTheDocument();
    expect(document.querySelector('.bg-red-500')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /任务详情|详情/ })).not.toBeInTheDocument();
  });

  it('周视图 inline 展开后，点击其他区域会自动收起', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getAllByText('每天9点日报').length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getAllByText('每天9点日报')[0]);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '运行任务' })).toBeInTheDocument();
    });

    fireEvent.mouseDown(document.body);

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '运行任务' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /暂停任务|启用任务/ })).not.toBeInTheDocument();
    });
  });

  it('周视图点击运行后展示动态执行反馈', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getAllByText('每天9点日报').length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getAllByText('每天9点日报')[0]);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '运行任务' })).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: '运行任务' }).className).toContain('bg-claude-hover/70');

    fireEvent.click(screen.getByRole('button', { name: '运行任务' }));

    await waitFor(() => {
      const runButtons = screen.getAllByRole('button', { name: '运行任务' });
      expect(runButtons.some((btn) => btn.getAttribute('aria-busy') === 'true')).toBe(true);
    });

    fireEvent.mouseDown(document.body);

    await waitFor(() => {
      const runButtons = screen.getAllByRole('button', { name: '运行任务' });
      expect(runButtons.some((btn) => btn.getAttribute('aria-busy') === 'true')).toBe(true);
    });

    expect(screen.queryByText('执行中')).not.toBeInTheDocument();
  });

  it('长任务反馈持续到退出 running（不受固定超时影响）', async () => {
    const { getCronRunStatus } = await import('../../services/configApi');
    let pollCount = 0;
    vi.mocked(getCronRunStatus).mockImplementation(async () => {
      pollCount += 1;
      return {
        id: 'fake-run-id',
        job_name: 'daily_report',
        cron_expr: '0 9 * * *',
        started_at: '2026-04-22T09:00:00Z',
        completed_at: pollCount >= 61 ? '2026-04-22T09:03:30Z' : null,
        status: pollCount >= 61 ? 'success' : 'running',
        output: pollCount >= 61 ? 'ok' : null,
        is_read: false,
        artifacts: null,
        run_workspace: null,
      };
    });

    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;

    vi.useFakeTimers();

    try {
      fireEvent.click(within(targetRow).getByRole('button', { name: '执行' }));

      // 提交触发后先进入运行中
      await Promise.resolve();
      await Promise.resolve();
      expect(within(targetRow).getByRole('button', { name: '执行中…' })).toBeInTheDocument();

      // 前 60 次轮询均为 running（对应超过 2 分钟）
      for (let i = 0; i < 60; i += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2000);
        });
      }
      expect(getCronRunStatus).toHaveBeenCalledTimes(60);
      expect(within(targetRow).getByRole('button', { name: '执行中…' })).toBeInTheDocument();

      // 第 61 次返回 success 后才退出运行态
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      await Promise.resolve();
      await Promise.resolve();
      expect(getCronRunStatus).toHaveBeenCalledTimes(61);

      expect(within(targetRow).getByRole('button', { name: '执行' })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('列表视图点击启用状态可切换为暂停并调用 updateCronJob', async () => {
    const { updateCronJob } = await import('../../services/configApi');
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    const toggleSwitch = within(targetRow).getByRole('switch', { name: '暂停任务' });
    fireEvent.click(toggleSwitch);

    await waitFor(() => {
      expect(updateCronJob).toHaveBeenCalledWith('daily_report', { enabled: false });
      expect(within(targetRow).getByRole('switch', { name: '启用任务' })).toBeInTheDocument();
    });
  });

  it('列表视图点击执行后不弹成功提示，使用行内执行中反馈', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('button', { name: '执行' }));

    await waitFor(() => {
      expect(within(targetRow).getByRole('button', { name: '执行中…' })).toBeInTheDocument();
    });

    expect(screen.queryByText('任务 daily_report 已提交后台执行')).not.toBeInTheDocument();
    expect(screen.queryByText('任务 daily_report 执行成功')).not.toBeInTheDocument();
  });

  it('列表视图点击任务标题不会打开详情页面', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByText('每天9点日报'));
    expect(screen.queryByTestId('task-detail-panel')).not.toBeInTheDocument();
  });

  it('列表更多操作菜单仅保留编辑和删除', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('button', { name: '更多操作' }));

    await waitFor(() => {
      expect(within(targetRow).getByRole('button', { name: '编辑' })).toBeInTheDocument();
      expect(within(targetRow).getByRole('button', { name: '删除' })).toBeInTheDocument();
    });
    expect(within(targetRow).queryByRole('button', { name: '复制' })).not.toBeInTheDocument();
    expect(within(targetRow).queryByRole('button', { name: '查看详情' })).not.toBeInTheDocument();
  });

  it('删除任务使用站内确认弹窗，并明确说明后续停止、历史保留', async () => {
    const { deleteCronJob } = await import('../../services/configApi');
    render(<CronSchedule />);

    fireEvent.click(await screen.findByText('列表'));
    await screen.findByText(/3\s*个任务/);

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    const moreButton = within(targetRow).getByRole('button', { name: '更多操作' });
    fireEvent.click(moreButton);
    fireEvent.click(within(targetRow).getByRole('button', { name: '删除' }));

    const dialog = screen.getByRole('alertdialog', { name: '删除这个任务？' });
    expect(within(dialog).getByText('daily_report')).toBeInTheDocument();
    expect(within(dialog).getByText('删除后不再自动执行')).toBeInTheDocument();
    expect(within(dialog).getByText('已产生的结果不会删除')).toBeInTheDocument();
    expect(window.confirm).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(deleteCronJob).not.toHaveBeenCalled();
    await waitFor(() => expect(moreButton).toHaveFocus());
  });

  it('确认删除后调用接口，失败时在弹窗内保留错误供重试', async () => {
    const { deleteCronJob } = await import('../../services/configApi');
    vi.mocked(deleteCronJob).mockRejectedValueOnce(new Error('网络暂时不可用'));
    render(<CronSchedule />);

    fireEvent.click(await screen.findByText('列表'));
    await screen.findByText(/3\s*个任务/);

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('button', { name: '更多操作' }));
    fireEvent.click(within(targetRow).getByRole('button', { name: '删除' }));
    fireEvent.click(screen.getByRole('button', { name: '删除任务' }));

    await waitFor(() => {
      expect(deleteCronJob).toHaveBeenCalledWith('daily_report');
      expect(screen.getByRole('alert')).toHaveTextContent('网络暂时不可用');
    });
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  });

  it('列表更多操作菜单点击空白区域会自动收起', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('button', { name: '更多操作' }));

    await waitFor(() => {
      expect(within(targetRow).getByRole('button', { name: '编辑' })).toBeInTheDocument();
    });

    fireEvent.mouseDown(document.body);

    await waitFor(() => {
      expect(within(targetRow).queryByRole('button', { name: '编辑' })).not.toBeInTheDocument();
    });
  });

  it('编辑抽屉仅保留任务内容字段，且保存时同步写入 description/content', async () => {
    const { updateCronJob } = await import('../../services/configApi');
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('button', { name: '更多操作' }));
    fireEvent.click(within(targetRow).getByRole('button', { name: '编辑' }));

    await waitFor(() => {
      expect(screen.getByText('编辑任务')).toBeInTheDocument();
    });

    expect(screen.queryByText('显示名（用于列表展示）')).not.toBeInTheDocument();
    expect(screen.queryByText('摘要')).not.toBeInTheDocument();
    expect(screen.queryByText('未来 5 次执行')).not.toBeInTheDocument();

    const contentField = screen.getByRole('textbox', { name: '任务内容' }) as HTMLTextAreaElement;
    expect(contentField.value).toBe('每天9点日报');

    fireEvent.change(contentField, {
      target: { value: '执行健康检查并输出结果' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => {
      expect(updateCronJob).toHaveBeenCalledWith(
        'daily_report',
        expect.objectContaining({
          description: '执行健康检查并输出结果',
          content: '执行健康检查并输出结果',
        }),
      );
    });
  });

  it('编辑保存时 description 截断到 500，content 保留全文', async () => {
    const { updateCronJob } = await import('../../services/configApi');
    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('button', { name: '更多操作' }));
    fireEvent.click(within(targetRow).getByRole('button', { name: '编辑' }));

    await waitFor(() => {
      expect(screen.getByText('编辑任务')).toBeInTheDocument();
    });

    const longContent = 'A'.repeat(520);
    fireEvent.change(screen.getByRole('textbox', { name: '任务内容' }), {
      target: { value: longContent },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => {
      expect(updateCronJob).toHaveBeenCalledWith(
        'daily_report',
        expect.objectContaining({
          description: longContent.slice(0, 500),
          content: longContent,
        }),
      );
    });
  });

  it('保存失败时仅在抽屉内渲染中文错误，不重复调用 window.alert', async () => {
    const { updateCronJob } = await import('../../services/configApi');
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    vi.mocked(updateCronJob).mockRejectedValueOnce({
      response: {
        data: {
          detail: [
            {
              loc: ['body', 'content'],
              msg: 'String should have at most 8000 characters',
              type: 'string_too_long',
            },
          ],
        },
      },
      message: 'Request failed with status code 422',
    });

    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('button', { name: '更多操作' }));
    fireEvent.click(within(targetRow).getByRole('button', { name: '编辑' }));

    await waitFor(() => {
      expect(screen.getByText('编辑任务')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => {
      expect(screen.getByText(/任务内容最多 8000 个字符/)).toBeInTheDocument();
    });
    expect(alertSpy).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole('textbox', { name: '任务内容' }), {
      target: { value: '修正后的任务内容' },
    });
    await waitFor(() => {
      expect(screen.queryByText(/任务内容最多 8000 个字符/)).not.toBeInTheDocument();
    });

    alertSpy.mockRestore();
  });

  it('列表视图默认按时间升序排列（无固定时间排最后）', async () => {
    const { getCronJobs, getCronRuns } = await import('../../services/configApi');

    vi.mocked(getCronJobs).mockResolvedValueOnce([
      { name: 'task_16', cron_expr: '0 16 * * *', schedule: null, content: '', description: '16点任务', enabled: true },
      { name: 'task_730', cron_expr: '30 7 * * *', schedule: null, content: '', description: '7点半任务', enabled: true },
      { name: 'task_10', cron_expr: '0 10 * * *', schedule: null, content: '', description: '10点任务', enabled: true },
      { name: 'task_interval', cron_expr: '*/30 * * * *', schedule: null, content: '', description: '间隔任务', enabled: true },
    ]);
    vi.mocked(getCronRuns).mockResolvedValueOnce({ runs: [], total: 0, offset: 0, limit: 20 });

    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/4\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const order = Array.from(list.querySelectorAll('[data-task-name]')).map((el) => el.getAttribute('data-task-name'));
    expect(order).toEqual(['task_730', 'task_10', 'task_16', 'task_interval']);
  });

  it('StrictMode 下列表视图点击暂停不会卡在处理中，随后可恢复为启用任务', async () => {
    const { updateCronJob } = await import('../../services/configApi');

    render(
      <StrictMode>
        <CronSchedule />
      </StrictMode>,
    );

    await waitFor(() => {
      expect(screen.getByText('列表')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('列表'));

    await waitFor(() => {
      expect(screen.getByText(/3\s*个任务/)).toBeInTheDocument();
    });

    const list = screen.getByTestId('schedule-list-cards');
    const targetRow = list.querySelector('[data-task-name="daily_report"]') as HTMLElement;
    fireEvent.click(within(targetRow).getByRole('switch', { name: '暂停任务' }));

    await waitFor(() => {
      expect(updateCronJob).toHaveBeenCalledWith('daily_report', { enabled: false });
      expect(within(targetRow).getByRole('switch', { name: '启用任务' })).toBeInTheDocument();
    });
  });
});

describe('cronToReadable', () => {
  it('每N分钟', () => {
    expect(cronToReadable('*/5 * * * *')).toBe('每5分钟');
    expect(cronToReadable('*/30 * * * *')).toBe('每30分钟');
  });

  it('每N小时', () => {
    expect(cronToReadable('0 */2 * * *')).toBe('每2小时');
  });

  it('每天固定时间', () => {
    expect(cronToReadable('0 9 * * *')).toBe('每天 09:00');
    expect(cronToReadable('30 14 * * *')).toBe('每天 14:30');
  });

  it('工作日', () => {
    expect(cronToReadable('0 10 * * 1-5')).toBe('工作日 10:00');
  });

  it('周末', () => {
    expect(cronToReadable('0 9 * * 6,0')).toBe('周末 09:00');
    expect(cronToReadable('0 9 * * 0,6')).toBe('周末 09:00');
  });

  it('每周单天', () => {
    expect(cronToReadable('0 9 * * 0')).toBe('每周日 09:00');
    expect(cronToReadable('0 9 * * 6')).toBe('每周六 09:00');
  });

  it('每周多天', () => {
    expect(cronToReadable('0 9 * * 1,3,5')).toBe('每周一、三、五 09:00');
  });

  it('每月某日', () => {
    expect(cronToReadable('0 9 1 * *')).toBe('每月1日 09:00');
    expect(cronToReadable('0 9 15 * *')).toBe('每月15日 09:00');
  });

  it('每年特定月日', () => {
    expect(cronToReadable('0 11 15 4 *')).toBe('每年4月15日 11:00');
    expect(cronToReadable('0 0 1 1 *')).toBe('每年1月1日 00:00');
  });

  it('无法解析时原样返回', () => {
    expect(cronToReadable('invalid')).toBe('invalid');
    expect(cronToReadable('* * * * * *')).toBe('* * * * * *');
  });
});

describe('标准 Cron 星期匹配', () => {
  const monday = new Date(2026, 6, 27, 9, 0);
  const saturday = new Date(2026, 6, 25, 9, 0);
  const sunday = new Date(2026, 6, 26, 9, 0);

  it('使用 0/7=周日、1=周一 到 6=周六', () => {
    expect(taskVisibleOnDate('0 9 * * 1-5', monday)).toBe(true);
    expect(taskVisibleOnDate('0 9 * * 1-5', saturday)).toBe(false);
    expect(taskVisibleOnDate('0 9 * * 1-6', saturday)).toBe(true);
    expect(taskVisibleOnDate('0 9 * * 2-6,0', monday)).toBe(false);
    expect(taskVisibleOnDate('0 9 * * 2-6,0', sunday)).toBe(true);
    expect(taskVisibleOnDate('0 9 * * 7', sunday)).toBe(true);
  });

  it('日与星期同时受限时使用 OR 语义', () => {
    const mondayNotFifteenth = new Date(2025, 0, 6, 9, 0);
    const fifteenthNotMonday = new Date(2025, 0, 15, 9, 0);
    expect(taskVisibleOnDate('0 9 15 * 1', mondayNotFifteenth)).toBe(true);
    expect(taskVisibleOnDate('0 9 15 * 1', fifteenthNotMonday)).toBe(true);
  });

  it('按字段范围匹配步进表达式', () => {
    const tuesday = new Date(2026, 6, 28, 9, 0);
    const wednesday = new Date(2026, 6, 29, 9, 0);
    const januaryThird = new Date(2026, 0, 3, 9, 0);
    const januarySecond = new Date(2026, 0, 2, 9, 0);
    const februaryFirst = new Date(2026, 1, 1, 9, 0);

    expect(taskVisibleOnDate('0 9 * * 1-5/2', monday)).toBe(true);
    expect(taskVisibleOnDate('0 9 * * 1-5/2', tuesday)).toBe(false);
    expect(taskVisibleOnDate('0 9 * * 1-5/2', wednesday)).toBe(true);
    expect(taskVisibleOnDate('0 9 */2 * *', januaryThird)).toBe(true);
    expect(taskVisibleOnDate('0 9 */2 * *', januarySecond)).toBe(false);
    expect(taskVisibleOnDate('0 9 * */2 *', februaryFirst)).toBe(false);
  });
});
