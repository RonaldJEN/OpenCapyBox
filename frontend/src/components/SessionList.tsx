import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiService } from '../services/api';
import type { Session } from '../types';
import { Blocks, Database, ChevronDown, MessageSquare, Trash2, LogOut, Loader2, PenSquare, Plus, Settings, Clock, Search, ShieldCheck, X } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { zhCN } from 'date-fns/locale/zh-CN';
import { ConfirmDialog } from './ConfirmDialog';
import { WorkspaceSidebarContent } from './workspace/WorkspaceSidebarContent';
import type { WorkspaceEntry } from '../types/workspace';
import { discardSessionDrafts } from '../services/sessionDraftOutbox';

interface SessionListProps {
  currentSessionId?: string;
  onSessionSelect: (sessionId: string, target?: { roundId: string }) => void;
  refreshTrigger?: number;
  optimisticSession?: Session | null;
  executingSessionIds?: Set<string>;
  isCollapsed?: boolean;
  onModelChange?: (modelId: string) => void;
  onNewChat?: () => void;
  cronUnreadCount?: number;
  onOpenConfig?: () => void;
  onOpenCron?: () => void;
  activePrimarySurface?: 'chat' | 'schedule' | 'skills' | 'connections';
  onOpenSkills?: () => void;
  onOpenConnections?: () => void;
  sidebarMode?: 'sessions' | 'workspace';
  onSidebarModeChange?: (mode: 'sessions' | 'workspace') => void;
  activeWorkspaceEntryId?: string | null;
  onOpenWorkspaceEntry?: (entry: WorkspaceEntry) => void;
  mobileSheet?: boolean;
  onCloseMobileSheet?: () => void;
}

interface DeleteFocusOrigin {
  sessionId: string;
  adjacentSessionIds: string[];
}

export function SessionList({ currentSessionId, onSessionSelect, refreshTrigger, optimisticSession, executingSessionIds, onModelChange, onNewChat, cronUnreadCount = 0, onOpenConfig, onOpenCron, activePrimarySurface = 'chat', onOpenSkills, onOpenConnections, sidebarMode = 'sessions', onSidebarModeChange, activeWorkspaceEntryId, onOpenWorkspaceEntry, mobileSheet = false, onCloseMobileSheet }: SessionListProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState('');
  const [searchLoading, setSearchLoading] = useState(false);
  const [sessionLoadError, setSessionLoadError] = useState('');
  const [mountedSidebarModes, setMountedSidebarModes] = useState(() => ({
    sessions: sidebarMode === 'sessions',
    workspace: sidebarMode === 'workspace',
  }));
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const searchRequestSeqRef = useRef(0);
  const debouncedSearchQueryRef = useRef('');
  const allSessionsRef = useRef<Session[]>([]);
  const deleteButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const sessionItemRefs = useRef(new Map<string, HTMLButtonElement>());
  const searchInputRef = useRef<HTMLInputElement>(null);
  const newChatButtonRef = useRef<HTMLButtonElement>(null);
  const deleteFocusOriginRef = useRef<DeleteFocusOrigin | null>(null);
  const restoreDeleteFocusRef = useRef<DeleteFocusOrigin | null>(null);
  const recentlyDeletedIdsRef = useRef(new Set<string>());
  const navigate = useNavigate();
  const userId = apiService.getUserId() || 'user';
  const userInitial = userId.trim().charAt(0).toUpperCase() || 'U';

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearchQuery(searchQuery);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    setMountedSidebarModes((current) => current[sidebarMode]
      ? current
      : { ...current, [sidebarMode]: true });
  }, [sidebarMode]);

  // 切换选中项只使用本地列表；仅显式刷新和搜索变化才重新请求列表。
  useEffect(() => {
    debouncedSearchQueryRef.current = debouncedSearchQuery;
    loadSessions(debouncedSearchQuery);
  }, [refreshTrigger, debouncedSearchQuery]);

  // 创建接口已确认成功后立即投影到本地列表，不等待下一次全量刷新。
  useEffect(() => {
    if (!optimisticSession || debouncedSearchQueryRef.current.trim()) return;
    setSessions((currentSessions) => [
      optimisticSession,
      ...currentSessions.filter((session) => session.id !== optimisticSession.id),
    ]);
    setLoading(false);
  }, [optimisticSession]);

  useEffect(() => {
    if (!currentSessionId || !onModelChange) return;
    const currentSession = sessions.find((session) => session.id === currentSessionId);
    if (currentSession?.model_id) {
      onModelChange(currentSession.model_id);
    }
  }, [currentSessionId, onModelChange, sessions]);

  // 30s 自动刷新会话列表
  useEffect(() => {
    const timer = setInterval(() => {
      loadSessions(debouncedSearchQueryRef.current);
    }, 30000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (deleteTarget) return;

    const restoreDeleteFocus = restoreDeleteFocusRef.current;
    if (!restoreDeleteFocus) return;
    restoreDeleteFocusRef.current = null;

    const deleteButton = deleteButtonRefs.current.get(restoreDeleteFocus.sessionId);
    if (deleteButton) {
      deleteButton.focus();
      return;
    }

    const sameSessionItem = sessionItemRefs.current.get(restoreDeleteFocus.sessionId);
    if (sameSessionItem) {
      sameSessionItem.focus();
      return;
    }

    for (const adjacentSessionId of restoreDeleteFocus.adjacentSessionIds) {
      const adjacentSessionItem = sessionItemRefs.current.get(adjacentSessionId);
      if (adjacentSessionItem) {
        adjacentSessionItem.focus();
        return;
      }
    }

    const firstAvailableSessionItem = sessions
      .map((session) => sessionItemRefs.current.get(session.id))
      .find((sessionItem): sessionItem is HTMLButtonElement => Boolean(sessionItem));
    if (firstAvailableSessionItem) {
      firstAvailableSessionItem.focus();
      return;
    }

    newChatButtonRef.current?.focus();
  }, [deleteTarget, sessions]);

  const loadSessions = async (query = debouncedSearchQueryRef.current) => {
    const requestSeq = ++searchRequestSeqRef.current;
    const normalizedQuery = query.trim();
    setSearchLoading(normalizedQuery.length > 0);
    setSessionLoadError('');

    try {
      const response = normalizedQuery
        ? await apiService.getSessions(normalizedQuery)
        : await apiService.getSessions();
      if (requestSeq !== searchRequestSeqRef.current) return;
      const deletedIds = recentlyDeletedIdsRef.current;
      const nextSessions = deletedIds.size
        ? response.sessions.filter((session) => !deletedIds.has(session.id))
        : response.sessions;
      if (!normalizedQuery) allSessionsRef.current = nextSessions;
      setSessions(nextSessions);
    } catch (error) {
      if (requestSeq !== searchRequestSeqRef.current) return;
      console.error('Failed to load sessions:', error);
      setSessionLoadError('会话加载失败，请重试。');
    } finally {
      if (requestSeq === searchRequestSeqRef.current) {
        setLoading(false);
        setSearchLoading(false);
      }
    }
  };

  const selectSession = (session: Session) => {
    if (session.match_round_id) {
      onSessionSelect(session.id, { roundId: session.match_round_id });
    } else {
      onSessionSelect(session.id);
    }
    if (session.model_id && onModelChange) {
      onModelChange(session.model_id);
    }
  };

  const requestDeleteSession = (session: Session, event: React.MouseEvent) => {
    event.stopPropagation();
    const sessionIndex = sessions.findIndex((candidate) => candidate.id === session.id);
    const adjacentSessionIds: string[] = [];
    for (let distance = 1; distance < sessions.length; distance += 1) {
      const nextSessionId = sessions[sessionIndex + distance]?.id;
      const previousSessionId = sessions[sessionIndex - distance]?.id;
      if (nextSessionId) adjacentSessionIds.push(nextSessionId);
      if (previousSessionId) adjacentSessionIds.push(previousSessionId);
    }
    deleteFocusOriginRef.current = { sessionId: session.id, adjacentSessionIds };
    setDeleteError(null);
    setDeleteTarget(session);
  };

  const cancelDeleteSession = () => {
    if (!deleteTarget || deletePending) return;
    restoreDeleteFocusRef.current = deleteFocusOriginRef.current ?? {
      sessionId: deleteTarget.id,
      adjacentSessionIds: [],
    };
    deleteFocusOriginRef.current = null;
    setDeleteError(null);
    setDeleteTarget(null);
  };

  const confirmDeleteSession = async () => {
    if (!deleteTarget || deletePending) return;

    const sessionToDelete = deleteTarget;
    const deletingCurrentSession = currentSessionId === sessionToDelete.id;
    const deleteFocusOrigin = deleteFocusOriginRef.current ?? {
      sessionId: sessionToDelete.id,
      adjacentSessionIds: [],
    };

    setDeletePending(true);
    setDeleteError(null);
    try {
      await apiService.deleteSession(sessionToDelete.id);
      await discardSessionDrafts(sessionToDelete.id);
    } catch (error) {
      console.error('Failed to delete session:', error);
      setDeletePending(false);
      setDeleteError('删除失败，请重试。');
      return;
    }

    // 删除成功即视为完成：本地立即移除目标行、关闭弹窗并恢复焦点。列表刷新作为
    // 二级异步步骤进行，绝不因刷新阻塞而让弹窗停留在不可取消的“删除中”状态。
    // recentlyDeletedIds 保证迟到/陈旧的刷新结果不会把已删行重新加回。
    recentlyDeletedIdsRef.current.add(sessionToDelete.id);
    setSessions((currentSessions) => currentSessions.filter((session) => session.id !== sessionToDelete.id));
    deleteFocusOriginRef.current = null;
    restoreDeleteFocusRef.current = deletingCurrentSession ? null : deleteFocusOrigin;
    setDeletePending(false);
    setDeleteTarget(null);
    if (deletingCurrentSession) {
      onSessionSelect('');
    }
    void loadSessions();
  };

  const handleLogout = () => {
    apiService.logout();
    navigate('/login');
  };

  const handleOpenSettings = () => {
    setAccountMenuOpen(false);
    onOpenConfig?.();
  };

  const handleOpenAdmin = () => {
    setAccountMenuOpen(false);
    navigate('/admin');
  };

  const updateSearchQuery = (value: string) => {
    setSearchQuery(value);
    if (value.trim()) {
      setSearchLoading(true);
      return;
    }
    // 清空必须立即恢复最近一次完整列表，并使仍在途的旧搜索响应失效；
    // 后续 effect 仍会发起一次无查询刷新，缓存只负责消除空白等待。
    searchRequestSeqRef.current += 1;
    debouncedSearchQueryRef.current = '';
    setDebouncedSearchQuery('');
    setSearchLoading(false);
    const deletedIds = recentlyDeletedIdsRef.current;
    setSessions(allSessionsRef.current.filter((session) => !deletedIds.has(session.id)));
  };

  const isSearchActive = debouncedSearchQuery.trim().length > 0;
  const matchSourceLabel: Partial<Record<NonNullable<Session['match_type']>, string>> = {
    user: '我的问题',
    assistant: 'Agent 回复',
  };

  return (
    <aside
      className={`${mobileSheet ? 'flex' : 'hidden md:flex'} h-full w-full flex-shrink-0 flex-col overflow-hidden whitespace-nowrap border-r border-claude-border bg-claude-surface p-4`}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-2">
        <div className="flex items-center space-x-3">
          <img src="/logo.jpg" alt="OpenCapyBox" className="w-8 h-8 rounded-lg object-cover transition-transform active:scale-95 cursor-pointer" />
          <span className="font-sans font-semibold text-lg tracking-tight text-claude-text">bsbox</span>
        </div>
        <div className="flex items-center gap-1">
        {onNewChat && (
          <button
            ref={newChatButtonRef}
            type="button"
            onClick={onNewChat}
            aria-label="新建对话"
            title="新建对话"
            className="p-2 text-claude-secondary hover:text-claude-text hover:bg-claude-hover rounded-lg transition-colors duration-200 active:scale-95 cursor-pointer"
          >
            <PenSquare size={18} />
          </button>
        )}
        {mobileSheet && <button type="button" onClick={onCloseMobileSheet} className="inline-flex h-10 w-10 items-center justify-center rounded-lg text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 md:hidden" aria-label="关闭侧栏"><X size={18} /></button>}
        </div>
      </div>

      <div data-testid="sidebar-search-slot" className="px-1 pt-4 pb-3">
        <div data-testid="sidebar-search-control" className="relative h-9">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-claude-muted pointer-events-none" />
          <input
            ref={searchInputRef}
            aria-label="搜索会话"
            value={searchQuery}
            onChange={(event) => updateSearchQuery(event.target.value)}
            placeholder="搜索对话"
            className="h-9 w-full rounded-lg border border-claude-border bg-white pl-9 pr-12 text-[13px] text-claude-text placeholder:text-claude-muted focus:outline-none focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/20 transition-colors"
          />
          {searchLoading && (
            <span className="absolute right-8 top-1/2 -translate-y-1/2 flex h-4 w-4 items-center justify-center" role="status" aria-label="正在搜索会话">
              <Loader2 size={14} className="text-claude-muted animate-spin" />
            </span>
          )}
          {searchQuery && (
            <button
              type="button"
              aria-label="清空搜索"
              title="清空搜索"
              onClick={() => {
                updateSearchQuery('');
                searchInputRef.current?.focus();
              }}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-claude-muted hover:text-claude-secondary hover:bg-claude-hover rounded transition-colors"
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>

      <nav aria-label="主要功能" className="mb-3 border-b border-claude-border px-1 pb-3">
        <button
          type="button"
          onClick={onOpenCron}
          aria-label="日程管理"
          aria-describedby="primary-schedule-hint"
          aria-current={activePrimarySurface === 'schedule' ? 'page' : undefined}
          className={`group flex min-w-0 w-full items-center gap-3 overflow-hidden rounded-lg px-3 py-2 text-sm transition-colors ${
            activePrimarySurface === 'schedule'
              ? 'bg-white font-semibold text-[#9a6a36] shadow-sm'
              : 'text-claude-secondary hover:bg-claude-hover'
          }`}
        >
          <Clock size={16} className={`shrink-0 ${activePrimarySurface === 'schedule' ? 'text-[#9a6a36]' : 'text-claude-muted group-hover:text-claude-secondary'}`} />
          <span className="shrink-0 text-left">日程管理</span>
          <span id="primary-schedule-hint" className="ml-auto min-w-0 truncate text-right text-[11px] font-normal text-claude-muted">安排自动任务</span>
          {cronUnreadCount > 0 && (
            <span className="inline-flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold leading-none text-white">
              {cronUnreadCount > 99 ? '99+' : cronUnreadCount}
            </span>
          )}
        </button>
        <button
          type="button"
          onClick={onOpenSkills}
          aria-label="Skills"
          aria-describedby="primary-skills-hint"
          aria-current={activePrimarySurface === 'skills' ? 'page' : undefined}
          className={`group flex min-w-0 w-full items-center gap-3 overflow-hidden rounded-lg px-3 py-2 text-sm transition-colors ${
            activePrimarySurface === 'skills'
              ? 'bg-white font-semibold text-[#8a5a2f] shadow-sm'
              : 'text-claude-secondary hover:bg-claude-hover'
          }`}
        >
          <Blocks size={16} className={`shrink-0 ${activePrimarySurface === 'skills' ? 'text-[#8a5a2f]' : 'text-claude-muted group-hover:text-claude-secondary'}`} />
          <span className="shrink-0 text-left">Skills</span>
          <span id="primary-skills-hint" className="ml-auto min-w-0 truncate text-right text-[11px] font-normal text-claude-muted">复用优质经验</span>
        </button>
        <button
          type="button"
          onClick={onOpenConnections}
          aria-label="数据"
          aria-describedby="primary-data-hint"
          aria-current={activePrimarySurface === 'connections' ? 'page' : undefined}
          className={`group flex min-w-0 w-full items-center gap-3 overflow-hidden rounded-lg px-3 py-2 text-sm transition-colors ${
            activePrimarySurface === 'connections'
              ? 'bg-white font-semibold text-[#426a59] shadow-sm'
              : 'text-claude-secondary hover:bg-claude-hover'
          }`}
        >
          <Database size={16} className={`shrink-0 ${activePrimarySurface === 'connections' ? 'text-[#426a59]' : 'text-claude-muted group-hover:text-claude-secondary'}`} />
          <span className="shrink-0 text-left">数据</span>
          <span id="primary-data-hint" className="ml-auto min-w-0 truncate text-right text-[11px] font-normal text-claude-muted">连接内外部数据</span>
        </button>
      </nav>

      <div data-testid="sidebar-mode-tabs" role="tablist" aria-label="左侧栏内容" className="relative flex h-11 shrink-0 items-stretch border-b border-claude-border px-1">
        <button
          id="sidebar-sessions-tab"
          type="button"
          role="tab"
          aria-selected={sidebarMode === 'sessions'}
          aria-controls="sidebar-sessions-panel"
          onClick={() => onSidebarModeChange?.('sessions')}
          onKeyDown={(event) => {
            if (event.key === 'ArrowRight') {
              event.preventDefault();
              onSidebarModeChange?.('workspace');
              document.getElementById('sidebar-workspace-tab')?.focus();
            }
          }}
          className={`relative flex h-11 items-center justify-center px-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/45 ${sidebarMode === 'sessions' ? 'text-[15px] font-semibold text-claude-text' : 'text-sm font-medium text-claude-muted hover:text-claude-secondary'}`}
        >
          会话
          {sidebarMode === 'sessions' && <span className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-claude-text" aria-hidden="true" />}
        </button>
        <button
          id="sidebar-workspace-tab"
          type="button"
          role="tab"
          aria-selected={sidebarMode === 'workspace'}
          aria-controls="sidebar-workspace-panel"
          onClick={() => onSidebarModeChange?.('workspace')}
          onKeyDown={(event) => {
            if (event.key === 'ArrowLeft') {
              event.preventDefault();
              onSidebarModeChange?.('sessions');
              document.getElementById('sidebar-sessions-tab')?.focus();
            }
          }}
          className={`relative flex h-11 items-center justify-center px-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/45 ${sidebarMode === 'workspace' ? 'text-[15px] font-semibold text-[#35658c]' : 'text-sm font-medium text-claude-muted hover:text-claude-secondary'}`}
        >
          工作区
          {sidebarMode === 'workspace' && <span className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-[#35658c]" aria-hidden="true" />}
        </button>
        {sidebarMode === 'sessions' && onNewChat && (
          <div data-testid="session-mode-actions" className="absolute inset-y-0 right-1 flex items-center">
            <button
              type="button"
              onClick={onNewChat}
              aria-label="新建对话"
              title="新建对话"
              className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
            >
              <Plus size={17} />
            </button>
          </div>
        )}
      </div>

      {(mountedSidebarModes.sessions || sidebarMode === 'sessions') && <div id="sidebar-sessions-panel" role="tabpanel" aria-labelledby="sidebar-sessions-tab" hidden={sidebarMode !== 'sessions'} className={sidebarMode === 'sessions' ? 'flex min-h-0 flex-1 flex-col pt-2' : 'hidden'}>
        {loading ? (
          <div data-testid="session-loading-state" role="status" aria-label="正在加载会话" className="min-h-0 flex-1 px-1 py-1">
            <span className="sr-only">正在加载会话</span>
            <div className="space-y-1.5" aria-hidden="true">
              {[0, 1, 2].map((index) => (
                <div key={index} className="h-12 rounded-lg border border-claude-border/55 bg-white/45 px-2.5 py-2 motion-safe:animate-pulse">
                  <div className="h-3 w-3/5 rounded bg-claude-border/70" />
                  <div className="mt-2 h-2.5 w-2/5 rounded bg-claude-border/45" />
                </div>
              ))}
            </div>
          </div>
        ) : sessionLoadError ? (
          <div data-testid="session-load-error" role="alert" className="flex min-h-0 flex-1 flex-col items-center justify-center px-4 text-center">
            <p className="whitespace-normal text-[12px] font-medium text-claude-secondary">{sessionLoadError}</p>
            <button
              type="button"
              onClick={() => {
                setLoading(true);
                void loadSessions();
              }}
              className="mt-3 h-8 rounded-lg border border-claude-border bg-white px-3 text-[11px] font-medium text-claude-secondary hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35"
            >
              重试
            </button>
          </div>
        ) : sessions.length === 0 ? (
          <div data-testid="session-empty-state" className="flex min-h-0 flex-1 flex-col items-center justify-center px-4 text-center">
            <MessageSquare className="mb-3 h-7 w-7 text-claude-border" aria-hidden="true" />
            <p className="text-[13px] font-medium text-claude-secondary">
              {isSearchActive ? '没有匹配的对话' : '暂无对话记录'}
            </p>
            <p className="mt-1.5 max-w-[176px] whitespace-normal text-[11px] leading-5 text-claude-muted">
              {isSearchActive ? '换个关键词，或清空搜索查看全部会话' : '新建对话后，会话会保存在这里'}
            </p>
          </div>
        ) : (
          <div data-testid="session-list-scroll" className="-mx-1 min-h-0 flex-1 space-y-1 overflow-y-auto px-1 scrollbar-hide">
          {sessions.map((session) => (
            <div
              key={session.id}
              data-testid={`session-row-${session.id}`}
              className={`
                group relative h-12 cursor-pointer rounded-lg border px-2.5 transition-[background-color,color,border-color,box-shadow]
                ${currentSessionId === session.id
                  ? 'border-claude-border bg-white/90 text-claude-text shadow-[0_1px_2px_rgba(30,26,20,0.05)]'
                  : 'border-transparent text-claude-secondary hover:border-claude-border/70 hover:bg-white/55 hover:text-claude-text'
                }
              `}>
              <button
                ref={(element) => {
                  if (element) sessionItemRefs.current.set(session.id, element);
                  else sessionItemRefs.current.delete(session.id);
                }}
                type="button"
                aria-label={`打开会话 ${session.title || session.id.slice(0, 8)}`}
                aria-current={activePrimarySurface === 'chat' && currentSessionId === session.id ? 'page' : undefined}
                onClick={() => selectSession(session)}
                className="absolute inset-0 z-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/30"
              />
              <div className="pointer-events-none relative z-[1] flex h-full min-w-0 flex-col justify-center pr-6">
                <div className="flex min-w-0 items-center">
                <span className={`min-w-0 flex-1 truncate font-sans text-[13px] font-medium leading-5 ${
                  currentSessionId === session.id ? 'font-semibold' : ''
                }`}>
                  {session.title || `会话 ${session.id.slice(0, 8)}`}
                </span>

                {/* 执行状态动画 */}
                {executingSessionIds?.has(session.id) && (
                  <div className="ml-2 flex shrink-0 items-center">
                    <div className="h-1.5 w-1.5 animate-dot-pulse rounded-full bg-claude-accent" />
                  </div>
                )}
                </div>
                {session.match_type && session.match_type !== 'title' && session.match_excerpt ? (
                  <p className="mt-0.5 flex min-w-0 items-center truncate text-[10.5px] leading-4 text-claude-muted">
                    {matchSourceLabel[session.match_type] && (
                      <span className="mr-1 shrink-0 text-claude-secondary">
                        {matchSourceLabel[session.match_type]}:
                      </span>
                    )}
                    <span className="truncate">{session.match_excerpt}</span>
                  </p>
                ) : (
                  <p className="mt-0.5 truncate text-[10.5px] leading-4 text-claude-muted">
                    {formatDistanceToNow(new Date(session.updated_at), {
                      addSuffix: true,
                      locale: zhCN,
                    })}
                  </p>
                )}
              </div>

                {/* 桌面端悬停显示，移动端始终可见。 */}
                <button
                  ref={(element) => {
                    if (element) deleteButtonRefs.current.set(session.id, element);
                    else deleteButtonRefs.current.delete(session.id);
                  }}
                  type="button"
                  aria-label={`删除会话 ${session.title || session.id.slice(0, 8)}`}
                  title={`删除会话 ${session.title || session.id.slice(0, 8)}`}
                  onClick={(event) => requestDeleteSession(session, event)}
                  className="pointer-events-auto absolute right-1.5 top-1/2 z-10 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-claude-muted opacity-100 transition-[background-color,color,opacity] hover:bg-red-50 hover:text-claude-error focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100"
                >
                  <Trash2 size={12} />
                </button>
            </div>
          ))}
          </div>
        )}
      </div>}
      {(mountedSidebarModes.workspace || sidebarMode === 'workspace') && <div id="sidebar-workspace-panel" role="tabpanel" aria-labelledby="sidebar-workspace-tab" hidden={sidebarMode !== 'workspace'} className={sidebarMode === 'workspace' ? '-mx-3 flex min-h-0 flex-1' : 'hidden'}><WorkspaceSidebarContent activeEntryId={activeWorkspaceEntryId} isActive={sidebarMode === 'workspace'} onOpenEntry={(entry) => onOpenWorkspaceEntry?.(entry)} /></div>}

      <div className="relative mt-2 border-t border-claude-border pt-2">
        <button
          type="button"
          aria-label="账户菜单"
          onClick={() => setAccountMenuOpen((open) => !open)}
          className="w-full flex items-center gap-3 px-2 py-2 rounded-lg text-left text-claude-secondary hover:bg-claude-hover transition-colors"
        >
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-claude-accent text-xs font-bold text-white">
            {userInitial}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px] font-semibold text-claude-text">
              {userId}
            </span>
          </span>
          <ChevronDown size={14} className="shrink-0 text-claude-muted" />
        </button>

        {accountMenuOpen && (
          <>
            <button
              type="button"
              aria-label="关闭账户菜单"
              className="fixed inset-0 z-30 cursor-default bg-transparent"
              onClick={() => setAccountMenuOpen(false)}
            />
            <div className="absolute bottom-14 left-0 right-0 z-40 rounded-xl border border-claude-border bg-white p-1.5 shadow-[0_12px_32px_rgba(30,26,20,0.14)]">
              <button
                type="button"
                onClick={handleOpenSettings}
                className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[13.5px] text-claude-text transition hover:bg-claude-surface"
              >
                <Settings size={16} className="text-claude-muted" />
                <span>设置</span>
              </button>
              {apiService.isAdminUser() && (
                <button
                  type="button"
                  onClick={handleOpenAdmin}
                  className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[13.5px] text-claude-text transition hover:bg-claude-surface"
                >
                  <ShieldCheck size={16} className="text-claude-muted" />
                  <span>管理后台</span>
                </button>
              )}
              <div className="mx-0.5 my-1 h-px bg-claude-border" />
              <button
                type="button"
                onClick={handleLogout}
                className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[13.5px] text-claude-error transition hover:bg-red-50"
              >
                <LogOut size={16} />
                <span>退出登录</span>
              </button>
            </div>
          </>
        )}
      </div>

      {deleteTarget && (
        <ConfirmDialog
          title={`删除会话“${deleteTarget.title || deleteTarget.id.slice(0, 8)}”？`}
          description="删除后无法恢复。"
          confirmLabel={deleteError ? '重试删除' : '确认删除'}
          busyLabel="删除中…"
          busy={deletePending}
          error={deleteError}
          onCancel={cancelDeleteSession}
          onConfirm={confirmDeleteSession}
        />
      )}
    </aside>
  );
}
