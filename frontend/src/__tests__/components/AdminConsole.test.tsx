import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import AdminConsole from '../../components/AdminConsole';
import { apiService } from '../../services/api';
import {
  createAdminSandboxProfile,
  createAdminLdapUser,
  createAdminSimpleUser,
  deleteAdminModel,
  deleteAdminUser,
  exportAdminUsers,
  getAdminLLMCallRecordDetail,
  getAdminOverview,
  getAdminRoundsTree,
  getAdminSandboxProfiles,
  getAdminSessionRounds,
  getAdminSystem,
  getAdminUserLoginEvents,
  getAdminUsers,
  resetAdminSimpleUserPassword,
  setAdminSandboxProfileDefault,
  setAdminSandboxProfileEnabled,
  updateAdminSandboxProfile,
  updateAdminUserAdmin,
  updateAdminUserEnabled,
  updateAdminUserSandboxProfile,
  updateAdminUserTokenLimits,
  updateAdminLLMCallReview,
  type AdminSandboxProfile,
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
  createAdminSandboxProfile: vi.fn(),
  createAdminModel: vi.fn(),
  createAdminModelPermissionGroup: vi.fn(),
  createAdminSimpleUser: vi.fn(),
  createAdminLdapUser: vi.fn(),
  deleteAdminModel: vi.fn(),
  deleteAdminUser: vi.fn(),
  exportAdminUsers: vi.fn(),
  getAdminModels: vi.fn(),
  getAdminModelPermissionGroups: vi.fn(),
  getAdminOverview: vi.fn(),
  getAdminRoundsTree: vi.fn(),
  getAdminSessionRounds: vi.fn(),
  getAdminLLMCallRecordDetail: vi.fn(),
  getAdminUsers: vi.fn(),
  getAdminSandboxProfiles: vi.fn(),
  getAdminSystem: vi.fn(),
  getAdminUserLoginEvents: vi.fn(),
  updateAdminSandboxProfile: vi.fn(),
  updateAdminModel: vi.fn(),
  updateAdminModelPermissionGroupModels: vi.fn(),
  updateAdminModelPermissionGroupUsers: vi.fn(),
  updateAdminModelSettings: vi.fn(),
  setAdminSandboxProfileDefault: vi.fn(),
  setAdminSandboxProfileEnabled: vi.fn(),
  updateAdminUserSandboxProfile: vi.fn(),
  updateAdminUserEnabled: vi.fn(),
  updateAdminUserAdmin: vi.fn(),
  updateAdminUserTokenLimits: vi.fn(),
  resetAdminSimpleUserPassword: vi.fn(),
  updateAdminLLMCallReview: vi.fn(),
}));

vi.mock('../../components/AdminMcpCatalogPanel', () => ({
  default: ({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) => (
    <><div>官方 MCP 目录面板</div><button type="button" onClick={() => onDirtyChange?.(true)}>模拟修改官方 MCP</button></>
  ),
}));

vi.mock('../../components/AdminToolPermissionsPanel', () => ({
  default: () => <div>平台工具权限面板</div>,
}));

vi.mock('../../components/AdminAuditLogPanel', () => ({
  default: () => <div>管理员操作日志面板</div>,
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
    last_login_ip: '198.51.100.7',
    created_by: 'admin',
    created_at: '2026-05-01T08:00:00',
    updated_at: '2026-05-10T09:00:00',
    ...overrides,
  };
}

function makeSandboxProfile(overrides: Partial<AdminSandboxProfile> = {}): AdminSandboxProfile {
  return {
    id: 'default-profile',
    name: '默认沙箱',
    description: null,
    department: '默认',
    domain: '127.0.0.1:8080',
    protocol: 'http',
    api_key_set: true,
    use_server_proxy: true,
    is_default: true,
    enabled: true,
    version: 1,
    bound_users: 1,
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
    vi.mocked(getAdminSessionRounds).mockResolvedValue({
      session_id: 'session-1',
      rounds: [],
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

    vi.mocked(getAdminSandboxProfiles).mockResolvedValue({
      profiles: [makeSandboxProfile()],
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
      database: {
        pool: {
          url_database: 'open_capy_box',
          pool_class: 'QueuePool',
          status: 'Pool size: 10  Connections in pool: 8 Current Overflow: 0 Current Checked out connections: 2',
          size: 10,
          checked_in: 8,
          checked_out: 2,
          overflow: -7,
          configured: {
            pool_size: 10,
            max_overflow: 20,
            pool_timeout_seconds: 5,
            pool_recycle_seconds: 1800,
          },
        },
        activity: [
          {
            state: 'active',
            wait_event_type: 'none',
            wait_event: 'none',
            count: 1,
          },
        ],
        blocked_locks: 0,
        long_queries: [],
      },
    });

    vi.mocked(getAdminUserLoginEvents).mockResolvedValue({
      user_id: 'demo',
      events: [
        {
          id: 1,
          user_id: 'demo',
          username: 'demo',
          auth_type: 'simple',
          ip_address: '198.51.100.7',
          user_agent: 'pytest-browser',
          login_at: '2026-05-10T09:00:00',
        },
      ],
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
    vi.mocked(deleteAdminModel).mockResolvedValue({
      model_id: 'demo-model',
      deleted: true,
      replacement_model_id: null,
      sessions_reassigned: 0,
      defaults_reassigned: [],
    });
    vi.mocked(deleteAdminUser).mockResolvedValue({ user_id: 'demo', deleted: true });
    vi.mocked(exportAdminUsers).mockResolvedValue(new Blob(['user_id\r\ndemo'], { type: 'text/csv' }));
    vi.mocked(createAdminSandboxProfile).mockResolvedValue(makeSandboxProfile());
    vi.mocked(updateAdminSandboxProfile).mockResolvedValue(makeSandboxProfile());
    vi.mocked(setAdminSandboxProfileDefault).mockResolvedValue(makeSandboxProfile());
    vi.mocked(setAdminSandboxProfileEnabled).mockResolvedValue(makeSandboxProfile());
    vi.mocked(updateAdminUserSandboxProfile).mockResolvedValue({
      sandbox_profile_id: 'default-profile',
      sandbox_profile_name: '默认沙箱',
      sandbox_profile_source: 'default',
      sandbox_profile_error: null,
      sandbox_id: null,
      sandbox_status: 'none',
      sandbox_active_profile_id: null,
      sandbox_active_profile_version: null,
      sandbox_desired_profile_id: 'default-profile',
      sandbox_desired_profile_version: 1,
      sandbox_needs_recreate: false,
    });
  });

  it('导航中应显示 Session监控 文案', async () => {
    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    expect(screen.getByRole('button', { name: /Session监控/ })).toBeInTheDocument();
  });

  it('官方 MCP 导航应懒加载独立目录面板', async () => {
    render(<AdminConsole />);
    await waitFor(() => expect(getAdminOverview).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: '官方 MCP' }));

    expect(await screen.findByText('官方 MCP 目录面板')).toBeInTheDocument();
  });

  it('官方 MCP 有未保存修改时切换后台模块需要确认', async () => {
    render(<AdminConsole />);
    await waitFor(() => expect(getAdminOverview).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: '官方 MCP' }));
    fireEvent.click(await screen.findByRole('button', { name: '模拟修改官方 MCP' }));

    fireEvent.click(screen.getByRole('button', { name: '工具权限' }));
    const discardDialog = screen.getByRole('alertdialog', { name: '离开并放弃官方 MCP 修改？' });
    expect(discardDialog).toBeInTheDocument();
    expect(screen.getByText('官方 MCP 目录面板')).toBeInTheDocument();

    await waitFor(() => expect(discardDialog).toHaveFocus());
    fireEvent.keyDown(discardDialog, { key: 'Tab', shiftKey: true });
    expect(screen.getByRole('button', { name: '放弃修改并离开' })).toHaveFocus();

    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(screen.queryByRole('alertdialog', { name: '离开并放弃官方 MCP 修改？' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '工具权限' }));
    fireEvent.click(screen.getByRole('button', { name: '放弃修改并离开' }));

    expect(await screen.findByText('平台工具权限面板')).toBeInTheDocument();
  });

  it('工具权限导航应与官方 MCP 独立并懒加载平台策略面板', async () => {
    render(<AdminConsole />);
    await waitFor(() => expect(getAdminOverview).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: '工具权限' }));

    expect(await screen.findByText('平台工具权限面板')).toBeInTheDocument();
    expect(screen.queryByText('官方 MCP 目录面板')).not.toBeInTheDocument();
  });

  it('操作日志导航应懒加载独立只读面板', async () => {
    render(<AdminConsole />);
    await waitFor(() => expect(getAdminOverview).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: '操作日志' }));

    expect(await screen.findByText('管理员操作日志面板')).toBeInTheDocument();
  });

  it('系统监控应展示数据库运行态诊断', async () => {
    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /系统监控/ }));

    await waitFor(() => {
      expect(getAdminSystem).toHaveBeenCalledWith(24);
      expect(screen.getByText('数据库运行态')).toBeInTheDocument();
    });

    expect(screen.getByText(/QueuePool/)).toBeInTheDocument();
    expect(screen.getByText('Checked Out')).toBeInTheDocument();
    expect(screen.getByText('Pool Timeout')).toBeInTheDocument();
    expect(screen.getByText('Active Overflow')).toBeInTheDocument();
    expect(screen.getByText('连接活动')).toBeInTheDocument();
    expect(screen.getByText('暂无超过 30 秒的活动查询')).toBeInTheDocument();
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

  it('展开 Session 时才懒加载 Round 明细', async () => {
    vi.mocked(getAdminRoundsTree).mockResolvedValue({
      total_sessions: 1,
      offset: 0,
      limit: 5,
      sessions: [
        {
          session_id: 'session-lazy',
          user_id: 'admin',
          session_title: '懒加载会话',
          rounds_count: 1,
          last_round_at: '2026-04-26T10:00:00',
          sum_step_count: 1,
          total_tokens: 10,
          llm_calls: 1,
          error_calls: 0,
          compaction_steps: 0,
          total_duration_s: 0,
          status: 'completed',
          rounds_loaded: false,
          rounds: [],
        },
      ],
    });
    vi.mocked(getAdminSessionRounds).mockResolvedValue({
      session_id: 'session-lazy',
      rounds: [
        {
          round_id: 'round-lazy',
          session_id: 'session-lazy',
          user_id: 'admin',
          session_title: '懒加载会话',
          run_kind: 'main',
          parent_run_id: null,
          root_run_id: 'round-lazy',
          subagent_edge_id: null,
          subagent_type: null,
          subagent_description: null,
          subagent_prompt_preview: null,
          subagent_child_count: 0,
          status: 'completed',
          step_count: 1,
          started_at: '2026-04-26T10:00:00',
          completed_at: '2026-04-26T10:00:01',
          duration_s: 1,
          user_message_preview: '懒加载问题',
          final_response_preview: '懒加载回答',
          total_tokens: 10,
          llm_calls: 1,
          error_calls: 0,
          compaction_steps: 0,
          steps: [],
        },
      ],
    });

    render(<AdminConsole />);

    fireEvent.click(screen.getByRole('button', { name: /Session监控/ }));

    await waitFor(() => {
      expect(screen.getByText('懒加载会话')).toBeInTheDocument();
    });
    expect(getAdminSessionRounds).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /懒加载会话/ }));

    await waitFor(() => {
      expect(getAdminSessionRounds).toHaveBeenCalledWith('session-lazy', {
        status: 'all',
        search: undefined,
      });
    });
    expect(await screen.findByText('懒加载问题')).toBeInTheDocument();
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
              run_kind: 'main',
              parent_run_id: null,
              root_run_id: 'round-1',
              subagent_edge_id: null,
              subagent_type: null,
              subagent_description: null,
              subagent_prompt_preview: null,
              subagent_child_count: 0,
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
                  request_messages: JSON.stringify([
                    {
                      role: 'assistant',
                      tool_calls: [
                        {
                          function: {
                            name: 'sub_agent',
                            arguments: '{"description":"vlog\\u5b8c\\u6574\\u5236\\u4f5c\\u65b9\\u6848","prompt":"\\u4f60\\u597d"}',
                          },
                        },
                      ],
                    },
                  ]),
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
      expect(screen.getByText(/vlog完整制作方案/)).toBeInTheDocument();
      expect(screen.getByText(/compaction_pre_tokens/)).toBeInTheDocument();
    });

    fireEvent.change(screen.getByDisplayValue('没问题'), { target: { value: '有问题' } });

    await waitFor(() => {
      expect(updateAdminLLMCallReview).toHaveBeenCalledWith(101, '有问题');
    });
  });

  it('Session监控应区分主Agent和子Agent Round', async () => {
    vi.mocked(getAdminRoundsTree).mockResolvedValue({
      total_sessions: 1,
      offset: 0,
      limit: 20,
      sessions: [
        {
          session_id: 'session-subagent',
          user_id: 'admin',
          session_title: '请求提问',
          rounds_count: 2,
          last_round_at: '2026-06-05T10:00:00',
          sum_step_count: 3,
          total_tokens: 1000,
          llm_calls: 3,
          error_calls: 0,
          compaction_steps: 0,
          total_duration_s: 10,
          status: 'completed',
          rounds: [
            {
              round_id: 'parent-run',
              session_id: 'session-subagent',
              user_id: 'admin',
              session_title: '请求提问',
              run_kind: 'main',
              parent_run_id: null,
              root_run_id: 'parent-run',
              subagent_edge_id: null,
              subagent_type: null,
              subagent_description: null,
              subagent_prompt_preview: null,
              subagent_child_count: 1,
              status: 'completed',
              step_count: 2,
              started_at: '2026-06-05T10:00:00',
              completed_at: '2026-06-05T10:00:05',
              duration_s: 5,
              user_message_preview: '你能不能给她派生长一点的任务',
              final_response_preview: 'done',
              total_tokens: 600,
              llm_calls: 2,
              error_calls: 0,
              compaction_steps: 0,
              steps: [],
            },
            {
              round_id: 'child-run',
              session_id: 'session-subagent',
              user_id: 'admin',
              session_title: '请求提问',
              run_kind: 'main',
              parent_run_id: 'parent-run',
              root_run_id: 'parent-run',
              subagent_edge_id: 'edge-1',
              subagent_type: 'general-purpose',
              subagent_description: 'vlog完整制作方案+分镜脚本+爆款标题',
              subagent_prompt_preview: 'You are a child agent run spawned by a parent OpenCapyBox agent.',
              subagent_child_count: 0,
              status: 'completed',
              step_count: 1,
              started_at: '2026-06-05T10:00:01',
              completed_at: '2026-06-05T10:00:06',
              duration_s: 5,
              user_message_preview: 'You are a child agent run spawned by a parent OpenCapyBox agent.',
              final_response_preview: 'child done',
              total_tokens: 400,
              llm_calls: 1,
              error_calls: 0,
              compaction_steps: 0,
              steps: [],
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
      expect(screen.getByText('请求提问')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /请求提问/ }));

    expect(screen.getByText('主Agent')).toBeInTheDocument();
    expect(screen.getByText('子Agent')).toBeInTheDocument();
    expect(screen.getByText('派生 1')).toBeInTheDocument();
    expect(screen.getByText('general-purpose')).toBeInTheDocument();
    expect(screen.getByText('vlog完整制作方案+分镜脚本+爆款标题')).toBeInTheDocument();
    expect(screen.getByText(/parent parent-run/)).toBeInTheDocument();
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
        sandbox_profile_id: null,
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
        sandbox_profile_id: null,
      });
    });
  });

  it('沙箱配置保存成功但用户列表刷新失败时应提示刷新失败', async () => {
    vi.mocked(getAdminUsers)
      .mockResolvedValueOnce({
        summary: {
          users_total: 1,
          admins_total: 1,
          active_total: 1,
          running_total: 0,
        },
        users: [],
      })
      .mockRejectedValueOnce(new Error('refresh users failed'));

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /沙箱管理/ }));

    await waitFor(() => {
      expect(screen.getByText('沙箱后端列表')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    fireEvent.click(screen.getByRole('button', { name: /保存后端/ }));

    await waitFor(() => {
      expect(updateAdminSandboxProfile).toHaveBeenCalled();
      expect(screen.getByText('沙箱后端已更新，但用户列表刷新失败，请手动刷新')).toBeInTheDocument();
    });
    expect(screen.queryByText('沙箱配置操作失败，请稍后重试')).not.toBeInTheDocument();
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
    const createObjectURLMock = vi.fn<[Blob], string>(() => 'blob:users-csv');
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

      await waitFor(() => {
        expect(exportAdminUsers).toHaveBeenCalledWith(['demo']);
        expect(createObjectURLMock).toHaveBeenCalledTimes(1);
      });
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

  it('用户导出失败时展示 Blob 中的后端错误详情', async () => {
    vi.mocked(getAdminUsers).mockResolvedValue({
      summary: {
        users_total: 1,
        admins_total: 0,
        active_total: 1,
        running_total: 0,
      },
      users: [makeAdminUser()],
    });
    vi.mocked(exportAdminUsers).mockRejectedValueOnce({
      response: {
        status: 404,
        data: new Blob([
          JSON.stringify({ detail: '用户不存在: demo' }),
        ], { type: 'application/json' }),
      },
    });
    render(<AdminConsole />);
    await waitFor(() => expect(getAdminOverview).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: /用户管理/ }));
    await screen.findByText('demo');

    fireEvent.click(screen.getByRole('button', { name: /导出/ }));

    expect(await screen.findByText('用户不存在: demo')).toBeInTheDocument();
  });

  it('用户管理页可查看登录历史', async () => {
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
      expect(screen.getByText('IP 198.51.100.7')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '更多 demo' }));
    fireEvent.click(screen.getByRole('button', { name: '登录历史 demo' }));

    await waitFor(() => {
      expect(getAdminUserLoginEvents).toHaveBeenCalledWith('demo', 50);
    });

    expect(screen.getByRole('dialog', { name: '登录历史' })).toBeInTheDocument();
    expect(screen.getAllByText('198.51.100.7').length).toBeGreaterThan(0);
    expect(screen.getByText('pytest-browser')).toBeInTheDocument();
  });

  it('LDAP 用户也可查看登录历史', async () => {
    vi.mocked(getAdminUsers).mockResolvedValue({
      summary: {
        users_total: 1,
        admins_total: 0,
        active_total: 1,
        running_total: 0,
      },
      users: [
        makeAdminUser({
          user_id: 'ldap-user',
          username: 'ldap-user',
          auth_type: 'ldap',
          last_login_ip: '203.0.113.9',
        }),
      ],
    });
    vi.mocked(getAdminUserLoginEvents).mockResolvedValue({
      user_id: 'ldap-user',
      events: [
        {
          id: 2,
          user_id: 'ldap-user',
          username: 'ldap-user',
          auth_type: 'ldap',
          ip_address: '203.0.113.9',
          user_agent: 'ldap-browser',
          login_at: '2026-05-10T09:30:00',
        },
      ],
    });

    render(<AdminConsole />);

    await waitFor(() => {
      expect(getAdminOverview).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: /用户管理/ }));

    await waitFor(() => {
      expect(screen.getByText('IP 203.0.113.9')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '更多 ldap-user' }));
    fireEvent.click(screen.getByRole('button', { name: '登录历史 ldap-user' }));

    await waitFor(() => {
      expect(getAdminUserLoginEvents).toHaveBeenCalledWith('ldap-user', 50);
    });

    expect(screen.getByRole('dialog', { name: '登录历史' })).toBeInTheDocument();
    expect(screen.getAllByText('203.0.113.9').length).toBeGreaterThan(0);
    expect(screen.getByText('ldap-browser')).toBeInTheDocument();
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
              run_kind: 'main',
              parent_run_id: null,
              root_run_id: 'round-obj',
              subagent_edge_id: null,
              subagent_type: null,
              subagent_description: null,
              subagent_prompt_preview: null,
              subagent_child_count: 0,
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
              run_kind: 'main',
              parent_run_id: null,
              root_run_id: 'round-404',
              subagent_edge_id: null,
              subagent_type: null,
              subagent_description: null,
              subagent_prompt_preview: null,
              subagent_child_count: 0,
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
