import { Fragment, useCallback, useEffect, useMemo, useState, type ComponentType } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertTriangle,
  BarChart3,
  ChevronDown,
  ChevronRight,
  Gauge,
  LayoutDashboard,
  LogOut,
  RefreshCw,
  ShieldCheck,
  Users,
} from 'lucide-react';

import { apiService } from '../services/api';
import {
  getAdminLLMCallRecordDetail,
  getAdminOverview,
  getAdminRoundsTree,
  getAdminSystem,
  getAdminUsers,
  updateAdminLLMCallReview,
  type AdminOverview,
  type AdminRoundStepItem,
  type AdminRoundTreeResponse,
  type AdminSystemResponse,
  type AdminUsersResponse,
} from '../services/adminApi';
import './AdminConsole.css';

type AdminTab = 'overview' | 'rounds' | 'users' | 'system';

const NAV_ITEMS: Array<{ id: AdminTab; label: string; icon: ComponentType<{ size?: string | number }> }> = [
  { id: 'overview', label: '概览', icon: LayoutDashboard },
  { id: 'rounds', label: 'Session监控', icon: BarChart3 },
  { id: 'users', label: '用户管理', icon: Users },
  { id: 'system', label: '系统监控', icon: Gauge },
];

function formatDateTime(iso: string | null): string {
  if (!iso) return '-';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

function formatNumber(value: number): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-';
  return value.toLocaleString('zh-CN');
}

function formatDetailText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatPercent(value: number | null): string {
  if (value === null || Number.isNaN(value)) return '-';
  return `${(value * 100).toFixed(1)}%`;
}

function parseJsonLike(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed) return value;
  const isObjectLike = (trimmed.startsWith('{') && trimmed.endsWith('}'))
    || (trimmed.startsWith('[') && trimmed.endsWith(']'));
  if (!isObjectLike) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function normalizeText(value: unknown, maxLen: number = 140): string {
  if (value === null || value === undefined) return '-';
  let text = '';
  if (typeof value === 'string') {
    text = value;
  } else if (Array.isArray(value)) {
    const pieces = value
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const record = item as Record<string, unknown>;
          if (typeof record.text === 'string') return record.text;
          if (typeof record.content === 'string') return record.content;
        }
        return '';
      })
      .filter(Boolean);
    text = pieces.join(' ');
  } else {
    text = String(value);
  }
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (!normalized) return '-';
  return normalized.length > maxLen ? `${normalized.slice(0, maxLen)}...` : normalized;
}

function formatFinishReason(reason: string | null): string {
  if (!reason) return '未知';
  if (reason === 'stop') return '正常结束';
  if (reason === 'tool_calls') return '调用工具后结束';
  if (reason === 'length') return '达到输出上限';
  if (reason === 'content_filter') return '内容被过滤';
  return reason;
}

function hasStepRawPayload(step: AdminRoundStepItem): boolean {
  const hasValue = (value: unknown): boolean => {
    if (value === null || value === undefined) return false;
    if (typeof value === 'string') return value.trim().length > 0;
    return true;
  };

  return Boolean(
    hasValue(step.request_messages)
      || hasValue(step.request_tools)
      || hasValue(step.response_content)
      || hasValue(step.response_thinking)
      || hasValue(step.response_tool_calls),
  );
}

function buildStepAnalysis(step: AdminRoundStepItem): {
  provider: string;
  model: string;
  latestUserMessage: string;
  requestToolCount: number;
  responseToolCallCount: number;
  responseErrorText: string;
  responseContentChars: number;
  responseThinkingChars: number;
  compactionRate: number | null;
  suggestedReview: '没问题' | '建议复核' | '有问题';
  findings: string[];
} {
  const parsedRequestMessages = parseJsonLike(step.request_messages);
  const parsedRequestTools = parseJsonLike(step.request_tools);
  const parsedResponseToolCalls = parseJsonLike(step.response_tool_calls);
  const parsedResponseContent = parseJsonLike(step.response_content);
  const parsedResponseThinking = parseJsonLike(step.response_thinking);

  let provider = '-';
  let model = '-';
  let messageList: unknown[] = [];

  if (Array.isArray(parsedRequestMessages) && parsedRequestMessages.length > 0) {
    const first = parsedRequestMessages[0];
    if (first && typeof first === 'object') {
      const record = first as Record<string, unknown>;
      if (typeof record.provider === 'string') provider = record.provider;
      if (typeof record.model === 'string') model = record.model;
      if (Array.isArray(record.messages)) {
        messageList = record.messages;
      } else {
        messageList = parsedRequestMessages;
      }
    }
  }

  const latestUserMessage = (() => {
    const userMessages = messageList
      .filter((item) => !!item && typeof item === 'object')
      .map((item) => item as Record<string, unknown>)
      .filter((item) => item.role === 'user');
    if (userMessages.length === 0) return '-';
    const lastUser = userMessages[userMessages.length - 1];
    return normalizeText(lastUser.content, 160);
  })();

  const requestToolCount = Array.isArray(parsedRequestTools) ? parsedRequestTools.length : 0;
  const responseToolCallCount = Array.isArray(parsedResponseToolCalls) ? parsedResponseToolCalls.length : 0;
  const responseErrorText = normalizeText(step.response_error, 180);
  const responseContentChars = formatDetailText(parsedResponseContent).length;
  const responseThinkingChars = formatDetailText(parsedResponseThinking).length;
  const compactionRate = step.compaction_pre_tokens > 0
    ? step.compaction_tokens_saved / step.compaction_pre_tokens
    : null;

  const findings: string[] = [];
  if (step.response_error) {
    findings.push('存在 response_error，优先排查工具调用结果和上下文完整性。');
  }
  if (step.finish_reason === 'length') {
    findings.push('finish_reason=length，回答可能被截断。');
  }
  if (step.compaction_summary_quality_repair_count > 0) {
    findings.push('发生压缩质量修复，建议核对关键事实是否被摘要遗漏。');
  }
  if (step.compaction_emergency_truncate_dropped_rounds > 0) {
    findings.push('发生紧急截断，当前轮上下文压力较高。');
  }
  if ((step.completion_latency_s ?? 0) >= 45) {
    findings.push('完成延迟偏高（>=45s），建议关注模型负载和工具耗时。');
  }
  if (findings.length === 0) {
    findings.push('未发现明显异常，建议结合业务语义做抽样复核。');
  }

  let suggestedReview: '没问题' | '建议复核' | '有问题' = '没问题';
  if (step.response_error) {
    suggestedReview = '有问题';
  } else if (
    step.finish_reason === 'length'
    || step.compaction_summary_quality_repair_count > 0
    || step.compaction_emergency_truncate_dropped_rounds > 0
    || (step.completion_latency_s ?? 0) >= 45
  ) {
    suggestedReview = '建议复核';
  }

  return {
    provider,
    model,
    latestUserMessage,
    requestToolCount,
    responseToolCallCount,
    responseErrorText,
    responseContentChars,
    responseThinkingChars,
    compactionRate,
    suggestedReview,
    findings,
  };
}

function statusClass(status: string): string {
  if (status === 'running' || status === 'resumed') return 'running';
  if (status === 'ok' || status === 'active' || status === 'completed' || status === 'success') return 'ok';
  if (status === 'failed' || status === 'error' || status === 'cancelled') return 'error';
  if (status === 'admin' || status === 'user') return status;
  return 'paused';
}

export default function AdminConsole() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<AdminTab>('overview');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [rounds, setRounds] = useState<AdminRoundTreeResponse | null>(null);
  const [users, setUsers] = useState<AdminUsersResponse | null>(null);
  const [systemData, setSystemData] = useState<AdminSystemResponse | null>(null);

  const [roundStatus, setRoundStatus] = useState('all');
  const [roundSearch, setRoundSearch] = useState('');
  const [roundPage, setRoundPage] = useState(1);
  const [roundPageSize, setRoundPageSize] = useState(5);
  const [reviewUpdatingIds, setReviewUpdatingIds] = useState<Record<number, boolean>>({});
  const [stepLoadingIds, setStepLoadingIds] = useState<Record<number, boolean>>({});
  const [reviewError, setReviewError] = useState('');
  const [stepDetailError, setStepDetailError] = useState('');

  const handleLogout = () => {
    apiService.logout();
    navigate('/login', { replace: true });
  };

  const handleStepReviewChange = useCallback(async (llmRecordId: number, manualReviewStatus: string) => {
    setReviewError('');
    setReviewUpdatingIds((prev) => ({ ...prev, [llmRecordId]: true }));
    try {
      await updateAdminLLMCallReview(llmRecordId, manualReviewStatus);
      setRounds((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          sessions: prev.sessions.map((session) => ({
            ...session,
            rounds: session.rounds.map((round) => ({
              ...round,
              steps: round.steps.map((step) => (
                step.llm_record_id === llmRecordId
                  ? { ...step, manual_review_status: manualReviewStatus }
                  : step
              )),
            })),
          })),
        };
      });
    } catch (err) {
      console.error('Failed to update manual review status:', err);
      const maybeErr = err as {
        response?: {
          status?: number;
          data?: {
            detail?: string;
          };
        };
      };
      if (maybeErr.response?.status === 404) {
        const detail = maybeErr.response?.data?.detail;
        setReviewError(
          typeof detail === 'string' && detail
            ? `llm_record_id=${llmRecordId} 写回失败（404）：${detail}`
            : `llm_record_id=${llmRecordId} 写回失败（404），记录可能已被清理，请刷新后重试。`,
        );
      } else {
        setReviewError('更新审阅状态失败，请重试');
      }
    } finally {
      setReviewUpdatingIds((prev) => {
        const next = { ...prev };
        delete next[llmRecordId];
        return next;
      });
    }
  }, []);

  const handleStepDetailOpen = useCallback(async (llmRecordId: number) => {
    setStepDetailError('');
    setStepLoadingIds((prev) => ({ ...prev, [llmRecordId]: true }));
    try {
      const detail = await getAdminLLMCallRecordDetail(llmRecordId);
      setRounds((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          sessions: prev.sessions.map((session) => ({
            ...session,
            rounds: session.rounds.map((round) => ({
              ...round,
              steps: round.steps.map((step) => (
                step.llm_record_id === llmRecordId
                  ? {
                    ...step,
                    request_message_count: detail.request_message_count,
                    request_messages: detail.request_messages,
                    request_tools: detail.request_tools,
                    finish_reason: detail.finish_reason,
                    response_error: detail.response_error,
                    response_preview: detail.response_preview,
                    response_content: detail.response_content,
                    response_thinking: detail.response_thinking,
                    response_tool_calls: detail.response_tool_calls,
                    usage_prompt_tokens: detail.usage_prompt_tokens,
                    usage_completion_tokens: detail.usage_completion_tokens,
                    usage_total_tokens: detail.usage_total_tokens,
                    first_token_latency_s: detail.first_token_latency_s,
                    completion_latency_s: detail.completion_latency_s,
                    compaction_triggered: detail.compaction_triggered,
                    compaction_pre_tokens: detail.compaction_pre_tokens,
                    compaction_post_tokens: detail.compaction_post_tokens,
                    compaction_tokens_saved: detail.compaction_tokens_saved,
                    compaction_microcompact_compacted_messages: detail.compaction_microcompact_compacted_messages,
                    compaction_summary_generated_count: detail.compaction_summary_generated_count,
                    compaction_summary_reused_count: detail.compaction_summary_reused_count,
                    compaction_summary_quality_repair_count: detail.compaction_summary_quality_repair_count,
                    compaction_emergency_truncate_dropped_rounds: detail.compaction_emergency_truncate_dropped_rounds,
                    manual_review_status: detail.manual_review_status,
                    created_at: detail.created_at,
                  }
                  : step
              )),
            })),
          })),
        };
      });
    } catch (err) {
      console.error('Failed to load step detail:', err);
      setStepDetailError('加载 step 详情失败，请重试');
    } finally {
      setStepLoadingIds((prev) => {
        const next = { ...prev };
        delete next[llmRecordId];
        return next;
      });
    }
  }, []);

  const refreshActiveTab = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      if (activeTab === 'overview') {
        setOverview(await getAdminOverview(7));
      }
      if (activeTab === 'rounds') {
        setRounds(await getAdminRoundsTree({
          limit: roundPageSize,
          offset: (roundPage - 1) * roundPageSize,
          status: roundStatus,
          search: roundSearch || undefined,
        }));
      }
      if (activeTab === 'users') {
        setUsers(await getAdminUsers());
      }
      if (activeTab === 'system') {
        setSystemData(await getAdminSystem(24));
      }
    } catch (err) {
      console.error('Failed to load admin data:', err);
      setError('加载管理数据失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  }, [activeTab, roundPage, roundPageSize, roundSearch, roundStatus]);

  useEffect(() => {
    setRoundPage(1);
  }, [roundSearch, roundStatus]);

  useEffect(() => {
    if (!rounds) return;
    const totalPages = Math.max(1, Math.ceil(rounds.total_sessions / roundPageSize));
    if (roundPage > totalPages) {
      setRoundPage(totalPages);
    }
  }, [roundPage, roundPageSize, rounds]);

  useEffect(() => {
    if (!apiService.isAdminUser()) {
      navigate('/', { replace: true });
      return;
    }
    refreshActiveTab();
  }, [navigate, refreshActiveTab]);

  const currentUser = useMemo(() => apiService.getUserId() || '-', []);

  return (
    <div className="admin-console">
      <aside className="admin-sidebar">
        <div className="admin-brand">
          <img src="/logo.jpg" alt="OpenCapyBox" />
          <div>
            <div className="admin-brand-title">OpenCapyBox</div>
            <div className="admin-brand-sub">Admin Console</div>
          </div>
        </div>

        <div className="admin-nav-list">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const isActive = activeTab === item.id;
            return (
              <button
                key={item.id}
                className={`admin-nav-btn ${isActive ? 'active' : ''}`}
                onClick={() => setActiveTab(item.id)}
              >
                <Icon size={15} />
                <span>{item.label}</span>
                {item.id === 'rounds' && rounds?.total_sessions ? (
                  <span className="admin-nav-badge">{rounds.total_sessions}</span>
                ) : null}
              </button>
            );
          })}
        </div>

        <div className="admin-nav-grow" />
        <div className="admin-nav-user">
          <div>当前账号：{currentUser}</div>
          <div>角色：管理员</div>
          <button className="admin-button admin-logout-btn" onClick={handleLogout}>
            <LogOut size={14} />
            退出登录
          </button>
        </div>
      </aside>

      <main className="admin-main">
        <div className="admin-topbar">
          <div>
            <h1>{NAV_ITEMS.find((item) => item.id === activeTab)?.label}</h1>
            <div className="admin-topbar-sub">管理后台数据每次进入模块时实时拉取</div>
          </div>
          <div style={{ flex: 1 }} />
          <button className="admin-button" onClick={refreshActiveTab}>
            <RefreshCw size={14} />
            刷新
          </button>
        </div>

        <div className="admin-content">
          {error ? <div className="admin-error">{error}</div> : null}
          {loading ? <div className="admin-loading">加载中...</div> : null}

          {!loading && !error && activeTab === 'overview' && overview ? (
            <OverviewPanel data={overview} />
          ) : null}

          {!loading && !error && activeTab === 'rounds' ? (
            <RoundsPanel
              data={rounds}
              roundStatus={roundStatus}
              roundSearch={roundSearch}
              roundPage={roundPage}
              roundPageSize={roundPageSize}
              reviewUpdatingIds={reviewUpdatingIds}
              stepLoadingIds={stepLoadingIds}
              reviewError={reviewError}
              stepDetailError={stepDetailError}
              onStatusChange={setRoundStatus}
              onSearchChange={setRoundSearch}
              onPageChange={setRoundPage}
              onPageSizeChange={(value) => {
                setRoundPage(1);
                setRoundPageSize(value);
              }}
              onStepReviewChange={handleStepReviewChange}
              onStepDetailOpen={handleStepDetailOpen}
            />
          ) : null}

          {!loading && !error && activeTab === 'users' ? (
            <UsersPanel data={users} />
          ) : null}

          {!loading && !error && activeTab === 'system' ? (
            <SystemPanel data={systemData} />
          ) : null}
        </div>
      </main>
    </div>
  );
}

function OverviewPanel({ data }: { data: AdminOverview }) {
  return (
    <>
      <div className="admin-grid-4">
        <MetricCard label="用户总数" value={formatNumber(data.summary.users_total)} hint={`管理员 ${data.summary.admins_total}`} />
        <MetricCard label="24h Rounds" value={formatNumber(data.summary.rounds_24h)} hint={`运行中 ${data.summary.rounds_running}`} />
        <MetricCard label="24h Tokens" value={formatNumber(data.summary.tokens_24h)} hint={`LLM 调用 ${data.summary.llm_calls_24h}`} />
        <MetricCard
          label="平均完成延迟"
          value={data.summary.avg_completion_latency_24h !== null ? `${data.summary.avg_completion_latency_24h}s` : '-'}
          hint={`定时任务失败 ${data.summary.cron_failed_24h}`}
        />
      </div>

      <div className="admin-trend-grid">
        <div className="admin-card">
          <div className="admin-card-header">
            <h3 className="admin-card-header-title">近 {data.window_days} 天 Rounds 趋势</h3>
          </div>
          <div className="admin-card-body admin-trend-list">
            {data.trends.map((item) => (
              <div className="admin-trend-row" key={item.date}>
                <span>{item.date}</span>
                <strong>{formatNumber(item.rounds)}</strong>
              </div>
            ))}
          </div>
        </div>

        <div className="admin-card">
          <div className="admin-card-header">
            <h3 className="admin-card-header-title">近 {data.window_days} 天 Token 趋势</h3>
          </div>
          <div className="admin-card-body admin-trend-list">
            {data.trends.map((item) => (
              <div className="admin-trend-row" key={item.date}>
                <span>{item.date}</span>
                <strong>{formatNumber(item.tokens)}</strong>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

function RoundsPanel({
  data,
  roundStatus,
  roundSearch,
  roundPage,
  roundPageSize,
  reviewUpdatingIds,
  stepLoadingIds,
  reviewError,
  stepDetailError,
  onStatusChange,
  onSearchChange,
  onPageChange,
  onPageSizeChange,
  onStepReviewChange,
  onStepDetailOpen,
}: {
  data: AdminRoundTreeResponse | null;
  roundStatus: string;
  roundSearch: string;
  roundPage: number;
  roundPageSize: number;
  reviewUpdatingIds: Record<number, boolean>;
  stepLoadingIds: Record<number, boolean>;
  reviewError: string;
  stepDetailError: string;
  onStatusChange: (value: string) => void;
  onSearchChange: (value: string) => void;
  onPageChange: (value: number) => void;
  onPageSizeChange: (value: number) => void;
  onStepReviewChange: (llmRecordId: number, manualReviewStatus: string) => Promise<void>;
  onStepDetailOpen: (llmRecordId: number) => Promise<void>;
}) {
  const [expandedSessions, setExpandedSessions] = useState<Record<string, boolean>>({});
  const [expandedRounds, setExpandedRounds] = useState<Record<string, boolean>>({});
  const [expandedStepDetails, setExpandedStepDetails] = useState<Record<number, boolean>>({});
  const totalSessions = data?.total_sessions || 0;
  const totalPages = Math.max(1, Math.ceil(totalSessions / roundPageSize));

  useEffect(() => {
    setExpandedSessions({});
    setExpandedRounds({});
    setExpandedStepDetails({});
  }, [data?.offset, data?.total_sessions, roundStatus, roundSearch]);

  const toggleSession = (sessionId: string) => {
    setExpandedSessions((prev) => ({ ...prev, [sessionId]: !prev[sessionId] }));
  };

  const toggleRound = (roundId: string) => {
    setExpandedRounds((prev) => ({ ...prev, [roundId]: !prev[roundId] }));
  };

  const toggleStepDetail = async (step: AdminRoundStepItem) => {
    const llmRecordId = step.llm_record_id;
    const isExpanded = !!expandedStepDetails[llmRecordId];
    if (!isExpanded && !hasStepRawPayload(step)) {
      await onStepDetailOpen(llmRecordId);
    }
    setExpandedStepDetails((prev) => ({ ...prev, [llmRecordId]: !prev[llmRecordId] }));
  };

  return (
    <div className="admin-card">
      <div className="admin-card-header">
        <h3 className="admin-card-header-title">Session监控（Session → Round → Step）</h3>
        <div style={{ flex: 1 }} />
        <div className="admin-toolbar">
          <select className="admin-select" value={roundStatus} onChange={(e) => onStatusChange(e.target.value)}>
            <option value="all">全部状态</option>
            <option value="running">running</option>
            <option value="completed">completed</option>
            <option value="failed">failed</option>
            <option value="interrupted">interrupted</option>
            <option value="cancelled">cancelled</option>
          </select>
          <input
            className="admin-input"
            value={roundSearch}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="搜索消息内容..."
          />
          <select
            className="admin-select"
            aria-label="每页数量"
            value={String(roundPageSize)}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
          >
            <option value="5">5 / 页</option>
            <option value="10">10 / 页</option>
            <option value="15">15 / 页</option>
          </select>
        </div>
      </div>
      <div className="admin-table-wrap">
        {reviewError ? <div className="admin-step-review-error">{reviewError}</div> : null}
        {stepDetailError ? <div className="admin-step-review-error">{stepDetailError}</div> : null}
        <table className="admin-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>用户</th>
              <th>状态</th>
              <th>Rounds</th>
              <th>Tokens</th>
              <th>LLM调用</th>
              <th>压缩步数</th>
              <th>最近时间</th>
            </tr>
          </thead>
          <tbody>
            {(data?.sessions || []).map((session) => {
              const sessionExpanded = !!expandedSessions[session.session_id];
              const SessionChevron = sessionExpanded ? ChevronDown : ChevronRight;
              return (
                <Fragment key={session.session_id}>
                  <tr className="admin-session-row">
                    <td>
                      <button className="admin-tree-toggle" onClick={() => toggleSession(session.session_id)}>
                        <SessionChevron size={14} />
                        <span>{session.session_title || '未命名会话'}</span>
                      </button>
                      <div className="admin-subline">{session.session_id}</div>
                    </td>
                    <td>{session.user_id || '-'}</td>
                    <td>
                      <span className={`admin-status ${statusClass(session.status)}`}>{session.status}</span>
                    </td>
                    <td>{session.rounds_count}</td>
                    <td>{formatNumber(session.total_tokens)}</td>
                    <td>{session.llm_calls}</td>
                    <td>{session.compaction_steps}</td>
                    <td>{formatDateTime(session.last_round_at)}</td>
                  </tr>

                  {sessionExpanded ? (
                    <tr>
                      <td className="admin-nested-cell" colSpan={8}>
                        <table className="admin-table admin-subtable">
                          <thead>
                            <tr>
                              <th>Round</th>
                              <th>状态</th>
                              <th>Steps</th>
                              <th>Tokens</th>
                              <th>LLM调用</th>
                              <th>压缩步数</th>
                              <th>耗时</th>
                              <th>开始时间</th>
                            </tr>
                          </thead>
                          <tbody>
                            {session.rounds.map((round) => {
                              const roundExpanded = !!expandedRounds[round.round_id];
                              const RoundChevron = roundExpanded ? ChevronDown : ChevronRight;
                              return (
                                <Fragment key={round.round_id}>
                                  <tr className="admin-round-row">
                                    <td>
                                      <button className="admin-tree-toggle" onClick={() => toggleRound(round.round_id)}>
                                        <RoundChevron size={14} />
                                        <span>{round.user_message_preview || '无用户消息'}</span>
                                      </button>
                                      <div className="admin-subline">{round.round_id}</div>
                                    </td>
                                    <td>
                                      <span className={`admin-status ${statusClass(round.status)}`}>{round.status}</span>
                                    </td>
                                    <td>{round.step_count}</td>
                                    <td>{formatNumber(round.total_tokens)}</td>
                                    <td>{round.llm_calls}</td>
                                    <td>{round.compaction_steps}</td>
                                    <td>{round.duration_s.toFixed(2)}s</td>
                                    <td>{formatDateTime(round.started_at)}</td>
                                  </tr>

                                  {roundExpanded ? (
                                    <tr>
                                      <td className="admin-nested-cell admin-step-cell" colSpan={8}>
                                        <table className="admin-table admin-subtable admin-step-table">
                                          <thead>
                                            <tr>
                                              <th>Step</th>
                                              <th>消息数</th>
                                              <th>Prompt</th>
                                              <th>Completion</th>
                                              <th>Total</th>
                                              <th>首Token</th>
                                              <th>完成延迟</th>
                                              <th>结束原因</th>
                                              <th>压缩</th>
                                              <th>审阅</th>
                                              <th>时间</th>
                                              <th>详情</th>
                                            </tr>
                                          </thead>
                                          <tbody>
                                            {round.steps.map((step) => {
                                              const detailExpanded = !!expandedStepDetails[step.llm_record_id];
                                              const analysis = buildStepAnalysis(step);
                                              const rawDetailItems = [
                                                { key: 'request_messages', title: '请求消息', value: step.request_messages },
                                                { key: 'request_tools', title: '可用工具', value: step.request_tools },
                                                { key: 'response_content', title: '模型回答', value: step.response_content },
                                                { key: 'response_thinking', title: '思考过程', value: step.response_thinking },
                                                { key: 'response_tool_calls', title: '工具调用结果', value: step.response_tool_calls },
                                                { key: 'response_error', title: '错误信息', value: step.response_error },
                                              ];
                                              return (
                                                <Fragment key={step.llm_record_id}>
                                                  <tr>
                                                    <td>{step.step_index}</td>
                                                    <td>{step.request_message_count}</td>
                                                    <td>{formatNumber(step.usage_prompt_tokens)}</td>
                                                    <td>{formatNumber(step.usage_completion_tokens)}</td>
                                                    <td>{formatNumber(step.usage_total_tokens)}</td>
                                                    <td>{step.first_token_latency_s !== null ? `${step.first_token_latency_s}s` : '-'}</td>
                                                    <td>{step.completion_latency_s !== null ? `${step.completion_latency_s}s` : '-'}</td>
                                                    <td>{step.finish_reason || '-'}</td>
                                                    <td>{step.compaction_triggered ? '是' : '否'}</td>
                                                    <td>
                                                      <select
                                                        className="admin-select admin-review-select"
                                                        value={step.manual_review_status || '没问题'}
                                                        disabled={!!reviewUpdatingIds[step.llm_record_id]}
                                                        onChange={(e) => {
                                                          void onStepReviewChange(step.llm_record_id, e.target.value);
                                                        }}
                                                      >
                                                        <option value="没问题">没问题</option>
                                                        <option value="有问题">有问题</option>
                                                      </select>
                                                    </td>
                                                    <td>{formatDateTime(step.created_at)}</td>
                                                    <td>
                                                      <button
                                                        className="admin-link-button"
                                                        disabled={!!stepLoadingIds[step.llm_record_id]}
                                                        onClick={() => {
                                                          void toggleStepDetail(step);
                                                        }}
                                                      >
                                                        {stepLoadingIds[step.llm_record_id] ? '加载中...' : detailExpanded ? '收起' : '详情'}
                                                      </button>
                                                    </td>
                                                  </tr>

                                                  {detailExpanded ? (
                                                    <tr>
                                                      <td className="admin-step-detail-cell" colSpan={12}>
                                                        <div className="admin-step-analysis-block">
                                                          <div className="admin-step-analysis-title">管理员分析摘要</div>
                                                          <div className="admin-step-analysis-grid">
                                                            <div className="admin-step-analysis-card">
                                                              <div className="admin-step-analysis-card-title">请求概览</div>
                                                              <div className="admin-step-analysis-row"><span>Provider</span><strong>{analysis.provider}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>模型</span><strong>{analysis.model}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>消息条数</span><strong>{formatNumber(step.request_message_count)}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>声明工具数</span><strong>{analysis.requestToolCount}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>用户诉求摘要</span><strong>{analysis.latestUserMessage}</strong></div>
                                                            </div>

                                                            <div className="admin-step-analysis-card">
                                                              <div className="admin-step-analysis-card-title">响应概览</div>
                                                              <div className="admin-step-analysis-row"><span>结束原因</span><strong>{formatFinishReason(step.finish_reason)}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>工具调用次数</span><strong>{analysis.responseToolCallCount}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>回答字符数</span><strong>{formatNumber(analysis.responseContentChars)}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>思考字符数</span><strong>{formatNumber(analysis.responseThinkingChars)}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>response_error</span><strong>{analysis.responseErrorText}</strong></div>
                                                            </div>

                                                            <div className="admin-step-analysis-card">
                                                              <div className="admin-step-analysis-card-title">性能与压缩</div>
                                                              <div className="admin-step-analysis-row"><span>Prompt / Completion / Total</span><strong>{formatNumber(step.usage_prompt_tokens)} / {formatNumber(step.usage_completion_tokens)} / {formatNumber(step.usage_total_tokens)}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>首 Token 延迟</span><strong>{step.first_token_latency_s !== null ? `${step.first_token_latency_s}s` : '-'}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>完成延迟</span><strong>{step.completion_latency_s !== null ? `${step.completion_latency_s}s` : '-'}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>压缩触发</span><strong>{step.compaction_triggered ? '是' : '否'}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>压缩节省率</span><strong>{formatPercent(analysis.compactionRate)}</strong></div>
                                                            </div>
                                                          </div>

                                                          <div className={`admin-step-analysis-suggestion ${analysis.suggestedReview === '有问题' ? 'error' : analysis.suggestedReview === '建议复核' ? 'warn' : 'ok'}`}>
                                                            建议审阅结论：{analysis.suggestedReview}
                                                          </div>

                                                          <div className="admin-step-analysis-findings">
                                                            {analysis.findings.map((item) => (
                                                              <div key={item} className="admin-step-analysis-finding">- {item}</div>
                                                            ))}
                                                          </div>
                                                        </div>

                                                        <div className="admin-step-detail-raw-title">原始字段（排障证据）</div>
                                                        <div className="admin-step-detail-grid">
                                                          {rawDetailItems.map((item) => (
                                                            <div className="admin-step-detail-card" key={item.key}>
                                                              <div className="admin-step-detail-title">{item.title}（{item.key}）</div>
                                                              <pre>{formatDetailText(item.value)}</pre>
                                                            </div>
                                                          ))}
                                                        </div>

                                                        <div className="admin-step-compaction-grid">
                                                          <div>是否触发压缩（compaction_triggered）: {step.compaction_triggered ? '是' : '否'}</div>
                                                          <div>压缩前 Token（compaction_pre_tokens）: {formatNumber(step.compaction_pre_tokens)}</div>
                                                          <div>压缩后 Token（compaction_post_tokens）: {formatNumber(step.compaction_post_tokens)}</div>
                                                          <div>节省 Token（compaction_tokens_saved）: {formatNumber(step.compaction_tokens_saved)}</div>
                                                          <div>
                                                            微压缩消息数（compaction_microcompact_compacted_messages）: {formatNumber(step.compaction_microcompact_compacted_messages)}
                                                          </div>
                                                          <div>
                                                            摘要生成次数（compaction_summary_generated_count）: {formatNumber(step.compaction_summary_generated_count)}
                                                          </div>
                                                          <div>
                                                            摘要复用次数（compaction_summary_reused_count）: {formatNumber(step.compaction_summary_reused_count)}
                                                          </div>
                                                          <div>
                                                            摘要质量修复次数（compaction_summary_quality_repair_count）: {formatNumber(step.compaction_summary_quality_repair_count)}
                                                          </div>
                                                          <div>
                                                            紧急截断丢弃轮数（compaction_emergency_truncate_dropped_rounds）: {formatNumber(step.compaction_emergency_truncate_dropped_rounds)}
                                                          </div>
                                                        </div>
                                                      </td>
                                                    </tr>
                                                  ) : null}
                                                </Fragment>
                                              );
                                            })}
                                          </tbody>
                                        </table>
                                        {round.steps.some((step) => !!step.response_error) ? (
                                          <div className="admin-step-error-tip">
                                            检测到该 Round 存在 step 级错误记录，可在 llm_call_records 表按 round_id={round.round_id} 进一步排查。
                                          </div>
                                        ) : null}
                                      </td>
                                    </tr>
                                  ) : null}
                                </Fragment>
                              );
                            })}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="admin-pagination">
        <div className="admin-pagination-info">
          共 {totalSessions} 个 Session，当前第 {roundPage} / {totalPages} 页
        </div>
        <div className="admin-pagination-actions">
          <button
            className="admin-button"
            disabled={roundPage <= 1}
            onClick={() => onPageChange(roundPage - 1)}
          >
            上一页
          </button>
          <button
            className="admin-button"
            disabled={roundPage >= totalPages}
            onClick={() => onPageChange(roundPage + 1)}
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  );
}

function UsersPanel({ data }: { data: AdminUsersResponse | null }) {
  return (
    <>
      <div className="admin-grid-4">
        <MetricCard label="用户总数" value={formatNumber(data?.summary.users_total || 0)} />
        <MetricCard label="管理员" value={formatNumber(data?.summary.admins_total || 0)} />
        <MetricCard label="活跃用户" value={formatNumber(data?.summary.active_total || 0)} />
        <MetricCard label="运行中用户" value={formatNumber(data?.summary.running_total || 0)} />
      </div>

      <div className="admin-card">
        <div className="admin-card-header">
          <h3 className="admin-card-header-title">用户管理（管理员 / 用户）</h3>
        </div>
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>用户ID</th>
                <th>角色</th>
                <th>状态</th>
                <th>Sessions</th>
                <th>Rounds</th>
                <th>运行中</th>
                <th>Tokens</th>
                <th>Cron任务</th>
                <th>最近活跃</th>
              </tr>
            </thead>
            <tbody>
              {(data?.users || []).map((item) => (
                <tr key={item.user_id}>
                  <td>{item.user_id}</td>
                  <td>
                    <span className={`admin-status ${statusClass(item.role)}`}>{item.role}</span>
                  </td>
                  <td>
                    <span className={`admin-status ${statusClass(item.status)}`}>{item.status}</span>
                  </td>
                  <td>{item.sessions_count}</td>
                  <td>{item.rounds_count}</td>
                  <td>{item.running_rounds}</td>
                  <td>{formatNumber(item.total_tokens)}</td>
                  <td>{item.cron_jobs_enabled} / {item.cron_jobs_total}</td>
                  <td>{formatDateTime(item.last_active_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function SystemPanel({ data }: { data: AdminSystemResponse | null }) {
  if (!data) {
    return <div className="admin-loading">暂无系统数据</div>;
  }

  const summary = data.summary;
  const compactionRatio = summary.llm_calls > 0
    ? `${((summary.compaction_calls / summary.llm_calls) * 100).toFixed(1)}%`
    : '0%';

  return (
    <>
      <div className="admin-grid-4">
        <MetricCard label="活跃会话(30m)" value={formatNumber(summary.active_sessions_30m)} />
        <MetricCard label="运行中Rounds" value={formatNumber(summary.running_rounds)} />
        <MetricCard
          label="P95 完成延迟"
          value={summary.p95_completion_latency_s !== null ? `${summary.p95_completion_latency_s}s` : '-'}
        />
        <MetricCard label="LLM响应错误" value={formatNumber(summary.llm_response_errors)} />
      </div>

      <div className="admin-trend-grid">
        <div className="admin-card">
          <div className="admin-card-header">
            <h3 className="admin-card-header-title">Round 状态分布（近 {data.window_hours} 小时）</h3>
          </div>
          <div className="admin-card-body admin-trend-list">
            {Object.entries(summary.round_status_counts).map(([status, count]) => (
              <div className="admin-trend-row" key={status}>
                <span>{status}</span>
                <strong>{formatNumber(count)}</strong>
              </div>
            ))}
          </div>
        </div>

        <div className="admin-card">
          <div className="admin-card-header">
            <h3 className="admin-card-header-title">Cron 状态分布（近 {data.window_hours} 小时）</h3>
          </div>
          <div className="admin-card-body admin-trend-list">
            {Object.entries(summary.cron_status_counts).map(([status, count]) => (
              <div className="admin-trend-row" key={status}>
                <span>{status}</span>
                <strong>{formatNumber(count)}</strong>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="admin-card">
        <div className="admin-card-header">
          <h3 className="admin-card-header-title">上下文压缩观测</h3>
        </div>
        <div className="admin-card-body">
          <div className="admin-grid-4">
            <MetricCard label="LLM调用" value={formatNumber(summary.llm_calls)} />
            <MetricCard label="触发压缩" value={`${formatNumber(summary.compaction_calls)} (${compactionRatio})`} />
            <MetricCard label="节省Token" value={formatNumber(summary.compaction_tokens_saved)} />
            <MetricCard label="质量修复" value={formatNumber(summary.compaction_quality_repairs)} />
          </div>
          {summary.compaction_emergency_drops > 0 ? (
            <div style={{ marginTop: 12, color: '#b84a3a', display: 'flex', alignItems: 'center', gap: 6 }}>
              <AlertTriangle size={14} />
              <span>检测到紧急截断 {summary.compaction_emergency_drops} 次，建议排查上下文窗口压力。</span>
            </div>
          ) : (
            <div style={{ marginTop: 12, color: '#2f8b5f', display: 'flex', alignItems: 'center', gap: 6 }}>
              <ShieldCheck size={14} />
              <span>最近窗口内未发生紧急截断。</span>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function MetricCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="admin-card admin-metric">
      <div className="admin-metric-label">{label}</div>
      <div className="admin-metric-value">{value}</div>
      {hint ? <div className="admin-metric-hint">{hint}</div> : null}
    </div>
  );
}
