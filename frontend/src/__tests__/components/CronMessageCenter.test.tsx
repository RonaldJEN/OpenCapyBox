import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

import CronMessageCenter from '../../components/CronMessageCenter';
import * as configApi from '../../services/configApi';

vi.mock('../../components/FilePreview', () => ({
  FilePreview: () => null,
}));

vi.mock('../../services/configApi', () => ({
  getCronRuns: vi.fn(),
  getUnreadCount: vi.fn(),
  getCronRunFiles: vi.fn().mockResolvedValue({ files: [] }),
  markCronRunsRead: vi.fn().mockResolvedValue({ marked: 1 }),
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
    vi.mocked(configApi.getUnreadCount).mockResolvedValue({ count: 0 });

    render(<CronMessageCenter onUnreadChange={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /daily_iraq_news/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /daily_iraq_news/i }));

    await waitFor(() => {
      expect(screen.getByText('done')).toBeInTheDocument();
    });
    expect(configApi.markCronRunsRead).not.toHaveBeenCalled();
    expect(screen.queryByTitle('未读')).not.toBeInTheDocument();
  });

  it('marks only the clicked unread run as read on expand', async () => {
    vi.mocked(configApi.getCronRuns).mockResolvedValue({
      runs: [{ ...baseRun, is_read: false }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    vi.mocked(configApi.getUnreadCount)
      .mockResolvedValueOnce({ count: 2 })
      .mockResolvedValueOnce({ count: 1 });
    vi.mocked(configApi.markCronRunsRead).mockResolvedValue({ marked: 1 });

    const onUnreadChange = vi.fn();
    render(<CronMessageCenter onUnreadChange={onUnreadChange} />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /daily_iraq_news/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /daily_iraq_news/i }));

    await waitFor(() => {
      expect(configApi.markCronRunsRead).toHaveBeenCalledWith('run-1');
    });
    await waitFor(() => {
      expect(onUnreadChange).toHaveBeenLastCalledWith(1);
    });
  });
});
