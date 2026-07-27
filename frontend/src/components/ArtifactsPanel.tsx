import { useEffect, useLayoutEffect, useState, useCallback, useRef } from 'react';
import { apiService } from '../services/api';
import { FileInfo } from '../types';
import {
  Folder,
  X,
  Download,
  ChevronLeft,
  ChevronRight,
  ArrowUp,
  Loader2,
  FolderOpen,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { zhCN } from 'date-fns/locale/zh-CN';
import { getFileIcon, getFileIconClass } from '../utils/fileUtils';
import { FilePreview } from './FilePreview';

interface ArtifactsPanelProps {
  sessionId: string;
  isOpen: boolean;
  onClose: () => void;
  targetFile?: FileInfo | null;
  targetFileNonce?: number;
  variant?: 'drawer' | 'workspace';
}

export function ArtifactsPanel({ sessionId, isOpen, onClose, targetFile, targetFileNonce, variant = 'drawer' }: ArtifactsPanelProps) {
  const [isMounted, setIsMounted] = useState(isOpen);
  const [items, setItems] = useState<FileInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [currentPath, setCurrentPath] = useState('');
  const [pathHistory, setPathHistory] = useState<string[]>(['']);
  const [historyIndex, setHistoryIndex] = useState(0);
  const [selectedFile, setSelectedFile] = useState<FileInfo | null>(null);
  // 每次目录请求使用独立序号；session/path 任一变化都会使旧响应失效。
  const directoryRequestSeqRef = useRef(0);
  const listScrollRef = useRef<HTMLDivElement>(null);
  const savedListScrollTopRef = useRef(0);
  const selectedTriggerRef = useRef<HTMLElement | null>(null);
  const selectedTriggerPathRef = useRef('');
  const restoreListOnNextRenderRef = useRef(false);

  useEffect(() => {
    if (isOpen) {
      setIsMounted(true);
    }
  }, [isOpen]);

  // 面板打开或 session 切换时重置到根目录
  useEffect(() => {
    if (isOpen && sessionId) {
      directoryRequestSeqRef.current += 1;
      setCurrentPath('');
      setPathHistory(['']);
      setHistoryIndex(0);
      setSelectedFile(null);
      setItems([]);
      setLoading(false);
    }
  }, [isOpen, sessionId]);

  useEffect(() => {
    if (isOpen && sessionId && targetFile) {
      savedListScrollTopRef.current = listScrollRef.current?.scrollTop ?? 0;
      const normalizedTarget = normalizeTargetFile(targetFile);
      const parentPath = getParentPath(normalizedTarget.path);
      setCurrentPath(parentPath);
      setPathHistory([parentPath]);
      setHistoryIndex(0);
      setSelectedFile(normalizedTarget);
    }
  }, [isOpen, sessionId, targetFile, targetFileNonce]);

  // currentPath 变化时加载目录
  const loadDir = useCallback(async (path: string) => {
    const requestSeq = ++directoryRequestSeqRef.current;
    const requestedSessionId = sessionId;
    setLoading(true);
    try {
      const response = await apiService.getSessionFiles(
        requestedSessionId,
        path || undefined,
      );
      if (directoryRequestSeqRef.current === requestSeq) {
        setItems(response.files);
      }
    } catch (error) {
      console.error('Failed to load directory:', error);
      if (directoryRequestSeqRef.current === requestSeq) {
        setItems([]);
      }
    } finally {
      if (directoryRequestSeqRef.current === requestSeq) {
        setLoading(false);
      }
    }
  }, [sessionId]);

  useEffect(() => {
    if (isOpen && sessionId) {
      loadDir(currentPath);
    }
  }, [isOpen, sessionId, currentPath, loadDir]);

  useEffect(() => {
    if (selectedFile) {
      const selectedPath = normalizePathForCompare(selectedFile.path);
      const matched = items.find((item) => !item.is_directory && normalizePathForCompare(item.path) === selectedPath);
      if (matched && matched !== selectedFile) {
        setSelectedFile(matched);
      }
    }
  }, [items, selectedFile]);

  useLayoutEffect(() => {
    if (selectedFile || !restoreListOnNextRenderRef.current) return;
    restoreListOnNextRenderRef.current = false;
    if (listScrollRef.current) {
      listScrollRef.current.scrollTop = savedListScrollTopRef.current;
    }
    const restoredTrigger = Array.from(
      listScrollRef.current?.querySelectorAll<HTMLElement>('[data-file-path]') ?? [],
    ).find((element) => element.dataset.filePath === selectedTriggerPathRef.current);
    (restoredTrigger ?? selectedTriggerRef.current)?.focus({ preventScroll: true });
  }, [selectedFile]);

  // --- 导航逻辑 ---
  const navigateTo = (subPath: string) => {
    const newHistory = pathHistory.slice(0, historyIndex + 1);
    newHistory.push(subPath);
    setPathHistory(newHistory);
    setHistoryIndex(newHistory.length - 1);
    setCurrentPath(subPath);
    setSelectedFile(null);
  };

  const goBack = () => {
    if (historyIndex > 0) {
      const newIndex = historyIndex - 1;
      setHistoryIndex(newIndex);
      setCurrentPath(pathHistory[newIndex]);
      setSelectedFile(null);
    }
  };

  const goForward = () => {
    if (historyIndex < pathHistory.length - 1) {
      const newIndex = historyIndex + 1;
      setHistoryIndex(newIndex);
      setCurrentPath(pathHistory[newIndex]);
      setSelectedFile(null);
    }
  };

  const goUp = () => {
    if (!currentPath) return;
    const parent = currentPath.includes('/')
      ? currentPath.substring(0, currentPath.lastIndexOf('/'))
      : '';
    navigateTo(parent);
  };

  // --- 工具函数 ---
  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const handleDownload = async (file: FileInfo, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await apiService.downloadFile(sessionId, file.path);
    } catch (error) {
      console.error('Failed to download file:', error);
    }
  };

  const shortSessionId = sessionId.length > 12
    ? sessionId.substring(0, 8) + '...'
    : sessionId;

  const displayPath = currentPath
    ? `~/sessions/${shortSessionId}/${currentPath}`
    : `~/sessions/${shortSessionId}`;

  const closeSelectedFile = () => {
    restoreListOnNextRenderRef.current = true;
    setSelectedFile(null);
  };

  const handleItemClick = (item: FileInfo, trigger?: HTMLElement) => {
    if (item.is_directory) {
      navigateTo(item.path);
    } else {
      savedListScrollTopRef.current = listScrollRef.current?.scrollTop ?? 0;
      selectedTriggerRef.current = trigger ?? null;
      selectedTriggerPathRef.current = item.path;
      setSelectedFile(item);
    }
  };

  const selectedFilePath = selectedFile?.path;

  const headerClassName = variant === 'workspace'
    ? 'px-6 pt-5 pb-4 border-b border-claude-border bg-white'
    : 'px-4 pt-2 pb-3 border-b border-claude-border';

  const content = (
    <div className="h-full flex flex-col bg-white">
      <div className={headerClassName}>
        {selectedFile ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={closeSelectedFile}
              className="px-2 py-1 text-xs rounded-lg border border-claude-border bg-claude-surface text-claude-text hover:bg-claude-hover transition-colors"
              title="返回文件列表"
            >
              返回列表
            </button>
            <div className="flex-1 px-3 py-1.5 bg-claude-surface rounded-lg text-[11px] text-claude-muted truncate" title={selectedFile.name}>
              {selectedFile.name}
            </div>
            <button
              type="button"
              onClick={onClose}
              className="hover:bg-claude-hover p-2 rounded-full active:scale-90 transition-[background-color,transform]"
              aria-label="关闭面板"
            >
              <X size={18} className="text-claude-muted" />
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={goBack}
              disabled={historyIndex <= 0}
              className="p-1.5 rounded-lg hover:bg-claude-hover disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              title="后退"
            >
              <ChevronLeft size={16} className="text-claude-muted" />
            </button>
            <button
              type="button"
              onClick={goForward}
              disabled={historyIndex >= pathHistory.length - 1}
              className="p-1.5 rounded-lg hover:bg-claude-hover disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              title="前进"
            >
              <ChevronRight size={16} className="text-claude-muted" />
            </button>
            <button
              type="button"
              onClick={goUp}
              disabled={!currentPath}
              className="p-1.5 rounded-lg hover:bg-claude-hover disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              title="上级目录"
            >
              <ArrowUp size={16} className="text-claude-muted" />
            </button>
            <div className="flex-1 ml-1 px-3 py-1.5 bg-claude-surface rounded-lg text-[11px] text-claude-muted truncate font-mono select-all" title={displayPath}>
              {displayPath}
            </div>
            <button
              type="button"
              onClick={onClose}
              className="hover:bg-claude-hover p-2 rounded-full active:scale-90 transition-[background-color,transform]"
              aria-label="关闭面板"
            >
              <X size={18} className="text-claude-muted" />
            </button>
          </div>
        )}
      </div>

      {selectedFile ? (
        <div className="flex-1 min-h-0 bg-white">
          <FilePreview
            inline
            sessionId={sessionId}
            file={selectedFile}
            onClose={closeSelectedFile}
          />
        </div>
      ) : (
        <div className="flex-1 min-h-0 flex flex-col">
          <div
            ref={listScrollRef}
            data-testid="artifacts-file-list"
            className="flex-1 overflow-y-auto p-3 space-y-1"
          >
            {loading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="w-6 h-6 text-claude-muted animate-spin" />
              </div>
            ) : items.length === 0 ? (
              <div className="h-full min-h-[360px] flex flex-col items-center justify-center text-center">
                <FolderOpen size={42} className="mb-4 text-claude-border" />
                <p className="text-[13px] text-claude-muted">空目录</p>
              </div>
            ) : (
              items.map((item) => {
                const isSelected = !item.is_directory && selectedFilePath === item.path;
                return (
                  <div
                    key={item.path}
                    onClick={(event) => handleItemClick(item, event.currentTarget)}
                    tabIndex={0}
                    role="button"
                    aria-label={`${item.is_directory ? '打开目录' : '预览文件'} ${item.name}`}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget) return;
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        handleItemClick(item, event.currentTarget);
                      }
                    }}
                    data-file-path={item.path}
                    className={`group flex items-center justify-between px-3 py-2.5 rounded-xl transition-colors cursor-pointer active:scale-[0.99] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/50 ${
                      isSelected ? 'bg-claude-hover' : 'hover:bg-claude-hover'
                    }`}
                  >
                    <div className="flex items-center space-x-3 overflow-hidden min-w-0">
                      <div className="w-8 h-8 bg-claude-surface rounded-lg flex items-center justify-center shrink-0">
                        {item.is_directory ? (
                          <Folder size={15} className="text-claude-accent" />
                        ) : (() => {
                          const Icon = getFileIcon(item);
                          return <Icon size={15} className={getFileIconClass(item)} />;
                        })()}
                      </div>
                      <div className="truncate min-w-0">
                        <p className="text-[13px] font-medium text-claude-text truncate leading-tight">
                          {item.name}{item.is_directory ? '/' : ''}
                        </p>
                        <p className="text-[10px] text-claude-muted mt-0.5">
                          {item.is_directory
                            ? formatDistanceToNow(new Date(item.modified), { addSuffix: true, locale: zhCN })
                            : `${formatFileSize(item.size)} · ${formatDistanceToNow(new Date(item.modified), { addSuffix: true, locale: zhCN })}`
                          }
                        </p>
                      </div>
                    </div>

                    {!item.is_directory && (
                      <div className="flex items-center opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                        <button
                          type="button"
                          onClick={(e) => handleDownload(item, e)}
                          className="p-1.5 hover:bg-claude-surface rounded-lg text-claude-muted hover:text-claude-text transition-colors"
                          title="下载"
                        >
                          <Download size={13} />
                        </button>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>

          <div className="px-4 py-2.5 border-t border-claude-border flex items-center justify-between text-[10px] text-claude-muted">
            <span>{items.length} 项</span>
            <span className="truncate ml-2 font-mono" title={displayPath}>
              {displayPath}
            </span>
          </div>
        </div>
      )}
    </div>
  );

  if (variant === 'workspace') {
    if (!isOpen) return null;

    return (
      <div className="h-full min-h-0 bg-[#d8d6ce] p-4 md:p-5" data-testid="artifacts-panel-workspace">
        <div className="h-full min-h-0 overflow-hidden rounded-[28px] border border-black/10 bg-white shadow-sm" data-testid="artifacts-panel-drawer">
          {content}
        </div>
      </div>
    );
  }

  if (!isMounted) return null;

  return (
    <>
      <div
        className={`fixed inset-0 z-20 bg-black/10 transition-opacity duration-200 ${isOpen ? 'opacity-100' : 'opacity-0'}`}
        onClick={onClose}
        onTransitionEnd={() => {
          if (!isOpen) setIsMounted(false);
        }}
      />
      <div
        className={`fixed top-0 right-0 bottom-0 bg-claude-bg border-l border-claude-border z-30 transition-transform duration-300 ease-out shadow-xl ${
          isOpen ? 'translate-x-0' : 'translate-x-full'
        }`}
        style={{ width: 'min(920px, calc(100vw - 48px))' }}
        data-testid="artifacts-panel-drawer"
      >
        {content}
      </div>
    </>
  );
}

function normalizeTargetFile(file: FileInfo): FileInfo {
  const path = file.path.replace(/^\/+/, '');
  return {
    ...file,
    path,
  };
}

function normalizePathForCompare(path: string): string {
  return path.replace(/^\/+/, '');
}

function getParentPath(path: string): string {
  const normalizedPath = path.replace(/^\/+/, '');
  return normalizedPath.includes('/')
    ? normalizedPath.substring(0, normalizedPath.lastIndexOf('/'))
    : '';
}
