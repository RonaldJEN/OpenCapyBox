import { useEffect, useState } from 'react';
import { apiService } from '../services/api';
import { FileInfo } from '../types';
import {
  Folder,
  X,
  Download,
  Layers,
  Loader2
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { zhCN } from 'date-fns/locale/zh-CN';
import { getFileIcon, getFileIconClass } from '../utils/fileUtils';

interface ArtifactsPanelProps {
  sessionId: string;
  isOpen: boolean;
  onClose: () => void;
  onFilePreview: (file: FileInfo) => void;
}

export function ArtifactsPanel({ sessionId, isOpen, onClose, onFilePreview }: ArtifactsPanelProps) {
  const [isMounted, setIsMounted] = useState(isOpen);
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (isOpen) {
      setIsMounted(true);
    }
  }, [isOpen]);

  useEffect(() => {
    if (isOpen && sessionId) {
      loadFiles();
    }
  }, [isOpen, sessionId]);

  const loadFiles = async () => {
    setLoading(true);
    try {
      const response = await apiService.getSessionFiles(sessionId);
      setFiles(response.files);
    } catch (error) {
      console.error('Failed to load files:', error);
    } finally {
      setLoading(false);
    }
  };

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
        className={`fixed top-0 right-0 bottom-0 w-[380px] bg-claude-bg border-l border-claude-border z-30 transition-transform duration-300 ease-out shadow-xl ${
          isOpen ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
      <div className="h-full flex flex-col pt-12 bg-claude-bg">
        {/* Header */}
        <div className="px-6 py-6 border-b border-claude-border flex items-center justify-between">
          <span className="text-[16px] font-semibold flex items-center space-x-2">
            <Folder size={18} className="text-claude-accent" />
            <span className="tracking-tight text-claude-text">会话资源管理</span>
          </span>
          <button
            type="button"
            onClick={onClose}
            className="hover:bg-claude-hover p-2 rounded-full active:scale-90 transition-all"
          >
            <X size={18} className="text-claude-muted" />
          </button>
        </div>

        {/* File List */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          <p className="px-2 text-[10px] font-medium text-claude-muted uppercase tracking-widest mb-2">
            最近生成的资产
          </p>

          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 text-claude-muted animate-spin" />
            </div>
          ) : files.length === 0 ? (
            <div className="py-12 text-center">
              <Layers size={32} className="mx-auto mb-4 text-claude-border" />
              <p className="text-[12px] text-claude-muted">暂无文件</p>
            </div>
          ) : (
            files.map((file) => (
              <div
                key={file.path}
                onClick={() => onFilePreview(file)}
                className="group relative flex items-center justify-between p-4 bg-white hover:bg-claude-hover rounded-2xl border border-claude-border hover:border-claude-border-strong transition-all cursor-pointer active:scale-[0.98]"
              >
                <div className="flex items-center space-x-4 overflow-hidden">
                  <div className="w-10 h-10 bg-claude-surface rounded-xl flex items-center justify-center shrink-0">
                    {(() => {
                      const Icon = getFileIcon(file);
                      return <Icon size={16} className={getFileIconClass(file)} />;
                    })()}
                  </div>
                  <div className="truncate">
                    <p className="text-[14px] font-medium text-claude-text truncate leading-tight tracking-tight">
                      {file.name}
                    </p>
                    <p className="text-[11px] text-claude-muted mt-1">
                      {formatFileSize(file.size)} · {formatDistanceToNow(new Date(file.modified), {
                        addSuffix: true,
                        locale: zhCN,
                      })}
                    </p>
                  </div>
                </div>

                <div className="flex items-center space-x-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    type="button"
                    onClick={(e) => handleDownload(file, e)}
                    className="p-2 hover:bg-claude-hover rounded-full text-claude-muted hover:text-claude-text transition-colors"
                    title="下载资源"
                  >
                    <Download size={14} />
                  </button>
                </div>
              </div>
            ))
          )}

          {/* Sync Indicator */}
          <div className="mt-8 px-4 py-8 border-2 border-dashed border-claude-border/30 rounded-2xl flex flex-col items-center justify-center text-center opacity-40 select-none">
            <Layers size={32} className="mb-4 text-claude-muted" />
            <p className="text-[12px] font-medium text-claude-text tracking-tight">
              AI 逻辑组件实时同步中
            </p>
          </div>
        </div>
      </div>
    </div>
    </>
  );
}
