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

// ────────────────────────────────────────────
// Sub-component: 产物文件列表（支持 lazy fetch）
// ────────────────────────────────────────────

const RunArtifacts: React.FC<{ run: CronJobRun; onPreview: (runId: string, file: ArtifactFile) => void }> = ({ run, onPreview }) => {
  const [files, setFiles] = useState<ArtifactFile[]>(run.artifacts ?? []);
  const [fetching, setFetching] = useState(false);
  const fetched = useRef(false);

  // 如果 run 自带 artifacts 直接用；否则 lazy fetch
  useEffect(() => {
    if (run.artifacts && run.artifacts.length > 0) {
      setFiles(run.artifacts);
      return;
    }
    if (fetched.current || !run.run_workspace) return;
    fetched.current = true;
    setFetching(true);
    getCronRunFiles(run.id)
      .then((resp) => {
        setFiles((prev) => {
          if (resp.files.length === 0 && prev.length > 0) return prev;
          return resp.files;
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
      modified: '',
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

    // 仅在用户首次展开 success 且未读任务时，标记该条为已读
    if (isExpanded || run.is_read || run.status !== 'success') return;

    setRuns((prev) => prev.map((r) => (r.id === run.id ? { ...r, is_read: true } : r)));
    try {
      await markCronRunsRead(run.id);
      const unreadResp = await getUnreadCount();
      onUnreadChange?.(unreadResp.count);
    } catch (e) {
      console.error('标记已读失败', e);
      setRuns((prev) => prev.map((r) => (r.id === run.id ? { ...r, is_read: false } : r)));
    }
  }, [onUnreadChange]);

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
    <div className="flex-1 overflow-y-auto p-4 space-y-2">
      {runs.map((run) => {
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
                {run.status === 'success' && !run.is_read && (
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
  );
};

export default CronMessageCenter;
