import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Download,
  FileClock,
  Filter,
  Loader2,
  Search,
  ShieldAlert,
} from 'lucide-react';

import {
  exportAdminOperationLogs,
  getAdminOperationLogs,
  type AdminOperationLogFilters,
  type AdminOperationLogItem,
  type AdminOperationLogOutcome,
  type AdminOperationLogRiskLevel,
  type AdminOperationLogsResponse,
} from '../services/adminApi';
import { extractBlobAwareErrorMessage, extractErrorMessage } from '../utils/errorMessages';
import './AdminAuditLogPanel.css';

interface AdminAuditLogPanelProps {
  refreshToken?: number;
}

interface AuditFilterDraft {
  from: string;
  to: string;
  action: string;
  targetUserId: string;
  sessionId: string;
  outcome: '' | AdminOperationLogOutcome;
  riskLevel: '' | AdminOperationLogRiskLevel;
}

const ACTION_LABELS: Record<string, string> = {
  'overview.read': '查看管理概览',
  'system.read': '查看系统运行状态',
  'session.list': '加载会话概览',
  'session.search': '搜索会话',
  'session.view': '查看会话列表',
  'step.view': '查看会话步骤原文',
  'step.review.update': '更新步骤审阅',
  'user.list': '查看用户列表',
  'user.login_history.view': '查看用户登录历史',
  'user.create': '创建用户',
  'user.enabled.update': '更新用户启用状态',
  'user.admin.update': '更新管理员权限',
  'user.token_limits.update': '更新用户 Token 限额',
  'user.model_groups.update': '更新用户模型权限包',
  'user.password.reset': '重置用户密码',
  'user.delete': '删除用户',
  'user.export': '导出用户数据',
  'sandbox.list': '查看沙箱列表',
  'sandbox.create': '创建沙箱配置',
  'sandbox.update': '更新沙箱配置',
  'sandbox.default.set': '设置默认沙箱',
  'sandbox.enabled.update': '更新沙箱启用状态',
  'user.sandbox.update': '更新用户沙箱',
  'model.list': '查看模型列表',
  'model.create': '创建模型',
  'model.update': '更新模型',
  'model.delete': '删除模型',
  'model.settings.update': '更新模型设置',
  'model_group.list': '查看模型权限包',
  'model_group.create': '创建模型权限包',
  'model_group.update': '更新模型权限包',
  'model_group.models.update': '更新权限包模型',
  'model_group.users.update': '更新权限包用户',
  'mcp.list': '查看 MCP 列表',
  'mcp.create': '创建 MCP',
  'mcp.update': '更新 MCP',
  'mcp.delete': '删除 MCP',
  'mcp.test': '测试 MCP 连接',
  'tool_permission.list': '查看工具权限',
  'tool_permission.create': '创建工具权限',
  'tool_permission.update': '更新工具权限',
  'tool_permission.delete': '删除工具权限',
  'audit_log.list': '查看操作日志',
  'audit_log.export': '导出操作日志',
};

const TARGET_TYPE_LABELS: Record<string, string> = {
  user: '用户',
  session: '会话',
  step: '步骤',
  sandbox: '沙箱',
  model: '模型',
  model_group: '模型权限包',
  mcp: 'MCP',
  tool_permission: '工具权限',
  audit_log: '操作日志',
};

type AdminAuditDisplayCategory =
  | 'high'
  | 'session-access'
  | 'user-access'
  | 'audit-access'
  | 'account-permission'
  | 'config-change'
  | 'delete'
  | 'export'
  | 'governance'
  | 'external-test'
  | 'routine';

type AdminAuditBadgeTone = 'high' | 'attention' | 'important';

const ACTION_CATEGORIES: Record<string, AdminAuditDisplayCategory> = {
  'session.list': 'session-access',
  'session.search': 'session-access',
  'session.view': 'session-access',
  'user.list': 'user-access',
  'user.login_history.view': 'user-access',
  'audit_log.list': 'audit-access',
  'step.view': 'high',
  'user.create': 'account-permission',
  'user.enabled.update': 'account-permission',
  'user.admin.update': 'account-permission',
  'user.model_groups.update': 'account-permission',
  'user.password.reset': 'account-permission',
  'model_group.models.update': 'config-change',
  'model_group.users.update': 'account-permission',
  'tool_permission.create': 'account-permission',
  'tool_permission.update': 'account-permission',
  'user.token_limits.update': 'config-change',
  'user.sandbox.update': 'config-change',
  'sandbox.create': 'config-change',
  'sandbox.update': 'config-change',
  'sandbox.default.set': 'config-change',
  'sandbox.enabled.update': 'config-change',
  'model.create': 'config-change',
  'model.update': 'config-change',
  'model.settings.update': 'config-change',
  'model_group.create': 'config-change',
  'model_group.update': 'config-change',
  'mcp.create': 'config-change',
  'mcp.update': 'config-change',
  'user.delete': 'delete',
  'model.delete': 'delete',
  'mcp.delete': 'delete',
  'tool_permission.delete': 'delete',
  'user.export': 'export',
  'audit_log.export': 'export',
  'step.review.update': 'governance',
  'mcp.test': 'external-test',
};

const CATEGORY_LABELS: Record<AdminAuditDisplayCategory, string> = {
  high: '高危 · 会话步骤原文',
  'session-access': '会话信息查阅',
  'user-access': '用户信息查阅',
  'audit-access': '审计日志查阅',
  'account-permission': '账号与权限',
  'config-change': '配置变更',
  delete: '删除操作',
  export: '数据导出',
  governance: '治理操作',
  'external-test': '外联测试',
  routine: '',
};

const OUTCOME_LABELS: Record<AdminOperationLogOutcome, string> = {
  succeeded: '成功',
  failed: '失败',
  started: '中断 / 结果未知',
};

function localDateTimeValue(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function initialFilters(): AuditFilterDraft {
  return {
    from: localDateTimeValue(new Date(Date.now() - 24 * 60 * 60 * 1000)),
    to: '',
    action: '',
    targetUserId: '',
    sessionId: '',
    outcome: '',
    riskLevel: '',
  };
}

function toIso(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function requestFilters(filters: AuditFilterDraft): Omit<AdminOperationLogFilters, 'cursor' | 'limit'> {
  return {
    from: toIso(filters.from),
    to: toIso(filters.to),
    action: filters.action || undefined,
    target_user_id: filters.targetUserId.trim() || undefined,
    session_id: filters.sessionId.trim() || undefined,
    outcome: filters.outcome || undefined,
    risk_level: filters.riskLevel || undefined,
  };
}

function formatDateTime(value: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

function formatJson(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'string') return value || '-';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function errorText(error: unknown, fallback: string): string {
  return extractErrorMessage(error) || fallback;
}

function targetText(item: AdminOperationLogItem): string {
  const targetMatchesCanonical = (
    (item.target_type === 'user' && item.target_id === item.target_user_id)
    || (item.target_type === 'session' && item.target_id === item.session_id)
    || (item.target_type === 'step'
      && item.step_record_id !== null
      && item.step_record_id !== undefined
      && item.target_id === String(item.step_record_id))
  );
  const parts = [
    item.target_type && item.target_id && !targetMatchesCanonical
      ? `${TARGET_TYPE_LABELS[item.target_type] || item.target_type}: ${item.target_id}`
      : null,
    item.target_user_id ? `用户: ${item.target_user_id}` : null,
    item.session_id ? `会话: ${item.session_id}` : null,
    item.step_record_id !== null && item.step_record_id !== undefined
      ? `步骤: ${item.step_record_id}`
      : null,
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : '-';
}

function actionLabel(action: string): string {
  return ACTION_LABELS[action] || '未识别操作';
}

function displayCategory(action: string): AdminAuditDisplayCategory {
  return ACTION_CATEGORIES[action] || 'routine';
}

function badgeTone(category: AdminAuditDisplayCategory): AdminAuditBadgeTone | null {
  if (category === 'high') return 'high';
  if (category === 'session-access' || category === 'user-access' || category === 'audit-access') {
    return 'attention';
  }
  return category === 'routine' ? null : 'important';
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

export default function AdminAuditLogPanel({ refreshToken = 0 }: AdminAuditLogPanelProps) {
  const [draft, setDraft] = useState<AuditFilterDraft>(initialFilters);
  const draftRef = useRef(draft);
  const [applied, setApplied] = useState<AuditFilterDraft>(initialFilters);
  const [cursorHistory, setCursorHistory] = useState<Array<string | undefined>>([undefined]);
  const [pageIndex, setPageIndex] = useState(0);
  const [queryRevision, setQueryRevision] = useState(0);
  const [data, setData] = useState<AdminOperationLogsResponse>({ items: [], next_cursor: null });
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [exportError, setExportError] = useState('');
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const currentCursor = cursorHistory[pageIndex];

  const updateDraft = (patch: Partial<AuditFilterDraft>): AuditFilterDraft => {
    const next = { ...draftRef.current, ...patch };
    draftRef.current = next;
    setDraft(next);
    return next;
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setExpandedId(null);
    void getAdminOperationLogs({
      ...requestFilters(applied),
      cursor: currentCursor,
      limit: 50,
    })
      .then((response) => {
        if (!cancelled) setData(response);
      })
      .catch((loadError) => {
        if (!cancelled) {
          setData({ items: [], next_cursor: null });
          setError(errorText(loadError, '操作日志加载失败，请稍后重试'));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applied, currentCursor, queryRevision, refreshToken]);

  const activeFilterCount = useMemo(() => [
    applied.from,
    applied.to,
    applied.action,
    applied.targetUserId,
    applied.sessionId,
    applied.outcome,
    applied.riskLevel,
  ].filter(Boolean).length, [applied]);

  const applyFilters = () => {
    setApplied({ ...draftRef.current });
    setCursorHistory([undefined]);
    setPageIndex(0);
    setQueryRevision((value) => value + 1);
  };

  const applyFilterPatch = (patch: Partial<AuditFilterDraft>) => {
    updateDraft(patch);
    setApplied((value) => ({ ...value, ...patch }));
    setCursorHistory([undefined]);
    setPageIndex(0);
    setQueryRevision((value) => value + 1);
  };

  const clearFilters = () => {
    const next = initialFilters();
    draftRef.current = next;
    setDraft(next);
    setApplied(next);
    setCursorHistory([undefined]);
    setPageIndex(0);
    setQueryRevision((value) => value + 1);
  };

  const nextPage = () => {
    if (!data.next_cursor) return;
    const nextHistory = cursorHistory.slice(0, pageIndex + 1);
    nextHistory.push(data.next_cursor);
    setCursorHistory(nextHistory);
    setPageIndex(pageIndex + 1);
  };

  const previousPage = () => {
    if (pageIndex > 0) setPageIndex(pageIndex - 1);
  };

  const exportLogs = async () => {
    setExporting(true);
    setExportError('');
    try {
      const blob = await exportAdminOperationLogs(requestFilters(applied));
      const day = new Date().toISOString().slice(0, 10);
      downloadBlob(`opencapybox-operation-logs-${day}.csv`, blob);
    } catch (exportFailure) {
      setExportError(
        (await extractBlobAwareErrorMessage(exportFailure))
        || '操作日志导出失败，请稍后重试',
      );
    } finally {
      setExporting(false);
    }
  };

  return (
    <section className="admin-audit-page" aria-labelledby="admin-audit-title">
      <div className="admin-audit-intro">
        <div className="admin-audit-intro-icon" aria-hidden="true"><FileClock size={24} /></div>
        <div>
          <div className="admin-audit-eyebrow">审计记录 · 只读</div>
          <h2 id="admin-audit-title">管理员操作留痕</h2>
          <p>按查阅对象和管理操作性质分类；仅会话步骤原文访问标记为高危。日志只展示脱敏元数据，不包含密码与密钥。</p>
        </div>
        <div className="admin-audit-window">
          <span>当前页</span>
          <strong>{data.items.length}</strong>
          <small>第 {pageIndex + 1} 页 · {activeFilterCount} 个筛选条件</small>
        </div>
      </div>

      <div className="admin-card admin-audit-filter-card">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title"><Filter size={14} /> 筛选日志</h3>
            <div className="admin-card-header-sub">下拉条件选择后立即生效；时间和 ID 输入后点击“查询”</div>
          </div>
          <button className="admin-button" type="button" onClick={clearFilters}>恢复最近 24 小时</button>
        </div>
        <form
          className="admin-audit-filters"
          onSubmit={(event) => {
            event.preventDefault();
            applyFilters();
          }}
        >
          <label>
            <span>开始时间</span>
            <input
              className="admin-input"
              type="datetime-local"
              value={draft.from}
              onChange={(event) => updateDraft({ from: event.target.value })}
            />
          </label>
          <label>
            <span>结束时间</span>
            <input
              className="admin-input"
              type="datetime-local"
              value={draft.to}
              onChange={(event) => updateDraft({ to: event.target.value })}
            />
          </label>
          <label>
            <span>操作类型</span>
            <select
              className="admin-select"
              value={draft.action}
              onChange={(event) => applyFilterPatch({ action: event.target.value })}
            >
              <option value="">全部操作</option>
              {Object.entries(ACTION_LABELS)
                .filter(([action]) => action in ACTION_CATEGORIES)
                .map(([action, label]) => (
                  <option key={action} value={action}>{label}（{action}）</option>
                ))}
            </select>
          </label>
          <label>
            <span>风险级别</span>
            <select
              className="admin-select"
              value={draft.riskLevel}
              onChange={(event) => applyFilterPatch({
                riskLevel: event.target.value as AuditFilterDraft['riskLevel'],
              })}
            >
              <option value="">全部风险</option>
              <option value="high">高危 · 会话步骤原文</option>
              <option value="normal">非高危操作</option>
            </select>
          </label>
          <label>
            <span>结果</span>
            <select
              className="admin-select"
              value={draft.outcome}
              onChange={(event) => applyFilterPatch({
                outcome: event.target.value as AuditFilterDraft['outcome'],
              })}
            >
              <option value="">全部结果</option>
              <option value="succeeded">成功</option>
              <option value="failed">失败</option>
              <option value="started">中断 / 结果未知</option>
            </select>
          </label>
          <label>
            <span>目标用户</span>
            <input
              className="admin-input"
              value={draft.targetUserId}
              placeholder="输入用户 ID"
              onChange={(event) => updateDraft({ targetUserId: event.target.value })}
            />
          </label>
          <label>
            <span>会话 ID</span>
            <input
              className="admin-input"
              value={draft.sessionId}
              placeholder="输入完整会话 ID"
              onChange={(event) => updateDraft({ sessionId: event.target.value })}
            />
          </label>
          <button className="admin-button admin-primary-button admin-audit-search" type="submit">
            <Search size={14} /> 查询
          </button>
        </form>
      </div>

      {error ? <div className="admin-error"><AlertCircle size={15} />{error}</div> : null}
      {exportError ? <div className="admin-error"><AlertCircle size={15} />{exportError}</div> : null}

      <div className="admin-card admin-audit-list-card">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title">操作记录</h3>
            <div className="admin-card-header-sub">“中断 / 结果未知”表示日志已开始，但未成功写入终态</div>
          </div>
          <button className="admin-button" type="button" onClick={() => void exportLogs()} disabled={exporting}>
            {exporting ? <Loader2 className="admin-audit-spin" size={14} /> : <Download size={14} />}
            {exporting ? '导出中...' : '导出当前筛选'}
          </button>
        </div>

        {loading ? (
          <div className="admin-audit-state"><Loader2 className="admin-audit-spin" size={18} /> 正在读取操作日志...</div>
        ) : data.items.length === 0 ? (
          <div className="admin-audit-state">当前筛选条件下没有操作记录</div>
        ) : (
          <div className="admin-table-wrap">
            <table className="admin-table admin-audit-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>管理员</th>
                  <th>操作</th>
                  <th>目标</th>
                  <th>结果</th>
                  <th>来源 IP</th>
                  <th>Request ID</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => {
                  const expanded = expandedId === item.id;
                  const itemDisplayCategory = displayCategory(item.action);
                  const itemBadgeTone = badgeTone(itemDisplayCategory);
                  const itemActionLabel = actionLabel(item.action);
                  return (
                    <Fragment key={item.id}>
                      <tr data-outcome={item.outcome} data-audit-level={itemBadgeTone || 'routine'}>
                        <td className="admin-audit-time">{formatDateTime(item.started_at)}</td>
                        <td><code>{item.actor_user_id}</code></td>
                        <td>
                          <div className="admin-audit-action-cell">
                            <div className="admin-audit-action-summary">
                              <span className="admin-audit-action-label">{itemActionLabel}</span>
                              {itemBadgeTone ? (
                                <span className={`admin-audit-level-badge ${itemBadgeTone}`}>
                                  {itemDisplayCategory === 'high' ? (
                                    <ShieldAlert size={11} aria-hidden="true" />
                                  ) : null}
                                  {CATEGORY_LABELS[itemDisplayCategory]}
                                </span>
                              ) : null}
                            </div>
                            <code className="admin-audit-action-code">{item.action}</code>
                          </div>
                        </td>
                        <td className="admin-audit-target" title={targetText(item)}>{targetText(item)}</td>
                        <td>
                          <span className={`admin-audit-outcome ${item.outcome}`}>{OUTCOME_LABELS[item.outcome]}</span>
                        </td>
                        <td><code>{item.ip_address || '-'}</code></td>
                        <td><code className="admin-audit-request-id" title={item.request_id}>{item.request_id}</code></td>
                        <td>
                          <button
                            className="admin-audit-detail-button"
                            type="button"
                            aria-expanded={expanded}
                            aria-label={`${expanded ? '收起' : '查看'}操作详情：${itemActionLabel}（${item.action}）`}
                            onClick={() => setExpandedId(expanded ? null : item.id)}
                          >
                            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                            {expanded ? '收起' : '查看'}
                          </button>
                        </td>
                      </tr>
                      {expanded ? (
                        <tr className="admin-audit-detail-row">
                          <td colSpan={8}>
                            <dl className="admin-audit-details">
                              <div><dt>请求</dt><dd><code>{item.http_method} {item.route_template}</code></dd></div>
                              <div><dt>HTTP 状态</dt><dd>{item.status_code ?? '-'}</dd></div>
                              <div><dt>完成时间</dt><dd>{formatDateTime(item.completed_at)}</dd></div>
                              <div><dt>User-Agent</dt><dd>{item.user_agent || '-'}</dd></div>
                              <div className="admin-audit-detail-wide"><dt>变更字段</dt><dd><pre>{formatJson(item.changed_fields)}</pre></dd></div>
                              <div className="admin-audit-detail-wide"><dt>补充信息（已脱敏）</dt><dd><pre>{formatJson(item.details)}</pre></dd></div>
                            </dl>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="admin-audit-pagination">
          <span>第 {pageIndex + 1} 页 · 每页最多 50 条</span>
          <div>
            <button className="admin-button" type="button" onClick={previousPage} disabled={loading || pageIndex === 0}>
              <ChevronLeft size={14} /> 上一页
            </button>
            <button className="admin-button" type="button" onClick={nextPage} disabled={loading || !data.next_cursor}>
              下一页 <ChevronRight size={14} />
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
