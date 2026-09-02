import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

import CronMessageCenter, { runDateGroupKey } from '../../components/CronMessageCenter';
import * as configApi from '../../services/configApi';
import { subscribeWorkspaceMutation } from '../../services/workspaceEvents';

vi.mock('../../components/FilePreview', () => ({
  FilePreview: () => null,
}));

vi.mock('../../services/configApi', () => ({
  getCronRuns: vi.fn(),
  getUnreadCount: vi.fn(),
  getCronRunFiles: vi.fn().mockResolvedValue({ files: [] }),
  markCronRunsRead: vi.fn().mockResolvedValue({ marked: 1, unread_count: 0 }),
  downloadCronRunFile: vi.fn(),
}));

const baseRun = {
  id: 'run-1',
  job_name: 'daily_iraq_news',
  cron_expr: '0 9 * * *',
  started_at: '2026-04-16T18:32:50Z',
  completed_at: '2026-04-16T18:34:00Z',
  status: 'success',
  output: 'done',
  artifacts: null,
  run_workspace: null,
};

describe('CronMessageCenter unread behavior', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not mark all read when entering message center', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, is_read: false }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 3 });

    const onUnreadChange = vi.fn();
    render(<CronMessageCenter onUnreadChange={onUnreadChange} />);

    await waitFor(() => {
      expect(screen.getByText('daily_iraq_news')).toBeInTheDocument();
    });

    expect(configApi.markCronRunsRead).not.toHaveBeenCalled();
    expect(onUnreadChange).toHaveBeenCalledWith(3);
  });

  it('does not mark running run as read when expanded', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, status: 'running', is_read: false }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 1 });

    const onUnreadChange = vi.fn();
    render(<CronMessageCenter onUnreadChange={onUnreadChange} />);

    const card = await screen.findByRole('button', { name: /daily_iraq_news/i });
    fireEvent.click(card);

    await waitFor(() => {
      expect(configApi.markCronRunsRead).not.toHaveBeenCalled();
      expect(screen.getByTitle('未读')).toBeInTheDocument();
      expect(onUnreadChange).toHaveBeenLastCalledWith(1);
    });
  });

  it('does not mark queued run as read when expanded', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, status: 'queued', queued_at: '2026-04-16T18:31:00Z', started_at: null, completed_at: null, phase: 'claiming', is_read: false }],
      total: 1, offset: 0, limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 1 });
    render(<CronMessageCenter onUnreadChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /daily_iraq_news/i }));
    expect(configApi.markCronRunsRead).not.toHaveBeenCalled();
    expect(screen.getByText('排队中')).toBeInTheDocument();
    expect(screen.getByText('claiming')).toBeInTheDocument();
  });

  it('separates committed workspace changes and opens stable workspace entry', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{
        ...baseRun,
        is_read: true,
        workspace_changes: [{ entry_id: 'entry-1', operation: 'updated', path: 'reports/daily.md', name: 'daily.md', revision: 2, mutation_id: 'm1' }],
      }],
      total: 1, offset: 0, limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 0 });
    const navigation = vi.fn();
    window.addEventListener('workspace:navigate', navigation);
    render(<CronMessageCenter />);
    fireEvent.click(await screen.findByRole('button', { name: /daily_iraq_news/i }));
    fireEvent.click(screen.getByRole('button', { name: '在工作区打开 daily.md' }));
    expect((navigation.mock.calls[0][0] as CustomEvent).detail).toEqual({ entryId: 'entry-1' });
    window.removeEventListener('workspace:navigate', navigation);
  });

  it('加载 Cron workspace_changes 时通过统一入口只失效同一 entry 的最新 revision', async () => {
    const invalidations: Array<{ entryId?: string; revision?: number }> = [];
    const unsubscribe = subscribeWorkspaceMutation((detail) => {
      if (detail.entryId === 'cron-entry-refresh') invalidations.push(detail);
    });
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{
        ...baseRun,
        is_read: true,
        workspace_changes: [
          { entry_id: 'cron-entry-refresh', operation: 'updated', path: 'daily.md', revision: 2 },
          { entry_id: 'cron-entry-refresh', operation: 'updated', path: 'daily.md', revision: 3 },
          { entry_id: 'cron-entry-refresh', operation: 'updated', path: 'daily.md', revision: 1 },
        ],
      }],
      total: 1, offset: 0, limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 0 });

    render(<CronMessageCenter />);
    await screen.findByText('daily_iraq_news');

    expect(invalidations).toEqual([expect.objectContaining({ entryId: 'cron-entry-refresh', revision: 3 })]);
    unsubscribe();
  });

  it('expanding unread run marks it as read immediately', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, status: 'success', is_read: false }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount)
      .mockResolvedValueOnce({ count: 1 })
      .mockResolvedValueOnce({ count: 0 });

    const onUnreadChange = vi.fn();
    render(<CronMessageCenter onUnreadChange={onUnreadChange} />);

    const card = await screen.findByRole('button', { name: /daily_iraq_news/i });
    fireEvent.click(card);

    await waitFor(() => {
      expect(screen.getByText('done')).toBeInTheDocument();
      expect(configApi.markCronRunsRead).toHaveBeenCalledWith('run-1');
      expect(screen.queryByTitle('未读')).not.toBeInTheDocument();
      expect(onUnreadChange).toHaveBeenLastCalledWith(0);
    });
  });

  it('「全部标已读」按钮显式触发 markCronRunsRead() 且未读清零', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [
        { ...baseRun, id: 'r1', is_read: false, status: 'success' },
        { ...baseRun, id: 'r2', is_read: false, status: 'failed', job_name: 'weekly' },
      ],
      total: 2,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount)
      .mockResolvedValueOnce({ count: 2 })
      .mockResolvedValueOnce({ count: 0 });
    vi.mocked(configApi.markCronRunsRead).mockResolvedValue({ marked: 2, unread_count: 0 });

    const onUnreadChange = vi.fn();
    render(<CronMessageCenter onUnreadChange={onUnreadChange} />);

    const btn = await screen.findByRole('button', { name: '全部标已读' });
    expect(btn).not.toBeDisabled();

    fireEvent.click(btn);

    await waitFor(() => {
      // 不带 run_id —— 全量标记
      expect(configApi.markCronRunsRead).toHaveBeenCalledWith();
      expect(onUnreadChange).toHaveBeenLastCalledWith(0);
    });
  });

  it('marked=0 但服务端未读归零时，也会清空本地未读红点', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, id: 'r1', is_read: false, status: 'success' }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount)
      .mockResolvedValueOnce({ count: 1 })
      .mockResolvedValueOnce({ count: 0 });
    vi.mocked(configApi.markCronRunsRead).mockResolvedValue({ marked: 0, unread_count: 0 });

    const onUnreadChange = vi.fn();
    render(<CronMessageCenter onUnreadChange={onUnreadChange} />);

    const btn = await screen.findByRole('button', { name: '全部标已读' });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(onUnreadChange).toHaveBeenLastCalledWith(0);
      expect(screen.queryByTitle('未读')).not.toBeInTheDocument();
      expect(btn).toBeDisabled();
    });
  });

  it('分组内仅 failed+unread 置顶，其余严格按 started_at 倒序', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [
        {
          ...baseRun,
          id: 'r-older-unread',
          job_name: 'job_success_older_unread',
          status: 'success',
          is_read: false,
          started_at: '2026-04-16T09:00:00Z',
        },
        {
          ...baseRun,
          id: 'r-failed-unread',
          job_name: 'job_failed_unread',
          status: 'failed',
          is_read: false,
          started_at: '2026-04-16T10:00:00Z',
        },
        {
          ...baseRun,
          id: 'r-newer-read',
          job_name: 'job_success_newer_read',
          status: 'success',
          is_read: true,
          started_at: '2026-04-16T11:00:00Z',
        },
      ],
      total: 3,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 2 });

    render(<CronMessageCenter />);

    await waitFor(() => {
      expect(screen.getByText('job_failed_unread')).toBeInTheDocument();
      expect(screen.getByText('job_success_newer_read')).toBeInTheDocument();
      expect(screen.getByText('job_success_older_unread')).toBeInTheDocument();
    });

    const cardOrder = screen
      .getAllByRole('button')
      .filter((btn) => /job_/.test(btn.textContent ?? ''))
      .map((btn) => btn.textContent?.match(/job_[a-z_]+/)?.[0] ?? '');

    expect(cardOrder).toEqual([
      'job_failed_unread',
      'job_success_newer_read',
      'job_success_older_unread',
    ]);
  });

  it('全部已读时「全部标已读」按钮 disabled', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, is_read: true }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 0 });

    render(<CronMessageCenter />);
    const btn = await screen.findByRole('button', { name: '全部标已读' });
    expect(btn).toBeDisabled();
  });

  it('failed 状态未读也显示红点（cron-spec §4: 所有 is_read=false 都算未读）', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, status: 'failed', is_read: false }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 1 });

    render(<CronMessageCenter />);
    await waitFor(() => {
      expect(screen.getByTitle('未读')).toBeInTheDocument();
    });
  });

  it('同一条记录重复展开时复用产物缓存，不重复请求文件列表', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, is_read: true, run_workspace: '/home/user/cron/runs/run-1', artifacts: null }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 0 });
    vi.mocked(configApi.getCronRunFiles).mockResolvedValue({
      files: [{ name: 'daily-summary.md', path: 'daily-summary.md', size: 120, type: 'md' }],
    });

    render(<CronMessageCenter />);

    const card = await screen.findByRole('button', { name: /daily_iraq_news/i });

    fireEvent.click(card);
    await waitFor(() => {
      expect(configApi.getCronRunFiles).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByRole('button', { name: 'daily-summary.md' })).toBeInTheDocument();

    fireEvent.click(card);
    fireEvent.click(card);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'daily-summary.md' })).toBeInTheDocument();
    });
    expect(configApi.getCronRunFiles).toHaveBeenCalledTimes(1);
  });

  it('same instant with different timezone offsets uses same date group key', () => {
    const isoUtc = '2026-04-16T01:00:00Z';
    const isoOffset = '2026-04-15T20:00:00-05:00';

    expect(runDateGroupKey(isoUtc)).toBe(runDateGroupKey(isoOffset));
  });
});
