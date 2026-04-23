import { useEffect, useState, useCallback, useRef } from 'react';
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
}

export function ArtifactsPanel({ sessionId, isOpen, onClose }: ArtifactsPanelProps) {
  const [isMounted, setIsMounted] = useState(isOpen);
  const [items, setItems] = useState<FileInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [currentPath, setCurrentPath] = useState('');
  const [pathHistory, setPathHistory] = useState<string[]>(['']);
  const [historyIndex, setHistoryIndex] = useState(0);
  const [selectedFile, setSelectedFile] = useState<FileInfo | null>(null);
  // 用 ref 跟踪最新 path 防止竞态
  const latestPathRef = useRef(currentPath);

  useEffect(() => {
    if (isOpen) {
      setIsMounted(true);
    }
  }, [isOpen]);

  // 面板打开或 session 切换时重置到根目录
  useEffect(() => {
    if (isOpen && sessionId) {
      setCurrentPath('');
      setPathHistory(['']);
      setHistoryIndex(0);
      setSelectedFile(null);
    }
  }, [isOpen, sessionId]);

  // currentPath 变化时加载目录
  const loadDir = useCallback(async (path: string) => {
    latestPathRef.current = path;
    setLoading(true);
    try {
      const response = await apiService.getSessionFiles(sessionId, path || undefined);
      // 防止旧请求覆盖新结果
      if (latestPathRef.current === path) {
        setItems(response.files);
      }
    } catch (error) {
      console.error('Failed to load directory:', error);
      if (latestPathRef.current === path) {
        setItems([]);
      }
    } finally {
      if (latestPathRef.current === path) {
        setLoading(false);
      }
    }
  }, [sessionId]);

  useEffect(() => {
    if (isOpen && sessionId) {
      loadDir(currentPath);
    }
  }, [isOpen, sessionId, currentPath, loadDir]);

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

  const handleItemClick = (item: FileInfo) => {
    if (item.is_directory) {
      navigateTo(item.path);
    } else {
      setSelectedFile(item);
    }
  };

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
        className={`fixed top-0 right-0 bottom-0 w-[760px] bg-claude-bg border-l border-claude-border z-30 transition-transform duration-300 ease-out shadow-xl ${
          isOpen ? 'translate-x-0' : 'translate-x-full'
        }`}
        data-testid="artifacts-panel-drawer"
      >
        <div className="h-full flex flex-col bg-claude-bg">
          {/* Header */}
          <div className="px-4 pt-2 pb-3 border-b border-claude-border">
            {selectedFile ? (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setSelectedFile(null)}
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
                  className="hover:bg-claude-hover p-2 rounded-full active:scale-90 transition-all"
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
                  className="hover:bg-claude-hover p-2 rounded-full active:scale-90 transition-all"
                  aria-label="关闭面板"
                >
                  <X size={18} className="text-claude-muted" />
                </button>
              </div>
            )}
          </div>

          {selectedFile ? (
            <div className="flex-1 min-h-0 bg-claude-bg">
              <FilePreview
                inline
                sessionId={sessionId}
                file={selectedFile}
                onClose={() => setSelectedFile(null)}
              />
            </div>
          ) : (
            <div className="flex-1 min-h-0 flex flex-col">
              <div className="flex-1 overflow-y-auto p-3 space-y-1">
                {loading ? (
                  <div className="flex items-center justify-center py-12">
                    <Loader2 className="w-6 h-6 text-claude-muted animate-spin" />
                  </div>
                ) : items.length === 0 ? (
                  <div className="py-12 text-center">
                    <FolderOpen size={32} className="mx-auto mb-4 text-claude-border" />
                    <p className="text-[12px] text-claude-muted">空目录</p>
                  </div>
                ) : (
                  items.map((item) => {
                    const isSelected = !item.is_directory && selectedFile?.path === item.path;
                    return (
                      <div
                        key={item.path}
                        onClick={() => handleItemClick(item)}
                        className={`group flex items-center justify-between px-3 py-2.5 rounded-xl transition-colors cursor-pointer active:scale-[0.99] ${
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
      </div>
    </>
  );
}
