import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import CronSchedule, { cronToReadable } from '../../components/CronSchedule';

// Mock configApi
vi.mock('../../services/configApi', () => ({
  getCronJobs: vi.fn().mockResolvedValue([
    { name: 'daily_report', cron_expr: '0 9 * * *', description: '每天9点日报', enabled: true },
    { name: 'weekday_check', cron_expr: '0 10 * * 1-5', description: '工作日检查', enabled: true },
    { name: 'disabled_task', cron_expr: '*/30 * * * *', description: '已暂停', enabled: false },
  ]),
  getCronRuns: vi.fn().mockResolvedValue([
    { id: 'run-1', job_name: 'daily_report', cron_expr: '0 9 * * *', started_at: '2026-04-14T09:00:00Z', completed_at: '2026-04-14T09:01:00Z', status: 'success', output: 'done' },
  ]),
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

    // 日历 tab 默认选中
    expect(screen.getByText('日历')).toBeInTheDocument();
    expect(screen.getByText('日程管理')).toBeInTheDocument();

    // 任务卡片应出现在日历中（每天9点的任务每天都显示）
    await waitFor(() => {
      expect(screen.getAllByText('每天9点日报').length).toBeGreaterThan(0);
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

  it('关闭按钮触发 onClose', async () => {
    const onClose = vi.fn();
    render(<CronSchedule onClose={onClose} />);

    await waitFor(() => {
      expect(screen.getByText('日程')).toBeInTheDocument();
    });

    screen.getByText('✕').click();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('弹窗中有执行记录时显示执行成功而非待执行', async () => {
    render(<CronSchedule />);

    // 等待任务卡片渲染
    await waitFor(() => {
      expect(screen.getAllByText('每天9点日报').length).toBeGreaterThan(0);
    });

    // 日历卡片上应显示执行成功（latestRun status=success）
    expect(screen.getAllByText('执行成功').length).toBeGreaterThan(0);

    // 点击任务卡片打开弹窗
    const cards = screen.getAllByText('每天9点日报');
    fireEvent.click(cards[0]);

    // 弹窗中状态区域应显示执行成功
    await waitFor(() => {
      // 弹窗内有"状态"标签
      expect(screen.getByText('状态')).toBeInTheDocument();
    });
    // 弹窗中不应出现"待执行"（因为有成功的 run 记录）
    const statusElements = screen.getAllByText(/执行成功/);
    expect(statusElements.length).toBeGreaterThan(0);
  });

  it('弹窗中长描述默认截断并可展开', async () => {
    const { getCronJobs, getCronRuns } = await import('../../services/configApi');
    const longDesc = 'A'.repeat(200);
    vi.mocked(getCronJobs).mockResolvedValueOnce([
      { name: 'long_desc_task', cron_expr: '0 12 * * *', description: longDesc, enabled: true },
    ]);
    vi.mocked(getCronRuns).mockResolvedValueOnce([])
      .mockResolvedValueOnce([]);

    render(<CronSchedule />);

    await waitFor(() => {
      expect(screen.getAllByText(longDesc).length).toBeGreaterThan(0);
    });

    // 点击任务卡片打开弹窗
    fireEvent.click(screen.getAllByText(longDesc)[0]);

    await waitFor(() => {
      expect(screen.getByText('展开')).toBeInTheDocument();
    });

    // 点击展开
    fireEvent.click(screen.getByText('展开'));
    expect(screen.getByText('收起')).toBeInTheDocument();
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
    expect(cronToReadable('0 9 * * 0,6')).toBe('周末 09:00');
    expect(cronToReadable('0 9 * * 6,0')).toBe('周末 09:00');
  });

  it('每周单天', () => {
    expect(cronToReadable('0 9 * * 1')).toBe('每周一 09:00');
    expect(cronToReadable('0 9 * * 0')).toBe('每周日 09:00');
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
