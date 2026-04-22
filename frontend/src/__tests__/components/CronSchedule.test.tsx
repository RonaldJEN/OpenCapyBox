import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { StrictMode } from 'react';
import CronSchedule, { cronToReadable } from '../../components/CronSchedule';

// Mock configApi
vi.mock('../../services/configApi', () => ({
  getCronJobs: vi.fn().mockResolvedValue([
    { name: 'daily_report', cron_expr: '0 9 * * *', schedule: null, content: '', description: '每天9点日报', enabled: true },
    { name: 'weekday_check', cron_expr: '0 10 * * 0-4', schedule: null, content: '', description: '工作日检查', enabled: true },
    { name: 'disabled_task', cron_expr: '*/30 * * * *', schedule: null, content: '', description: '已暂停', enabled: false },
  ]),
  getCronRuns: vi.fn().mockResolvedValue({
    runs: [{ id: 'run-1', job_name: 'daily_report', cron_expr: '0 9 * * *', started_at: '2026-04-14T09:00:00Z', completed_at: '2026-04-14T09:01:00Z', status: 'success', output: 'done', is_read: true, artifacts: null, run_workspace: null }],
    total: 1, offset: 0, limit: 20,
  }),
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

  it('工作日 cron 表达式 (0-4) 的任务在工作日显示', async () => {
    render(<CronSchedule />);

    await waitFor(() => {
      // weekday_check 使用 0-4，应该在工作日列中显示
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

    screen.getByText('✕').click();
    expect(onClose).toHaveBeenCalledOnce();
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

  it('保存失败时弹窗提示并渲染中文错误（body.content 长度超限）', async () => {
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
      expect(alertSpy).toHaveBeenCalledTimes(1);
      expect(alertSpy.mock.calls[0][0]).toContain('任务内容最多 8000 个字符');
      expect(screen.getByText(/任务内容最多 8000 个字符/)).toBeInTheDocument();
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
    expect(cronToReadable('0 10 * * 0-4')).toBe('工作日 10:00');
  });

  it('周末', () => {
    expect(cronToReadable('0 9 * * 5,6')).toBe('周末 09:00');
    expect(cronToReadable('0 9 * * 6,5')).toBe('周末 09:00');
  });

  it('每周单天', () => {
    expect(cronToReadable('0 9 * * 0')).toBe('每周一 09:00');
    expect(cronToReadable('0 9 * * 6')).toBe('每周日 09:00');
  });

  it('每周多天', () => {
    expect(cronToReadable('0 9 * * 1,3,5')).toBe('每周二、四、六 09:00');
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
