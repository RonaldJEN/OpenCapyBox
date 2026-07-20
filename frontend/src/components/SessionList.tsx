import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiService } from '../services/api';
import type { Session } from '../types';
import { ChevronDown, MessageSquare, Trash2, LogOut, Loader2, PenSquare, Settings, Clock, Search, X } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { zhCN } from 'date-fns/locale/zh-CN';
import { ConfirmDialog } from './ConfirmDialog';

interface SessionListProps {
  currentSessionId?: string;
  onSessionSelect: (sessionId: string, target?: { roundId: string }) => void;
  refreshTrigger?: number;
  executingSessionIds?: Set<string>;
  isCollapsed?: boolean;
  onModelChange?: (modelId: string) => void;
  onNewChat?: () => void;
  cronUnreadCount?: number;
  onOpenConfig?: () => void;
  onOpenCron?: () => void;
}

interface DeleteFocusOrigin {
  sessionId: string;
  adjacentSessionIds: string[];
}

export function SessionList({ currentSessionId, onSessionSelect, refreshTrigger, executingSessionIds, isCollapsed = false, onModelChange, onNewChat, cronUnreadCount = 0, onOpenConfig, onOpenCron }: SessionListProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState('');
  const [searchLoading, setSearchLoading] = useState(false);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const searchRequestSeqRef = useRef(0);
  const debouncedSearchQueryRef = useRef('');
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

  // 合併為單一 useEffect，避免重複 API 請求
  useEffect(() => {
    debouncedSearchQueryRef.current = debouncedSearchQuery;
    loadSessions(debouncedSearchQuery);
  }, [refreshTrigger, currentSessionId, debouncedSearchQuery]);

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

    try {
      const response = normalizedQuery
        ? await apiService.getSessions(normalizedQuery)
        : await apiService.getSessions();
      if (requestSeq !== searchRequestSeqRef.current) return;
      const deletedIds = recentlyDeletedIdsRef.current;
      setSessions(deletedIds.size
        ? response.sessions.filter((session) => !deletedIds.has(session.id))
        : response.sessions);
    } catch (error) {
      if (requestSeq !== searchRequestSeqRef.current) return;
      console.error('Failed to load sessions:', error);
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

  const isSearchActive = debouncedSearchQuery.trim().length > 0;
  const matchSourceLabel: Partial<Record<NonNullable<Session['match_type']>, string>> = {
    user: '我的问题',
    assistant: 'Agent 回复',
  };

  if (loading) {
    return (
      <aside
        className={`hidden md:flex flex-col bg-claude-surface border-r border-claude-border transition-[width,opacity,padding,border-color] duration-300 ease-in-out ${
          isCollapsed ? 'w-0 opacity-0 overflow-hidden' : 'w-[260px] p-4 opacity-100'
        }`}
      >
        <div className="flex-1 flex items-center justify-center">
          <Loader2 className="w-6 h-6 text-claude-muted animate-spin" />
        </div>
      </aside>
    );
  }

  return (
    <aside
      className={`hidden md:flex flex-col bg-claude-surface border-r border-claude-border flex-shrink-0 transition-[width,opacity,padding,border-color] duration-300 ease-in-out whitespace-nowrap overflow-hidden ${
        isCollapsed ? 'w-0 opacity-0 border-r-0' : 'w-[260px] p-4 opacity-100'
      }`}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-2">
        <div className="flex items-center space-x-3">
          <img src="/logo.jpg" alt="OpenCapyBox" className="w-8 h-8 rounded-lg object-cover transition-transform active:scale-95 cursor-pointer" />
          <span className="font-sans font-semibold text-lg tracking-tight text-claude-text">OpenCapyBox</span>
        </div>
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
      </div>

      <div className="px-1 pt-4 pb-3">
        <div className="relative">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-claude-muted pointer-events-none" />
          <input
            ref={searchInputRef}
            aria-label="搜索会话"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索对话"
            className="w-full h-9 rounded-lg border border-claude-border bg-white pl-9 pr-8 text-[13px] text-claude-text placeholder:text-claude-muted focus:outline-none focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/20 transition-colors"
          />
          {searchLoading ? (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 flex h-4 w-4 items-center justify-center">
              <Loader2 size={14} className="text-claude-muted animate-spin" />
            </span>
          ) : searchQuery ? (
            <button
              type="button"
              aria-label="清空搜索"
              title="清空搜索"
              onClick={() => {
                setSearchQuery('');
                searchInputRef.current?.focus();
              }}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-claude-muted hover:text-claude-secondary hover:bg-claude-hover rounded transition-colors"
            >
              <X size={13} />
            </button>
          ) : null}
        </div>
      </div>

      {/* History List */}
      <div className="flex-1 overflow-y-auto space-y-1.5 scrollbar-hide -mx-2 px-2">
        <p className="px-3 pb-2 text-xs font-medium text-claude-muted uppercase tracking-widest">History</p>

        {sessions.length === 0 ? (
          <div className="px-2 py-12 text-center">
            <MessageSquare className="w-8 h-8 mx-auto mb-3 text-claude-border" />
            <p className="text-sm text-claude-muted">
              {isSearchActive ? '没有匹配的对话' : '暂无对话记录'}
            </p>
          </div>
        ) : (
          sessions.map((session) => (
            <div
              key={session.id}
              className={`
                group relative px-3 py-2.5 rounded-lg cursor-pointer transition-[background-color,color,border-color,box-shadow] border border-transparent
                ${currentSessionId === session.id
                  ? 'bg-white text-claude-text shadow-sm border-claude-border'
                  : 'text-claude-secondary hover:bg-claude-hover hover:text-claude-text'
                }
              `}>
              <button
                ref={(element) => {
                  if (element) sessionItemRefs.current.set(session.id, element);
                  else sessionItemRefs.current.delete(session.id);
                }}
                type="button"
                aria-label={`打开会话 ${session.title || session.id.slice(0, 8)}`}
                aria-current={currentSessionId === session.id ? 'page' : undefined}
                onClick={() => selectSession(session)}
                className="absolute inset-0 z-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/30"
              />
              <div className="pointer-events-none relative z-[1] flex items-center justify-between">
                <span className={`text-[13px] font-medium truncate flex-1 font-sans ${
                  currentSessionId === session.id ? 'font-semibold' : ''
                }`}>
                  {session.title || `会话 ${session.id.slice(0, 8)}`}
                </span>

                {/* 执行状态动画 */}
                {executingSessionIds?.has(session.id) && (
                  <div className="flex items-center ml-2">
                    <div className="w-1.5 h-1.5 bg-claude-accent rounded-full animate-dot-pulse" />
                  </div>
                )}

                {/* 删除按钮 - hover 时显示 */}
                <button
                  ref={(element) => {
                    if (element) deleteButtonRefs.current.set(session.id, element);
                    else deleteButtonRefs.current.delete(session.id);
                  }}
                  type="button"
                  aria-label={`删除会话 ${session.title || session.id.slice(0, 8)}`}
                  title={`删除会话 ${session.title || session.id.slice(0, 8)}`}
                  onClick={(event) => requestDeleteSession(session, event)}
                  className="pointer-events-auto relative z-10 ml-1 rounded p-1 text-claude-muted opacity-0 transition-colors hover:bg-red-50 hover:text-claude-error group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
                >
                  <Trash2 size={12} />
                </button>
              </div>

              {/* 时间 - 仅在选中时显示 */}
              {currentSessionId === session.id && (
                <p className="pointer-events-none relative z-[1] text-[10px] text-claude-muted mt-1">
                  {formatDistanceToNow(new Date(session.updated_at), {
                    addSuffix: true,
                    locale: zhCN,
                  })}
                </p>
              )}

              {session.match_type && session.match_type !== 'title' && session.match_excerpt && (
                <p className="pointer-events-none relative z-[1] text-[11px] text-claude-muted mt-1 truncate">
                  {matchSourceLabel[session.match_type] && (
                    <span className="mr-1 text-claude-secondary">
                      {matchSourceLabel[session.match_type]}:
                    </span>
                  )}
                  {session.match_excerpt}
                </p>
              )}
            </div>
          ))
        )}
      </div>

      {/* Footer */}
      <div className="pt-3 border-t border-claude-border">
        <button
          onClick={onOpenCron}
          className="w-full flex items-center gap-3 px-3 py-2 text-sm text-claude-secondary hover:bg-claude-hover rounded-lg transition-colors group"
        >
          <Clock size={16} className="text-claude-muted group-hover:text-claude-secondary" />
          <span className="flex-1 text-left">日程管理</span>
          {cronUnreadCount > 0 && (
            <span className="ml-auto inline-flex min-w-[18px] h-[18px] items-center justify-center px-1 text-[10px] font-bold leading-none text-white bg-red-500 rounded-full">
              {cronUnreadCount > 99 ? '99+' : cronUnreadCount}
            </span>
          )}
        </button>
      </div>

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
