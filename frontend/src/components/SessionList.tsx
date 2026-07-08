import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiService } from '../services/api';
import type { Session } from '../types';
import { MessageSquare, Trash2, LogOut, Loader2, PenSquare, Settings, Zap, Clock, Search, X } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { zhCN } from 'date-fns/locale/zh-CN';

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
  onOpenSkills?: () => void;
  onOpenCron?: () => void;
}

export function SessionList({ currentSessionId, onSessionSelect, refreshTrigger, executingSessionIds, isCollapsed = false, onModelChange, onNewChat, cronUnreadCount = 0, onOpenConfig, onOpenSkills, onOpenCron }: SessionListProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState('');
  const [searchLoading, setSearchLoading] = useState(false);
  const searchRequestSeqRef = useRef(0);
  const debouncedSearchQueryRef = useRef('');
  const navigate = useNavigate();

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
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const loadSessions = async (query = debouncedSearchQueryRef.current) => {
    const requestSeq = ++searchRequestSeqRef.current;
    const normalizedQuery = query.trim();
    setSearchLoading(normalizedQuery.length > 0);

    try {
      const response = normalizedQuery
        ? await apiService.getSessions(normalizedQuery)
        : await apiService.getSessions();
      if (requestSeq !== searchRequestSeqRef.current) return;
      setSessions(response.sessions);
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

  const handleDeleteSession = async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('确定要删除这个会话吗？')) return;

    try {
      await apiService.deleteSession(sessionId);
      await loadSessions();
      if (currentSessionId === sessionId) {
        onSessionSelect('');
      }
    } catch (error) {
      console.error('Failed to delete session:', error);
    }
  };

  const handleLogout = () => {
    apiService.logout();
    navigate('/login');
  };

  const isSearchActive = debouncedSearchQuery.trim().length > 0;
  const matchSourceLabel: Partial<Record<NonNullable<Session['match_type']>, string>> = {
    user: '我的问题',
    assistant: 'Agent 回复',
  };

  if (loading) {
    return (
      <aside
        className={`hidden md:flex flex-col bg-claude-surface border-r border-claude-border transition-all duration-300 ease-in-out ${
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
      className={`hidden md:flex flex-col bg-claude-surface border-r border-claude-border flex-shrink-0 transition-all duration-300 ease-in-out whitespace-nowrap overflow-hidden ${
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
            onClick={onNewChat}
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
              onClick={() => setSearchQuery('')}
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
              onClick={() => {
                if (session.match_round_id) {
                  onSessionSelect(session.id, { roundId: session.match_round_id });
                } else {
                  onSessionSelect(session.id);
                }
                // 切換 session 時同步模型顯示
                if (session.model_id && onModelChange) {
                  onModelChange(session.model_id);
                }
              }}
              className={`
                group relative px-3 py-2.5 rounded-lg cursor-pointer transition-all border border-transparent
                ${currentSessionId === session.id
                  ? 'bg-white text-claude-text shadow-sm border-claude-border'
                  : 'text-claude-secondary hover:bg-claude-hover hover:text-claude-text'
                }
              `}>
              <div className="flex items-center justify-between">
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
                  onClick={(e) => handleDeleteSession(session.id, e)}
                  className="opacity-0 group-hover:opacity-100 ml-1 p-1 text-claude-muted hover:text-claude-error hover:bg-red-50 transition-all rounded"
                >
                  <Trash2 size={12} />
                </button>
              </div>

              {/* 时间 - 仅在选中时显示 */}
              {currentSessionId === session.id && (
                <p className="text-[10px] text-claude-muted mt-1">
                  {formatDistanceToNow(new Date(session.updated_at), {
                    addSuffix: true,
                    locale: zhCN,
                  })}
                </p>
              )}

              {session.match_type && session.match_type !== 'title' && session.match_excerpt && (
                <p className="text-[11px] text-claude-muted mt-1 truncate">
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
      <div className="space-y-1 pt-4 border-t border-claude-border">
        <button
          onClick={onOpenConfig}
          className="w-full flex items-center space-x-3 px-3 py-2 text-sm text-claude-secondary hover:bg-claude-hover rounded-lg transition-all group"
        >
          <Settings size={16} className="text-claude-muted group-hover:text-claude-secondary" />
          <span>Agent 配置</span>
        </button>
        <button
          onClick={onOpenSkills}
          className="w-full flex items-center space-x-3 px-3 py-2 text-sm text-claude-secondary hover:bg-claude-hover rounded-lg transition-all group"
        >
          <Zap size={16} className="text-claude-muted group-hover:text-claude-secondary" />
          <span>Skills 管理</span>
        </button>
        <button
          onClick={onOpenCron}
          className="w-full flex items-center gap-3 px-3 py-2 text-sm text-claude-secondary hover:bg-claude-hover rounded-lg transition-all group"
        >
          <Clock size={16} className="text-claude-muted group-hover:text-claude-secondary" />
          <span className="flex-1 text-left">日程管理</span>
          {cronUnreadCount > 0 && (
            <span className="ml-auto inline-flex min-w-[18px] h-[18px] items-center justify-center px-1 text-[10px] font-bold leading-none text-white bg-red-500 rounded-full">
              {cronUnreadCount > 99 ? '99+' : cronUnreadCount}
            </span>
          )}
        </button>
        <button
          onClick={handleLogout}
          className="w-full flex items-center space-x-3 px-3 py-2.5 text-sm text-claude-secondary hover:bg-claude-hover rounded-lg transition-all group"
        >
          <LogOut size={16} className="text-claude-muted group-hover:text-claude-secondary" />
          <span>退出登录</span>
        </button>
      </div>
    </aside>
  );
}
