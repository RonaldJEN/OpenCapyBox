import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Plus,
  RefreshCw,
  Shield,
  Trash2,
} from 'lucide-react';

import { getAdminMcpServers, type McpServer } from '../services/mcpApi';
import {
  createAdminPermissionRule,
  deleteAdminPermissionRule,
  getAdminPermissionRules,
  updateAdminPermissionRule,
  type PermissionEffect,
  type ToolPermissionRule,
  type ToolProvider,
} from '../services/permissionApi';
import FeedbackMessage from './FeedbackMessage';
import './AdminToolPermissionsPanel.css';

interface AdminToolPermissionsPanelProps {
  refreshToken?: number;
}

interface RuleForm {
  provider: ToolProvider;
  serverId: string;
  toolName: string;
  effect: PermissionEffect;
  priority: string;
  description: string;
}

const BUILTIN_TOOLS = [
  'read_file',
  'read_image_file',
  'write_file',
  'edit_file',
  'bash',
  'bash_output',
  'bash_kill',
  'record_note',
  'recall_notes',
  'update_long_term_memory',
  'search_memory',
  'read_user',
  'update_user',
  'manage_cron',
  'ask_user',
  'sub_agent',
  'get_skill',
  'mcp_tool_search',
  'glm_search',
  'glm_batch_search',
] as const;

const EFFECT_LABELS: Record<PermissionEffect, string> = {
  allow: 'ALLOW',
  ask: 'ASK',
  deny: 'DENY',
};

function emptyForm(): RuleForm {
  return {
    provider: 'builtin',
    serverId: '',
    toolName: '*',
    effect: 'ask',
    priority: '0',
    description: '',
  };
}

function errorText(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  return error instanceof Error ? error.message : '平台权限规则操作失败';
}

export default function AdminToolPermissionsPanel({ refreshToken = 0 }: AdminToolPermissionsPanelProps) {
  const [rules, setRules] = useState<ToolPermissionRule[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [form, setForm] = useState<RuleForm>(emptyForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyRuleId, setBusyRuleId] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [nextRules, nextServers] = await Promise.all([
        getAdminPermissionRules(),
        getAdminMcpServers(),
      ]);
      setRules(nextRules);
      setServers(nextServers);
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  const serverNames = useMemo(
    () => new Map(servers.map((server) => [server.id, server.name])),
    [servers],
  );

  const updateForm = <K extends keyof RuleForm>(key: K, value: RuleForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    if (form.provider === 'mcp' && !form.serverId) {
      setError('请选择要约束的官方 MCP 服务');
      return;
    }
    const toolName = form.toolName.trim();
    if (!toolName) {
      setMessage('');
      setError('请输入工具名，或使用 * 匹配全部工具');
      return;
    }
    const priority = Number(form.priority);
    if (!Number.isInteger(priority) || priority < -10000 || priority > 10000) {
      setError('优先级必须是 -10000 到 10000 之间的整数');
      return;
    }

    setSaving(true);
    setError('');
    setMessage('');
    try {
      await createAdminPermissionRule({
        provider: form.provider,
        server_id: form.provider === 'mcp' ? form.serverId : null,
        tool_name: toolName,
        effect: form.effect,
        priority,
        description: form.description.trim() || null,
      });
      setForm(emptyForm());
      setMessage('平台权限规则已创建');
      await load();
    } catch (createError) {
      setError(errorText(createError));
    } finally {
      setSaving(false);
    }
  };

  const updateRule = async (
    rule: ToolPermissionRule,
    changes: Partial<Pick<ToolPermissionRule, 'effect' | 'enabled'>>,
  ) => {
    if (busyRuleId) return;
    setBusyRuleId(rule.id);
    setError('');
    setMessage('');
    try {
      const updated = await updateAdminPermissionRule(rule.id, changes);
      setRules((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setMessage('平台权限规则已更新');
    } catch (updateError) {
      setError(errorText(updateError));
    } finally {
      setBusyRuleId('');
    }
  };

  const removeRule = async (rule: ToolPermissionRule) => {
    if (busyRuleId || !window.confirm(`确认删除平台规则 ${rule.tool_ref}？`)) return;
    setBusyRuleId(rule.id);
    setError('');
    setMessage('');
    try {
      await deleteAdminPermissionRule(rule.id);
      setRules((current) => current.filter((item) => item.id !== rule.id));
      setMessage('平台权限规则已删除');
    } catch (deleteError) {
      setError(errorText(deleteError));
    } finally {
      setBusyRuleId('');
    }
  };

  const denyCount = rules.filter((rule) => rule.enabled && rule.effect === 'deny').length;
  const askCount = rules.filter((rule) => rule.enabled && rule.effect === 'ask').length;

  return (
    <div className="admin-tool-policy-page">
      <section className="admin-tool-policy-hero">
        <div>
          <span className="admin-tool-policy-eyebrow"><Shield size={13} /> PLATFORM POLICY</span>
          <h2>工具权限边界</h2>
          <p>
            平台规则独立于 MCP 连接目录。DENY 是不可被用户放宽的硬限制，ASK 是用户侧策略的审批上限；执行前仍会再次校验。
          </p>
        </div>
        <div className="admin-tool-policy-metrics" aria-label="平台权限规则统计">
          <div><strong>{rules.length}</strong><span>规则总数</span></div>
          <div><strong>{denyCount}</strong><span>强制拒绝</span></div>
          <div><strong>{askCount}</strong><span>强制确认</span></div>
        </div>
      </section>

      {error ? (
        <FeedbackMessage
          className="admin-error admin-inline-message"
          tone="error"
          icon={<AlertCircle size={14} />}
          onDismiss={() => setError('')}
        >
          {error}
        </FeedbackMessage>
      ) : null}
      {message ? (
        <FeedbackMessage
          className="admin-toast"
          tone="success"
          icon={<CheckCircle2 size={14} />}
          onDismiss={() => setMessage('')}
        >
          {message}
        </FeedbackMessage>
      ) : null}

      <section className="admin-card admin-tool-policy-create">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title">新建平台规则</h3>
            <div className="admin-card-header-sub">使用 * 匹配某一提供方或 MCP 服务下的全部工具</div>
          </div>
        </div>
        <form onSubmit={handleCreate}>
          <label className="admin-field">
            <span>提供方</span>
            <select
              aria-label="规则提供方"
              className="admin-select"
              value={form.provider}
              onChange={(event) => {
                const provider = event.target.value as ToolProvider;
                setForm((current) => ({ ...current, provider, serverId: '' }));
              }}
            >
              <option value="builtin">内置工具</option>
              <option value="mcp">官方 MCP</option>
            </select>
          </label>

          {form.provider === 'mcp' ? (
            <label className="admin-field">
              <span>MCP 服务</span>
              <select
                aria-label="MCP 服务"
                className="admin-select"
                value={form.serverId}
                onChange={(event) => updateForm('serverId', event.target.value)}
                required
              >
                <option value="">请选择</option>
                {servers.map((server) => <option key={server.id} value={server.id}>{server.name}</option>)}
              </select>
            </label>
          ) : null}

          <label className="admin-field admin-tool-policy-tool-field">
            <span>工具名</span>
            <input
              aria-label="工具名"
              className="admin-input"
              list={form.provider === 'builtin' ? 'builtin-tool-names' : undefined}
              value={form.toolName}
              onChange={(event) => updateForm('toolName', event.target.value)}
              placeholder="* 或原始工具名"
              required
            />
            <datalist id="builtin-tool-names">
              <option value="*" />
              {BUILTIN_TOOLS.map((tool) => <option key={tool} value={tool} />)}
            </datalist>
          </label>

          <label className="admin-field">
            <span>策略</span>
            <select
              aria-label="规则策略"
              className="admin-select"
              value={form.effect}
              onChange={(event) => updateForm('effect', event.target.value as PermissionEffect)}
            >
              <option value="deny">DENY</option>
              <option value="ask">ASK</option>
              <option value="allow">ALLOW</option>
            </select>
          </label>

          <label className="admin-field">
            <span>优先级</span>
            <input
              aria-label="规则优先级"
              className="admin-input"
              type="number"
              min={-10000}
              max={10000}
              step={1}
              value={form.priority}
              onChange={(event) => updateForm('priority', event.target.value)}
            />
          </label>

          <label className="admin-field admin-tool-policy-description-field">
            <span>说明</span>
            <input
              aria-label="规则说明"
              className="admin-input"
              value={form.description}
              onChange={(event) => updateForm('description', event.target.value)}
              placeholder="说明设置该平台边界的原因"
            />
          </label>

          <button className="admin-button admin-primary-button" type="submit" disabled={saving}>
            {saving ? <Loader2 size={14} className="admin-tool-policy-spin" /> : <Plus size={14} />}
            创建规则
          </button>
        </form>
      </section>

      <section className="admin-card">
        <div className="admin-card-header">
          <div>
            <h3 className="admin-card-header-title">平台规则</h3>
            <div className="admin-card-header-sub">平台规则优先于用户规则；用户不能越过平台 DENY / ASK 上限</div>
          </div>
          <button className="admin-button" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={14} className={loading ? 'admin-tool-policy-spin' : ''} />刷新
          </button>
        </div>

        {loading && rules.length === 0 ? (
          <div className="admin-loading"><Loader2 size={15} className="admin-tool-policy-spin" />加载平台规则...</div>
        ) : (
          <div className="admin-table-wrap">
            <table className="admin-table admin-tool-policy-table">
              <thead>
                <tr>
                  <th>工具边界</th>
                  <th>策略</th>
                  <th>优先级</th>
                  <th>状态</th>
                  <th>说明</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {rules.map((rule) => {
                  const busy = busyRuleId === rule.id;
                  return (
                    <tr key={rule.id}>
                      <td>
                        <code>{rule.tool_ref}</code>
                        <div className="admin-subline">
                          {rule.provider === 'mcp'
                            ? `官方 MCP · ${serverNames.get(rule.server_id || '') || rule.server_id || '-'}`
                            : '内置工具'}
                        </div>
                      </td>
                      <td>
                        <select
                          aria-label={`${rule.tool_ref} 策略`}
                          className={`admin-select admin-tool-policy-effect ${rule.effect}`}
                          value={rule.effect}
                          disabled={busy}
                          onChange={(event) => void updateRule(rule, { effect: event.target.value as PermissionEffect })}
                        >
                          {(['allow', 'ask', 'deny'] as PermissionEffect[]).map((effect) => (
                            <option key={effect} value={effect}>{EFFECT_LABELS[effect]}</option>
                          ))}
                        </select>
                      </td>
                      <td>{rule.priority}</td>
                      <td>
                        <button
                          type="button"
                          className={`admin-status ${rule.enabled ? 'ok' : 'disabled'}`}
                          aria-label={`${rule.tool_ref} ${rule.enabled ? '禁用' : '启用'}`}
                          disabled={busy}
                          onClick={() => void updateRule(rule, { enabled: !rule.enabled })}
                        >
                          {rule.enabled ? '已启用' : '已禁用'}
                        </button>
                      </td>
                      <td>{rule.description || '-'}</td>
                      <td>
                        <button
                          type="button"
                          className="admin-button admin-danger-button"
                          aria-label={`删除 ${rule.tool_ref}`}
                          disabled={busy}
                          onClick={() => void removeRule(rule)}
                        >
                          {busy ? <Loader2 size={13} className="admin-tool-policy-spin" /> : <Trash2 size={13} />}
                          删除
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {rules.length === 0 ? (
                  <tr><td colSpan={6}><div className="admin-loading">暂无平台权限规则</div></td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
