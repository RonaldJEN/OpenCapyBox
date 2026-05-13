import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import AdminConsole from '../../components/AdminConsole';
import { apiService } from '../../services/api';
import {
  createAdminLdapUser,
  createAdminSimpleUser,
  deleteAdminUser,
  getAdminLLMCallRecordDetail,
  getAdminOverview,
  getAdminRoundsTree,
  getAdminSystem,
  getAdminUsers,
  resetAdminSimpleUserPassword,
  updateAdminUserAdmin,
  updateAdminUserEnabled,
  updateAdminUserTokenLimits,
  updateAdminLLMCallReview,
  type AdminUserItem,
} from '../../services/adminApi';

vi.mock('../../services/api', () => ({
  apiService: {
    isAdminUser: vi.fn(() => true),
    getUserId: vi.fn(() => 'admin'),
    logout: vi.fn(),
  },
}));

vi.mock('../../services/adminApi', () => ({
  createAdminSimpleUser: vi.fn(),
  createAdminLdapUser: vi.fn(),
  deleteAdminUser: vi.fn(),
  getAdminOverview: vi.fn(),
  getAdminRoundsTree: vi.fn(),
  getAdminLLMCallRecordDetail: vi.fn(),
  getAdminUsers: vi.fn(),
  getAdminSystem: vi.fn(),
  updateAdminUserEnabled: vi.fn(),
  updateAdminUserAdmin: vi.fn(),
  updateAdminUserTokenLimits: vi.fn(),
  resetAdminSimpleUserPassword: vi.fn(),
  updateAdminLLMCallReview: vi.fn(),
}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

function makeAdminUser(overrides: Partial<AdminUserItem> = {}): AdminUserItem {
  return {
    user_id: 'demo',
    username: 'demo',
    auth_type: 'simple',
    enabled: true,
    role: 'user',
    is_admin: false,
    status: 'active',
    sessions_count: 2,
    rounds_count: 5,
    running_rounds: 0,
    total_tokens: 1200,
    weekly_tokens_used: 200,
    monthly_tokens_used: 800,
    token_limit_per_week: 1000,
    token_limit_per_month: 5000,
    cron_jobs_total: 1,
    cron_jobs_enabled: 1,
    cron_failed_24h: 0,
    last_active_at: '2026-05-10T10:00:00',
    last_login_at: '2026-05-10T09:00:00',
    created_by: 'admin',
    created_at: '2026-05-01T08:00:00',
    updated_at: '2026-05-10T09:00:00',
    ...overrides,
  };
}

describe('AdminConsole 组件', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  beforeEach(() => {
    vi.clearAllMocks();

    vi.mocked(getAdminOverview).mockResolvedValue({
      window_days: 7,
      summary: {
        users_total: 1,
        admins_total: 1,
        sessions_total: 3,
        rounds_total: 10,
        rounds_24h: 4,
        rounds_running: 1,
        cron_jobs_total: 0,
        cron_jobs_enabled: 0,
        cron_failed_24h: 0,
        llm_calls_24h: 8,
        tokens_24h: 1000,
        avg_completion_latency_24h: 1.2,
      },
      trends: [
        { date: '2026-04-20', rounds: 2, tokens: 300 },
        { date: '2026-04-21', rounds: 3, tokens: 450 },
      ],
    });

    vi.mocked(getAdminRoundsTree).mockResolvedValue({
      total_sessions: 45,
      offset: 0,
      limit: 5,
      sessions: [],
    });

    vi.mocked(getAdminLLMCallRecordDetail).mockResolvedValue({
      llm_record_id: 1,
      round_id: 'round-1',
      step_index: 1,
      request_message_count: 0,
      request_messages: '[]',
      request_tools: '[]',
      finish_reason: 'stop',
      response_error: null,
      response_preview: '',
      response_content: '',
      response_thinking: '',
      response_tool_calls: '[]',
      usage_prompt_tokens: 0,
      usage_completion_tokens: 0,
      usage_total_tokens: 0,
      first_token_latency_s: null,
      completion_latency_s: null,
      compaction_triggered: false,
      compaction_pre_tokens: 0,
      compaction_post_tokens: 0,
      compaction_tokens_saved: 0,
      compaction_microcompact_compacted_messages: 0,
      compaction_summary_generated_count: 0,
      compaction_summary_reused_count: 0,
      compaction_summary_quality_repair_count: 0,
      compaction_emergency_truncate_dropped_rounds: 0,
      manual_review_status: '没问题',
      created_at: null,
    });

    vi.mocked(getAdminUsers).mockResolvedValue({
      summary: {
        users_total: 1,
        admins_total: 1,
        active_total: 1,
        running_total: 0,
      },
      users: [],
    });

    vi.mocked(getAdminSystem).mockResolvedValue({
      window_hours: 24,
      summary: {
        running_rounds: 0,
        active_sessions_30m: 0,
        round_status_counts: {},
        cron_status_counts: {},
        avg_completion_latency_s: null,
        p50_completion_latency_s: null,
        p95_completion_latency_s: null,
        avg_first_token_latency_s: null,
        llm_calls: 0,
        compaction_calls: 0,
        compaction_tokens_saved: 0,
        compaction_quality_repairs: 0,
        compaction_emergency_drops: 0,
        llm_response_errors: 0,
      },
    });

    vi.mocked(updateAdminLLMCallReview).mockResolvedValue({
      llm_record_id: 1,
      manual_review_status: '有问题',
    });

    const userPayload = makeAdminUser();
    vi.mocked(createAdminSimpleUser).mockResolvedValue(userPayload);
    vi.mocked(createAdminLdapUser).mockResolvedValue(makeAdminUser({ auth_type: 'ldap' }));
    vi.mocked(updateAdminUserEnabled).mockResolvedValue(userPayload);
    vi.mocked(updateAdminUserAdmin).mockResolvedValue(userPayload);
    vi.mocked(updateAdminUserTokenLimits).mockResolvedValue(userPayload);
    vi.mocked(resetAdminSimpleUserPassword).mockResolvedValue(userPayload);
    vi.mocked(deleteAdminUser).mockResolvedValue({ user_id: 'demo', deleted: true });
  });

  it('导航中应显示 Session监控 文案', async () => {
    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    expect(screen.getByRole('button', { name: /Session监控/ })).toBeInTheDocument();
  });

  it('点击退出登录应清理登录状态并跳转登录页', async () => {
    render(<AdminConsole />);

    await waitFor(() => {
      expect(screen.getByText('退出登录')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('退出登录'));

    expect(apiService.logout).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/login', { replace: true });
  });

  it('Session监控分页应按 offset/limit 请求下一页', async () => {
    vi.mocked(getAdminRoundsTree)
      .mockResolvedValueOnce({
        total_sessions: 45,
        offset: 0,
        limit: 5,
        sessions: [],
      })
      .mockResolvedValueOnce({
        total_sessions: 45,
        offset: 5,
        limit: 5,
        sessions: [],
      });

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /Session监控/ }));

    await waitFor(() => {
      expect(getAdminRoundsTree).toHaveBeenCalledWith({
        limit: 5,
        offset: 0,
        status: 'all',
        search: undefined,
      });
    });

    expect(screen.getByText('共 45 个 Session，当前第 1 / 9 页')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '下一页' }));

    await waitFor(() => {
      expect(getAdminRoundsTree).toHaveBeenLastCalledWith({
        limit: 5,
        offset: 5,
        status: 'all',
        search: undefined,
      });
    });
  });

  it('step详情可展开并可将审阅状态改为有问题', async () => {
    vi.mocked(getAdminRoundsTree).mockResolvedValue({
      total_sessions: 1,
      offset: 0,
      limit: 20,
      sessions: [
        {
          session_id: 'session-1',
          user_id: 'admin',
          session_title: '会话A',
          rounds_count: 1,
          last_round_at: '2026-04-26T10:00:00',
          sum_step_count: 1,
          total_tokens: 100,
          llm_calls: 1,
          error_calls: 0,
          compaction_steps: 1,
          total_duration_s: 1.2,
          status: 'completed',
          rounds: [
            {
              round_id: 'round-1',
              session_id: 'session-1',
              user_id: 'admin',
              session_title: '会话A',
              status: 'completed',
              step_count: 1,
              started_at: '2026-04-26T10:00:00',
              completed_at: '2026-04-26T10:00:05',
              duration_s: 5,
              user_message_preview: '用户提问',
              final_response_preview: '模型回答',
              total_tokens: 100,
              llm_calls: 1,
              error_calls: 0,
              compaction_steps: 1,
              steps: [
                {
                  llm_record_id: 101,
                  step_index: 1,
                  request_message_count: 3,
                  request_messages: '[{"role":"user","content":"hi"}]',
                  request_tools: '[]',
                  finish_reason: 'stop',
                  response_error: null,
                  response_preview: 'ok',
                  response_content: 'ok',
                  response_thinking: '',
                  response_tool_calls: '[]',
                  usage_prompt_tokens: 50,
                  usage_completion_tokens: 10,
                  usage_total_tokens: 60,
                  first_token_latency_s: 0.5,
                  completion_latency_s: 1.1,
                  compaction_triggered: true,
                  compaction_pre_tokens: 8000,
                  compaction_post_tokens: 6000,
                  compaction_tokens_saved: 2000,
                  compaction_microcompact_compacted_messages: 12,
                  compaction_summary_generated_count: 1,
                  compaction_summary_reused_count: 1,
                  compaction_summary_quality_repair_count: 0,
                  compaction_emergency_truncate_dropped_rounds: 0,
                  manual_review_status: '没问题',
                  created_at: '2026-04-26T10:00:02',
                },
              ],
            },
          ],
        },
      ],
    });

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /Session监控/ }));

    await waitFor(() => {
      expect(getAdminRoundsTree).toHaveBeenCalled();
      expect(screen.getByText('会话A')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /会话A/ }));
    fireEvent.click(screen.getByRole('button', { name: /用户提问/ }));
    fireEvent.click(screen.getByRole('button', { name: '详情' }));

    await waitFor(() => {
      expect(screen.getByText('管理员分析摘要')).toBeInTheDocument();
      expect(screen.getByText(/建议审阅结论：没问题/)).toBeInTheDocument();
      expect(screen.getByText(/request_messages/)).toBeInTheDocument();
      expect(screen.getByText(/compaction_pre_tokens/)).toBeInTheDocument();
    });

    fireEvent.change(screen.getByDisplayValue('没问题'), { target: { value: '有问题' } });

    await waitFor(() => {
      expect(updateAdminLLMCallReview).toHaveBeenCalledWith(101, '有问题');
    });
  });

  it('用户管理页可创建 simple 用户', async () => {
    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /用户管理/ }));

    await waitFor(() => {
      expect(getAdminUsers).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /新建用户/ }));

    expect(screen.queryByLabelText('显示名')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'demo3' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'pass123' } });
    fireEvent.change(screen.getByLabelText('周限额'), { target: { value: '1000' } });
    fireEvent.click(screen.getByText('访问全部用户与系统'));
    fireEvent.click(screen.getByRole('button', { name: /创建用户/ }));

    await waitFor(() => {
      expect(createAdminSimpleUser).toHaveBeenCalledWith({
        username: 'demo3',
        password: 'pass123',
        enabled: true,
        is_admin: true,
        token_limit_per_week: 1000,
        token_limit_per_month: null,
      });
    });
  });

  it('用户管理页可创建 ldap 用户', async () => {
    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /用户管理/ }));

    await waitFor(() => {
      expect(getAdminUsers).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /新建用户/ }));
    fireEvent.click(screen.getByRole('radio', { name: /LDAP/ }));
    fireEvent.change(screen.getByLabelText('域账号ID'), { target: { value: 'zhangsan' } });
    fireEvent.change(screen.getByLabelText('显示名'), { target: { value: '张三' } });
    fireEvent.click(screen.getByRole('button', { name: /创建用户/ }));

    await waitFor(() => {
      expect(createAdminLdapUser).toHaveBeenCalledWith({
        user_id: 'zhangsan',
        username: '张三',
        enabled: true,
        is_admin: false,
        token_limit_per_week: null,
        token_limit_per_month: null,
      });
    });
  });

  it('用户管理页导出当前可见用户为 CSV', async () => {
    vi.mocked(getAdminUsers).mockResolvedValue({
      summary: {
        users_total: 1,
        admins_total: 0,
        active_total: 1,
        running_total: 0,
      },
      users: [makeAdminUser()],
    });
    const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
    const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
    const createObjectURLMock = vi.fn((_: Blob) => 'blob:users-csv');
    const revokeObjectURLMock = vi.fn();
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, writable: true, value: createObjectURLMock });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, writable: true, value: revokeObjectURLMock });

    try {
      render(<AdminConsole />);

      await waitFor(() => {
        expect(getAdminOverview).toHaveBeenCalled();
      });

      fireEvent.click(screen.getByRole('button', { name: /用户管理/ }));

      await waitFor(() => {
        expect(screen.getAllByText('demo').length).toBeGreaterThan(0);
      });

      expect(screen.queryByRole('button', { name: '详情' })).not.toBeInTheDocument();
      expect(screen.queryByLabelText('选择全部用户')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('选择 demo')).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: /导出/ }));

      expect(createObjectURLMock).toHaveBeenCalledTimes(1);
      expect(createObjectURLMock.mock.calls[0][0]).toBeInstanceOf(Blob);
      expect(clickSpy).toHaveBeenCalledTimes(1);
      expect(revokeObjectURLMock).toHaveBeenCalledWith('blob:users-csv');
    } finally {
      clickSpy.mockRestore();
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
    }
  });

  it('用户管理页可更新启用权限限额并重置密码', async () => {
    vi.mocked(getAdminUsers).mockResolvedValue({
      summary: {
        users_total: 1,
        admins_total: 0,
        active_total: 1,
        running_total: 0,
      },
      users: [makeAdminUser()],
    });

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /用户管理/ }));

    await waitFor(() => {
      expect(screen.getAllByText('demo').length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getByRole('button', { name: '已启用' }));
    await waitFor(() => {
      expect(updateAdminUserEnabled).toHaveBeenCalledWith('demo', false);
    });

    fireEvent.change(screen.getByLabelText('demo 管理员权限'), { target: { value: 'admin' } });
    fireEvent.click(screen.getByRole('button', { name: '保存 demo 权限' }));
    await waitFor(() => {
      expect(updateAdminUserAdmin).toHaveBeenCalledWith('demo', true);
    });

    fireEvent.change(screen.getByLabelText('demo 周限额'), { target: { value: '2000' } });
    fireEvent.change(screen.getByLabelText('demo 月限额'), { target: { value: '8000' } });
    fireEvent.click(screen.getByRole('button', { name: '保存 demo 限额' }));

    await waitFor(() => {
      expect(updateAdminUserTokenLimits).toHaveBeenCalledWith('demo', {
        token_limit_per_week: 2000,
        token_limit_per_month: 8000,
      });
    });

    fireEvent.click(screen.getByRole('button', { name: '更多 demo' }));
    fireEvent.change(screen.getByLabelText('demo 新密码'), { target: { value: 'new-pass' } });
    fireEvent.click(screen.getByRole('button', { name: '重置 demo 密码' }));

    await waitFor(() => {
      expect(resetAdminSimpleUserPassword).toHaveBeenCalledWith('demo', 'new-pass');
    });

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.useFakeTimers();
    fireEvent.click(screen.getByRole('button', { name: '删除 demo' }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

  expect(confirmSpy).toHaveBeenCalledWith('确认永久删除用户 demo？该用户的会话、记忆、定时任务和沙箱文件都会被清理。');
    expect(deleteAdminUser).toHaveBeenCalledWith('demo');
    expect(screen.getByRole('status')).toHaveTextContent('用户已删除');

    act(() => {
      vi.advanceTimersByTime(2400);
    });

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('当前管理员账号不能在前端自降权或自删除', async () => {
    vi.mocked(getAdminUsers).mockResolvedValue({
      summary: {
        users_total: 1,
        admins_total: 1,
        active_total: 1,
        running_total: 0,
      },
      users: [makeAdminUser({ user_id: 'admin', username: 'admin', role: 'admin', is_admin: true })],
    });

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /用户管理/ }));

    await waitFor(() => {
      expect(screen.getByText('当前账号')).toBeInTheDocument();
    });

    expect(screen.getByLabelText('admin 管理员权限')).toBeDisabled();
    expect(screen.getByRole('button', { name: '已启用' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '更多 admin' }));
    expect(screen.getByRole('button', { name: '删除 admin' })).toBeDisabled();
  });

  it('step详情字段为对象时不应白屏', async () => {
    vi.mocked(getAdminRoundsTree).mockResolvedValue({
      total_sessions: 1,
      offset: 0,
      limit: 20,
      sessions: [
        {
          session_id: 'session-obj',
          user_id: 'admin',
          session_title: '对象字段会话',
          rounds_count: 1,
          last_round_at: '2026-04-26T10:00:00',
          sum_step_count: 1,
          total_tokens: 100,
          llm_calls: 1,
          error_calls: 0,
          compaction_steps: 1,
          total_duration_s: 1.2,
          status: 'completed',
          rounds: [
            {
              round_id: 'round-obj',
              session_id: 'session-obj',
              user_id: 'admin',
              session_title: '对象字段会话',
              status: 'completed',
              step_count: 1,
              started_at: '2026-04-26T10:00:00',
              completed_at: '2026-04-26T10:00:05',
              duration_s: 5,
              user_message_preview: '用户提问-对象',
              final_response_preview: '模型回答-对象',
              total_tokens: 100,
              llm_calls: 1,
              error_calls: 0,
              compaction_steps: 1,
              steps: [
                {
                  llm_record_id: 202,
                  step_index: 1,
                  request_message_count: 3,
                  request_messages: [{ role: 'user', content: 'hi' }] as unknown as string,
                  request_tools: [{ name: 'read_file' }] as unknown as string,
                  finish_reason: 'stop',
                  response_error: null,
                  response_preview: 'ok',
                  response_content: { text: 'ok' } as unknown as string,
                  response_thinking: { thought: 'x' } as unknown as string,
                  response_tool_calls: [{ id: 'tool-1' }] as unknown as string,
                  usage_prompt_tokens: 50,
                  usage_completion_tokens: 10,
                  usage_total_tokens: 60,
                  first_token_latency_s: 0.5,
                  completion_latency_s: 1.1,
                  compaction_triggered: true,
                  compaction_pre_tokens: 8000,
                  compaction_post_tokens: 6000,
                  compaction_tokens_saved: 2000,
                  compaction_microcompact_compacted_messages: 12,
                  compaction_summary_generated_count: 1,
                  compaction_summary_reused_count: 1,
                  compaction_summary_quality_repair_count: 0,
                  compaction_emergency_truncate_dropped_rounds: 0,
                  manual_review_status: '没问题',
                  created_at: '2026-04-26T10:00:02',
                },
              ],
            },
          ],
        },
      ],
    });

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /Session监控/ }));

    await waitFor(() => {
      expect(screen.getByText('对象字段会话')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /对象字段会话/ }));
    fireEvent.click(screen.getByRole('button', { name: /用户提问-对象/ }));
    fireEvent.click(screen.getByRole('button', { name: '详情' }));

    await waitFor(() => {
      expect(screen.getByText('管理员分析摘要')).toBeInTheDocument();
      expect(screen.getByText(/用户诉求摘要/)).toBeInTheDocument();
      expect(screen.getByText(/request_messages/)).toBeInTheDocument();
      expect(screen.getByText(/"role": "user"/)).toBeInTheDocument();
    });
  });

  it('审阅写回接口返回404时应仅提示失败并保持下拉可用', async () => {
    vi.mocked(getAdminRoundsTree).mockResolvedValue({
      total_sessions: 1,
      offset: 0,
      limit: 20,
      sessions: [
        {
          session_id: 'session-404',
          user_id: 'admin',
          session_title: '写回404会话',
          rounds_count: 1,
          last_round_at: '2026-04-26T10:00:00',
          sum_step_count: 1,
          total_tokens: 100,
          llm_calls: 1,
          error_calls: 0,
          compaction_steps: 1,
          total_duration_s: 1.2,
          status: 'completed',
          rounds: [
            {
              round_id: 'round-404',
              session_id: 'session-404',
              user_id: 'admin',
              session_title: '写回404会话',
              status: 'completed',
              step_count: 1,
              started_at: '2026-04-26T10:00:00',
              completed_at: '2026-04-26T10:00:05',
              duration_s: 5,
              user_message_preview: '用户提问-404',
              final_response_preview: '模型回答-404',
              total_tokens: 100,
              llm_calls: 1,
              error_calls: 0,
              compaction_steps: 1,
              steps: [
                {
                  llm_record_id: 303,
                  step_index: 1,
                  request_message_count: 3,
                  request_messages: '[]',
                  request_tools: '[]',
                  finish_reason: 'stop',
                  response_error: null,
                  response_preview: 'ok',
                  response_content: 'ok',
                  response_thinking: '',
                  response_tool_calls: '[]',
                  usage_prompt_tokens: 50,
                  usage_completion_tokens: 10,
                  usage_total_tokens: 60,
                  first_token_latency_s: 0.5,
                  completion_latency_s: 1.1,
                  compaction_triggered: true,
                  compaction_pre_tokens: 8000,
                  compaction_post_tokens: 6000,
                  compaction_tokens_saved: 2000,
                  compaction_microcompact_compacted_messages: 12,
                  compaction_summary_generated_count: 1,
                  compaction_summary_reused_count: 1,
                  compaction_summary_quality_repair_count: 0,
                  compaction_emergency_truncate_dropped_rounds: 0,
                  manual_review_status: '没问题',
                  created_at: '2026-04-26T10:00:02',
                },
              ],
            },
          ],
        },
      ],
    });
    vi.mocked(updateAdminLLMCallReview).mockRejectedValue({
      response: {
        status: 404,
        data: {
          detail: 'llm_call_record 不存在',
        },
      },
    });

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /Session监控/ }));

    await waitFor(() => {
      expect(screen.getByText('写回404会话')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /写回404会话/ }));
    fireEvent.click(screen.getByRole('button', { name: /用户提问-404/ }));

    fireEvent.change(screen.getByDisplayValue('没问题'), { target: { value: '有问题' } });

    await waitFor(() => {
      expect(screen.getByText(/llm_record_id=303 写回失败（404）/)).toBeInTheDocument();
      expect(screen.getByDisplayValue('没问题')).not.toBeDisabled();
    });
  });
});
