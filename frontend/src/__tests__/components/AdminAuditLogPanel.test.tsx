import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
import AdminAuditLogPanel from '../../components/AdminAuditLogPanel';
import {
  exportAdminOperationLogs,
  getAdminOperationLogs,
  type AdminOperationLogItem,
} from '../../services/adminApi';

vi.mock('../../services/adminApi', () => ({
  exportAdminOperationLogs: vi.fn(),
  getAdminOperationLogs: vi.fn(),
}));

function operationLog(overrides: Partial<AdminOperationLogItem> = {}): AdminOperationLogItem {
  return {
    id: 17,
    request_id: 'request-audit-17',
    actor_user_id: 'admin',
    action: 'session.view',
    risk_level: 'normal',
    target_type: 'session',
    target_id: 'session-1',
    target_user_id: 'demo',
    session_id: 'session-1',
    step_record_id: null,
    outcome: 'succeeded',
    http_method: 'GET',
    route_template: '/api/admin/sessions/{session_id}/rounds',
    status_code: 200,
    ip_address: '198.51.100.8',
    user_agent: 'audit-test-browser',
    changed_fields: null,
    details: { returned_rounds: 3 },
    started_at: '2026-08-01T02:00:00Z',
    completed_at: '2026-08-01T02:00:01Z',
    ...overrides,
  };
}

describe('AdminAuditLogPanel', () => {
  const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
  const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAdminOperationLogs).mockResolvedValue({
      items: [operationLog()],
      next_cursor: 'cursor-page-2',
    });
    vi.mocked(exportAdminOperationLogs).mockResolvedValue(new Blob(['id,action'], { type: 'text/csv' }));
  });

  afterEach(() => {
    if (originalCreateObjectURL) {
      Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL);
    } else {
      delete (URL as { createObjectURL?: unknown }).createObjectURL;
    }
    if (originalRevokeObjectURL) {
      Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL);
    } else {
      delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
    }
    vi.restoreAllMocks();
  });

  it('默认读取最近 24 小时并展示只读详情', async () => {
    render(<AdminAuditLogPanel />);

    await waitFor(() => {
      expect(screen.getByText('查看会话列表')).toBeInTheDocument();
    });
    expect(getAdminOperationLogs).toHaveBeenCalledWith(expect.objectContaining({
      from: expect.any(String),
      cursor: undefined,
      limit: 50,
    }));
    expect(screen.getAllByText('成功').length).toBeGreaterThan(0);
    expect(screen.getByText('session.view')).toBeInTheDocument();
    expect(screen.getByText('会话: session-1', { exact: false })).toBeInTheDocument();
    const sessionRow = screen.getByRole('row', { name: /查看会话列表/ });
    expect(sessionRow).toHaveTextContent('会话信息查阅');
    expect(sessionRow).not.toHaveTextContent('高危');
    expect(sessionRow.textContent?.match(/session-1/g)).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', {
      name: '查看操作详情：查看会话列表（session.view）',
    }));

    expect(screen.getByText('audit-test-browser')).toBeInTheDocument();
    expect(screen.getByText('补充信息（已脱敏）')).toBeInTheDocument();
    expect(screen.getByText(/returned_rounds/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /删除/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /编辑/ })).not.toBeInTheDocument();
  });

  it('区分失败与中断结果', async () => {
    vi.mocked(getAdminOperationLogs).mockResolvedValueOnce({
      items: [
        operationLog({ id: 18, outcome: 'failed', status_code: 404 }),
        operationLog({
          id: 19,
          request_id: 'request-interrupted-19',
          outcome: 'started',
          status_code: null,
          completed_at: null,
        }),
      ],
      next_cursor: null,
    });

    render(<AdminAuditLogPanel />);

    await waitFor(() => {
      expect(screen.getAllByText('失败').length).toBeGreaterThan(1);
      expect(screen.getAllByText('中断 / 结果未知').length).toBeGreaterThan(1);
    });
  });

  it('用户信息读取展示具体查阅对象', async () => {
    vi.mocked(getAdminOperationLogs).mockResolvedValueOnce({
      items: [operationLog({ action: 'user.list', risk_level: 'normal' })],
      next_cursor: null,
    });

    render(<AdminAuditLogPanel />);

    const row = await screen.findByRole('row', { name: /查看用户列表/ });
    expect(row).toHaveTextContent('user.list');
    expect(row).toHaveTextContent('用户信息查阅');
    expect(row).not.toHaveTextContent('高危');
  });

  it('仅查看会话步骤原文标记为高危', async () => {
    vi.mocked(getAdminOperationLogs).mockResolvedValueOnce({
      items: [operationLog({
        action: 'step.view',
        risk_level: 'high',
        target_type: 'step',
        target_id: '628',
        step_record_id: 628,
      })],
      next_cursor: null,
    });

    render(<AdminAuditLogPanel />);

    const row = await screen.findByRole('row', { name: /查看会话步骤原文/ });
    expect(row).toHaveTextContent('step.view');
    expect(row).toHaveTextContent('高危 · 会话步骤原文');
  });

  it('按审计目的展示管理操作分类', async () => {
    vi.mocked(getAdminOperationLogs).mockResolvedValueOnce({
      items: [
        operationLog({ id: 21, action: 'audit_log.list' }),
        operationLog({ id: 22, action: 'user.password.reset' }),
        operationLog({ id: 23, action: 'sandbox.update' }),
        operationLog({ id: 24, action: 'user.delete' }),
        operationLog({ id: 25, action: 'audit_log.export' }),
        operationLog({ id: 26, action: 'step.review.update' }),
        operationLog({ id: 27, action: 'mcp.test' }),
      ],
      next_cursor: null,
    });

    render(<AdminAuditLogPanel />);

    expect(await screen.findByRole('row', { name: /查看操作日志/ })).toHaveTextContent('审计日志查阅');
    expect(screen.getByRole('row', { name: /重置用户密码/ })).toHaveTextContent('账号与权限');
    expect(screen.getByRole('row', { name: /更新沙箱配置/ })).toHaveTextContent('配置变更');
    expect(screen.getByRole('row', { name: /删除用户/ })).toHaveTextContent('删除操作');
    expect(screen.getByRole('row', { name: /导出操作日志/ })).toHaveTextContent('数据导出');
    expect(screen.getByRole('row', { name: /更新步骤审阅/ })).toHaveTextContent('治理操作');
    expect(screen.getByRole('row', { name: /测试 MCP 连接/ })).toHaveTextContent('外联测试');
    expect(screen.queryByText('关注访问')).not.toBeInTheDocument();
    expect(screen.queryByText('重要变更')).not.toBeInTheDocument();
  });

  it('下拉筛选选择后立即执行服务端查询', async () => {
    render(<AdminAuditLogPanel />);
    await screen.findByText('session.view');

    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        cursor: 'cursor-page-2',
      }));
    });
    expect(screen.getByText('第 2 页 · 每页最多 50 条')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('风险级别'), { target: { value: 'high' } });
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        risk_level: 'high',
        cursor: undefined,
      }));
    });
    expect(screen.getByText('第 1 页 · 每页最多 50 条')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('结果'), { target: { value: 'failed' } });
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        risk_level: 'high',
        outcome: 'failed',
      }));
    });

    fireEvent.change(screen.getByLabelText('操作类型'), { target: { value: 'step.view' } });
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        action: 'step.view',
        risk_level: 'high',
        outcome: 'failed',
      }));
    });
  });

  it('操作类型筛选只展示会持久化的审计动作', async () => {
    render(<AdminAuditLogPanel />);
    await screen.findByText('session.view');

    const actionSelect = screen.getByLabelText('操作类型') as HTMLSelectElement;
    const actionValues = Array.from(actionSelect.options).map((option) => option.value);

    expect(actionValues).toContain('session.view');
    expect(actionValues).toContain('session.list');
    expect(actionValues).toContain('user.list');
    expect(actionValues).toContain('audit_log.list');
    expect(actionValues).toContain('step.view');
    expect(actionValues).toContain('audit_log.export');
    expect(actionValues).not.toEqual(expect.arrayContaining([
      'overview.read',
      'system.read',
      'sandbox.list',
      'model.list',
      'model_group.list',
      'mcp.list',
      'tool_permission.list',
    ]));
  });

  it('下拉筛选不会提交尚未查询的时间和 ID 草稿', async () => {
    render(<AdminAuditLogPanel />);
    await screen.findByText('session.view');

    fireEvent.change(screen.getByLabelText('目标用户'), { target: { value: 'draft-user' } });
    fireEvent.change(screen.getByLabelText('会话 ID'), { target: { value: 'draft-session' } });
    fireEvent.change(screen.getByLabelText('结束时间'), { target: { value: '2026-08-02T12:00' } });
    fireEvent.change(screen.getByLabelText('风险级别'), { target: { value: 'high' } });

    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        risk_level: 'high',
        target_user_id: undefined,
        session_id: undefined,
        to: undefined,
      }));
    });
  });

  it('日志加载失败时展示服务端错误', async () => {
    vi.mocked(getAdminOperationLogs).mockRejectedValueOnce({
      response: { data: { detail: '管理员操作审计暂不可用' } },
    });

    render(<AdminAuditLogPanel />);

    expect(await screen.findByText('管理员操作审计暂不可用')).toBeInTheDocument();
  });

  it('刷新令牌变化时保留筛选并重新查询', async () => {
    const { rerender } = render(<AdminAuditLogPanel refreshToken={0} />);
    await screen.findByText('session.view');

    fireEvent.change(screen.getByLabelText('目标用户'), { target: { value: 'user-refresh' } });
    fireEvent.click(screen.getByRole('button', { name: '查询' }));
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        target_user_id: 'user-refresh',
      }));
    });
    const callsBeforeRefresh = vi.mocked(getAdminOperationLogs).mock.calls.length;

    rerender(<AdminAuditLogPanel refreshToken={1} />);

    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenCalledTimes(callsBeforeRefresh + 1);
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        target_user_id: 'user-refresh',
      }));
    });
  });

  it('提交服务端筛选并使用游标前后翻页', async () => {
    vi.mocked(getAdminOperationLogs).mockImplementation(async (params) => ({
      items: [operationLog({ id: params.cursor ? 18 : 17 })],
      next_cursor: params.cursor ? null : 'cursor-page-2',
    }));
    render(<AdminAuditLogPanel />);

    await screen.findByText('session.view');
    fireEvent.change(screen.getByLabelText('操作类型'), { target: { value: 'step.view' } });
    fireEvent.change(screen.getByLabelText('风险级别'), { target: { value: 'high' } });
    fireEvent.change(screen.getByLabelText('目标用户'), { target: { value: 'user-2' } });
    fireEvent.change(screen.getByLabelText('会话 ID'), { target: { value: 'session-2' } });
    fireEvent.change(screen.getByLabelText('结果'), { target: { value: 'failed' } });
    fireEvent.click(screen.getByRole('button', { name: '查询' }));

    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        action: 'step.view',
        risk_level: 'high',
        target_user_id: 'user-2',
        session_id: 'session-2',
        outcome: 'failed',
        cursor: undefined,
        limit: 50,
      }));
    });

    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        cursor: 'cursor-page-2',
      }));
    });
    expect(screen.getByText('第 2 页 · 每页最多 50 条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '上一页' }));
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: undefined }));
    });
  });

  it('按当前服务端筛选导出 CSV', async () => {
    const createObjectURL = vi.fn(() => 'blob:audit-log');
    const revokeObjectURL = vi.fn();
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, writable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, writable: true, value: revokeObjectURL });
    render(<AdminAuditLogPanel />);

    await screen.findByText('session.view');
    fireEvent.change(screen.getByLabelText('操作类型'), { target: { value: 'step.view' } });
    fireEvent.change(screen.getByLabelText('风险级别'), { target: { value: 'high' } });
    fireEvent.click(screen.getByRole('button', { name: '查询' }));
    await waitFor(() => {
      expect(getAdminOperationLogs).toHaveBeenLastCalledWith(expect.objectContaining({
        action: 'step.view',
        risk_level: 'high',
      }));
    });

    fireEvent.click(screen.getByRole('button', { name: '导出当前筛选' }));

    await waitFor(() => {
      expect(exportAdminOperationLogs).toHaveBeenCalledWith(expect.objectContaining({
        action: 'step.view',
        risk_level: 'high',
      }));
      expect(createObjectURL).toHaveBeenCalledTimes(1);
    });
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:audit-log');
    clickSpy.mockRestore();
  });

  it('导出失败时展示 Blob 中的后端错误详情', async () => {
    vi.mocked(exportAdminOperationLogs).mockRejectedValueOnce({
      response: {
        status: 400,
        data: new Blob([
          JSON.stringify({ detail: '导出结果超过 50000 条，请缩小筛选范围' }),
        ], { type: 'application/json' }),
      },
    });
    render(<AdminAuditLogPanel />);
    await screen.findByText('session.view');

    fireEvent.click(screen.getByRole('button', { name: '导出当前筛选' }));

    expect(await screen.findByText('导出结果超过 50000 条，请缩小筛选范围')).toBeInTheDocument();
  });
});
