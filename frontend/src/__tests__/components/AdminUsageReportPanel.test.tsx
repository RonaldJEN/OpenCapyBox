import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import AdminUsageReportPanel from '../../components/AdminUsageReportPanel';
import { exportAdminUsageReport, getAdminUsageReport, type UsageReportData, type UsageReportParams } from '../../services/adminApi';

vi.mock('../../services/adminApi', () => ({ getAdminUsageReport: vi.fn(), exportAdminUsageReport: vi.fn() }));

function result(params: UsageReportParams = {}): UsageReportData {
  return {
    period: { start_date: params.start_date || '2026-09-01', end_date: params.end_date || '2026-09-07', as_of: '2026-09-07T10:00:00+08:00', timezone: 'Asia/Shanghai' },
    note: '有调用即活跃', summary: { open_accounts: 2, active_accounts: 1, unused_accounts: 1, calls: 3, error_calls: 3, input_tokens: 9, output_tokens: 1, total_tokens: 10, missing_usage_calls: 0 },
    view: params.view || 'accounts', sort_by: 'total_tokens', direction: 'desc', account_filter: params.account_filter || '', model_filter: '', page: 1, page_size: 20, total: 0, rows: [],
  };
}

describe('AdminUsageReportPanel', () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it('新查询取消旧请求；失败保留旧结果日期；导出使用成功查询而非未提交日期', async () => {
    let resolveOld!: (data: UsageReportData) => void;
    let oldSignal: AbortSignal | undefined;
    vi.mocked(getAdminUsageReport).mockImplementationOnce((_params, signal) => {
      oldSignal = signal;
      return new Promise(resolve => { resolveOld = resolve; });
    }).mockImplementationOnce(async params => result(params));
    render(<AdminUsageReportPanel />);
    fireEvent.click(screen.getByRole('button', { name: '近 30 天' }));
    await screen.findByRole('heading', { level: 2 });
    const successful = screen.getByRole('heading', { level: 2 }).textContent;
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => resolveOld(result({ start_date: '2020-01-01' })));
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe(successful);

    vi.mocked(getAdminUsageReport).mockRejectedValueOnce(new Error('network'));
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-09-02' } });
    fireEvent.click(screen.getByRole('button', { name: '查询' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe(successful);

    vi.mocked(exportAdminUsageReport).mockRejectedValueOnce(new Error('offline'));
    fireEvent.click(screen.getByRole('button', { name: '导出完整报表' }));
    expect(exportAdminUsageReport).toHaveBeenCalledWith(result(vi.mocked(getAdminUsageReport).mock.calls[1][0]).period);
    await waitFor(() => expect(screen.getByRole('button', { name: '导出完整报表' })).toBeEnabled());
    expect(screen.getByRole('alert')).toHaveTextContent('导出失败');
  });

  it('排序分页复用截止时间；全局刷新更新截止点；超跨度不请求后端', async () => {
    vi.mocked(getAdminUsageReport).mockImplementation(async params => result(params));
    const view = render(<AdminUsageReportPanel />);
    await screen.findByRole('heading', { level: 2 });
    fireEvent.click(screen.getByRole('button', { name: '总 Token' }));
    await waitFor(() => expect(getAdminUsageReport).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminUsageReport).mock.calls[1][0].as_of).toBe('2026-09-07T10:00:00+08:00');
    await waitFor(() => expect(screen.queryByText('正在查询使用数据…')).not.toBeInTheDocument());
    view.rerender(<AdminUsageReportPanel refreshToken={1} />);
    await waitFor(() => expect(getAdminUsageReport).toHaveBeenCalledTimes(3));
    expect(vi.mocked(getAdminUsageReport).mock.calls[2][0].as_of).toBeUndefined();
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2020-01-01' } });
    fireEvent.click(screen.getByRole('button', { name: '查询' }));
    expect(screen.getByRole('alert')).toHaveTextContent('最多366天');
    expect(getAdminUsageReport).toHaveBeenCalledTimes(3);
  });
});
