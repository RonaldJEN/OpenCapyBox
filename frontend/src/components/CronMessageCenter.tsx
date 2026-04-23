import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Download, FileText, ChevronDown, ChevronUp } from 'lucide-react';
import { FilePreview } from './FilePreview';
import {
  getCronRuns,
  getUnreadCount,
  getCronRunFiles,
  markCronRunsRead,
  downloadCronRunFile,
  type CronJobRun,
  type ArtifactFile,
} from '../services/configApi';
import type { FileInfo } from '../types';

// ────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────

function statusIcon(s: string) {
  switch (s) {
    case 'success': return '✓';
    case 'failed': return '✕';
    case 'running': return '⟳';
    default: return '○';
  }
}

function statusColor(s: string) {
  switch (s) {
    case 'success': return 'text-green-600';
    case 'failed': return 'text-red-500';
    case 'running': return 'text-yellow-500';
    default: return 'text-claude-muted';
  }
}

function statusBg(s: string) {
  switch (s) {
    case 'success': return 'bg-green-50 border-green-200';
    case 'failed': return 'bg-red-50 border-red-200';
    case 'running': return 'bg-yellow-50 border-yellow-200';
    default: return 'bg-claude-surface border-claude-border';
  }
}

function statusLabel(s: string) {
  switch (s) {
    case 'success': return '成功';
    case 'failed': return '失败';
    case 'running': return '运行中';
    default: return s;
  }
}

function formatDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt || !completedAt) return '';
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${minutes}m${secs}s`;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function localDateKey(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/**
 * 把 started_at 转成分组 key（YYYY-MM-DD，本地时区）。
 *
 * 统一 key 口径，避免：
 * - 分组用原始字符串日期
 * - 今天/昨天用 UTC 日期
 * 两者混用导致的跨时区错标。
 */
export function runDateGroupKey(startedAt: string | null): string {
  if (!startedAt) return '未知日期';
  const dt = new Date(startedAt);
  if (!Number.isNaN(dt.getTime())) {
    return localDateKey(dt);
  }
  const m = startedAt.match(/^(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : '未知日期';
}

const RUN_ARTIFACTS_CACHE_LIMIT = 80;
const runArtifactsCache = new Map<string, ArtifactFile[]>();

function readRunArtifactsCache(runId: string): ArtifactFile[] | null {
  const cached = runArtifactsCache.get(runId) || null;
  if (!cached) {
    return null;
  }
  runArtifactsCache.delete(runId);
  runArtifactsCache.set(runId, cached);
  return cached;
}

function writeRunArtifactsCache(runId: string, files: ArtifactFile[]) {
  runArtifactsCache.delete(runId);
  runArtifactsCache.set(runId, files);
  if (runArtifactsCache.size > RUN_ARTIFACTS_CACHE_LIMIT) {
    const oldestKey = runArtifactsCache.keys().next().value;
    if (oldestKey) {
      runArtifactsCache.delete(oldestKey);
    }
  }
}

function initialRunArtifacts(run: CronJobRun): ArtifactFile[] {
  if (run.artifacts && run.artifacts.length > 0) {
    writeRunArtifactsCache(run.id, run.artifacts);
    return run.artifacts;
  }
  return readRunArtifactsCache(run.id) ?? [];
}

// ────────────────────────────────────────────
// Sub-component: 产物文件列表（支持 lazy fetch）
// ────────────────────────────────────────────

const RunArtifacts: React.FC<{ run: CronJobRun; onPreview: (runId: string, file: ArtifactFile) => void }> = ({ run, onPreview }) => {
  const [files, setFiles] = useState<ArtifactFile[]>(() => initialRunArtifacts(run));
  const [fetching, setFetching] = useState(false);
  const fetched = useRef(files.length > 0);

  // 如果 run 自带 artifacts 直接用；否则 lazy fetch
  useEffect(() => {
    if (run.artifacts && run.artifacts.length > 0) {
      setFiles(run.artifacts);
      writeRunArtifactsCache(run.id, run.artifacts);
      fetched.current = true;
      return;
    }

    const cached = readRunArtifactsCache(run.id);
    if (cached && cached.length > 0) {
      setFiles(cached);
      fetched.current = true;
      return;
    }

    if (fetched.current || !run.run_workspace) return;
    fetched.current = true;
    setFetching(true);
    getCronRunFiles(run.id)
      .then((resp) => {
        setFiles((prev) => {
          const next = resp.files.length === 0 && prev.length > 0 ? prev : resp.files;
          if (next.length > 0) {
            writeRunArtifactsCache(run.id, next);
          }
          return next;
        });
      })
      .catch(() => {})
      .finally(() => setFetching(false));
  }, [run.id, run.artifacts, run.run_workspace]);

  if (fetching) {
    return (
      <div className="px-4 py-3 border-t border-claude-border text-xs text-claude-muted">
        正在获取产物文件...
      </div>
    );
  }
  if (files.length === 0) return null;

  return (
    <div className="px-4 py-3 border-t border-claude-border">
      <div className="text-xs font-medium text-claude-secondary mb-2">
        产物文件 ({files.length})
      </div>
      <div className="space-y-1.5">
        {files.map((file: ArtifactFile) => (
          <div
            key={file.path}
            className="flex items-center justify-between px-3 py-2 rounded-lg bg-claude-surface border border-claude-border"
          >
            <div className="flex items-center gap-2 min-w-0">
              <FileText size={14} className="text-claude-muted shrink-0" />
              <button
                type="button"
                onClick={() => onPreview(run.id, file)}
                className="text-sm text-claude-text truncate hover:text-claude-accent hover:underline text-left"
                title="预览"
              >
                {file.name}
              </button>
              <span className="text-xs text-claude-muted shrink-0">
                {formatFileSize(file.size)}
              </span>
            </div>
            <button
              type="button"
              onClick={() => {
                void downloadCronRunFile(run.id, file.path, file.name).catch((e) => {
                  console.error('下载文件失败', e);
                });
              }}
              className="p-1.5 rounded-lg hover:bg-claude-hover text-claude-muted hover:text-claude-accent transition-colors shrink-0"
              title="下载"
            >
              <Download size={14} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};

// ────────────────────────────────────────────
// Component
// ────────────────────────────────────────────

interface Props {
  onUnreadChange?: (count: number) => void;
}

const PAGE_SIZE = 20;

const CronMessageCenter: React.FC<Props> = ({ onUnreadChange }) => {
  const [runs, setRuns] = useState<CronJobRun[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [previewTarget, setPreviewTarget] = useState<{ runId: string; file: ArtifactFile } | null>(null);
  const offsetRef = useRef(0);

  const previewFile = useMemo<FileInfo | null>(() => {
    if (!previewTarget) return null;
    const f = previewTarget.file;
    return {
      name: f.name,
      path: f.path,
      size: f.size,
      modified: previewTarget.runId,
      type: f.type,
      is_directory: false,
    };
  }, [previewTarget]);

  const buildCronPreviewUrl = useCallback((runId: string, filePath: string): string => {
    const encodedPath = filePath
      .split('/')
      .map((seg) => encodeURIComponent(seg))
      .join('/');
    return `/api/cron/runs/${encodeURIComponent(runId)}/files/${encodedPath}?preview=true`;
  }, []);

  // 首次加载（不自动标记已读）
  useEffect(() => {
    const init = async () => {
      setLoading(true);
      try {
        const [resp, unreadResp] = await Promise.all([
          getCronRuns(undefined, PAGE_SIZE, 0),
          getUnreadCount(),
        ]);
        setRuns(resp.runs);
        setTotal(resp.total);
        offsetRef.current = resp.runs.length;
        setUnreadCount(unreadResp.count);
        onUnreadChange?.(unreadResp.count);
      } catch (e) {
        console.error('加载消息中心失败', e);
      } finally {
        setLoading(false);
      }
    };
    init();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleLoadMore = useCallback(async () => {
    setLoadingMore(true);
    try {
      const resp = await getCronRuns(undefined, PAGE_SIZE, offsetRef.current);
      setRuns((prev) => [...prev, ...resp.runs]);
      setTotal(resp.total);
      offsetRef.current += resp.runs.length;
    } catch (e) {
      console.error('加载更多失败', e);
    } finally {
      setLoadingMore(false);
    }
  }, []);

  const handleToggleRun = useCallback(async (run: CronJobRun, isExpanded: boolean) => {
    setExpandedRun(isExpanded ? null : run.id);

    // 仅在首次展开「终态且未读」记录时做单条已读标记。
    // running 记录不自动标记，避免把进行中的失败风险提前“读掉”。
    if (isExpanded || run.is_read || run.status === 'running') return;

    setRuns((prev) => {
      const next = prev.map((r) => (r.id === run.id ? { ...r, is_read: true } : r));
      return next;
    });
    setUnreadCount((prev) => {
      const next = Math.max(0, prev - 1);
      onUnreadChange?.(next);
      return next;
    });

    try {
      await markCronRunsRead(run.id);
      const unreadResp = await getUnreadCount();
      setUnreadCount(unreadResp.count);
      onUnreadChange?.(unreadResp.count);
    } catch (e) {
      console.error('标记单条已读失败', e);
      setRuns((prev) => {
        const next = prev.map((r) => (r.id === run.id ? { ...r, is_read: false } : r));
        return next;
      });
      const unreadResp = await getUnreadCount();
      setUnreadCount(unreadResp.count);
      onUnreadChange?.(unreadResp.count);
    }
  }, [onUnreadChange]);

  const handleMarkAllRead = useCallback(async () => {
    try {
      const resp = await markCronRunsRead();
      const unreadResp = await getUnreadCount();
      // 并发场景下可能出现 marked=0 但服务端未读已归零（例如其他端已先标记），
      // 此时也需要把当前列表的红点同步清掉，避免本地状态残留。
      if (resp.marked > 0 || unreadResp.count === 0) {
        setRuns((prev) => prev.map((r) => (r.is_read ? r : { ...r, is_read: true })));
      }
      setUnreadCount(unreadResp.count);
      onUnreadChange?.(unreadResp.count);
    } catch (e) {
      console.error('全部标已读失败', e);
    }
  }, [onUnreadChange]);

  /** 按日期分组（YYYY-MM-DD），组内：失败优先 → 时间倒序 */
  const groupedRuns = useMemo(() => {
    const groups = new Map<string, CronJobRun[]>();
    for (const r of runs) {
      const key = runDateGroupKey(r.started_at);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(r);
    }
    for (const arr of groups.values()) {
      arr.sort((a, b) => {
        // 仅 failed && unread 置顶；其余全部按时间倒序。
        const aTop = a.status === 'failed' && !a.is_read;
        const bTop = b.status === 'failed' && !b.is_read;
        if (aTop !== bTop) return aTop ? -1 : 1;
        const at = a.started_at ? new Date(a.started_at).getTime() : 0;
        const bt = b.started_at ? new Date(b.started_at).getTime() : 0;
        return bt - at;
      });
    }
    // 日期倒序
    return [...groups.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));
  }, [runs]);

  const formatGroupDate = (key: string): string => {
    if (key === '未知日期') return key;
    const today = localDateKey(new Date());
    const yesterday = localDateKey(new Date(Date.now() - 86400000));
    if (key === today) return '今天';
    if (key === yesterday) return '昨天';
    const parts = key.split('-').map(Number);
    if (parts.length === 3 && parts.every((n) => Number.isInteger(n))) {
      const [, month, day] = parts;
      return `${month}月${day}日`;
    }
    return key;
  };

  const hasMore = runs.length < total;

  if (loading) {
    return <div className="flex items-center justify-center h-32 text-claude-muted">加载中...</div>;
  }

  if (runs.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-claude-muted py-12">
        <p className="mb-2">暂无执行记录</p>
        <p className="text-xs">定时任务执行后，结果会在此展示</p>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Sticky 顶栏：未读计数 + 全部标已读 */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-claude-border bg-claude-bg">
        <span className="text-xs text-claude-secondary">
          共 {total} 条 · {unreadCount > 0 ? <span className="text-red-500">{unreadCount} 条未读</span> : '全部已读'}
        </span>
        <button
          onClick={() => { void handleMarkAllRead(); }}
          disabled={unreadCount === 0}
          className="px-2.5 py-1 text-xs rounded border border-claude-border bg-claude-surface text-claude-text hover:bg-claude-hover disabled:opacity-40 disabled:cursor-not-allowed"
        >
          全部标已读
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
      {groupedRuns.map(([dateKey, dayRuns]) => (
        <div key={dateKey} className="space-y-2">
          <div className="text-xs font-medium text-claude-secondary px-1 py-1">
            {formatGroupDate(dateKey)} · {dayRuns.length} 条
          </div>
          {dayRuns.map((run) => {
        const isExpanded = expandedRun === run.id;
        return (
          <div key={run.id} className="rounded-lg border border-claude-border overflow-hidden">
            {/* Card header */}
            <button
              onClick={() => { void handleToggleRun(run, isExpanded); }}
              className="w-full flex items-center justify-between px-4 py-3 hover:bg-claude-hover/50 transition-colors"
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className={`text-sm font-medium ${statusColor(run.status)}`}>
                  {statusIcon(run.status)}
                </span>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-claude-text truncate">
                      {run.job_name}
                    </span>
                    <span className={`px-1.5 py-0.5 text-[10px] rounded border ${statusBg(run.status)} ${statusColor(run.status)}`}>
                      {statusLabel(run.status)}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 text-xs text-claude-secondary mt-0.5">
                    {run.started_at && (
                      <span>{new Date(run.started_at).toLocaleString('zh-CN')}</span>
                    )}
                    {run.started_at && run.completed_at && (
                      <span className="text-claude-muted">
                        耗时 {formatDuration(run.started_at, run.completed_at)}
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {!run.is_read && (
                  <span className="inline-block w-2 h-2 rounded-full bg-red-500" title="未读" />
                )}
                {run.artifacts && run.artifacts.length > 0 && (
                  <span className="text-xs text-claude-muted flex items-center gap-1">
                    <FileText size={12} /> {run.artifacts.length}
                  </span>
                )}
                {isExpanded ? <ChevronUp size={16} className="text-claude-muted" /> : <ChevronDown size={16} className="text-claude-muted" />}
              </div>
            </button>

            {/* Expanded detail */}
            {isExpanded && (
              <div className="border-t border-claude-border">
                {/* Output (Markdown) */}
                {run.output && (
                  <div className="px-4 py-3">
                    <div className="text-xs font-medium text-claude-secondary mb-2">执行输出</div>
                    <div className="prose prose-sm max-w-none text-claude-text bg-claude-surface/50 rounded-lg p-3 overflow-x-auto [&_pre]:bg-claude-bg [&_pre]:border [&_pre]:border-claude-border [&_pre]:rounded [&_code]:text-xs [&_table]:text-xs">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {run.output}
                      </ReactMarkdown>
                    </div>
                  </div>
                )}

                {/* Artifacts（支持 lazy fetch） */}
                <RunArtifacts
                  run={run}
                  onPreview={(runId, file) => setPreviewTarget({ runId, file })}
                />
              </div>
            )}
          </div>
        );
      })}
        </div>
      ))}

      {/* Load more */}
      {hasMore && (
        <div className="flex justify-center pt-2 pb-4">
          <button
            onClick={handleLoadMore}
            disabled={loadingMore}
            className="px-4 py-2 text-sm rounded-lg bg-claude-surface text-claude-secondary hover:bg-claude-hover border border-claude-border disabled:opacity-50"
          >
            {loadingMore ? '加载中...' : `加载更多（还有 ${total - runs.length} 条）`}
          </button>
        </div>
      )}

      <FilePreview
        file={previewFile}
        sessionId={previewTarget?.runId || ''}
        onClose={() => setPreviewTarget(null)}
        previewUrlBuilder={(file) => {
          if (!previewTarget) return '';
          return buildCronPreviewUrl(previewTarget.runId, file.path);
        }}
        onDownloadFile={async (file) => {
          if (!previewTarget) return;
          await downloadCronRunFile(previewTarget.runId, file.path, file.name);
        }}
      />
      </div>
    </div>
  );
};

export default CronMessageCenter;
