import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import {
  Activity,
  AlertCircle,
  Cable,
  CheckCircle2,
  Cloud,
  Edit3,
  Globe2,
  KeyRound,
  Loader2,
  Plus,
  Search,
  ShieldAlert,
  Trash2,
  X,
  Zap,
} from 'lucide-react';

import {
  createAdminMcpServer,
  deleteAdminMcpServer,
  getAdminMcpServers,
  testAdminMcpServer,
  updateAdminMcpServer,
  type AdminMcpServerPayload,
  type McpAuthMode,
  type McpServer,
  type McpServerStatus,
  type McpTestResult,
} from '../services/mcpApi';
import { extractValidationErrorMessage } from '../utils/errorMessages';
import './AdminMcpCatalogPanel.css';

interface AdminMcpCatalogPanelProps {
  refreshToken?: number;
  onDirtyChange?: (dirty: boolean) => void;
}

interface AdminMcpForm {
  name: string;
  description: string;
  url: string;
  status: McpServerStatus;
  authType: McpAuthMode;
  bearerToken: string;
  headersText: string;
  clearCredential: boolean;
  allowPrivateNetwork: boolean;
  allowInsecureHttp: boolean;
  required: boolean;
}

function emptyForm(): AdminMcpForm {
  return {
    name: '',
    description: '',
    url: '',
    status: 'draft',
    authType: 'none',
    bearerToken: '',
    headersText: '',
    clearCredential: false,
    allowPrivateNetwork: false,
    allowInsecureHttp: false,
    required: false,
  };
}

function formFromServer(server: McpServer): AdminMcpForm {
  return {
    name: server.name,
    description: server.description,
    url: server.url,
    status: server.status,
    authType: server.auth_type,
    bearerToken: '',
    headersText: '',
    clearCredential: false,
    allowPrivateNetwork: server.allow_private_network,
    allowInsecureHttp: server.allow_insecure_http,
    required: server.required,
  };
}

function normalizedOrigin(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

function trapDialogFocus(
  event: ReactKeyboardEvent<HTMLElement>,
  dialog: HTMLElement | null,
) {
  if (event.key !== 'Tab' || !dialog) return;
  const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((element) => element.getAttribute('aria-hidden') !== 'true');
  if (!focusable.length) {
    event.preventDefault();
    dialog.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (!active || active === dialog || !dialog.contains(active)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

function errorText(error: unknown): string {
  return extractValidationErrorMessage(error) || '官方 MCP 操作失败';
}

function parseHeaders(value: string): Record<string, string> | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed: unknown = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('请求头必须是 JSON 对象');
  }
  const entries = Object.entries(parsed);
  if (!entries.every(([key, item]) => key.trim() && typeof item === 'string')) {
    throw new Error('请求头名称不能为空，值必须是字符串');
  }
  return Object.fromEntries(entries);
}

function testMessage(result: McpTestResult): string {
  if (!result.ok) return result.error || '连接测试失败';
  const latency = result.latency_ms === null ? '' : ` · ${result.latency_ms} ms`;
  return `连接成功 · ${result.tools_count} 个工具${latency}`;
}

function statusText(status: McpServerStatus): string {
  if (status === 'published') return '已发布';
  if (status === 'disabled') return '已停用';
  return '草稿';
}

export default function AdminMcpCatalogPanel({
  refreshToken = 0,
  onDirtyChange,
}: AdminMcpCatalogPanelProps) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | McpServerStatus>('all');
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(() => new Set());
  const [pendingServerIds, setPendingServerIds] = useState<Set<string>>(() => new Set());
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingServer, setEditingServer] = useState<McpServer | null>(null);
  const [form, setForm] = useState<AdminMcpForm>(emptyForm);
  const [formError, setFormError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<McpServer | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const pendingKeysRef = useRef<Set<string>>(new Set());
  const pendingServerIdsRef = useRef<Set<string>>(new Set());
  const drawerDialogRef = useRef<HTMLElement>(null);
  const discardDialogRef = useRef<HTMLElement>(null);
  const deleteDialogRef = useRef<HTMLElement>(null);
  const drawerReturnFocusRef = useRef<HTMLElement | null>(null);
  const deleteReturnFocusRef = useRef<HTMLElement | null>(null);
  const loadRequestRef = useRef(0);

  const initialForm = useMemo(
    () => (editingServer ? formFromServer(editingServer) : emptyForm()),
    [editingServer],
  );
  const formDirty = drawerOpen && JSON.stringify(form) !== JSON.stringify(initialForm);
  const saving = pendingKeys.has(`save-${editingServer?.id ?? 'new'}`);
  const credentialContextChanged = Boolean(
    editingServer
    && (form.authType !== editingServer.auth_type
      || normalizedOrigin(form.url) !== normalizedOrigin(editingServer.url)),
  );
  const credentialCanBeRetained = Boolean(
    editingServer?.credential_set && !credentialContextChanged,
  );

  const loadServers = useCallback(async () => {
    const requestId = ++loadRequestRef.current;
    setLoading(true);
    setError('');
    try {
      const nextServers = await getAdminMcpServers();
      if (requestId === loadRequestRef.current) setServers(nextServers);
    } catch (loadError) {
      if (requestId === loadRequestRef.current) setError(errorText(loadError));
    } finally {
      if (requestId === loadRequestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadServers();
  }, [loadServers, refreshToken]);

  useEffect(() => {
    onDirtyChange?.(formDirty);
  }, [formDirty, onDirtyChange]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  useEffect(() => {
    if (confirmDiscard) discardDialogRef.current?.focus();
    else if (drawerOpen) drawerDialogRef.current?.focus();
  }, [confirmDiscard, drawerOpen]);

  useEffect(() => {
    if (deleteTarget) deleteDialogRef.current?.focus();
  }, [deleteTarget]);

  const beginOperation = (key: string, serverId: string): boolean => {
    if (pendingKeysRef.current.has(key) || pendingServerIdsRef.current.has(serverId)) return false;
    pendingKeysRef.current.add(key);
    pendingServerIdsRef.current.add(serverId);
    setPendingKeys(new Set(pendingKeysRef.current));
    setPendingServerIds(new Set(pendingServerIdsRef.current));
    return true;
  };

  const endOperation = (key: string, serverId: string) => {
    pendingKeysRef.current.delete(key);
    pendingServerIdsRef.current.delete(serverId);
    setPendingKeys(new Set(pendingKeysRef.current));
    setPendingServerIds(new Set(pendingServerIdsRef.current));
  };

  const publishedCount = servers.filter((server) => server.status === 'published').length;
  const connectedCount = servers.filter((server) => server.last_tested_at && !server.last_error).length;
  const toolCount = servers.reduce((sum, server) => sum + (server.tools_count || 0), 0);

  const visibleServers = useMemo(() => {
    const query = search.trim().toLowerCase();
    return servers.filter((server) => {
      if (statusFilter !== 'all' && server.status !== statusFilter) return false;
      if (!query) return true;
      return `${server.name} ${server.description} ${server.url}`.toLowerCase().includes(query);
    });
  }, [search, servers, statusFilter]);

  const openCreate = () => {
    drawerReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setEditingServer(null);
    setForm(emptyForm());
    setFormError('');
    setDrawerOpen(true);
  };

  const openEdit = (server: McpServer) => {
    if (pendingServerIds.has(server.id)) return;
    drawerReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setEditingServer(server);
    setForm(formFromServer(server));
    setFormError('');
    setDrawerOpen(true);
  };

  const resetDrawer = () => {
    setDrawerOpen(false);
    setEditingServer(null);
    setForm(emptyForm());
    setFormError('');
    drawerReturnFocusRef.current?.focus();
  };

  const closeDrawer = () => {
    if (pendingKeysRef.current.has(`save-${editingServer?.id ?? 'new'}`)) return;
    if (formDirty) {
      setConfirmDiscard(true);
      return;
    }
    resetDrawer();
  };

  const updateForm = <K extends keyof AdminMcpForm>(key: K, value: AdminMcpForm[K]) => {
    setForm((previous) => ({
      ...previous,
      [key]: value,
      ...((key === 'url' || key === 'authType') ? { clearCredential: false } : {}),
    }));
  };

  const saveServer = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = form.name.trim();
    const description = form.description.trim();
    const url = form.url.trim();
    if (!name) {
      setFormError('请输入服务名称');
      return;
    }
    if (!editingServer && form.required && form.status === 'published') {
      setFormError('平台必需 MCP 请先保存为草稿，测试连接成功后再发布');
      return;
    }

    try {
      const parsedUrl = new URL(url);
      if (!['http:', 'https:'].includes(parsedUrl.protocol)) throw new Error();
      if (parsedUrl.protocol === 'http:' && !form.allowInsecureHttp) {
        setFormError('HTTP 地址必须同时启用“允许不安全 HTTP”');
        return;
      }
    } catch {
      setFormError('请输入有效的 HTTP 或 HTTPS 地址');
      return;
    }

    let headers: Record<string, string> | undefined;
    try {
      headers = form.authType === 'headers' ? parseHeaders(form.headersText) : undefined;
    } catch (parseError) {
      setFormError(errorText(parseError));
      return;
    }

    const credentialMustBeReentered = Boolean(
      editingServer?.credential_set && credentialContextChanged && form.authType !== 'none',
    );
    if (credentialMustBeReentered && form.authType === 'bearer' && !form.bearerToken.trim()) {
      setFormError('连接地址或认证方式已变化，请重新输入 Bearer Token');
      return;
    }
    if (
      credentialMustBeReentered
      && form.authType === 'headers'
      && (!headers || Object.keys(headers).length === 0)
    ) {
      setFormError('连接地址或认证方式已变化，请重新输入请求头凭证');
      return;
    }

    const payload: AdminMcpServerPayload = {
      name,
      description: description || null,
      url,
      status: form.status,
      auth_type: form.authType,
      allow_private_network: form.allowPrivateNetwork,
      allow_insecure_http: form.allowInsecureHttp,
      required: form.required,
      ...(form.authType === 'bearer' && form.bearerToken ? { bearer_token: form.bearerToken } : {}),
      ...(headers ? { headers } : {}),
      ...(form.clearCredential ? { clear_credential: true } : {}),
    };

    const operationServerId = editingServer?.id ?? '__new__';
    const operationKey = `save-${editingServer?.id ?? 'new'}`;
    if (!beginOperation(operationKey, operationServerId)) return;
    setFormError('');
    setMessage('');
    try {
      if (editingServer) {
        await updateAdminMcpServer(editingServer.id, payload);
      } else {
        await createAdminMcpServer(payload);
      }
      resetDrawer();
      setMessage(editingServer ? '官方 MCP 已更新' : '官方 MCP 已创建');
      await loadServers();
    } catch (saveError) {
      setFormError(errorText(saveError));
    } finally {
      endOperation(operationKey, operationServerId);
    }
  };

  const runServerAction = async (
    key: string,
    serverId: string,
    action: () => Promise<void>,
    success?: string,
  ) => {
    if (!beginOperation(key, serverId)) return;
    setError('');
    setMessage('');
    try {
      await action();
      if (success) setMessage(success);
    } catch (actionError) {
      setError(errorText(actionError));
    } finally {
      endOperation(key, serverId);
    }
  };

  const handleTest = (server: McpServer) => {
    void runServerAction(`test-${server.id}`, server.id, async () => {
      const result = await testAdminMcpServer(server.id);
      // The probe persists last_tested_at/last_error and may auto-disable a
      // failing required+published server, so refresh regardless of ok before
      // surfacing the error, otherwise the UI keeps stale published state.
      await loadServers();
      if (!result.ok) throw new Error(result.error || '连接测试失败');
      setMessage(testMessage(result));
    });
  };

  const changeStatus = (server: McpServer, status: McpServerStatus) => {
    void runServerAction(`status-${server.id}`, server.id, async () => {
      await updateAdminMcpServer(server.id, { status });
      await loadServers();
    }, status === 'published' ? '官方 MCP 已发布' : '官方 MCP 已停用');
  };

  const handleDelete = () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    void runServerAction(`delete-${target.id}`, target.id, async () => {
      await deleteAdminMcpServer(target.id);
      setDeleteTarget(null);
      deleteReturnFocusRef.current?.focus();
      await loadServers();
    }, '官方 MCP 已删除');
  };

  const openDeleteConfirmation = (server: McpServer) => {
    if (pendingServerIds.has(server.id)) return;
    deleteReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setDeleteTarget(server);
  };

  const handleDrawerKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeDrawer();
      return;
    }
    trapDialogFocus(event, drawerDialogRef.current);
  };

  const handleConfirmKeyDown = (
    event: ReactKeyboardEvent<HTMLElement>,
    dialog: HTMLElement | null,
    cancel: () => void,
  ) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      cancel();
      return;
    }
    trapDialogFocus(event, dialog);
  };

  if (loading && !servers.length) {
    return <div className="admin-card admin-empty-card"><Loader2 className="admin-mcp-spin" size={18} />正在加载官方 MCP...</div>;
  }

  return (
    <div className="admin-mcp-page">
      {error ? <div className="admin-error admin-inline-message" role="alert">{error}</div> : null}
      {message ? <div className="admin-toast" role="status">{message}</div> : null}

      <section className="admin-mcp-hero">
        <div className="admin-mcp-hero-copy">
          <span className="admin-mcp-eyebrow"><Cable size={13} />STREAMABLE HTTP CATALOG</span>
          <h2>官方 MCP 目录</h2>
          <p>在平台统一维护连接定义和安全边界。用户只会看到已发布的服务，密钥写入后不再回显。</p>
        </div>
        <div className="admin-mcp-metrics" aria-label="官方 MCP 概览">
          <div><strong>{servers.length}</strong><span>服务</span></div>
          <div><strong>{publishedCount}</strong><span>已发布</span></div>
          <div><strong>{connectedCount}</strong><span>测试通过</span></div>
          <div><strong>{toolCount}</strong><span>发现工具</span></div>
        </div>
      </section>

      <section className="admin-mcp-catalog">
        <div className="admin-mcp-catalog-head">
          <div>
            <h3><Cloud size={16} />服务目录</h3>
            <p>发布状态决定用户是否可见；停用会立即阻止新调用。</p>
          </div>
          <button type="button" className="admin-button admin-primary-button" disabled={pendingServerIds.has('__new__')} onClick={openCreate}>
            <Plus size={14} />新增官方 MCP
          </button>
        </div>

        <div className="admin-mcp-filters">
          <label><Search size={15} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索名称、说明或 URL" /></label>
          <select aria-label="按发布状态筛选" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as 'all' | McpServerStatus)}>
            <option value="all">全部状态</option>
            <option value="published">已发布</option>
            <option value="draft">草稿</option>
            <option value="disabled">已停用</option>
          </select>
        </div>

        {visibleServers.length ? (
          <div className="admin-mcp-table-wrap">
            <table className="admin-mcp-table">
              <thead><tr><th>服务</th><th>状态</th><th>认证</th><th>安全边界</th><th>最近测试</th><th>操作</th></tr></thead>
              <tbody>
                {visibleServers.map((server) => {
                  const serverBusy = pendingServerIds.has(server.id);
                  return (
                  <tr key={server.id}>
                    <td>
                      <strong>{server.name}</strong>
                      <span>{server.description || '暂无说明'}</span>
                      <code title={server.url}>{server.url}</code>
                    </td>
                    <td>
                      <span className={`admin-mcp-status ${server.status}`}>{statusText(server.status)}</span>
                      {server.required ? <small>平台必需</small> : null}
                    </td>
                    <td>
                      <span className="admin-mcp-auth"><KeyRound size={12} />{server.auth_type === 'none' ? '无' : server.auth_type === 'bearer' ? 'Bearer' : 'Headers'}</span>
                      {server.credential_set ? <small>凭证已设置</small> : null}
                    </td>
                    <td>
                      <div className="admin-mcp-boundaries">
                        {server.allow_private_network ? <span><ShieldAlert size={11} />允许私网</span> : <span><Globe2 size={11} />仅公网</span>}
                        {server.allow_insecure_http ? <span className="warning">允许 HTTP</span> : <span>HTTPS</span>}
                      </div>
                    </td>
                    <td>
                      <span className={server.last_error ? 'admin-mcp-test failed' : 'admin-mcp-test'}>
                        <Activity size={12} />
                        {server.last_tested_at ? (server.last_error ? '失败' : `${server.tools_count ?? 0} 个工具`) : '未测试'}
                      </span>
                    </td>
                    <td>
                      <div className="admin-mcp-row-actions">
                        <button type="button" className="admin-button admin-icon-button" aria-label={`测试 ${server.name}`} disabled={serverBusy} onClick={() => handleTest(server)}>
                          {pendingKeys.has(`test-${server.id}`) ? <Loader2 className="admin-mcp-spin" size={13} /> : <Zap size={13} />}测试
                        </button>
                        <button type="button" className="admin-button admin-icon-button" aria-label={`编辑 ${server.name}`} disabled={serverBusy} onClick={() => openEdit(server)}><Edit3 size={13} />编辑</button>
                        {server.status === 'published' ? (
                          <button type="button" className="admin-button admin-icon-button" disabled={serverBusy} onClick={() => changeStatus(server, 'disabled')}>停用</button>
                        ) : (
                          <button type="button" className="admin-button admin-icon-button" disabled={serverBusy} onClick={() => changeStatus(server, 'published')}>发布</button>
                        )}
                        <button type="button" className="admin-button admin-icon-button admin-danger-button" aria-label={`删除 ${server.name}`} disabled={serverBusy} onClick={() => openDeleteConfirmation(server)}><Trash2 size={13} /></button>
                      </div>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="admin-mcp-empty"><Cloud size={28} /><strong>没有匹配的官方 MCP</strong><span>调整筛选条件，或创建一个新的服务定义。</span></div>
        )}
      </section>

      {drawerOpen ? (
        <div className="admin-modal-backdrop admin-mcp-drawer-backdrop" role="presentation" onMouseDown={closeDrawer}>
          <aside
            ref={drawerDialogRef}
            className="admin-mcp-drawer"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-mcp-drawer-title"
            aria-busy={saving}
            tabIndex={-1}
            onMouseDown={(event) => event.stopPropagation()}
            onKeyDown={handleDrawerKeyDown}
          >
            <div className="admin-mcp-drawer-head">
              <div><span>OFFICIAL MCP</span><h3 id="admin-mcp-drawer-title">{editingServer ? '编辑服务定义' : '新增服务定义'}</h3></div>
              <button type="button" className="admin-button admin-icon-button admin-icon-only-button" aria-label="关闭官方 MCP 编辑" disabled={saving} onClick={closeDrawer}><X size={14} /></button>
            </div>
            <form onSubmit={(event) => void saveServer(event)}>
              <label>服务名称<input value={form.name} onChange={(event) => updateForm('name', event.target.value)} placeholder="例如：企业知识库" /></label>
              <label>说明<textarea value={form.description} onChange={(event) => updateForm('description', event.target.value)} placeholder="告诉用户这个连接能做什么" /></label>
              <label>Streamable HTTP URL<input value={form.url} onChange={(event) => updateForm('url', event.target.value)} placeholder="https://mcp.example.com/mcp" /></label>
              <div className="admin-mcp-form-grid">
                <label>发布状态<select value={form.status} onChange={(event) => updateForm('status', event.target.value as McpServerStatus)}><option value="draft">草稿</option><option value="published">已发布</option><option value="disabled">停用</option></select></label>
                <label>认证方式<select value={form.authType} onChange={(event) => updateForm('authType', event.target.value as McpAuthMode)}><option value="none">无需认证</option><option value="bearer">Bearer Token</option><option value="headers">自定义请求头</option></select></label>
              </div>
              {credentialContextChanged && editingServer?.credential_set ? <div className="admin-mcp-credential-warning" role="status"><AlertCircle size={14} />连接地址或认证方式已变化，旧凭证将被清除；如仍需认证，请重新输入凭证。</div> : null}
              {form.authType === 'bearer' ? <label>Bearer Token<input type="password" value={form.bearerToken} onChange={(event) => updateForm('bearerToken', event.target.value)} placeholder={credentialCanBeRetained ? '留空保留已保存 Token' : '输入 Token'} autoComplete="new-password" /></label> : null}
              {form.authType === 'headers' ? <label>请求头 JSON<textarea className="code" value={form.headersText} onChange={(event) => updateForm('headersText', event.target.value)} placeholder={'{\n  "X-API-Key": "secret"\n}'} />{editingServer?.header_names.length ? <small>{credentialCanBeRetained ? `已保存：${editingServer.header_names.join('、')}；留空不会覆盖。` : `原已保存：${editingServer.header_names.join('、')}；配置变化后需重新输入。`}</small> : null}</label> : null}
              {editingServer?.credential_set && !credentialContextChanged ? <label className="admin-mcp-check"><input type="checkbox" checked={form.clearCredential} onChange={(event) => updateForm('clearCredential', event.target.checked)} />清除已保存凭证</label> : null}
              <div className="admin-mcp-risk-box">
                <strong><ShieldAlert size={14} />网络边界</strong>
                <label className="admin-mcp-check"><input type="checkbox" checked={form.required} onChange={(event) => updateForm('required', event.target.checked)} />平台必需（用户不可停用）</label>
                <label className="admin-mcp-check"><input type="checkbox" checked={form.allowPrivateNetwork} onChange={(event) => updateForm('allowPrivateNetwork', event.target.checked)} />允许访问私网地址</label>
                <label className="admin-mcp-check"><input type="checkbox" checked={form.allowInsecureHttp} onChange={(event) => updateForm('allowInsecureHttp', event.target.checked)} />允许不安全 HTTP</label>
                <p>{form.required
                  ? '平台必需 MCP 必须使用当前配置测试成功后才能发布；修改 URL、认证、凭证或网络边界后需重新测试。'
                  : '网络边界选项会扩大服务端出站访问范围，仅为受信任的内部服务启用。'}</p>
              </div>
              {formError ? <div className="admin-error admin-inline-message" role="alert"><AlertCircle size={14} />{formError}</div> : null}
              <div className="admin-mcp-drawer-foot"><button type="button" className="admin-button" disabled={saving} onClick={closeDrawer}>取消</button><button type="submit" className="admin-button admin-primary-button" disabled={saving}>{saving ? <Loader2 className="admin-mcp-spin" size={14} /> : <CheckCircle2 size={14} />}保存服务</button></div>
            </form>
          </aside>
        </div>
      ) : null}

      {deleteTarget ? (
        <div className="admin-modal-backdrop" role="presentation">
          <section
            ref={deleteDialogRef}
            className="admin-modal admin-mcp-delete-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="admin-mcp-delete-title"
            aria-busy={pendingKeys.has(`delete-${deleteTarget.id}`)}
            tabIndex={-1}
            onKeyDown={(event) => handleConfirmKeyDown(event, deleteDialogRef.current, () => {
              if (pendingKeysRef.current.has(`delete-${deleteTarget.id}`)) return;
              setDeleteTarget(null);
              deleteReturnFocusRef.current?.focus();
            })}
          >
            <h3 id="admin-mcp-delete-title">删除“{deleteTarget.name}”？</h3>
            <p>删除会移除所有用户对此官方服务的连接实例和已保存凭证，此操作不可恢复。</p>
            <div><button type="button" className="admin-button" disabled={pendingKeys.has(`delete-${deleteTarget.id}`)} onClick={() => { setDeleteTarget(null); deleteReturnFocusRef.current?.focus(); }}>取消</button><button type="button" className="admin-button admin-danger-button" disabled={pendingKeys.has(`delete-${deleteTarget.id}`)} onClick={handleDelete}>确认删除</button></div>
          </section>
        </div>
      ) : null}

      {confirmDiscard ? (
        <div className="admin-modal-backdrop admin-mcp-confirm-backdrop" role="presentation">
          <section
            ref={discardDialogRef}
            className="admin-modal admin-mcp-delete-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="admin-mcp-discard-title"
            tabIndex={-1}
            onKeyDown={(event) => handleConfirmKeyDown(event, discardDialogRef.current, () => {
              setConfirmDiscard(false);
              drawerDialogRef.current?.focus();
            })}
          >
            <h3 id="admin-mcp-discard-title">放弃未保存的官方 MCP 修改？</h3>
            <p>服务地址、认证或网络边界等修改尚未保存。</p>
            <div>
              <button type="button" className="admin-button" onClick={() => { setConfirmDiscard(false); drawerDialogRef.current?.focus(); }}>继续编辑</button>
              <button type="button" className="admin-button admin-danger-button" onClick={() => { setConfirmDiscard(false); resetDrawer(); }}>放弃修改</button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
