import { Fragment, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ComponentType, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertTriangle,
  BarChart3,
  Cable,
  ChevronDown,
  ChevronRight,
  Download,
  Gauge,
  History,
  Home,
  KeyRound,
  LayoutDashboard,
  LogOut,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Save,
  Search,
  Server,
  Shield,
  ShieldCheck,
  ScrollText,
  Trash2,
  Users,
  X,
} from 'lucide-react';

import { apiService } from '../services/api';
import AdminModelAccessPanel from './AdminModelAccessPanel';
import {
  createAdminLdapUser,
  createAdminSandboxProfile,
  createAdminSimpleUser,
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
  type AdminCreateLdapUserRequest,
  type AdminSandboxProfileSource,
  type AdminSandboxProfile,
  type AdminSandboxProfilePayload,
  type AdminSandboxProfilesResponse,
  type AdminCreateSimpleUserRequest,
  type AdminOverview,
  type AdminRoundStatus,
  type AdminRoundStatusFilter,
  type AdminRoundStepItem,
  type AdminRoundTreeResponse,
  type AdminSystemResponse,
  type AdminTokenLimitsUpdateRequest,
  type AdminUserItem,
  type AdminUserLoginEventsResponse,
  type AdminUsersResponse,
} from '../services/adminApi';
import { extractBlobAwareErrorMessage, extractErrorMessage } from '../utils/errorMessages';
import FeedbackMessage from './FeedbackMessage';
import './AdminConsole.css';

const LazyAdminMcpCatalogPanel = lazy(() => import('./AdminMcpCatalogPanel'));
const LazyAdminToolPermissionsPanel = lazy(() => import('./AdminToolPermissionsPanel'));
const LazyAdminAuditLogPanel = lazy(() => import('./AdminAuditLogPanel'));

type AdminTab = 'overview' | 'rounds' | 'users' | 'sandboxes' | 'models' | 'mcp' | 'permissions' | 'audit' | 'system';
type UserCreateMode = 'simple' | 'ldap';
type UserStatusFilter = 'all' | 'enabled' | 'disabled';
type UserRoleFilter = 'all' | 'admin' | 'user';
type UserAuthFilter = 'all' | 'simple' | 'ldap';
type UserSortKey = 'recent' | 'name' | 'tokens';
type OverviewDays = 7 | 14 | 30;

const ADMIN_ROUND_STATUS_OPTIONS: readonly AdminRoundStatus[] = [
  'running',
  'waiting_interaction',
  'completed',
  'failed',
  'cancelled',
  'max_steps_reached',
];

interface UserCreateFormValues {
  authType: UserCreateMode;
  userId: string;
  username: string;
  password: string;
  enabled: boolean;
  isAdmin: boolean;
  weeklyLimit: string;
  monthlyLimit: string;
  sandboxProfileId: string;
}

const NAV_ITEMS: Array<{ id: AdminTab; label: string; icon: ComponentType<{ size?: string | number }> }> = [
  { id: 'overview', label: '概览', icon: LayoutDashboard },
  { id: 'rounds', label: 'Session监控', icon: BarChart3 },
  { id: 'users', label: '用户管理', icon: Users },
  { id: 'sandboxes', label: '沙箱管理', icon: Server },
  { id: 'models', label: '模型权限', icon: KeyRound },
  { id: 'mcp', label: '官方 MCP', icon: Cable },
  { id: 'permissions', label: '工具权限', icon: Shield },
  { id: 'audit', label: '操作日志', icon: ScrollText },
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

function formatOptionalNumber(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-';
  return formatNumber(value);
}

function formatPoolOverflow(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-';
  return formatNumber(Math.max(0, value));
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

function formatDetailText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-';
  const readableValue = parseNestedJsonLike(value);
  if (typeof readableValue === 'string') return decodeEscapedUnicode(readableValue);
  try {
    return JSON.stringify(readableValue, null, 2);
  } catch {
    return String(readableValue);
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

function parseNestedJsonLike(value: unknown, depth: number = 0): unknown {
  if (depth > 12) return value;
  const parsed = parseJsonLike(value);
  if (parsed !== value) {
    return parseNestedJsonLike(parsed, depth + 1);
  }
  if (Array.isArray(value)) {
    return value.map((item) => parseNestedJsonLike(item, depth + 1));
  }
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value)) {
      out[key] = parseNestedJsonLike(child, depth + 1);
    }
    return out;
  }
  return value;
}

function decodeEscapedUnicode(value: string): string {
  return value.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex: string) => (
    String.fromCharCode(parseInt(hex, 16))
  ));
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

function getRoundDisplayTitle(round: {
  run_kind?: string;
  user_message_preview: string;
  subagent_description?: string | null;
  subagent_prompt_preview?: string | null;
}): string {
  if (isSubagentRoundLike(round)) {
    return round.subagent_description || round.subagent_prompt_preview || '子 Agent 任务';
  }
  return round.user_message_preview || '无用户消息';
}

function isSubagentRoundLike(round: {
  run_kind?: string;
  parent_run_id?: string | null;
  user_message_preview?: string | null;
}): boolean {
  if (round.run_kind === 'subagent') return true;
  const preview = round.user_message_preview || '';
  return preview.startsWith('You are a child agent run spawned by a parent OpenCapyBox agent.');
}

function getRoundKindLabel(round: { run_kind?: string; parent_run_id?: string | null; user_message_preview?: string | null }): string {
  return isSubagentRoundLike(round) ? '子Agent' : '主Agent';
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
  const parsedRequestMessages = parseNestedJsonLike(step.request_messages);
  const parsedRequestTools = parseNestedJsonLike(step.request_tools);
  const parsedResponseToolCalls = parseNestedJsonLike(step.response_tool_calls);
  const parsedResponseContent = parseNestedJsonLike(step.response_content);
  const parsedResponseThinking = parseNestedJsonLike(step.response_thinking);

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
  if (status === 'running') return 'running';
  if (status === 'ok' || status === 'active' || status === 'completed' || status === 'success') return 'ok';
  if (status === 'failed' || status === 'error' || status === 'cancelled') return 'error';
  if (status === 'admin' || status === 'user') return status;
  return 'paused';
}

function sandboxSourceLabel(source?: AdminSandboxProfileSource): string {
  if (source === 'explicit') return '固定绑定';
  if (source === 'missing') return '缺失';
  if (source === 'disabled') return '禁用';
  return '跟随默认';
}

function sandboxProfileOptionLabel(profile: AdminSandboxProfile): string {
  const suffix = profile.is_default ? '（当前默认）' : '';
  return `${profile.name}${suffix}`;
}

function assignableSandboxProfiles(
  profiles: AdminSandboxProfile[],
  currentProfileId?: string,
): AdminSandboxProfile[] {
  return profiles.filter((profile) => !profile.is_default || profile.id === currentProfileId);
}

function formatLimit(value: number | null): string {
  return value === null ? '不限' : formatNumber(value);
}

function parseLimitInput(value: string): number | null {
  return value.trim() === '' ? null : Number(value);
}

function userInitial(user: AdminUserItem): string {
  return (user.username || user.user_id).slice(0, 1).toUpperCase();
}

function userAvatarTone(user: AdminUserItem): string {
  const tones = ['sage', 'gold', 'clay', 'blue', 'rose'];
  const code = Array.from(user.user_id).reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return tones[code % tones.length];
}

function userSearchText(user: AdminUserItem): string {
  return `${user.user_id} ${user.username} ${user.auth_type} ${user.created_by || ''} ${user.sandbox_profile_name || ''} ${user.sandbox_profile_error || ''} ${(user.model_permission_group_names || []).join(' ')}`.toLowerCase();
}

function tokenPercent(used: number, limit: number | null): number {
  return limit === null || limit === 0 ? 0 : Math.min(100, Math.round((used / limit) * 100));
}

function apiErrorStatus(error: unknown): number | undefined {
  return (error as { response?: { status?: number } })?.response?.status;
}

function apiErrorDetail(error: unknown): string {
  return extractErrorMessage(error);
}

function downloadBlob(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export default function AdminConsole() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<AdminTab>('overview');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [rounds, setRounds] = useState<AdminRoundTreeResponse | null>(null);
  const [users, setUsers] = useState<AdminUsersResponse | null>(null);
  const [sandboxProfiles, setSandboxProfiles] = useState<AdminSandboxProfilesResponse | null>(null);
  const [systemData, setSystemData] = useState<AdminSystemResponse | null>(null);
  const [modelRefreshToken, setModelRefreshToken] = useState(0);
  const [mcpRefreshToken, setMcpRefreshToken] = useState(0);
  const [permissionRefreshToken, setPermissionRefreshToken] = useState(0);
  const [auditRefreshToken, setAuditRefreshToken] = useState(0);
  const [adminMcpDirty, setAdminMcpDirty] = useState(false);
  const [pendingAdminTab, setPendingAdminTab] = useState<AdminTab | null>(null);
  const adminMcpDiscardDialogRef = useRef<HTMLElement>(null);
  const adminMcpNavigationReturnFocusRef = useRef<HTMLElement | null>(null);

  const [roundStatus, setRoundStatus] = useState<AdminRoundStatusFilter>('all');
  const [roundSearch, setRoundSearch] = useState('');
  const debouncedRoundSearch = useDebouncedValue(roundSearch, 350);
  const [roundPage, setRoundPage] = useState(1);
  const [roundPageSize, setRoundPageSize] = useState(5);
  const [overviewDays, setOverviewDays] = useState<OverviewDays>(7);
  const [reviewUpdatingIds, setReviewUpdatingIds] = useState<Record<number, boolean>>({});
  const [stepLoadingIds, setStepLoadingIds] = useState<Record<number, boolean>>({});
  const [sessionRoundLoadingIds, setSessionRoundLoadingIds] = useState<Record<string, boolean>>({});
  const [reviewError, setReviewError] = useState('');
  const [stepDetailError, setStepDetailError] = useState('');
  const [sessionRoundLoadError, setSessionRoundLoadError] = useState('');
  const [userActionError, setUserActionError] = useState('');
  const [userActionMessage, setUserActionMessage] = useState('');
  const [userUpdatingKeys, setUserUpdatingKeys] = useState<Record<string, boolean>>({});
  const [sandboxActionError, setSandboxActionError] = useState('');
  const [sandboxActionMessage, setSandboxActionMessage] = useState('');
  const [sandboxUpdatingKeys, setSandboxUpdatingKeys] = useState<Record<string, boolean>>({});
  const currentUser = useMemo(() => apiService.getUserId() || '-', []);

  const requestAdminTabChange = (nextTab: AdminTab) => {
    if (nextTab === activeTab) return;
    if (activeTab === 'mcp' && adminMcpDirty) {
      adminMcpNavigationReturnFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      setPendingAdminTab(nextTab);
      return;
    }
    setActiveTab(nextTab);
  };

  useEffect(() => {
    if (pendingAdminTab) adminMcpDiscardDialogRef.current?.focus();
  }, [pendingAdminTab]);

  useEffect(() => {
    if (activeTab !== 'rounds') {
      setReviewError('');
      setStepDetailError('');
      setSessionRoundLoadError('');
    }
    if (activeTab !== 'users') {
      setUserActionError('');
      setUserActionMessage('');
    }
    if (activeTab !== 'sandboxes') {
      setSandboxActionError('');
      setSandboxActionMessage('');
    }
  }, [activeTab]);

  const cancelAdminTabChange = () => {
    setPendingAdminTab(null);
    adminMcpNavigationReturnFocusRef.current?.focus();
  };

  const handleLogout = () => {
    apiService.logout();
    navigate('/admin/login', { replace: true });
  };

  const handleOpenWorkspace = () => {
    navigate('/');
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
                    call_kind: detail.call_kind,
                    checkpoint_id: detail.checkpoint_id,
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

  const handleSessionRoundsLoad = useCallback(async (sessionId: string) => {
    setSessionRoundLoadError('');
    setSessionRoundLoadingIds((prev) => ({ ...prev, [sessionId]: true }));
    try {
      const detail = await getAdminSessionRounds(sessionId, {
        status: roundStatus,
        search: debouncedRoundSearch || undefined,
      });
      setRounds((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          sessions: prev.sessions.map((session) => (
            session.session_id === sessionId
              ? { ...session, rounds: detail.rounds, rounds_loaded: true }
              : session
          )),
        };
      });
    } catch (err) {
      console.error('Failed to load session rounds:', err);
      setSessionRoundLoadError('加载 Session 下的 Round 失败，请重试');
    } finally {
      setSessionRoundLoadingIds((prev) => {
        const next = { ...prev };
        delete next[sessionId];
        return next;
      });
    }
  }, [debouncedRoundSearch, roundStatus]);

  const runUserAction = useCallback(async (
    busyKey: string,
    action: () => Promise<void | boolean>,
    successMessage: string,
  ): Promise<boolean> => {
    setUserActionError('');
    setUserActionMessage('');
    setUserUpdatingKeys((prev) => ({ ...prev, [busyKey]: true }));
    try {
      const result = await action();
      if (result === false) {
        return false;
      }
      try {
        setUsers(await getAdminUsers());
      } catch (refreshErr) {
        console.error('Admin user action succeeded but user list refresh failed:', refreshErr);
        setUserActionError(`${successMessage}，但用户列表刷新失败，请手动刷新`);
        return true;
      }
      setUserActionMessage(successMessage);
      return true;
    } catch (err) {
      console.error('Failed to update admin user:', err);
      setUserActionError(apiErrorDetail(err) || '用户操作失败，请稍后重试');
      return false;
    } finally {
      setUserUpdatingKeys((prev) => {
        const next = { ...prev };
        delete next[busyKey];
        return next;
      });
    }
  }, []);

  const runSandboxAction = useCallback(async (
    busyKey: string,
    action: () => Promise<void>,
    successMessage: string,
  ): Promise<boolean> => {
    setSandboxActionError('');
    setSandboxActionMessage('');
    setSandboxUpdatingKeys((prev) => ({ ...prev, [busyKey]: true }));
    try {
      await action();
      try {
        setSandboxProfiles(await getAdminSandboxProfiles());
      } catch (refreshErr) {
        console.error('Sandbox action succeeded but profile list refresh failed:', refreshErr);
        setSandboxActionError(`${successMessage}，但沙箱后端列表刷新失败，请手动刷新`);
        return true;
      }
      setSandboxActionMessage(successMessage);
      return true;
    } catch (err) {
      console.error('Failed to update sandbox profiles:', err);
      setSandboxActionError(apiErrorDetail(err) || '沙箱配置操作失败，请稍后重试');
      return false;
    } finally {
      setSandboxUpdatingKeys((prev) => {
        const next = { ...prev };
        delete next[busyKey];
        return next;
      });
    }
  }, []);

  const handleCreateUser = useCallback(async (values: UserCreateFormValues) => {
    return runUserAction('create-user', async () => {
      if (values.authType === 'simple') {
        const payload: AdminCreateSimpleUserRequest = {
          username: values.username,
          password: values.password,
          enabled: values.enabled,
          is_admin: values.isAdmin,
          token_limit_per_week: parseLimitInput(values.weeklyLimit),
          token_limit_per_month: parseLimitInput(values.monthlyLimit),
          sandbox_profile_id: values.sandboxProfileId || null,
        };
        await createAdminSimpleUser(payload);
      } else {
        const payload: AdminCreateLdapUserRequest = {
          user_id: values.userId,
          username: values.username || null,
          enabled: values.enabled,
          is_admin: values.isAdmin,
          token_limit_per_week: parseLimitInput(values.weeklyLimit),
          token_limit_per_month: parseLimitInput(values.monthlyLimit),
          sandbox_profile_id: values.sandboxProfileId || null,
        };
        await createAdminLdapUser(payload);
      }
    }, '用户已创建');
  }, [runUserAction]);

  const handleToggleUserEnabled = useCallback(async (user: AdminUserItem) => {
    await runUserAction(`enabled-${user.user_id}`, async () => {
      await updateAdminUserEnabled(user.user_id, !user.enabled);
    }, user.enabled ? '用户已禁用' : '用户已启用');
  }, [runUserAction]);

  const handleUpdateUserAdmin = useCallback(async (user: AdminUserItem, nextIsAdmin: boolean) => {
    if (user.user_id === currentUser && !nextIsAdmin) return;
    if (user.is_admin && !nextIsAdmin && !window.confirm(`确认取消 ${user.user_id} 的管理员权限？`)) return;
    await runUserAction(`admin-${user.user_id}`, async () => {
      await updateAdminUserAdmin(user.user_id, nextIsAdmin);
    }, nextIsAdmin ? '管理员权限已授予' : '管理员权限已取消');
  }, [currentUser, runUserAction]);

  const handleUpdateTokenLimits = useCallback(async (
    userId: string,
    payload: AdminTokenLimitsUpdateRequest,
  ) => {
    await runUserAction(`limits-${userId}`, async () => {
      await updateAdminUserTokenLimits(userId, payload);
    }, 'Token 限额已更新');
  }, [runUserAction]);

  const handleResetPassword = useCallback(async (userId: string, password: string) => {
    await runUserAction(`password-${userId}`, async () => {
      await resetAdminSimpleUserPassword(userId, password);
    }, '密码已重置');
  }, [runUserAction]);

  const handleDeleteUser = useCallback(async (user: AdminUserItem) => {
    if (user.user_id === currentUser) return;
    if (!window.confirm(`确认永久删除用户 ${user.user_id}？该用户的会话、记忆、定时任务和沙箱文件都会被清理。`)) return;
    await runUserAction(`delete-${user.user_id}`, async () => {
      await deleteAdminUser(user.user_id);
    }, '用户已删除');
  }, [currentUser, runUserAction]);

  const handleUpdateUserSandboxProfile = useCallback(async (
    user: AdminUserItem,
    sandboxProfileId: string | null,
    forceRecreate: boolean,
  ) => {
    if (forceRecreate) {
      const confirmed = window.confirm(
        `确认失效用户 ${user.user_id} 当前 Agent/Sandbox，并在下次使用时按当前沙箱后端配置创建新 sandbox？`,
      );
      if (!confirmed) return;
    }
    await runUserAction(`sandbox-${user.user_id}`, async () => {
      try {
        await updateAdminUserSandboxProfile(user.user_id, sandboxProfileId, forceRecreate);
      } catch (err) {
        if (!forceRecreate && apiErrorStatus(err) === 409) {
          const detail = apiErrorDetail(err) || '用户当前可能有正在运行的任务。';
          const confirmed = window.confirm(`${detail}\n\n是否强制失效该用户当前 Agent/Sandbox 并切换沙箱后端？`);
          if (!confirmed) return false;
          await updateAdminUserSandboxProfile(user.user_id, sandboxProfileId, true);
        } else {
          throw err;
        }
      }
      try {
        setSandboxProfiles(await getAdminSandboxProfiles());
      } catch (refreshErr) {
        console.error('User sandbox profile updated but profile list refresh failed:', refreshErr);
        setUserActionError('用户沙箱后端已更新，但沙箱后端列表刷新失败，请手动刷新');
      }
    }, '用户沙箱后端已更新');
  }, [runUserAction]);

  const handleSaveSandboxProfile = useCallback(async (
    profileId: string | null,
    payload: AdminSandboxProfilePayload,
  ) => {
    const successMessage = profileId ? '沙箱后端已更新' : '沙箱后端已创建';
    return runSandboxAction(profileId ? `profile-${profileId}` : 'profile-create', async () => {
      if (profileId) {
        await updateAdminSandboxProfile(profileId, payload);
      } else {
        await createAdminSandboxProfile(payload);
      }
      try {
        setUsers(await getAdminUsers());
      } catch (refreshErr) {
        console.error('Sandbox profile saved but user list refresh failed:', refreshErr);
        setSandboxActionError(`${successMessage}，但用户列表刷新失败，请手动刷新`);
      }
    }, successMessage);
  }, [runSandboxAction]);

  const handleSetSandboxProfileDefault = useCallback(async (profile: AdminSandboxProfile) => {
    if (profile.is_default) return;
    const successMessage = '默认沙箱后端已更新';
    await runSandboxAction(`default-${profile.id}`, async () => {
      await setAdminSandboxProfileDefault(profile.id);
      try {
        setUsers(await getAdminUsers());
      } catch (refreshErr) {
        console.error('Default sandbox profile updated but user list refresh failed:', refreshErr);
        setSandboxActionError(`${successMessage}，但用户列表刷新失败，请手动刷新`);
      }
    }, successMessage);
  }, [runSandboxAction]);

  const handleToggleSandboxProfileEnabled = useCallback(async (profile: AdminSandboxProfile) => {
    const successMessage = profile.enabled ? '沙箱后端已禁用' : '沙箱后端已启用';
    await runSandboxAction(`enabled-${profile.id}`, async () => {
      await setAdminSandboxProfileEnabled(profile.id, !profile.enabled);
      try {
        setUsers(await getAdminUsers());
      } catch (refreshErr) {
        console.error('Sandbox profile enabled state updated but user list refresh failed:', refreshErr);
        setSandboxActionError(`${successMessage}，但用户列表刷新失败，请手动刷新`);
      }
    }, successMessage);
  }, [runSandboxAction]);

  const refreshActiveTab = useCallback(async () => {
    // The audit panel owns its filters and cursor. Let it load on mount, and
    // refresh it through a token so the global spinner does not unmount it.
    if (activeTab === 'audit') {
      setError('');
      return;
    }
    setLoading(true);
    setError('');
    try {
      if (activeTab === 'overview') {
        setOverview(await getAdminOverview(overviewDays));
      }
      if (activeTab === 'rounds') {
        setRounds(await getAdminRoundsTree({
          limit: roundPageSize,
          offset: (roundPage - 1) * roundPageSize,
          status: roundStatus,
          search: debouncedRoundSearch || undefined,
        }));
      }
      if (activeTab === 'users') {
        const [usersData, profilesData] = await Promise.all([
          getAdminUsers(),
          getAdminSandboxProfiles(),
        ]);
        setUsers(usersData);
        setSandboxProfiles(profilesData);
      }
      if (activeTab === 'sandboxes') {
        const [profilesData, usersData] = await Promise.all([
          getAdminSandboxProfiles(),
          getAdminUsers(),
        ]);
        setSandboxProfiles(profilesData);
        setUsers(usersData);
      }
      if (activeTab === 'models') {
        setModelRefreshToken((prev) => prev + 1);
      }
      if (activeTab === 'mcp') {
        setMcpRefreshToken((prev) => prev + 1);
      }
      if (activeTab === 'permissions') {
        setPermissionRefreshToken((prev) => prev + 1);
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
  }, [activeTab, overviewDays, roundPage, roundPageSize, debouncedRoundSearch, roundStatus]);

  const handleRefreshClick = useCallback(() => {
    if (activeTab === 'audit') {
      setAuditRefreshToken((prev) => prev + 1);
      return;
    }
    void refreshActiveTab();
  }, [activeTab, refreshActiveTab]);

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
      navigate('/admin/login', { replace: true });
      return;
    }
    refreshActiveTab();
  }, [navigate, refreshActiveTab]);

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
                onClick={() => requestAdminTabChange(item.id)}
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
          <button className="admin-button admin-logout-btn" onClick={handleOpenWorkspace}>
            <Home size={14} />
            用户工作台
          </button>
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
          <button className="admin-button" onClick={handleRefreshClick}>
            <RefreshCw size={14} />
            刷新
          </button>
        </div>

        <div className="admin-content">
          {error ? (
            <FeedbackMessage
              className="admin-error"
              tone="error"
              icon={<AlertTriangle size={14} />}
              onDismiss={() => setError('')}
            >
              {error}
            </FeedbackMessage>
          ) : null}
          {loading ? <div className="admin-loading">加载中...</div> : null}

          {!loading && !error && activeTab === 'overview' && overview ? (
            <OverviewPanel
              data={overview}
              selectedDays={overviewDays}
              onDaysChange={setOverviewDays}
            />
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
              sessionRoundLoadingIds={sessionRoundLoadingIds}
              reviewError={reviewError}
              stepDetailError={stepDetailError}
              sessionRoundLoadError={sessionRoundLoadError}
              onDismissReviewError={() => setReviewError('')}
              onDismissStepDetailError={() => setStepDetailError('')}
              onDismissSessionRoundLoadError={() => setSessionRoundLoadError('')}
              onStatusChange={setRoundStatus}
              onSearchChange={setRoundSearch}
              onPageChange={setRoundPage}
              onPageSizeChange={(value) => {
                setRoundPage(1);
                setRoundPageSize(value);
              }}
              onSessionRoundsLoad={handleSessionRoundsLoad}
              onStepReviewChange={handleStepReviewChange}
              onStepDetailOpen={handleStepDetailOpen}
            />
          ) : null}

          {!loading && !error && activeTab === 'users' ? (
            <UsersPanel
              data={users}
              sandboxProfiles={sandboxProfiles?.profiles || []}
              actionError={userActionError}
              actionMessage={userActionMessage}
              onDismissActionError={() => setUserActionError('')}
              onDismissActionMessage={() => setUserActionMessage('')}
              updatingKeys={userUpdatingKeys}
              onCreateUser={handleCreateUser}
              onToggleEnabled={handleToggleUserEnabled}
              onUpdateAdmin={handleUpdateUserAdmin}
              onUpdateTokenLimits={handleUpdateTokenLimits}
              onResetPassword={handleResetPassword}
              onDeleteUser={handleDeleteUser}
              onUpdateSandboxProfile={handleUpdateUserSandboxProfile}
              currentUserId={currentUser}
            />
          ) : null}

          {!loading && !error && activeTab === 'sandboxes' ? (
            <SandboxesPanel
              data={sandboxProfiles}
              actionError={sandboxActionError}
              actionMessage={sandboxActionMessage}
              onDismissActionError={() => setSandboxActionError('')}
              onDismissActionMessage={() => setSandboxActionMessage('')}
              updatingKeys={sandboxUpdatingKeys}
              onSaveProfile={handleSaveSandboxProfile}
              onSetDefault={handleSetSandboxProfileDefault}
              onToggleEnabled={handleToggleSandboxProfileEnabled}
            />
          ) : null}

          {!loading && !error && activeTab === 'models' ? (
            <AdminModelAccessPanel apiErrorDetail={apiErrorDetail} refreshToken={modelRefreshToken} />
          ) : null}

          {!loading && !error && activeTab === 'mcp' ? (
            <Suspense fallback={<div className="admin-card admin-empty-card">正在加载官方 MCP...</div>}>
              <LazyAdminMcpCatalogPanel refreshToken={mcpRefreshToken} onDirtyChange={setAdminMcpDirty} />
            </Suspense>
          ) : null}

          {!loading && !error && activeTab === 'permissions' ? (
            <Suspense fallback={<div className="admin-card admin-empty-card">正在加载工具权限...</div>}>
              <LazyAdminToolPermissionsPanel refreshToken={permissionRefreshToken} />
            </Suspense>
          ) : null}

          {!loading && !error && activeTab === 'audit' ? (
            <Suspense fallback={<div className="admin-card admin-empty-card">正在加载操作日志...</div>}>
              <LazyAdminAuditLogPanel refreshToken={auditRefreshToken} />
            </Suspense>
          ) : null}

          {!loading && !error && activeTab === 'system' ? (
            <SystemPanel data={systemData} />
          ) : null}
        </div>
      </main>

      {pendingAdminTab ? (
        <div className="admin-modal-backdrop admin-mcp-navigation-confirm" role="presentation">
          <section
            ref={adminMcpDiscardDialogRef}
            className="admin-modal admin-mcp-delete-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="admin-mcp-navigation-discard-title"
            tabIndex={-1}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                cancelAdminTabChange();
                return;
              }
              if (event.key !== 'Tab' || !adminMcpDiscardDialogRef.current) return;
              const buttons = Array.from(
                adminMcpDiscardDialogRef.current.querySelectorAll<HTMLButtonElement>('button:not([disabled])'),
              );
              if (!buttons.length) {
                event.preventDefault();
                adminMcpDiscardDialogRef.current.focus();
                return;
              }
              const first = buttons[0];
              const last = buttons[buttons.length - 1];
              if (document.activeElement === adminMcpDiscardDialogRef.current) {
                event.preventDefault();
                (event.shiftKey ? last : first).focus();
              } else if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <h3 id="admin-mcp-navigation-discard-title">离开并放弃官方 MCP 修改？</h3>
            <p>当前服务定义尚未保存，切换模块会丢失这些修改。</p>
            <div>
              <button type="button" className="admin-button" onClick={cancelAdminTabChange}>继续编辑</button>
              <button type="button" className="admin-button admin-danger-button" onClick={() => {
                const nextTab = pendingAdminTab;
                setPendingAdminTab(null);
                setAdminMcpDirty(false);
                setActiveTab(nextTab);
              }}>放弃修改并离开</button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function OverviewPanel({
  data,
  selectedDays,
  onDaysChange,
}: {
  data: AdminOverview;
  selectedDays: OverviewDays;
  onDaysChange: (days: OverviewDays) => void;
}) {
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

      <div className="admin-overview-toolbar">
        <div>
          <div className="admin-overview-toolbar-title">趋势范围</div>
          <div className="admin-card-header-sub">折线图按完成日期聚合</div>
        </div>
        <label className="admin-filter-pill admin-overview-range">
          日期
          <select
            aria-label="概览趋势范围"
            value={selectedDays}
            onChange={(event) => onDaysChange(Number(event.target.value) as OverviewDays)}
          >
            <option value={7}>近 7 天</option>
            <option value={14}>近 14 天</option>
            <option value={30}>近 30 天</option>
          </select>
        </label>
      </div>

      <div className="admin-trend-grid">
        <div className="admin-card">
          <div className="admin-card-header">
            <div>
              <h3 className="admin-card-header-title">近 {data.window_days} 天 Rounds 趋势</h3>
              <div className="admin-card-header-sub">按完成日期聚合</div>
            </div>
          </div>
          <div className="admin-card-body admin-trend-body">
            <TrendLineChart
              data={data.trends}
              metric="rounds"
              label="Rounds"
              accent="#244f46"
              gradientId="overview-rounds-trend"
            />
            <div className="admin-trend-list">
              {data.trends.map((item) => (
                <div className="admin-trend-row" key={item.date}>
                  <span>{item.date}</span>
                  <strong>{formatNumber(item.rounds)}</strong>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="admin-card">
          <div className="admin-card-header">
            <div>
              <h3 className="admin-card-header-title">近 {data.window_days} 天 Token 趋势</h3>
              <div className="admin-card-header-sub">按完成日期聚合</div>
            </div>
          </div>
          <div className="admin-card-body admin-trend-body">
            <TrendLineChart
              data={data.trends}
              metric="tokens"
              label="Tokens"
              accent="#576f9f"
              gradientId="overview-tokens-trend"
            />
            <div className="admin-trend-list">
              {data.trends.map((item) => (
                <div className="admin-trend-row" key={item.date}>
                  <span>{item.date}</span>
                  <strong>{formatNumber(item.tokens)}</strong>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function TrendLineChart({
  data,
  metric,
  label,
  accent,
  gradientId,
}: {
  data: AdminOverview['trends'];
  metric: 'rounds' | 'tokens';
  label: string;
  accent: string;
  gradientId: string;
}) {
  const width = 640;
  const height = 190;
  const padding = { top: 18, right: 18, bottom: 34, left: 42 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const values = data.map((item) => item[metric]);
  const maxValue = Math.max(...values, 0);
  const yMax = maxValue > 0 ? maxValue : 1;
  const bottomY = padding.top + chartHeight;
  const points = data.map((item, index) => {
    const x = padding.left + (data.length <= 1 ? chartWidth / 2 : (chartWidth / (data.length - 1)) * index);
    const y = bottomY - (item[metric] / yMax) * chartHeight;
    return { x, y, value: item[metric], date: item.date };
  });
  const linePath = points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
  const areaPath = points.length > 0
    ? `M ${points[0].x.toFixed(2)} ${bottomY.toFixed(2)} L ${linePath.slice(2)} L ${points[points.length - 1].x.toFixed(2)} ${bottomY.toFixed(2)} Z`
    : '';
  const firstDate = data[0]?.date || '-';
  const lastDate = data[data.length - 1]?.date || '-';
  const latestValue = values[values.length - 1] ?? 0;

  return (
    <div
      className="admin-line-chart"
      role="img"
      aria-label={`${label} 趋势，最新 ${formatNumber(latestValue)}，峰值 ${formatNumber(maxValue)}`}
    >
      <div className="admin-line-chart-meta">
        <span>峰值 {formatNumber(maxValue)}</span>
        <strong>最新 {formatNumber(latestValue)}</strong>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`}>
        <defs>
          <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={accent} stopOpacity="0.18" />
            <stop offset="100%" stopColor={accent} stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const y = padding.top + chartHeight * ratio;
          return (
            <line
              key={ratio}
              className="admin-line-chart-grid"
              x1={padding.left}
              x2={width - padding.right}
              y1={y}
              y2={y}
            />
          );
        })}
        <text className="admin-line-chart-axis" x={padding.left} y={padding.top + 2}>{formatNumber(yMax)}</text>
        <text className="admin-line-chart-axis" x={padding.left} y={bottomY + 18}>0</text>
        <text className="admin-line-chart-date" x={padding.left} y={height - 8}>{firstDate.slice(5)}</text>
        <text className="admin-line-chart-date" x={width - padding.right} y={height - 8} textAnchor="end">{lastDate.slice(5)}</text>
        {areaPath ? <path d={areaPath} fill={`url(#${gradientId})`} /> : null}
        {linePath ? <path className="admin-line-chart-line" d={linePath} stroke={accent} /> : null}
        {points.map((point) => (
          <circle
            key={`${point.date}-${point.value}`}
            className="admin-line-chart-point"
            cx={point.x}
            cy={point.y}
            r={point.value === maxValue && maxValue > 0 ? 4.2 : 3.2}
            fill={point.value === maxValue && maxValue > 0 ? accent : '#fffef9'}
            stroke={accent}
          />
        ))}
      </svg>
    </div>
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
  sessionRoundLoadingIds,
  reviewError,
  stepDetailError,
  sessionRoundLoadError,
  onDismissReviewError,
  onDismissStepDetailError,
  onDismissSessionRoundLoadError,
  onStatusChange,
  onSearchChange,
  onPageChange,
  onPageSizeChange,
  onSessionRoundsLoad,
  onStepReviewChange,
  onStepDetailOpen,
}: {
  data: AdminRoundTreeResponse | null;
  roundStatus: AdminRoundStatusFilter;
  roundSearch: string;
  roundPage: number;
  roundPageSize: number;
  reviewUpdatingIds: Record<number, boolean>;
  stepLoadingIds: Record<number, boolean>;
  sessionRoundLoadingIds: Record<string, boolean>;
  reviewError: string;
  stepDetailError: string;
  sessionRoundLoadError: string;
  onDismissReviewError: () => void;
  onDismissStepDetailError: () => void;
  onDismissSessionRoundLoadError: () => void;
  onStatusChange: (value: AdminRoundStatusFilter) => void;
  onSearchChange: (value: string) => void;
  onPageChange: (value: number) => void;
  onPageSizeChange: (value: number) => void;
  onSessionRoundsLoad: (sessionId: string) => Promise<void>;
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
    const session = data?.sessions.find((item) => item.session_id === sessionId);
    const willExpand = !expandedSessions[sessionId];
    setExpandedSessions((prev) => ({ ...prev, [sessionId]: willExpand }));
    if (
      willExpand
      && session
      && session.rounds.length === 0
      && !session.rounds_loaded
      && !sessionRoundLoadingIds[sessionId]
    ) {
      void onSessionRoundsLoad(sessionId);
    }
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
          <select
            aria-label="Round 状态"
            className="admin-select"
            value={roundStatus}
            onChange={(e) => onStatusChange(e.target.value as AdminRoundStatusFilter)}
          >
            <option value="all">全部状态</option>
            {ADMIN_ROUND_STATUS_OPTIONS.map((status) => (
              <option key={status} value={status}>{status}</option>
            ))}
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
        {reviewError ? (
          <FeedbackMessage className="admin-step-review-error" tone="error" onDismiss={onDismissReviewError}>
            {reviewError}
          </FeedbackMessage>
        ) : null}
        {stepDetailError ? (
          <FeedbackMessage className="admin-step-review-error" tone="error" onDismiss={onDismissStepDetailError}>
            {stepDetailError}
          </FeedbackMessage>
        ) : null}
        {sessionRoundLoadError ? (
          <FeedbackMessage className="admin-step-review-error" tone="error" onDismiss={onDismissSessionRoundLoadError}>
            {sessionRoundLoadError}
          </FeedbackMessage>
        ) : null}
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
              const sessionRoundsLoading = !!sessionRoundLoadingIds[session.session_id];
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
                        {sessionRoundsLoading ? (
                          <div className="admin-loading">正在加载 Round...</div>
                        ) : session.rounds.length === 0 ? (
                          <div className="admin-loading">暂无匹配 Round</div>
                        ) : (
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
                              const isSubagentRound = isSubagentRoundLike(round);
                              return (
                                <Fragment key={round.round_id}>
                                  <tr className={`admin-round-row ${isSubagentRound ? 'subagent' : 'main-agent'}`}>
                                    <td>
                                      <div className="admin-round-title-line">
                                        <button className="admin-tree-toggle" onClick={() => toggleRound(round.round_id)}>
                                          <RoundChevron size={14} />
                                          <span>{getRoundDisplayTitle(round)}</span>
                                        </button>
                                        <span className={`admin-run-kind ${isSubagentRound ? 'subagent' : 'main'}`}>
                                          {getRoundKindLabel(round)}
                                        </span>
                                        {round.subagent_type ? (
                                          <span className="admin-run-kind muted">{round.subagent_type}</span>
                                        ) : null}
                                        {!isSubagentRound && round.subagent_child_count > 0 ? (
                                          <span className="admin-run-kind muted">派生 {round.subagent_child_count}</span>
                                        ) : null}
                                      </div>
                                      <div className="admin-subline">
                                        {round.round_id}
                                        {isSubagentRound && round.parent_run_id ? ` · parent ${round.parent_run_id}` : ''}
                                      </div>
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
                                              <th>类型</th>
                                              <th>详情</th>
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
                                                    <td>{step.call_kind === 'compaction' ? '压缩' : '普通'}</td>
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
                                                  </tr>

                                                  {detailExpanded ? (
                                                    <tr>
                                                      <td className="admin-step-detail-cell" colSpan={13}>
                                                        <div className="admin-step-analysis-block">
                                                          <div className="admin-step-analysis-title">管理员分析摘要</div>
                                                          <div className="admin-step-analysis-grid">
                                                            <div className="admin-step-analysis-card">
                                                              <div className="admin-step-analysis-card-title">请求概览</div>
                                                              <div className="admin-step-analysis-row"><span>Provider</span><strong>{analysis.provider}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>模型</span><strong>{analysis.model}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>调用类型</span><strong>{step.call_kind}</strong></div>
                                                              <div className="admin-step-analysis-row"><span>Checkpoint</span><strong>{step.checkpoint_id || '-'}</strong></div>
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
                                                          <div>调用类型（call_kind）: {step.call_kind}</div>
                                                          <div>Checkpoint（checkpoint_id）: {step.checkpoint_id || '-'}</div>
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
                        )}
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

function UsersPanel({
  data,
  sandboxProfiles,
  actionError,
  actionMessage,
  onDismissActionError,
  onDismissActionMessage,
  updatingKeys,
  onCreateUser,
  onToggleEnabled,
  onUpdateAdmin,
  onUpdateTokenLimits,
  onResetPassword,
  onDeleteUser,
  onUpdateSandboxProfile,
  currentUserId,
}: {
  data: AdminUsersResponse | null;
  sandboxProfiles: AdminSandboxProfile[];
  actionError: string;
  actionMessage: string;
  onDismissActionError: () => void;
  onDismissActionMessage: () => void;
  updatingKeys: Record<string, boolean>;
  onCreateUser: (values: UserCreateFormValues) => Promise<boolean>;
  onToggleEnabled: (user: AdminUserItem) => Promise<void>;
  onUpdateAdmin: (user: AdminUserItem, nextIsAdmin: boolean) => Promise<void>;
  onUpdateTokenLimits: (userId: string, payload: AdminTokenLimitsUpdateRequest) => Promise<void>;
  onResetPassword: (userId: string, password: string) => Promise<void>;
  onDeleteUser: (user: AdminUserItem) => Promise<void>;
  onUpdateSandboxProfile: (user: AdminUserItem, sandboxProfileId: string | null, forceRecreate: boolean) => Promise<void>;
  currentUserId: string;
}) {
  const defaultCreateForm: UserCreateFormValues = {
    authType: 'simple',
    userId: '',
    username: '',
    password: '',
    enabled: true,
    isAdmin: false,
    weeklyLimit: '',
    monthlyLimit: '',
    sandboxProfileId: '',
  };
  const [createForm, setCreateForm] = useState<UserCreateFormValues>({
    ...defaultCreateForm,
  });
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [tokenDrafts, setTokenDrafts] = useState<Record<string, { weeklyLimit: string; monthlyLimit: string }>>({});
  const [roleDrafts, setRoleDrafts] = useState<Record<string, 'admin' | 'user'>>({});
  const [sandboxDrafts, setSandboxDrafts] = useState<Record<string, string>>({});
  const [passwordDrafts, setPasswordDrafts] = useState<Record<string, string>>({});
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<UserStatusFilter>('all');
  const [roleFilter, setRoleFilter] = useState<UserRoleFilter>('all');
  const [authFilter, setAuthFilter] = useState<UserAuthFilter>('all');
  const [sortKey, setSortKey] = useState<UserSortKey>('recent');
  const [actionMenuUserId, setActionMenuUserId] = useState<string | null>(null);
  const [loginEventsUser, setLoginEventsUser] = useState<AdminUserItem | null>(null);
  const [loginEvents, setLoginEvents] = useState<AdminUserLoginEventsResponse | null>(null);
  const [loginEventsLoading, setLoginEventsLoading] = useState(false);
  const [loginEventsError, setLoginEventsError] = useState('');
  const [exportingUsers, setExportingUsers] = useState(false);
  const [exportUsersError, setExportUsersError] = useState('');

  useEffect(() => {
    const nextDrafts: Record<string, { weeklyLimit: string; monthlyLimit: string }> = {};
    const nextRoleDrafts: Record<string, 'admin' | 'user'> = {};
    const nextSandboxDrafts: Record<string, string> = {};
    for (const user of data?.users || []) {
      nextDrafts[user.user_id] = {
        weeklyLimit: user.token_limit_per_week === null ? '' : String(user.token_limit_per_week),
        monthlyLimit: user.token_limit_per_month === null ? '' : String(user.token_limit_per_month),
      };
      nextRoleDrafts[user.user_id] = user.is_admin ? 'admin' : 'user';
      nextSandboxDrafts[user.user_id] = user.sandbox_profile_id || '';
    }
    setTokenDrafts(nextDrafts);
    setRoleDrafts(nextRoleDrafts);
    setSandboxDrafts(nextSandboxDrafts);
  }, [data]);

  const users = useMemo(() => data?.users || [], [data]);
  const createSandboxProfiles = useMemo(
    () => assignableSandboxProfiles(sandboxProfiles),
    [sandboxProfiles],
  );
  const ldapCount = users.filter((user) => user.auth_type === 'ldap').length;
  const simpleCount = users.filter((user) => user.auth_type === 'simple').length;
  const visibleUsers = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return users
      .filter((user) => !query || userSearchText(user).includes(query))
      .filter((user) => statusFilter === 'all' || (statusFilter === 'enabled' ? user.enabled : !user.enabled))
      .filter((user) => roleFilter === 'all' || (roleFilter === 'admin' ? user.is_admin : !user.is_admin))
      .filter((user) => authFilter === 'all' || user.auth_type === authFilter)
      .slice()
      .sort((left, right) => {
        if (sortKey === 'name') return left.username.localeCompare(right.username);
        if (sortKey === 'tokens') return right.total_tokens - left.total_tokens;
        return new Date(right.last_active_at || right.last_login_at || 0).getTime()
          - new Date(left.last_active_at || left.last_login_at || 0).getTime();
      });
  }, [authFilter, roleFilter, searchQuery, sortKey, statusFilter, users]);

  const handleExportVisibleUsers = async () => {
    setExportingUsers(true);
    setExportUsersError('');
    try {
      const blob = await exportAdminUsers(visibleUsers.map((user) => user.user_id));
      const day = new Date().toISOString().slice(0, 10);
      downloadBlob(`opencapybox-users-${day}.csv`, blob);
    } catch (exportError) {
      console.error('Failed to export admin users:', exportError);
      setExportUsersError(
        (await extractBlobAwareErrorMessage(exportError))
        || '用户数据导出失败，请稍后重试',
      );
    } finally {
      setExportingUsers(false);
    }
  };

  const handleCreateSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const created = await onCreateUser({
      ...createForm,
      userId: createForm.userId.trim(),
      username: createForm.username.trim(),
      password: createForm.password,
      weeklyLimit: createForm.weeklyLimit.trim(),
      monthlyLimit: createForm.monthlyLimit.trim(),
    });
    if (!created) return;
    setCreateForm({ ...defaultCreateForm });
    setIsCreateModalOpen(false);
  };

  const updateCreateField = <Key extends keyof UserCreateFormValues>(key: Key, value: UserCreateFormValues[Key]) => {
    setCreateForm((prev) => ({ ...prev, [key]: value }));
  };

  const openLoginEvents = async (user: AdminUserItem) => {
    setActionMenuUserId(null);
    setLoginEventsUser(user);
    setLoginEvents(null);
    setLoginEventsError('');
    setLoginEventsLoading(true);
    try {
      setLoginEvents(await getAdminUserLoginEvents(user.user_id, 50));
    } catch (err) {
      console.error('Failed to load user login events:', err);
      setLoginEventsError('加载登录历史失败，请稍后重试');
    } finally {
      setLoginEventsLoading(false);
    }
  };

  const closeLoginEvents = () => {
    setLoginEventsUser(null);
    setLoginEvents(null);
    setLoginEventsError('');
    setLoginEventsLoading(false);
  };

  return (
    <>
      <div className="admin-grid-4">
        <MetricCard label="用户总数" value={formatNumber(data?.summary.users_total || 0)} hint={`${ldapCount} LDAP · ${simpleCount} Simple`} />
        <MetricCard label="管理员" value={formatNumber(data?.summary.admins_total || 0)} hint="拥有所有权限" />
        <MetricCard label="活跃用户" value={formatNumber(data?.summary.active_total || 0)} hint="最近 7 天有活动" />
        <MetricCard label="运行中用户" value={formatNumber(data?.summary.running_total || 0)} hint="正在执行任务" />
      </div>

      {actionError ? (
        <FeedbackMessage className="admin-error admin-inline-message" tone="error" onDismiss={onDismissActionError}>
          {actionError}
        </FeedbackMessage>
      ) : null}
      {actionMessage ? (
        <FeedbackMessage
          className="admin-toast"
          tone="success"
          onDismiss={onDismissActionMessage}
        >
          {actionMessage}
        </FeedbackMessage>
      ) : null}
      {exportUsersError ? (
        <FeedbackMessage
          className="admin-error admin-inline-message"
          tone="error"
          onDismiss={() => setExportUsersError('')}
        >
          {exportUsersError}
        </FeedbackMessage>
      ) : null}

      <div className="admin-card admin-users-card">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title">用户目录</h3>
            <div className="admin-card-header-sub">显示 {visibleUsers.length} / {users.length} 个账号</div>
          </div>
          <button
            className="admin-button admin-primary-button admin-header-action"
            type="button"
            onClick={() => setIsCreateModalOpen(true)}
          >
            <Plus size={14} />
            新建用户
          </button>
        </div>
        <div className="admin-user-toolbar">
          <label className="admin-search-box">
            <Search size={14} />
            <input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="搜索用户名、账号、认证来源..."
            />
          </label>
          <div className="admin-toolbar-filters">
            <label className="admin-filter-pill">
              状态
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as UserStatusFilter)}>
                <option value="all">全部</option>
                <option value="enabled">已启用</option>
                <option value="disabled">已停用</option>
              </select>
            </label>
            <label className="admin-filter-pill">
              权限
              <select value={roleFilter} onChange={(event) => setRoleFilter(event.target.value as UserRoleFilter)}>
                <option value="all">全部</option>
                <option value="admin">管理员</option>
                <option value="user">普通用户</option>
              </select>
            </label>
            <label className="admin-filter-pill">
              认证
              <select value={authFilter} onChange={(event) => setAuthFilter(event.target.value as UserAuthFilter)}>
                <option value="all">全部</option>
                <option value="simple">simple</option>
                <option value="ldap">ldap</option>
              </select>
            </label>
          </div>
          <div className="admin-toolbar-actions">
            <label className="admin-filter-pill">
              排序
              <select value={sortKey} onChange={(event) => setSortKey(event.target.value as UserSortKey)}>
                <option value="recent">最近活动</option>
                <option value="name">用户名</option>
                <option value="tokens">Token 用量</option>
              </select>
            </label>
            <button
              className="admin-button admin-icon-button"
              type="button"
              onClick={() => void handleExportVisibleUsers()}
              disabled={exportingUsers || visibleUsers.length === 0}
            >
              <Download size={14} />
              {exportingUsers ? '导出中...' : '导出'}
            </button>
          </div>
        </div>
        <div className="admin-table-wrap">
          <table className="admin-table admin-users-table">
            <colgroup>
              <col className="admin-users-col-user" />
              <col className="admin-users-col-access" />
              <col className="admin-users-col-models" />
              <col className="admin-users-col-sandbox" />
              <col className="admin-users-col-token" />
              <col className="admin-users-col-activity" />
              <col className="admin-users-col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th>用户</th>
                <th>状态 / 权限</th>
                <th>模型包</th>
                <th>沙箱后端</th>
                <th>Token</th>
                <th>活动</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {visibleUsers.map((item) => {
                const draft = tokenDrafts[item.user_id] || { weeklyLimit: '', monthlyLimit: '' };
                const roleDraft = roleDrafts[item.user_id] || (item.is_admin ? 'admin' : 'user');
                const currentRole = item.is_admin ? 'admin' : 'user';
                const sandboxDraft = sandboxDrafts[item.user_id] ?? (item.sandbox_profile_id || '');
                const rowSandboxProfiles = assignableSandboxProfiles(sandboxProfiles, sandboxDraft || undefined);
                const passwordDraft = passwordDrafts[item.user_id] || '';
                const isCurrentUser = item.user_id === currentUserId;
                return (
                  <tr
                    key={item.user_id}
                    className={`${isCurrentUser ? 'is-current-user' : ''} ${!item.enabled ? 'is-disabled-user' : ''}`}
                  >
                    <td>
                      <div className="admin-user-identity">
                        <div className={`admin-user-avatar ${userAvatarTone(item)}`}>{userInitial(item)}</div>
                        <div className="admin-user-main">
                          <div className="admin-user-name-line">
                            <strong>{item.username}</strong>
                            {isCurrentUser ? <span className="admin-current-badge">当前账号</span> : null}
                          </div>
                          <div className="admin-subline">@{item.user_id}</div>
                        </div>
                        <span className={`admin-auth-chip ${item.auth_type === 'ldap' ? 'ldap' : 'simple'}`}>{item.auth_type}</span>
                      </div>
                      <div className="admin-subline">创建：{item.created_by || '-'}</div>
                    </td>
                    <td>
                      <div className="admin-access-cell">
                        <button
                          className={`admin-switch-button ${item.enabled ? 'on' : ''}`}
                          disabled={isCurrentUser || !!updatingKeys[`enabled-${item.user_id}`]}
                          onClick={() => { void onToggleEnabled(item); }}
                        >
                          <span />
                          {item.enabled ? '已启用' : '已停用'}
                        </button>
                        <div className="admin-role-editor">
                          <select
                            className="admin-select admin-role-select"
                            aria-label={`${item.user_id} 管理员权限`}
                            value={roleDraft}
                            disabled={isCurrentUser || !!updatingKeys[`admin-${item.user_id}`]}
                            onChange={(event) => {
                              setRoleDrafts((prev) => ({
                                ...prev,
                                [item.user_id]: event.target.value as 'admin' | 'user',
                              }));
                            }}
                          >
                            <option value="user">普通用户</option>
                            <option value="admin">管理员</option>
                          </select>
                          <button
                            className="admin-button admin-icon-button admin-icon-only-button"
                            aria-label={`保存 ${item.user_id} 权限`}
                            title="保存权限"
                            disabled={isCurrentUser || roleDraft === currentRole || !!updatingKeys[`admin-${item.user_id}`]}
                            onClick={() => { void onUpdateAdmin(item, roleDraft === 'admin'); }}
                          >
                            <Save size={14} />
                          </button>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="admin-model-packages-cell">
                        {item.is_admin ? (
                          <span className="admin-status completed">全部模型</span>
                        ) : (
                          <>
                            <strong>{item.model_permission_default_group_name || '默认'}</strong>
                            {(item.model_permission_group_names || []).length > 0 ? (
                              <div className="admin-model-package-tags">
                                {(item.model_permission_group_names || []).map((name) => (
                                  <span key={name}>{name}</span>
                                ))}
                              </div>
                            ) : (
                              <div className="admin-subline">无额外业务包</div>
                            )}
                          </>
                        )}
                      </div>
                    </td>
                    <td>
                      <div className="admin-sandbox-cell">
                        <div className="admin-role-editor">
                          <select
                            className="admin-select admin-role-select"
                            aria-label={`${item.user_id} 沙箱后端`}
                            value={sandboxDraft}
                            disabled={!!updatingKeys[`sandbox-${item.user_id}`]}
                            onChange={(event) => {
                              setSandboxDrafts((prev) => ({
                                ...prev,
                                [item.user_id]: event.target.value,
                              }));
                            }}
                          >
                            <option value="">跟随全局默认</option>
                            {item.sandbox_profile_source === 'missing' && item.sandbox_profile_id ? (
                              <option value={item.sandbox_profile_id}>已删除：{item.sandbox_profile_id}</option>
                            ) : null}
                            {rowSandboxProfiles.map((profile) => (
                              <option key={profile.id} value={profile.id} disabled={!profile.enabled}>
                                {sandboxProfileOptionLabel(profile)}
                              </option>
                            ))}
                          </select>
                          <button
                            className="admin-button admin-icon-button admin-icon-only-button"
                            aria-label={`保存 ${item.user_id} 沙箱后端`}
                            title="保存沙箱后端"
                            disabled={(sandboxDraft || '') === (item.sandbox_profile_id || '') || !!updatingKeys[`sandbox-${item.user_id}`]}
                            onClick={() => {
                              void onUpdateSandboxProfile(item, sandboxDraft || null, false);
                            }}
                          >
                            <Save size={14} />
                          </button>
                        </div>
                        <div className="admin-token-stack admin-sandbox-summary">
                          <span className="admin-sandbox-title">
                            {item.sandbox_profile_name || (item.sandbox_profile_source === 'missing' ? '已删除沙箱后端' : '默认沙箱')}
                            {' · '}
                            {sandboxSourceLabel(item.sandbox_profile_source)}
                          </span>
                          <span className="admin-mono-line">ID {item.sandbox_id || '-'}</span>
                          {item.sandbox_profile_error ? (
                            <span className="admin-status error">{item.sandbox_profile_error}</span>
                          ) : null}
                          <span className={`admin-status ${item.sandbox_profile_error ? 'error' : item.sandbox_needs_recreate ? 'paused' : statusClass(item.sandbox_status || 'none')}`}>
                            {item.sandbox_profile_error ? '配置异常' : item.sandbox_needs_recreate ? '需重建' : (item.sandbox_status || 'none')}
                          </span>
                          {item.sandbox_needs_recreate && !item.sandbox_profile_error ? (
                            <button
                              className="admin-button admin-icon-button admin-sandbox-apply-button"
                              type="button"
                              disabled={!!updatingKeys[`sandbox-${item.user_id}`]}
                              onClick={() => {
                                void onUpdateSandboxProfile(item, item.sandbox_profile_id || null, true);
                              }}
                            >
                              <RefreshCw size={13} />
                              应用新配置
                            </button>
                          ) : null}
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="admin-token-cell">
                        <TokenUsageBar label="本周" used={item.weekly_tokens_used} limit={item.token_limit_per_week} />
                        <TokenUsageBar label="本月" used={item.monthly_tokens_used} limit={item.token_limit_per_month} />
                        <div className="admin-limit-editor">
                          <label className="admin-limit-field">
                            <span>周限额</span>
                            <input
                              className="admin-input admin-limit-input"
                              aria-label={`${item.user_id} 周限额`}
                              type="number"
                              min="0"
                              value={draft.weeklyLimit}
                              onChange={(event) => {
                                setTokenDrafts((prev) => ({
                                  ...prev,
                                  [item.user_id]: { ...draft, weeklyLimit: event.target.value },
                                }));
                              }}
                              placeholder={formatLimit(item.token_limit_per_week)}
                            />
                          </label>
                          <label className="admin-limit-field">
                            <span>月限额</span>
                            <input
                              className="admin-input admin-limit-input"
                              aria-label={`${item.user_id} 月限额`}
                              type="number"
                              min="0"
                              value={draft.monthlyLimit}
                              onChange={(event) => {
                                setTokenDrafts((prev) => ({
                                  ...prev,
                                  [item.user_id]: { ...draft, monthlyLimit: event.target.value },
                                }));
                              }}
                              placeholder={formatLimit(item.token_limit_per_month)}
                            />
                          </label>
                          <button
                            className="admin-button admin-icon-button admin-icon-only-button"
                            aria-label={`保存 ${item.user_id} 限额`}
                            title="保存限额"
                            disabled={!!updatingKeys[`limits-${item.user_id}`]}
                            onClick={() => {
                              void onUpdateTokenLimits(item.user_id, {
                                token_limit_per_week: parseLimitInput(draft.weeklyLimit),
                                token_limit_per_month: parseLimitInput(draft.monthlyLimit),
                              });
                            }}
                          >
                            <Save size={14} />
                          </button>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="admin-activity-cell">
                        <div className="admin-runtime-grid">
                          <span><b>{item.sessions_count}</b> Sessions</span>
                          <span><b>{item.rounds_count}</b> Rounds</span>
                          <span><b>{item.running_rounds}</b> 运行中</span>
                          <span><b>{item.cron_jobs_enabled}/{item.cron_jobs_total}</b> Cron</span>
                        </div>
                        <div className="admin-token-stack admin-recent-stack">
                          <span>活跃 {formatDateTime(item.last_active_at)}</span>
                          <span>登录 {formatDateTime(item.last_login_at)}</span>
                          <span>IP {item.last_login_ip || '-'}</span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="admin-row-actions">
                        <div className="admin-more-menu-wrap">
                          <button
                            className="admin-button admin-icon-button admin-icon-only-button"
                            type="button"
                            aria-label={`更多 ${item.user_id}`}
                            onClick={() => setActionMenuUserId((prev) => (prev === item.user_id ? null : item.user_id))}
                          >
                            <MoreHorizontal size={15} />
                          </button>
                          {actionMenuUserId === item.user_id ? (
                            <div className="admin-more-menu">
                              <button
                                className="admin-button admin-icon-button"
                                aria-label={`登录历史 ${item.user_id}`}
                                onClick={() => { void openLoginEvents(item); }}
                              >
                                <History size={14} />
                                登录历史
                              </button>
                              {item.auth_type === 'simple' ? (
                                <div className="admin-password-reset">
                                  <input
                                    className="admin-input"
                                    aria-label={`${item.user_id} 新密码`}
                                    type="password"
                                    value={passwordDraft}
                                    onChange={(event) => {
                                      setPasswordDrafts((prev) => ({ ...prev, [item.user_id]: event.target.value }));
                                    }}
                                    placeholder="新密码"
                                  />
                                  <button
                                    className="admin-button admin-icon-button"
                                    aria-label={`重置 ${item.user_id} 密码`}
                                    disabled={!passwordDraft || !!updatingKeys[`password-${item.user_id}`]}
                                    onClick={() => { void onResetPassword(item.user_id, passwordDraft); }}
                                  >
                                    <KeyRound size={14} />
                                    重置
                                  </button>
                                </div>
                              ) : (
                                <span className="admin-subline">LDAP 认证</span>
                              )}
                              <button
                                className="admin-button admin-icon-button admin-danger-button"
                                aria-label={`删除 ${item.user_id}`}
                                disabled={isCurrentUser || !!updatingKeys[`delete-${item.user_id}`]}
                                onClick={() => { void onDeleteUser(item); }}
                              >
                                <Trash2 size={14} />
                                删除
                              </button>
                            </div>
                          ) : null}
                        </div>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {isCreateModalOpen ? (
        <div className="admin-drawer-backdrop" role="presentation">
          <aside className="admin-user-drawer" role="dialog" aria-modal="true" aria-labelledby="admin-create-user-title">
            <div className="admin-drawer-header">
              <div>
                <h3 id="admin-create-user-title">新建用户</h3>
                <p>创建本地账号或 LDAP 目录账号</p>
              </div>
              <button
                className="admin-button admin-icon-button admin-icon-only-button"
                type="button"
                aria-label="关闭新建用户弹窗"
                onClick={() => setIsCreateModalOpen(false)}
              >
                <X size={14} />
              </button>
            </div>
            <form className="admin-user-drawer-form" onSubmit={handleCreateSubmit}>
              <div className="admin-drawer-section">
                <div className="admin-drawer-section-title">认证类型</div>
                <div className="admin-auth-card-grid" role="radiogroup" aria-label="认证类型">
                  <button
                    type="button"
                    role="radio"
                    aria-checked={createForm.authType === 'simple'}
                    className={`admin-auth-card ${createForm.authType === 'simple' ? 'active' : ''}`}
                    onClick={() => updateCreateField('authType', 'simple')}
                  >
                    <strong>Simple</strong>
                    <span>本地账号 · 系统管理密码</span>
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={createForm.authType === 'ldap'}
                    className={`admin-auth-card ${createForm.authType === 'ldap' ? 'active' : ''}`}
                    onClick={() => updateCreateField('authType', 'ldap')}
                  >
                    <strong>LDAP</strong>
                    <span>目录账号 · 使用目录密码验证</span>
                  </button>
                </div>
              </div>

              <div className={`admin-form-row-2 ${createForm.authType === 'simple' ? 'single' : ''}`}>
                <label className="admin-field">
                  <span>{createForm.authType === 'ldap' ? '域账号ID' : '用户名'}</span>
                  <input
                    className="admin-input"
                    aria-label={createForm.authType === 'ldap' ? '域账号ID' : '用户名'}
                    value={createForm.authType === 'ldap' ? createForm.userId : createForm.username}
                    onChange={(event) => {
                      if (createForm.authType === 'ldap') updateCreateField('userId', event.target.value);
                      else updateCreateField('username', event.target.value);
                    }}
                    placeholder={createForm.authType === 'ldap' ? '如 zhangsan' : '如 alex.chen'}
                    required
                  />
                </label>
                {createForm.authType === 'ldap' ? (
                  <label className="admin-field">
                    <span>显示名</span>
                    <input
                      className="admin-input"
                      aria-label="显示名"
                      value={createForm.username}
                      onChange={(event) => updateCreateField('username', event.target.value)}
                      placeholder="可选"
                    />
                  </label>
                ) : null}
              </div>

              {createForm.authType === 'simple' ? (
                <label className="admin-field">
                  <span>密码</span>
                  <input
                    className="admin-input"
                    aria-label="密码"
                    type="password"
                    value={createForm.password}
                    onChange={(event) => updateCreateField('password', event.target.value)}
                    placeholder="创建后仅展示一次"
                    required
                  />
                </label>
              ) : null}

              <label className="admin-field">
                <span>沙箱后端</span>
                <select
                  className="admin-select"
                  aria-label="沙箱后端"
                  value={createForm.sandboxProfileId}
                  onChange={(event) => updateCreateField('sandboxProfileId', event.target.value)}
                >
                  <option value="">跟随全局默认沙箱</option>
                  {createSandboxProfiles.map((profile) => (
                    <option key={profile.id} value={profile.id} disabled={!profile.enabled}>
                      {sandboxProfileOptionLabel(profile)}
                    </option>
                  ))}
                </select>
              </label>

              <div className="admin-drawer-section">
                <div className="admin-drawer-section-title">权限</div>
                <div className="admin-role-card-grid">
                  <button
                    type="button"
                    className={`admin-role-card ${!createForm.isAdmin ? 'active' : ''}`}
                    onClick={() => updateCreateField('isAdmin', false)}
                  >
                    <strong>普通用户</strong>
                    <span>仅访问自己的资源</span>
                  </button>
                  <button
                    type="button"
                    className={`admin-role-card ${createForm.isAdmin ? 'active' : ''}`}
                    onClick={() => updateCreateField('isAdmin', true)}
                  >
                    <strong>管理员</strong>
                    <span>访问全部用户与系统</span>
                  </button>
                </div>
              </div>

              <div className="admin-form-row-2">
                <label className="admin-field">
                  <span>周 Token 限额</span>
                  <input
                    className="admin-input"
                    aria-label="周限额"
                    type="number"
                    min="0"
                    value={createForm.weeklyLimit}
                    onChange={(event) => updateCreateField('weeklyLimit', event.target.value)}
                    placeholder="留空表示不限"
                  />
                </label>

                <label className="admin-field">
                  <span>月 Token 限额</span>
                  <input
                    className="admin-input"
                    aria-label="月限额"
                    type="number"
                    min="0"
                    value={createForm.monthlyLimit}
                    onChange={(event) => updateCreateField('monthlyLimit', event.target.value)}
                    placeholder="留空表示不限"
                  />
                </label>
              </div>

              <label className="admin-drawer-toggle">
                <input
                  type="checkbox"
                  checked={createForm.enabled}
                  onChange={(event) => updateCreateField('enabled', event.target.checked)}
                />
                <span>创建后立即启用</span>
              </label>

              <div className="admin-drawer-footer">
                <button
                  className="admin-button"
                  type="button"
                  onClick={() => setIsCreateModalOpen(false)}
                >
                  取消
                </button>
                <button className="admin-button admin-primary-button" type="submit" disabled={!!updatingKeys['create-user']}>
                  <Plus size={14} />
                  创建用户
                </button>
              </div>
            </form>
          </aside>
        </div>
      ) : null}

      {loginEventsUser ? (
        <div className="admin-modal-backdrop" role="presentation">
          <section className="admin-modal admin-login-events-modal" role="dialog" aria-modal="true" aria-labelledby="admin-login-events-title">
            <div className="admin-modal-header">
              <div>
                <h3 id="admin-login-events-title">登录历史</h3>
                <p>{loginEventsUser.username} · @{loginEventsUser.user_id}</p>
              </div>
              <button
                className="admin-button admin-icon-button admin-icon-only-button"
                type="button"
                aria-label="关闭登录历史"
                onClick={closeLoginEvents}
              >
                <X size={14} />
              </button>
            </div>
            <div className="admin-login-events-body">
              {loginEventsLoading ? (
                <div className="admin-loading">正在加载登录历史</div>
              ) : loginEventsError ? (
                <div className="admin-error admin-inline-message">{loginEventsError}</div>
              ) : (loginEvents?.events.length || 0) > 0 ? (
                <div className="admin-table-wrap">
                  <table className="admin-table admin-login-events-table">
                    <thead>
                      <tr>
                        <th>登录时间</th>
                        <th>IP</th>
                        <th>User-Agent</th>
                      </tr>
                    </thead>
                    <tbody>
                      {loginEvents?.events.map((event) => (
                        <tr key={event.id}>
                          <td>{formatDateTime(event.login_at)}</td>
                          <td>{event.ip_address || '-'}</td>
                          <td className="admin-user-agent-cell">{event.user_agent || '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="admin-loading">暂无登录记录</div>
              )}
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}

function TokenUsageBar({ label, used, limit }: { label: string; used: number; limit: number | null }) {
  const percent = tokenPercent(used, limit);
  return (
    <div className="admin-token-bar-row">
      <div className="admin-token-bar-meta">
        <span>{label}</span>
        <strong>{formatNumber(used)} / {formatLimit(limit)}</strong>
      </div>
      <div className={`admin-token-bar ${limit === null ? 'unlimited' : ''}`}>
        <span style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

interface SandboxProfileFormValues {
  name: string;
  description: string;
  department: string;
  domain: string;
  protocol: 'http' | 'https';
  apiKey: string;
  useServerProxy: boolean;
  enabled: boolean;
}

function defaultSandboxProfileForm(): SandboxProfileFormValues {
  return {
    name: '',
    description: '',
    department: '',
    domain: '',
    protocol: 'http',
    apiKey: '',
    useServerProxy: true,
    enabled: true,
  };
}

function formFromSandboxProfile(profile: AdminSandboxProfile): SandboxProfileFormValues {
  return {
    name: profile.name,
    description: profile.description || '',
    department: profile.department || '',
    domain: profile.domain,
    protocol: profile.protocol,
    apiKey: '',
    useServerProxy: profile.use_server_proxy,
    enabled: profile.enabled,
  };
}

function payloadFromSandboxProfileForm(
  form: SandboxProfileFormValues,
  editing: boolean,
): AdminSandboxProfilePayload {
  const payload: AdminSandboxProfilePayload = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    department: form.department.trim() || null,
    domain: form.domain.trim(),
    protocol: form.protocol,
    use_server_proxy: form.useServerProxy,
  };
  const apiKey = form.apiKey.trim();
  if (!editing) {
    payload.enabled = form.enabled;
    payload.api_key = apiKey;
  } else if (apiKey) {
    payload.api_key = apiKey;
  }
  return payload;
}

function SandboxesPanel({
  data,
  actionError,
  actionMessage,
  onDismissActionError,
  onDismissActionMessage,
  updatingKeys,
  onSaveProfile,
  onSetDefault,
  onToggleEnabled,
}: {
  data: AdminSandboxProfilesResponse | null;
  actionError: string;
  actionMessage: string;
  onDismissActionError: () => void;
  onDismissActionMessage: () => void;
  updatingKeys: Record<string, boolean>;
  onSaveProfile: (profileId: string | null, payload: AdminSandboxProfilePayload) => Promise<boolean>;
  onSetDefault: (profile: AdminSandboxProfile) => Promise<void>;
  onToggleEnabled: (profile: AdminSandboxProfile) => Promise<void>;
}) {
  const profiles = data?.profiles || [];
  const [editingProfileId, setEditingProfileId] = useState<string | null>(null);
  const [form, setForm] = useState<SandboxProfileFormValues>(() => defaultSandboxProfileForm());

  const updateForm = <Key extends keyof SandboxProfileFormValues>(key: Key, value: SandboxProfileFormValues[Key]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleEdit = (profile: AdminSandboxProfile) => {
    setEditingProfileId(profile.id);
    setForm(formFromSandboxProfile(profile));
  };

  const handleReset = () => {
    setEditingProfileId(null);
    setForm(defaultSandboxProfileForm());
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const saved = await onSaveProfile(
      editingProfileId,
      payloadFromSandboxProfileForm(form, Boolean(editingProfileId)),
    );
    if (saved) handleReset();
  };

  const enabledCount = profiles.filter((profile) => profile.enabled).length;

  return (
    <>
      <div className="admin-grid-3">
        <MetricCard label="沙箱后端" value={formatNumber(profiles.length)} hint={`${enabledCount} 个启用`} />
        <MetricCard label="默认后端" value={profiles.find((profile) => profile.is_default)?.name || '-'} />
        <MetricCard label="需关注" value={formatNumber(profiles.filter((profile) => !profile.enabled).length)} hint="已禁用后端" />
      </div>

      {actionError ? (
        <FeedbackMessage className="admin-error admin-inline-message" tone="error" onDismiss={onDismissActionError}>
          {actionError}
        </FeedbackMessage>
      ) : null}
      {actionMessage ? (
        <FeedbackMessage
          className="admin-toast"
          tone="success"
          onDismiss={onDismissActionMessage}
        >
          {actionMessage}
        </FeedbackMessage>
      ) : null}

      <div className="admin-card">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title">{editingProfileId ? '编辑沙箱后端' : '注册沙箱后端'}</h3>
            <div className="admin-card-header-sub">保存连接信息会使绑定用户的旧 sandbox 在下次使用时重建</div>
          </div>
          {editingProfileId ? (
            <button className="admin-button" type="button" onClick={handleReset}>
              取消编辑
            </button>
          ) : null}
        </div>
        <form className="admin-sandbox-form" onSubmit={handleSubmit}>
          <label className="admin-field">
            <span>名称</span>
            <input className="admin-input" value={form.name} onChange={(event) => updateForm('name', event.target.value)} required />
          </label>
          <label className="admin-field">
            <span>部门</span>
            <input className="admin-input" value={form.department} onChange={(event) => updateForm('department', event.target.value)} placeholder="交易 / 投研 / IT" />
          </label>
          <label className="admin-field">
            <span>Domain</span>
            <input className="admin-input" value={form.domain} onChange={(event) => updateForm('domain', event.target.value)} placeholder="10.0.0.10:8080" required />
          </label>
          <label className="admin-field">
            <span>协议</span>
            <select className="admin-select" value={form.protocol} onChange={(event) => updateForm('protocol', event.target.value as 'http' | 'https')}>
              <option value="http">http</option>
              <option value="https">https</option>
            </select>
          </label>
          <label className="admin-field">
            <span>API Key</span>
            <input className="admin-input" value={form.apiKey} onChange={(event) => updateForm('apiKey', event.target.value)} placeholder={editingProfileId ? '留空保持不变' : '必填'} required={!editingProfileId} />
          </label>
          <label className="admin-checkbox-field">
            <input type="checkbox" checked={form.useServerProxy} onChange={(event) => updateForm('useServerProxy', event.target.checked)} />
            Server Proxy
          </label>
          {!editingProfileId ? (
            <label className="admin-checkbox-field">
              <input type="checkbox" checked={form.enabled} onChange={(event) => updateForm('enabled', event.target.checked)} />
              启用
            </label>
          ) : null}
          <label className="admin-field admin-sandbox-description-field">
            <span>备注</span>
            <input className="admin-input" value={form.description} onChange={(event) => updateForm('description', event.target.value)} />
          </label>
          <button className="admin-button admin-primary-button" type="submit" disabled={!!updatingKeys[editingProfileId ? `profile-${editingProfileId}` : 'profile-create']}>
            <Save size={14} />
            {editingProfileId ? '保存后端' : '创建后端'}
          </button>
        </form>
      </div>

      <div className="admin-card">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title">沙箱后端列表</h3>
            <div className="admin-card-header-sub">状态来自数据库配置，不实时查询 OpenSandbox</div>
          </div>
        </div>
        <div className="admin-table-wrap">
          <table className="admin-table admin-sandbox-table">
            <thead>
              <tr>
                <th>后端</th>
                <th>连接</th>
                <th>绑定</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {profiles.map((profile) => (
                <tr key={profile.id}>
                  <td>
                    <strong>{profile.name}</strong>
                    <div className="admin-subline">{profile.department || '-'} · v{profile.version}</div>
                  </td>
                  <td>
                    <div className="admin-token-stack">
                      <span>{profile.protocol}://{profile.domain}</span>
                      <span>API Key {profile.api_key_set ? '已设置' : '未设置'}</span>
                      <span>{profile.use_server_proxy ? 'Server Proxy' : 'Direct'}</span>
                    </div>
                  </td>
                  <td>
                    <div className="admin-runtime-grid">
                      <span><b>{profile.bound_users}</b> 绑定用户</span>
                    </div>
                  </td>
                  <td>
                    <div className="admin-token-stack">
                      <span className={`admin-status ${profile.enabled ? 'ok' : 'disabled'}`}>{profile.enabled ? '启用' : '禁用'}</span>
                      {profile.is_default ? <span className="admin-status admin">默认</span> : null}
                    </div>
                  </td>
                  <td>
                    <div className="admin-row-actions">
                      <button className="admin-button admin-icon-button" type="button" onClick={() => handleEdit(profile)}>
                        编辑
                      </button>
                      <button
                        className="admin-button admin-icon-button"
                        type="button"
                        disabled={profile.is_default || !profile.enabled || !!updatingKeys[`default-${profile.id}`]}
                        onClick={() => { void onSetDefault(profile); }}
                      >
                        设为默认
                      </button>
                      <button
                        className={`admin-button admin-icon-button ${profile.enabled ? '' : 'admin-danger-button'}`}
                        type="button"
                        disabled={(profile.is_default && profile.enabled) || !!updatingKeys[`enabled-${profile.id}`]}
                        onClick={() => { void onToggleEnabled(profile); }}
                      >
                        {profile.enabled ? '禁用' : '启用'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {profiles.length === 0 ? (
                <tr>
                  <td colSpan={5}>
                    <div className="admin-loading">暂无沙箱后端</div>
                  </td>
                </tr>
              ) : null}
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
  const database = data.database;
  const pool = database?.pool;
  const activityRows = database?.activity || [];
  const longQueries = database?.long_queries || [];
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

      <div className="admin-card">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title">数据库运行态</h3>
            {pool ? (
              <div className="admin-card-header-sub">
                {pool.url_database || '-'} · {pool.pool_class}
              </div>
            ) : null}
          </div>
          {database?.error ? (
            <span className="admin-status error">诊断失败</span>
          ) : pool ? (
            <span className="admin-status ok">已连接</span>
          ) : null}
        </div>
        <div className="admin-card-body admin-db-body">
          {!pool ? (
            <div className="admin-loading">暂无数据库诊断数据</div>
          ) : (
            <>
              <div className="admin-db-metric-grid">
                <DatabaseMetric
                  label="Pool Size"
                  value={formatOptionalNumber(pool.size)}
                  hint={`配置 ${formatNumber(pool.configured.pool_size)} / 溢出 ${formatNumber(pool.configured.max_overflow)}`}
                />
                <DatabaseMetric label="Checked In" value={formatOptionalNumber(pool.checked_in)} />
                <DatabaseMetric label="Checked Out" value={formatOptionalNumber(pool.checked_out)} />
                <DatabaseMetric
                  label="Blocked Locks"
                  value={formatOptionalNumber(database?.blocked_locks)}
                  tone={database?.blocked_locks ? 'warn' : 'ok'}
                />
              </div>

              <div className="admin-db-config-grid">
                <div><span>Pool Timeout</span><strong>{formatNumber(pool.configured.pool_timeout_seconds)}s</strong></div>
                <div><span>Pool Recycle</span><strong>{formatNumber(pool.configured.pool_recycle_seconds)}s</strong></div>
                <div><span>Active Overflow</span><strong>{formatPoolOverflow(pool.overflow)}</strong></div>
                <div><span>Database</span><strong>{pool.url_database || '-'}</strong></div>
              </div>

              <div className="admin-db-status-line">
                <span>Pool Status</span>
                <code>{pool.status}</code>
              </div>

              {database?.error ? (
                <div className="admin-inline-message admin-db-error">{database.error}</div>
              ) : null}

              <div className="admin-db-tables">
                <div className="admin-db-section">
                  <div className="admin-db-section-title">连接活动</div>
                  <div className="admin-table-wrap">
                    <table className="admin-table admin-db-table">
                      <thead>
                        <tr>
                          <th>State</th>
                          <th>Wait Type</th>
                          <th>Wait Event</th>
                          <th>Count</th>
                        </tr>
                      </thead>
                      <tbody>
                        {activityRows.map((row) => (
                          <tr key={`${row.state || 'none'}-${row.wait_event_type}-${row.wait_event}`}>
                            <td>{row.state || 'none'}</td>
                            <td>{row.wait_event_type}</td>
                            <td>{row.wait_event}</td>
                            <td>{formatNumber(row.count)}</td>
                          </tr>
                        ))}
                        {activityRows.length === 0 ? (
                          <tr>
                            <td colSpan={4}>暂无活动数据</td>
                          </tr>
                        ) : null}
                      </tbody>
                    </table>
                  </div>
                </div>

                <div className="admin-db-section">
                  <div className="admin-db-section-title">长查询</div>
                  <div className="admin-table-wrap">
                    <table className="admin-table admin-db-table admin-db-query-table">
                      <thead>
                        <tr>
                          <th>PID</th>
                          <th>State</th>
                          <th>Wait</th>
                          <th>Age</th>
                          <th>Query</th>
                        </tr>
                      </thead>
                      <tbody>
                        {longQueries.map((query) => (
                          <tr key={`${query.pid}-${query.age_seconds}`}>
                            <td>{query.pid}</td>
                            <td>{query.state || 'none'}</td>
                            <td>{query.wait_event_type}/{query.wait_event}</td>
                            <td>{formatNumber(query.age_seconds)}s</td>
                            <td className="admin-db-query-sample">{query.query_sample || '-'}</td>
                          </tr>
                        ))}
                        {longQueries.length === 0 ? (
                          <tr>
                            <td colSpan={5}>暂无超过 30 秒的活动查询</td>
                          </tr>
                        ) : null}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}

function DatabaseMetric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'ok' | 'warn';
}) {
  return (
    <div className={`admin-db-metric ${tone || ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint ? <small>{hint}</small> : null}
    </div>
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
