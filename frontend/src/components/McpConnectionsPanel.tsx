import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import {
  Cable,
  CheckCircle2,
  AlertCircle,
  Cloud,
  Download,
  Edit3,
  Loader2,
  LockKeyhole,
  Plus,
  RefreshCw,
  Server,
  Settings2,
  Trash2,
  Upload,
  Wrench,
  X,
  Zap,
} from 'lucide-react';

import {
  activateMcpServer,
  createMcpServer,
  deleteMcpServer,
  exportMcpConfig,
  getMcpServerTools,
  getMcpServers,
  importMcpConfig,
  testMcpServer,
  updateMcpConnection,
  updateMcpServer,
  updateMcpToolVisibility,
  type McpAuthMode,
  type McpServer,
  type McpTestResult,
  type McpToolList,
} from '../services/mcpApi';
import { extractValidationErrorMessage } from '../utils/errorMessages';
import FeedbackMessage from './FeedbackMessage';
import './McpConnectionsPanel.css';

type McpTab = 'official' | 'personal';
type EditorMode = 'personal' | 'connection';

interface McpConnectionsPanelProps {
  active?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  onPermissionsInvalidated?: () => void;
}

interface LoadServersOptions {
  allowDuringMutation?: boolean;
  expectedMutationEpoch?: number;
}

type MessageTone = 'success' | 'warning';

interface EditorValues {
  name: string;
  description: string;
  url: string;
  authType: McpAuthMode;
  bearerToken: string;
  headersText: string;
  enabled: boolean;
  clearCredential: boolean;
}

interface EditorState {
  mode: EditorMode;
  server: McpServer | null;
  values: EditorValues;
  initial: EditorValues;
}

interface ToolManagerState {
  server: McpServer;
  catalog: McpToolList | null;
  enabledTools: string[] | null;
  disabledTools: string[];
  initialEnabledTools: string[] | null;
  initialDisabledTools: string[];
  loading: boolean;
  saving: boolean;
  error: string;
}

function emptyValues(): EditorValues {
  return {
    name: '',
    description: '',
    url: '',
    authType: 'none',
    bearerToken: '',
    headersText: '',
    enabled: true,
    clearCredential: false,
  };
}

function valuesFromServer(server: McpServer): EditorValues {
  return {
    name: server.name,
    description: server.description,
    url: server.url,
    authType: server.auth_type,
    bearerToken: '',
    headersText: '',
    enabled: server.enabled,
    clearCredential: false,
  };
}

function isEditorDirty(editor: EditorState | null): boolean {
  if (!editor) return false;
  return JSON.stringify(editor.values) !== JSON.stringify(editor.initial);
}

function normalizedOrigin(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

function credentialContextChanged(editor: EditorState | null): boolean {
  if (!editor?.server || editor.mode !== 'personal') return false;
  return editor.values.authType !== editor.server.auth_type
    || normalizedOrigin(editor.values.url) !== normalizedOrigin(editor.server.url);
}

function authLabel(authType: McpAuthMode): string {
  if (authType === 'bearer') return 'Bearer Token';
  if (authType === 'headers') return '自定义请求头';
  return '无需认证';
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

function normalizedToolNames(names: string[]): string[] {
  return [...new Set(names)].sort((left, right) => left.localeCompare(right));
}

function upsertServer(items: McpServer[], server: McpServer): McpServer[] {
  return items.some((item) => item.id === server.id)
    ? items.map((item) => item.id === server.id ? server : item)
    : [...items, server];
}

function isToolManagerDirty(manager: ToolManagerState | null): boolean {
  if (!manager || manager.loading) return false;
  const enabledTools = manager.enabledTools === null
    ? null
    : normalizedToolNames(manager.enabledTools);
  const initialEnabledTools = manager.initialEnabledTools === null
    ? null
    : normalizedToolNames(manager.initialEnabledTools);
  return JSON.stringify({ enabledTools, disabledTools: normalizedToolNames(manager.disabledTools) })
    !== JSON.stringify({
      enabledTools: initialEnabledTools,
      disabledTools: normalizedToolNames(manager.initialDisabledTools),
    });
}

function isManagedToolEnabled(manager: ToolManagerState, toolName: string): boolean {
  if (manager.disabledTools.includes(toolName)) return false;
  return manager.enabledTools === null || manager.enabledTools.includes(toolName);
}

function unknownToolRules(manager: ToolManagerState): {
  enabled: string[];
  disabled: string[];
} {
  const discovered = new Set(manager.catalog?.tools.map((tool) => tool.name) ?? []);
  return {
    enabled: manager.enabledTools === null
      ? []
      : manager.enabledTools.filter((name) => !discovered.has(name)),
    disabled: manager.disabledTools.filter((name) => !discovered.has(name)),
  };
}

function parseHeaderJson(text: string): Record<string, string> | undefined {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  const parsed: unknown = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('请求头必须是 JSON 对象');
  }
  const entries = Object.entries(parsed);
  if (!entries.every(([key, value]) => key.trim() && typeof value === 'string')) {
    throw new Error('请求头名称不能为空，值必须是字符串');
  }
  return Object.fromEntries(entries);
}

function errorText(error: unknown, fallback: string): string {
  return extractValidationErrorMessage(error) || fallback;
}

function errorStatus(error: unknown): number | null {
  const status = (error as { response?: { status?: unknown } })?.response?.status;
  return typeof status === 'number' ? status : null;
}

function formatTestResult(result: McpTestResult): string {
  if (!result.ok) return result.error || '连接测试失败';
  const latency = result.latency_ms === null ? '' : `，${result.latency_ms} ms`;
  return `连接成功，发现 ${result.tools_count} 个工具${latency}`;
}

function formatLastTest(server: McpServer): string {
  if (!server.last_tested_at) return '尚未测试';
  const time = new Date(server.last_tested_at);
  return Number.isNaN(time.getTime()) ? '已测试' : `测试于 ${time.toLocaleString('zh-CN', { hour12: false })}`;
}

function downloadJson(value: unknown) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'mcp.json';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export default function McpConnectionsPanel({
  active = true,
  onDirtyChange,
  onPermissionsInvalidated,
}: McpConnectionsPanelProps) {
  const [activeTab, setActiveTab] = useState<McpTab>('official');
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [messageTone, setMessageTone] = useState<MessageTone>('success');
  const [busyKeys, setBusyKeys] = useState<Set<string>>(() => new Set());
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [editorError, setEditorError] = useState('');
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<McpServer | null>(null);
  const [toolManager, setToolManager] = useState<ToolManagerState | null>(null);
  const [confirmToolDiscard, setConfirmToolDiscard] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const editorDialogRef = useRef<HTMLElement>(null);
  const toolDialogRef = useRef<HTMLElement>(null);
  const confirmDiscardDialogRef = useRef<HTMLElement>(null);
  const confirmToolDiscardDialogRef = useRef<HTMLElement>(null);
  const deleteDialogRef = useRef<HTMLElement>(null);
  const editorReturnFocusRef = useRef<HTMLElement | null>(null);
  const toolReturnFocusRef = useRef<HTMLElement | null>(null);
  const deleteReturnFocusRef = useRef<HTMLElement | null>(null);
  const loadRequestRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const mutationsInFlightRef = useRef(0);
  const reconciliationNeededRef = useRef(false);

  const editorDirty = isEditorDirty(editor);
  const toolManagerDirty = isToolManagerDirty(toolManager);
  const dirty = editorDirty || toolManagerDirty;
  const editorOpen = editor !== null;
  const editorSaveKey = editor ? `save-${editor.server?.id || 'new'}` : '';
  const editorSaving = Boolean(editorSaveKey && busyKeys.has(editorSaveKey));
  const editorCredentialContextChanged = credentialContextChanged(editor);
  const editorCredentialCanBeRetained = Boolean(
    editor?.server?.credential_set && !editorCredentialContextChanged,
  );

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  useEffect(() => {
    if (confirmDiscard) confirmDiscardDialogRef.current?.focus();
    else if (editorOpen) editorDialogRef.current?.focus();
  }, [confirmDiscard, editorOpen]);

  useEffect(() => {
    if (confirmToolDiscard) confirmToolDiscardDialogRef.current?.focus();
    else if (toolManager) toolDialogRef.current?.focus();
  }, [confirmToolDiscard, toolManager]);

  useEffect(() => {
    if (deleteTarget) deleteDialogRef.current?.focus();
  }, [deleteTarget]);

  useEffect(() => {
    if (active) return;
    setError('');
    setMessage('');
  }, [active]);

  const loadServers = useCallback(async (options: LoadServersOptions = {}) => {
    if (!options.allowDuringMutation && mutationsInFlightRef.current > 0) return;
    if (
      options.allowDuringMutation
      && reconciliationNeededRef.current
      && mutationsInFlightRef.current > 0
    ) {
      return;
    }
    const expectedMutationEpoch = options.expectedMutationEpoch ?? mutationEpochRef.current;
    if (expectedMutationEpoch !== mutationEpochRef.current) return;
    const requestId = ++loadRequestRef.current;
    setLoading(true);
    setError('');
    try {
      const nextServers = await getMcpServers();
      if (
        requestId === loadRequestRef.current
        && expectedMutationEpoch === mutationEpochRef.current
      ) {
        setServers(nextServers);
      }
    } catch (loadError) {
      if (
        requestId === loadRequestRef.current
        && expectedMutationEpoch === mutationEpochRef.current
      ) {
        setError(errorText(loadError, 'MCP 连接加载失败'));
      }
    } finally {
      if (requestId === loadRequestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadServers();
  }, [loadServers]);

  const beginMutation = useCallback((): number => {
    if (mutationsInFlightRef.current > 0) reconciliationNeededRef.current = true;
    mutationsInFlightRef.current += 1;
    mutationEpochRef.current += 1;
    return mutationEpochRef.current;
  }, []);

  const finishMutation = useCallback(() => {
    mutationsInFlightRef.current = Math.max(0, mutationsInFlightRef.current - 1);
    if (mutationsInFlightRef.current === 0 && reconciliationNeededRef.current) {
      reconciliationNeededRef.current = false;
      void loadServers({
        allowDuringMutation: true,
        expectedMutationEpoch: mutationEpochRef.current,
      });
    }
  }, [loadServers]);

  const officialServers = useMemo(
    () => servers.filter((server) => server.source === 'official'),
    [servers],
  );
  const personalServers = useMemo(
    () => servers.filter((server) => server.source === 'personal'),
    [servers],
  );
  const visibleServers = activeTab === 'official' ? officialServers : personalServers;

  const runAction = useCallback(async (
    key: string,
    action: () => Promise<void>,
    success?: string,
  ) => {
    setBusyKeys((previous) => new Set(previous).add(key));
    setError('');
    setMessage('');
    setMessageTone('success');
    try {
      await action();
      if (success) {
        setMessageTone('success');
        setMessage(success);
      }
    } catch (actionError) {
      setError(errorText(actionError, 'MCP 操作失败'));
    } finally {
      setBusyKeys((previous) => {
        const next = new Set(previous);
        next.delete(key);
        return next;
      });
    }
  }, []);

  const openNewPersonal = () => {
    editorReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const values = emptyValues();
    setEditor({ mode: 'personal', server: null, values, initial: values });
    setEditorError('');
  };

  const openEditPersonal = (server: McpServer) => {
    editorReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const values = valuesFromServer(server);
    setEditor({ mode: 'personal', server, values, initial: values });
    setEditorError('');
  };

  const openConnection = (server: McpServer) => {
    editorReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const values = valuesFromServer(server);
    setEditor({ mode: 'connection', server, values, initial: values });
    setEditorError('');
  };

  const closeEditor = () => {
    if (editorSaving) return;
    if (editorDirty) {
      setConfirmDiscard(true);
      return;
    }
    setEditor(null);
    setEditorError('');
    editorReturnFocusRef.current?.focus();
  };

  const openToolManager = (server: McpServer) => {
    toolReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setToolManager({
      server,
      catalog: null,
      enabledTools: null,
      disabledTools: [],
      initialEnabledTools: null,
      initialDisabledTools: [],
      loading: true,
      saving: false,
      error: '',
    });
    void getMcpServerTools(server.id).then((catalog) => {
      const disabledTools = normalizedToolNames(catalog.disabled_tools);
      const enabledTools = catalog.enabled_tools === null
        ? null
        : normalizedToolNames(catalog.enabled_tools);
      setToolManager((current) => current?.server.id === server.id ? {
        ...current,
        catalog,
        enabledTools,
        disabledTools,
        initialEnabledTools: enabledTools,
        initialDisabledTools: disabledTools,
        loading: false,
      } : current);
    }).catch((loadError) => {
      setToolManager((current) => current?.server.id === server.id ? {
        ...current,
        loading: false,
        error: errorText(loadError, 'MCP 工具列表加载失败'),
      } : current);
    });
  };

  const closeToolManager = () => {
    if (toolManager?.saving) return;
    if (toolManagerDirty) {
      setConfirmToolDiscard(true);
      return;
    }
    setToolManager(null);
    toolReturnFocusRef.current?.focus();
  };

  const toggleToolVisibility = (toolName: string) => {
    setToolManager((current) => {
      if (!current) return current;
      const enabled = isManagedToolEnabled(current, toolName);
      const disabled = new Set(current.disabledTools);
      const allowlist = current.enabledTools === null ? null : new Set(current.enabledTools);
      if (enabled) {
        if (allowlist === null) disabled.add(toolName);
        else allowlist.delete(toolName);
      } else {
        disabled.delete(toolName);
        allowlist?.add(toolName);
      }
      return {
        ...current,
        enabledTools: allowlist === null ? null : normalizedToolNames([...allowlist]),
        disabledTools: normalizedToolNames([...disabled]),
      };
    });
  };

  const setToolPublicationMode = (mode: 'default' | 'allowlist') => {
    setToolManager((current) => {
      if (!current || current.loading || !current.catalog) return current;
      if (mode === 'default') {
        return current.enabledTools === null ? current : { ...current, enabledTools: null };
      }
      if (current.enabledTools !== null) return current;
      const enabledTools = current.catalog.tools
        .filter((tool) => isManagedToolEnabled(current, tool.name))
        .map((tool) => tool.name);
      return { ...current, enabledTools: normalizedToolNames(enabledTools) };
    });
  };

  const removeUnknownToolRule = (kind: 'enabled' | 'disabled', toolName: string) => {
    setToolManager((current) => {
      if (!current) return current;
      if (kind === 'enabled') {
        return current.enabledTools === null ? current : {
          ...current,
          enabledTools: current.enabledTools.filter((name) => name !== toolName),
        };
      }
      return {
        ...current,
        disabledTools: current.disabledTools.filter((name) => name !== toolName),
      };
    });
  };

  const saveToolVisibility = async () => {
    if (!toolManager) return;
    beginMutation();
    const server = toolManager.server;
    const enabledTools = toolManager.enabledTools === null
      ? null
      : normalizedToolNames(toolManager.enabledTools);
    const disabledTools = normalizedToolNames(toolManager.disabledTools);
    setToolManager((current) => current ? { ...current, saving: true, error: '' } : current);
    try {
      const catalog = await updateMcpToolVisibility(server.id, {
        expected_revision: toolManager.catalog?.visibility_revision ?? 0,
        enabled_tools: enabledTools,
        disabled_tools: disabledTools,
      });
      setServers((items) => items.map((item) => item.id === server.id ? {
        ...item,
        tools_count: catalog.tools_count,
        enabled_tools_count: catalog.enabled_tools_count,
        enabled_tools: catalog.enabled_tools,
        disabled_tools: catalog.disabled_tools,
      } : item));
      onPermissionsInvalidated?.();
      setToolManager(null);
      toolReturnFocusRef.current?.focus();
      setMessageTone('success');
      setMessage('工具发布设置已保存');
    } catch (saveError) {
      const saveErrorText = errorText(saveError, '工具发布设置保存失败');
      if (errorStatus(saveError) === 409) {
        try {
          const latest = await getMcpServerTools(server.id);
          const latestEnabledTools = latest.enabled_tools === null
            ? null
            : normalizedToolNames(latest.enabled_tools);
          const latestDisabledTools = normalizedToolNames(latest.disabled_tools);
          setServers((items) => items.map((item) => item.id === server.id ? {
            ...item,
            tools_count: latest.tools_count,
            enabled_tools_count: latest.enabled_tools_count,
            enabled_tools: latest.enabled_tools,
            disabled_tools: latest.disabled_tools,
          } : item));
          setToolManager((current) => current?.server.id === server.id ? {
            ...current,
            catalog: latest,
            enabledTools: latestEnabledTools,
            disabledTools: latestDisabledTools,
            initialEnabledTools: latestEnabledTools,
            initialDisabledTools: latestDisabledTools,
            saving: false,
            error: `${saveErrorText}；已加载最新设置，请重新修改`,
          } : current);
          onPermissionsInvalidated?.();
          return;
        } catch (reloadError) {
          setToolManager((current) => current?.server.id === server.id ? {
            ...current,
            saving: false,
            error: `${saveErrorText}；${errorText(reloadError, '最新设置加载失败')}`,
          } : current);
          return;
        }
      }
      setToolManager((current) => current ? {
        ...current,
        saving: false,
        error: saveErrorText,
      } : current);
    } finally {
      finishMutation();
    }
  };

  const discardEditor = () => {
    if (editorSaving) return;
    setConfirmDiscard(false);
    setEditor(null);
    setEditorError('');
    editorReturnFocusRef.current?.focus();
  };

  const updateEditor = <K extends keyof EditorValues>(key: K, value: EditorValues[K]) => {
    setEditor((current) => current ? {
      ...current,
      values: {
        ...current.values,
        [key]: value,
        ...((key === 'url' || key === 'authType') ? { clearCredential: false } : {}),
      },
    } : current);
  };

  const saveEditor = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editor) return;
    const values = editor.values;
    const name = values.name.trim();
    const description = values.description.trim();
    const url = values.url.trim();
    const authType = editor.mode === 'connection' && editor.server
      ? editor.server.auth_type
      : values.authType;

    if (editor.mode === 'personal' && !name) {
      setEditorError('请输入连接名称');
      return;
    }
    if (editor.mode === 'personal') {
      try {
        const parsedUrl = new URL(url);
        if (!['http:', 'https:'].includes(parsedUrl.protocol)) {
          setEditorError('个人 MCP 仅支持 HTTP 或 HTTPS 地址');
          return;
        }
      } catch {
        setEditorError('请输入有效的 HTTP 或 HTTPS 地址');
        return;
      }
    }

    let headers: Record<string, string> | undefined;
    try {
      headers = authType === 'headers' ? parseHeaderJson(values.headersText) : undefined;
    } catch (parseError) {
      setEditorError(errorText(parseError, '请求头格式不正确'));
      return;
    }

    const credentialMustBeReentered = Boolean(
      editor.server?.credential_set
      && editor.mode === 'personal'
      && editorCredentialContextChanged
      && authType !== 'none',
    );
    if (credentialMustBeReentered && authType === 'bearer' && !values.bearerToken.trim()) {
      setEditorError('连接地址或认证方式已变化，请重新输入 Bearer Token');
      return;
    }
    if (
      credentialMustBeReentered
      && authType === 'headers'
      && (!headers || Object.keys(headers).length === 0)
    ) {
      setEditorError('连接地址或认证方式已变化，请重新输入请求头凭证');
      return;
    }

    setEditorError('');
    const key = `save-${editor.server?.id || 'new'}`;
    const mutationEpoch = beginMutation();
    setBusyKeys((previous) => new Set(previous).add(key));
    let persistedBeforeFailure = false;
    let stagedServer: McpServer | null = null;
    let discoveredToolsCount: number | null = null;
    try {
      const credentialChanged = Boolean(
        values.bearerToken.trim()
        || headers !== undefined
        || values.clearCredential,
      );
      const personalTargetChanged = Boolean(
        editor.mode === 'personal'
        && editor.server
        && (
          url !== editor.server.url
          || authType !== editor.server.auth_type
        ),
      );
      const activationNeeded = Boolean(
        values.enabled
        && (
          !editor.server
          || !editor.server.enabled
          || personalTargetChanged
          || credentialChanged
        ),
      );
      const credentialPayload = {
        auth_type: authType,
        ...(authType === 'bearer' && values.bearerToken ? { bearer_token: values.bearerToken } : {}),
        ...(headers ? { headers } : {}),
        ...(values.clearCredential ? { clear_credential: true } : {}),
      };

      if (editor.mode === 'connection' && editor.server) {
        if (activationNeeded && editor.server.required) {
          // Required connections cannot be staged as disabled. The activation
          // endpoint probes the proposed credential and commits it atomically.
          stagedServer = credentialChanged
            ? await activateMcpServer(editor.server.id, credentialPayload)
            : await activateMcpServer(editor.server.id);
          discoveredToolsCount = stagedServer.tools_count ?? 0;
        } else {
          stagedServer = await updateMcpConnection(editor.server.id, {
            enabled: activationNeeded ? false : values.enabled,
            ...credentialPayload,
          });
          persistedBeforeFailure = activationNeeded;
          if (activationNeeded) {
            stagedServer = await activateMcpServer(editor.server.id);
            discoveredToolsCount = stagedServer.tools_count ?? 0;
          }
        }
      } else if (editor.server) {
        stagedServer = await updateMcpServer(editor.server.id, {
          name,
          description: description || null,
          url,
          auth_type: authType,
          enabled: activationNeeded ? false : values.enabled,
          ...(authType === 'bearer' && values.bearerToken ? { bearer_token: values.bearerToken } : {}),
          ...(headers ? { headers } : {}),
          ...(values.clearCredential ? { clear_credential: true } : {}),
        });
        persistedBeforeFailure = activationNeeded;
        if (activationNeeded) {
          stagedServer = await activateMcpServer(editor.server.id);
          discoveredToolsCount = stagedServer.tools_count ?? 0;
        }
      } else {
        stagedServer = await createMcpServer({
          name,
          description: description || null,
          url,
          auth_type: authType,
          enabled: activationNeeded ? false : values.enabled,
          ...(authType === 'bearer' && values.bearerToken ? { bearer_token: values.bearerToken } : {}),
          ...(headers ? { headers } : {}),
        });
        persistedBeforeFailure = activationNeeded;
        if (activationNeeded) {
          stagedServer = await activateMcpServer(stagedServer.id);
          discoveredToolsCount = stagedServer.tools_count ?? 0;
        }
      }
      if (stagedServer) {
        setServers((items) => upsertServer(items, stagedServer as McpServer));
      }
      onPermissionsInvalidated?.();
      setEditor(null);
      editorReturnFocusRef.current?.focus();
      setMessageTone('success');
      setMessage(discoveredToolsCount === null
        ? (editor.mode === 'connection' ? '连接设置已保存' : '个人 MCP 已保存')
        : `连接已启用，发现 ${discoveredToolsCount} 个工具`);
      await loadServers({ allowDuringMutation: true, expectedMutationEpoch: mutationEpoch });
    } catch (saveError) {
      const failure = errorText(saveError, 'MCP 保存失败');
      if (persistedBeforeFailure && stagedServer) {
        const savedServer = stagedServer as McpServer;
        const savedValues = valuesFromServer(savedServer);
        setServers((items) => upsertServer(items, savedServer));
        setEditor((current) => current ? {
          ...current,
          server: savedServer,
          values: savedValues,
          initial: savedValues,
        } : current);
        setEditorError(`配置已保存但未启用：${failure}`);
        onPermissionsInvalidated?.();
        await loadServers({ allowDuringMutation: true, expectedMutationEpoch: mutationEpoch });
      } else {
        setEditorError(failure);
      }
    } finally {
      finishMutation();
      setBusyKeys((previous) => {
        const next = new Set(previous);
        next.delete(key);
        return next;
      });
    }
  };

  const toggleServer = (server: McpServer) => {
    const nextEnabled = !server.enabled;
    const key = `${nextEnabled ? 'enable' : 'disable'}-${server.id}`;
    const previous = server.enabled;
    const mutationEpoch = beginMutation();
    // Disabling is safe to render optimistically. Enabling stays visibly in a
    // connecting state until the backend atomically probes, snapshots, and
    // commits the enabled installation.
    if (!nextEnabled) {
      setServers((items) => items.map((item) => (
        item.id === server.id ? { ...item, enabled: false } : item
      )));
    }
    void runAction(key, async () => {
      try {
        const updated = nextEnabled
          ? await activateMcpServer(server.id)
          : await updateMcpConnection(server.id, { enabled: false });
        setServers((items) => upsertServer(items, updated));
        onPermissionsInvalidated?.();
        if (nextEnabled) {
          setMessageTone('success');
          setMessage(`连接已启用，发现 ${updated.tools_count ?? 0} 个工具`);
        }
      } catch (toggleError) {
        setServers((items) => items.map((item) => (
          item.id === server.id ? { ...item, enabled: previous } : item
        )));
        if (nextEnabled) {
          await loadServers({ allowDuringMutation: true, expectedMutationEpoch: mutationEpoch });
          onPermissionsInvalidated?.();
        }
        throw toggleError;
      } finally {
        finishMutation();
      }
    });
  };

  const handleTest = (server: McpServer) => {
    const mutationEpoch = beginMutation();
    void runAction(`test-${server.id}`, async () => {
      try {
        const result = await testMcpServer(server.id);
        // The probe persists last_tested_at/last_error server-side, so refresh
        // regardless of ok before surfacing the error to avoid stale UI state.
        await loadServers({ allowDuringMutation: true, expectedMutationEpoch: mutationEpoch });
        onPermissionsInvalidated?.();
        if (!result.ok) throw new Error(result.error || '连接测试失败');
        setMessageTone('success');
        setMessage(formatTestResult(result));
      } finally {
        finishMutation();
      }
    });
  };

  const handleDelete = () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    const mutationEpoch = beginMutation();
    void runAction(`delete-${target.id}`, async () => {
      try {
        await deleteMcpServer(target.id);
        setServers((items) => items.filter((item) => item.id !== target.id));
        setDeleteTarget(null);
        deleteReturnFocusRef.current?.focus();
        onPermissionsInvalidated?.();
        await loadServers({ allowDuringMutation: true, expectedMutationEpoch: mutationEpoch });
      } finally {
        finishMutation();
      }
    }, '个人 MCP 已删除');
  };

  const handleImport = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    const mutationEpoch = beginMutation();
    await runAction('import', async () => {
      try {
        const parsed: unknown = JSON.parse(await file.text());
        if (!parsed || typeof parsed !== 'object' || !('mcpServers' in parsed)) {
          throw new Error('请选择包含 mcpServers 的 mcp.json');
        }
        const result = await importMcpConfig(parsed);
        const savedNames = new Set(result.servers.map((server) => server.name));
        const activationErrors = result.errors.filter((item) => savedNames.has(item.name));
        const rejectedErrors = result.errors.filter((item) => !savedNames.has(item.name));
        const details = result.errors.map((item) => `${item.name}: ${item.error}`).join('；');
        if (result.imported === 0 && result.errors.length) {
          throw new Error(`个人 MCP 导入失败：${details}`);
        }
        if (result.servers.length) {
          setServers((items) => result.servers.reduce(upsertServer, items));
        }
        setMessageTone(result.errors.length ? 'warning' : 'success');
        const warningParts = [
          activationErrors.length
            ? `以下项目已保存但未启用：${activationErrors.map((item) => `${item.name}: ${item.error}`).join('；')}`
            : '',
          rejectedErrors.length
            ? `以下项目导入失败：${rejectedErrors.map((item) => `${item.name}: ${item.error}`).join('；')}`
            : '',
        ].filter(Boolean);
        setMessage(warningParts.length
          ? `已保存 ${result.imported} 个个人 MCP；${warningParts.join('；')}`
          : `已导入 ${result.imported} 个个人 MCP`);
        setActiveTab('personal');
        onPermissionsInvalidated?.();
        await loadServers({ allowDuringMutation: true, expectedMutationEpoch: mutationEpoch });
      } finally {
        finishMutation();
      }
    });
  };

  const handleExport = () => {
    void runAction('export', async () => {
      downloadJson(await exportMcpConfig());
    }, 'mcp.json 已导出（不含凭证）');
  };

  const openDeleteConfirmation = (server: McpServer) => {
    deleteReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setDeleteTarget(server);
  };

  const handleEditorKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeEditor();
      return;
    }
    trapDialogFocus(event, editorDialogRef.current);
  };

  const handleToolManagerKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeToolManager();
      return;
    }
    trapDialogFocus(event, toolDialogRef.current);
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

  return (
    <div className="mcp-user-panel">
      <div className="mcp-user-transport-note">
        <div className="mcp-user-transport-mark" aria-hidden="true">
          <span />
          <Cable size={17} />
          <span />
        </div>
        <div>
          <strong>Streamable HTTP</strong>
          <p>连接由 OpenCapyBox 服务端建立。个人连接默认仅允许公网 HTTPS；管理员白名单可放行受信任的内网或 HTTP 地址。凭证不会回显。</p>
        </div>
      </div>

      <div className="mcp-user-toolbar">
        <div className="mcp-user-tabs" role="tablist" aria-label="MCP 连接来源">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'official'}
            className={activeTab === 'official' ? 'active' : ''}
            onClick={() => setActiveTab('official')}
          >
            官方 MCP <span>{officialServers.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'personal'}
            className={activeTab === 'personal' ? 'active' : ''}
            onClick={() => setActiveTab('personal')}
          >
            个人 MCP <span>{personalServers.length}</span>
          </button>
        </div>

        <div className="mcp-user-actions">
          {activeTab === 'personal' ? (
            <>
              <input
                ref={fileInputRef}
                type="file"
                accept="application/json,.json"
                className="sr-only"
                aria-label="选择 mcp.json"
                onChange={(event) => void handleImport(event)}
              />
              <button type="button" onClick={() => fileInputRef.current?.click()} disabled={busyKeys.has('import')}>
                <Upload size={14} />导入
              </button>
              <button type="button" onClick={handleExport} disabled={busyKeys.has('export')}>
                <Download size={14} />导出
              </button>
              <button type="button" className="primary" onClick={openNewPersonal}>
                <Plus size={14} />添加连接
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => void loadServers()}
              disabled={loading || busyKeys.size > 0 || toolManager?.saving === true}
            >
              <RefreshCw size={14} className={loading ? 'spin' : ''} />刷新
            </button>
          )}
        </div>
      </div>

      {error ? (
        <FeedbackMessage
          className="mcp-user-alert error"
          tone="error"
          icon={<AlertCircle size={15} />}
          onDismiss={() => setError('')}
        >
          {error}
        </FeedbackMessage>
      ) : null}
      {message ? (
        <FeedbackMessage
          className={`mcp-user-alert ${messageTone}`}
          tone={messageTone}
          autoDismissMs={messageTone === 'success' ? 4000 : undefined}
          icon={messageTone === 'success' ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
          onDismiss={() => setMessage('')}
        >
          {message}
        </FeedbackMessage>
      ) : null}

      {loading && servers.length === 0 ? (
        <div className="mcp-user-empty"><Loader2 className="spin" size={20} />正在读取连接...</div>
      ) : visibleServers.length === 0 ? (
        <div className="mcp-user-empty">
          {activeTab === 'official' ? <Cloud size={28} /> : <Server size={28} />}
          <strong>{activeTab === 'official' ? '暂无可用的官方 MCP' : '还没有个人 MCP'}</strong>
          <p>{activeTab === 'official' ? '管理员发布服务后会显示在这里。' : '添加一个 Streamable HTTP 服务，扩展可调用的工具。'}</p>
          {activeTab === 'personal' ? (
            <button type="button" className="primary" onClick={openNewPersonal}><Plus size={14} />添加连接</button>
          ) : null}
        </div>
      ) : (
        <div className="mcp-user-list">
          {visibleServers.map((server) => {
            const enableBusy = busyKeys.has(`enable-${server.id}`);
            const disableBusy = busyKeys.has(`disable-${server.id}`);
            const toggleBusy = enableBusy || disableBusy;
            const testBusy = busyKeys.has(`test-${server.id}`);
            return (
              <article
                className="mcp-user-card"
                key={server.id}
                aria-busy={toggleBusy || testBusy}
              >
                <div className="mcp-user-card-icon" aria-hidden="true">
                  {server.source === 'official' ? <Cloud size={18} /> : <Server size={18} />}
                </div>
                <div className="mcp-user-card-main">
                  <div className="mcp-user-card-title">
                    <strong>{server.name}</strong>
                    <span className={`source ${server.source}`}>{server.source === 'official' ? '官方' : '个人'}</span>
                    {server.required ? <span className="required">平台必需</span> : null}
                    {server.credential_set ? <span className="credential"><LockKeyhole size={11} />已配置凭证</span> : null}
                  </div>
                  <p>{server.description || '暂无说明'}</p>
                  <code title={server.url}>{server.url}</code>
                  <div className="mcp-user-card-meta">
                    <span>{enableBusy
                      ? '正在连接并发现工具...'
                      : disableBusy
                        ? '正在停用...'
                        : !server.enabled
                          ? '未启用'
                          : server.tools_count === null
                            ? '工具数未知'
                            : `${server.enabled_tools_count}/${server.tools_count} 个工具已发布`}</span>
                    <span>{formatLastTest(server)}</span>
                    {server.last_error ? <span className="failed">上次测试失败</span> : null}
                  </div>
                </div>
                <div className="mcp-user-card-controls">
                  <button
                    type="button"
                    className="icon"
                    title="管理工具发布"
                    aria-label={`管理 ${server.name} 的工具`}
                    disabled={toggleBusy || testBusy}
                    onClick={() => openToolManager(server)}
                  >
                    <Wrench size={15} />
                  </button>
                  <button
                    type="button"
                    className="icon"
                    title="测试连接"
                    aria-label={`测试 ${server.name}`}
                    disabled={testBusy || toggleBusy}
                    onClick={() => handleTest(server)}
                  >
                    {testBusy ? <Loader2 size={15} className="spin" /> : <Zap size={15} />}
                  </button>
                  <button
                    type="button"
                    className="icon"
                    title={server.source === 'official' ? '连接设置' : '编辑连接'}
                    aria-label={`${server.source === 'official' ? '配置' : '编辑'} ${server.name}`}
                    disabled={toggleBusy || testBusy}
                    onClick={() => server.source === 'official' ? openConnection(server) : openEditPersonal(server)}
                  >
                    {server.source === 'official' ? <Settings2 size={15} /> : <Edit3 size={15} />}
                  </button>
                  {server.source === 'personal' ? (
                    <button
                      type="button"
                      className="icon danger"
                      title="删除连接"
                      aria-label={`删除 ${server.name}`}
                      disabled={toggleBusy || testBusy}
                      onClick={() => openDeleteConfirmation(server)}
                    >
                      <Trash2 size={15} />
                    </button>
                  ) : null}
                  <button
                    type="button"
                    role="switch"
                    aria-checked={server.enabled}
                    aria-label={`${server.enabled ? '停用' : '启用'} ${server.name}`}
                    className={`mcp-user-switch ${server.enabled ? 'on' : ''}`}
                    disabled={toggleBusy || testBusy || server.required}
                    onClick={() => toggleServer(server)}
                  >
                    <span />
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {toolManager ? (
        <div className="mcp-user-modal-backdrop" role="presentation" onMouseDown={closeToolManager}>
          <section
            ref={toolDialogRef}
            className="mcp-user-modal mcp-user-tool-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="mcp-tool-manager-title"
            aria-busy={toolManager.saving}
            tabIndex={-1}
            onMouseDown={(event) => event.stopPropagation()}
            onKeyDown={handleToolManagerKeyDown}
          >
            <div className="mcp-user-modal-head">
              <div>
                <span>TOOL PUBLICATION</span>
                <h3 id="mcp-tool-manager-title">管理 {toolManager.server.name} 的工具</h3>
              </div>
              <button type="button" className="icon" aria-label="关闭工具管理" disabled={toolManager.saving} onClick={closeToolManager}><X size={16} /></button>
            </div>
            <div className="mcp-user-tool-body">
              <div className="mcp-user-tool-note">
                此处只控制工具是否发布给 Agent。调用时的 <strong>ALLOW / ASK / DENY</strong> 在“权限管控”中单独设置。
              </div>

              {toolManager.error ? <div className="mcp-user-alert error" role="alert"><AlertCircle size={15} />{toolManager.error}</div> : null}
              {!toolManager.loading && toolManager.catalog ? (
                <fieldset className="mcp-user-tool-mode" disabled={toolManager.saving}>
                  <legend>新发现工具的发布方式</legend>
                  <label className={toolManager.enabledTools === null ? 'selected' : ''}>
                    <input
                      type="radio"
                      name="mcp-tool-publication-mode"
                      value="default"
                      checked={toolManager.enabledTools === null}
                      onChange={() => setToolPublicationMode('default')}
                    />
                    <span>
                      <strong>自动发布</strong>
                      <small>默认发布未来发现的工具，可逐项加入停用列表。</small>
                    </span>
                  </label>
                  <label className={toolManager.enabledTools !== null ? 'selected' : ''}>
                    <input
                      type="radio"
                      name="mcp-tool-publication-mode"
                      value="allowlist"
                      checked={toolManager.enabledTools !== null}
                      onChange={() => setToolPublicationMode('allowlist')}
                    />
                    <span>
                      <strong>仅发布允许列表</strong>
                      <small>只发布明确启用的工具，未来新工具默认不发布。</small>
                    </span>
                  </label>
                </fieldset>
              ) : null}
              {toolManager.loading ? (
                <div className="mcp-user-tool-empty"><Loader2 className="spin" size={18} />正在读取已发现工具...</div>
              ) : toolManager.catalog?.tools.length ? (
                <div className="mcp-user-tool-list">
                  {toolManager.catalog.tools.map((tool) => {
                    const enabled = isManagedToolEnabled(toolManager, tool.name);
                    return (
                      <div className="mcp-user-tool-row" key={tool.name}>
                        <div>
                          <strong>{tool.title || tool.name}</strong>
                          {tool.title && tool.title !== tool.name ? <code>{tool.name}</code> : null}
                          <p>{tool.description || '暂无工具说明'}</p>
                        </div>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={enabled}
                          aria-label={`${enabled ? '停用' : '启用'}工具 ${tool.title || tool.name}`}
                          className={`mcp-user-switch ${enabled ? 'on' : ''}`}
                          disabled={toolManager.saving}
                          onClick={() => toggleToolVisibility(tool.name)}
                        >
                          <span />
                        </button>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="mcp-user-tool-empty">
                  <Wrench size={22} />
                  <strong>尚未发现工具</strong>
                  <p>先测试连接，成功发现后即可逐项启停。</p>
                </div>
              )}

              {toolManager.catalog && (() => {
                const unknownRules = unknownToolRules(toolManager);
                if (!unknownRules.enabled.length && !unknownRules.disabled.length) return null;
                return (
                  <section className="mcp-user-tool-unknown" aria-labelledby="mcp-tool-unknown-title">
                    <div>
                      <strong id="mcp-tool-unknown-title">当前快照未发现的规则</strong>
                      <p>这些名称会继续生效；确认不再需要后可移除。</p>
                    </div>
                    <ul>
                      {unknownRules.enabled.map((name) => (
                        <li key={`enabled-${name}`}>
                          <span className="enabled">允许发布</span><code>{name}</code>
                          <button
                            type="button"
                            className="icon"
                            aria-label={`移除未知允许规则 ${name}`}
                            disabled={toolManager.saving}
                            onClick={() => removeUnknownToolRule('enabled', name)}
                          ><Trash2 size={14} /></button>
                        </li>
                      ))}
                      {unknownRules.disabled.map((name) => (
                        <li key={`disabled-${name}`}>
                          <span className="disabled">强制停用</span><code>{name}</code>
                          <button
                            type="button"
                            className="icon"
                            aria-label={`移除未知停用规则 ${name}`}
                            disabled={toolManager.saving}
                            onClick={() => removeUnknownToolRule('disabled', name)}
                          ><Trash2 size={14} /></button>
                        </li>
                      ))}
                    </ul>
                  </section>
                );
              })()}

              <div className="mcp-user-modal-foot">
                <button type="button" onClick={closeToolManager} disabled={toolManager.saving}>取消</button>
                <button
                  type="button"
                  className="primary"
                  disabled={toolManager.loading || toolManager.saving || !toolManagerDirty}
                  onClick={() => void saveToolVisibility()}
                >
                  {toolManager.saving ? <Loader2 size={14} className="spin" /> : null}
                  保存工具设置
                </button>
              </div>
            </div>
          </section>
        </div>
      ) : null}

      {editor ? (
        <div className="mcp-user-modal-backdrop" role="presentation" onMouseDown={closeEditor}>
          <section
            ref={editorDialogRef}
            className="mcp-user-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="mcp-user-editor-title"
            aria-busy={editorSaving}
            tabIndex={-1}
            onMouseDown={(event) => event.stopPropagation()}
            onKeyDown={handleEditorKeyDown}
          >
            <div className="mcp-user-modal-head">
              <div>
                <span>{editor.mode === 'connection' ? 'OFFICIAL CONNECTION' : 'PERSONAL CONNECTION'}</span>
                <h3 id="mcp-user-editor-title">
                  {editor.mode === 'connection' ? `配置 ${editor.server?.name || ''}` : editor.server ? '编辑个人 MCP' : '添加个人 MCP'}
                </h3>
              </div>
              <button type="button" className="icon" aria-label="关闭 MCP 编辑" disabled={editorSaving} onClick={closeEditor}><X size={16} /></button>
            </div>
            <form onSubmit={(event) => void saveEditor(event)}>
              {editor.mode === 'personal' ? (
                <>
                  <label>连接名称<input value={editor.values.name} onChange={(event) => updateEditor('name', event.target.value)} placeholder="例如：内部知识库" /></label>
                  <label>说明<textarea value={editor.values.description} onChange={(event) => updateEditor('description', event.target.value)} placeholder="说明这个服务提供什么能力" /></label>
                  <label>Streamable HTTP URL<input value={editor.values.url} onChange={(event) => updateEditor('url', event.target.value)} placeholder="https://mcp.example.com/mcp 或 http://mcp.internal/mcp" /></label>
                </>
              ) : (
                <div className="mcp-user-readonly-url"><span>服务地址</span><code>{editor.server?.url}</code></div>
              )}

              {editor.mode === 'connection' ? (
                <div className="mcp-user-readonly-url">
                  <span>认证方式</span><strong>{authLabel(editor.server?.auth_type ?? 'none')}</strong>
                </div>
              ) : (
                <label>
                  认证方式
                  <select value={editor.values.authType} onChange={(event) => updateEditor('authType', event.target.value as McpAuthMode)}>
                    <option value="none">无需认证</option>
                    <option value="bearer">Bearer Token</option>
                    <option value="headers">自定义请求头</option>
                  </select>
                </label>
              )}

              {editorCredentialContextChanged && editor.server?.credential_set ? (
                <div className="mcp-user-alert warning" role="status">
                  <AlertCircle size={15} />连接地址或认证方式已变化，旧凭证将被清除；如仍需认证，请重新输入凭证。
                </div>
              ) : null}

              {editor.values.authType === 'bearer' ? (
                <label>
                  Bearer Token
                  <input type="password" value={editor.values.bearerToken} onChange={(event) => updateEditor('bearerToken', event.target.value)} placeholder={editorCredentialCanBeRetained ? '留空保留已保存 Token' : '输入 Token'} autoComplete="new-password" />
                </label>
              ) : null}

              {editor.values.authType === 'headers' ? (
                <label>
                  请求头 JSON
                  <textarea className="code" value={editor.values.headersText} onChange={(event) => updateEditor('headersText', event.target.value)} placeholder={'{\n  "X-API-Key": "secret"\n}'} />
                  {editor.server?.header_names.length ? (
                    <small>{editorCredentialCanBeRetained
                      ? `已保存：${editor.server.header_names.join('、')}；留空不会覆盖。`
                      : `原已保存：${editor.server.header_names.join('、')}；配置变化后需重新输入。`}</small>
                  ) : null}
                </label>
              ) : null}

              {editor.server?.credential_set && !editorCredentialContextChanged ? (
                <label className="mcp-user-checkbox">
                  <input type="checkbox" checked={editor.values.clearCredential} onChange={(event) => updateEditor('clearCredential', event.target.checked)} />
                  清除已保存的凭证
                </label>
              ) : null}

              <label className="mcp-user-checkbox">
                <input
                  type="checkbox"
                  checked={editor.values.enabled}
                  disabled={editor.server?.required === true}
                  onChange={(event) => updateEditor('enabled', event.target.checked)}
                />
                {editor.server?.required ? '平台必需连接始终启用' : '保存后启用此连接'}
              </label>

              {editorError ? <div className="mcp-user-alert error" role="alert"><AlertCircle size={15} />{editorError}</div> : null}
              <div className="mcp-user-modal-foot">
                <button type="button" onClick={closeEditor} disabled={editorSaving}>取消</button>
                <button type="submit" className="primary" disabled={editorSaving}>
                  {editorSaving ? <Loader2 size={14} className="spin" /> : null}
                  保存连接
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}

      {confirmDiscard ? (
        <div className="mcp-user-modal-backdrop top" role="presentation">
          <section
            ref={confirmDiscardDialogRef}
            className="mcp-user-confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="mcp-discard-title"
            tabIndex={-1}
            onKeyDown={(event) => handleConfirmKeyDown(event, confirmDiscardDialogRef.current, () => {
              setConfirmDiscard(false);
              editorDialogRef.current?.focus();
            })}
          >
            <h3 id="mcp-discard-title">放弃未保存的连接修改？</h3>
            <p>当前表单内容尚未保存。</p>
            <div><button type="button" onClick={() => setConfirmDiscard(false)}>继续编辑</button><button type="button" className="danger-fill" onClick={discardEditor}>放弃修改</button></div>
          </section>
        </div>
      ) : null}

      {confirmToolDiscard ? (
        <div className="mcp-user-modal-backdrop top" role="presentation">
          <section
            ref={confirmToolDiscardDialogRef}
            className="mcp-user-confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="mcp-tool-discard-title"
            tabIndex={-1}
            onKeyDown={(event) => handleConfirmKeyDown(event, confirmToolDiscardDialogRef.current, () => {
              setConfirmToolDiscard(false);
              toolDialogRef.current?.focus();
            })}
          >
            <h3 id="mcp-tool-discard-title">放弃未保存的工具设置？</h3>
            <p>工具发布状态尚未保存。</p>
            <div>
              <button type="button" onClick={() => setConfirmToolDiscard(false)}>继续编辑</button>
              <button type="button" className="danger-fill" onClick={() => {
                setConfirmToolDiscard(false);
                setToolManager(null);
                toolReturnFocusRef.current?.focus();
              }}>放弃修改</button>
            </div>
          </section>
        </div>
      ) : null}

      {deleteTarget ? (
        <div className="mcp-user-modal-backdrop top" role="presentation">
          <section
            ref={deleteDialogRef}
            className="mcp-user-confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="mcp-delete-title"
            aria-busy={busyKeys.has(`delete-${deleteTarget.id}`)}
            tabIndex={-1}
            onKeyDown={(event) => handleConfirmKeyDown(event, deleteDialogRef.current, () => {
              if (busyKeys.has(`delete-${deleteTarget.id}`)) return;
              setDeleteTarget(null);
              deleteReturnFocusRef.current?.focus();
            })}
          >
            <h3 id="mcp-delete-title">删除“{deleteTarget.name}”？</h3>
            <p>删除后该连接及其工具将不再提供给 Agent，已保存凭证也会一并移除。</p>
            <div><button type="button" disabled={busyKeys.has(`delete-${deleteTarget.id}`)} onClick={() => { setDeleteTarget(null); deleteReturnFocusRef.current?.focus(); }}>取消</button><button type="button" className="danger-fill" onClick={handleDelete} disabled={busyKeys.has(`delete-${deleteTarget.id}`)}>删除连接</button></div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
