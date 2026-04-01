import { useRef, useEffect, useState } from 'react';
import { ArrowUp, FileText, Loader2, Paperclip, Square, X } from 'lucide-react';
import { FileInfo } from '../types';
import { getFileIcon, getFileExtLabel, getFileBadgeClass, getFileIconClass, isImageFile } from '../utils/fileUtils';

interface ChatInputProps {
  /** 当前输入文本 */
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  /** 停止生成回调，传入后发送中按钮变为可点击的 Stop */
  onStop?: () => void;

  /** 是否禁用（发送中 / 创建会话中） */
  disabled?: boolean;
  /** 发送按钮 loading 文案，为空时显示箭头 */
  sendingLabel?: string;

  placeholder?: string;

  // ---- 文件上传 ----
  attachedFiles?: FileInfo[];
  onRemoveAttachment?: (index: number) => void;
  onFileUpload?: (files: FileList | File[] | null) => void;
  onInputDropHandled?: () => void;
  onPreviewAttachment?: (file: FileInfo) => void;
  uploading?: boolean;

  // ---- @ 文件自动补全 ----
  availableFiles?: FileInfo[];
  showFileAutocomplete?: boolean;
  onInputChangeRaw?: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  onFileSelected?: (file: FileInfo, newInputValue: string) => void;
  onDismissAutocomplete?: () => void;
}

/**
 * Claude-style chat input — capsule shape, arrow send button
 */
export function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  disabled = false,
  sendingLabel,
  placeholder = '输入消息...',
  attachedFiles = [],
  onRemoveAttachment,
  onFileUpload,
  onInputDropHandled,
  onPreviewAttachment,
  uploading = false,
  availableFiles = [],
  showFileAutocomplete = false,
  onInputChangeRaw,
  onFileSelected,
  onDismissAutocomplete,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [previewImage, setPreviewImage] = useState<{ src: string; name: string } | null>(null);
  const [isInputDragging, setIsInputDragging] = useState(false);

  // 自动调整 textarea 高度
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [value]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (showFileAutocomplete && e.key === 'Escape') {
      e.preventDefault();
      onDismissAutocomplete?.();
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    if (onInputChangeRaw) {
      onInputChangeRaw(e);
    } else {
      onChange(e.target.value);
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (!onFileUpload) return;

    const items = Array.from(e.clipboardData.items || []);
    const files = items
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter((file): file is File => !!file);

    if (files.length > 0) {
      e.preventDefault();
      onFileUpload(files);
    }
  };

  const handleDragOverInput = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes('Files')) {
      setIsInputDragging(true);
    }
  };

  const handleDragLeaveInput = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
    if (
      e.clientX <= rect.left ||
      e.clientX >= rect.right ||
      e.clientY <= rect.top ||
      e.clientY >= rect.bottom
    ) {
      setIsInputDragging(false);
    }
  };

  const handleDropInput = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsInputDragging(false);
    onInputDropHandled?.();
    if (!onFileUpload) return;

    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      onFileUpload(files);
    }
  };

  const hasContent = value.trim().length > 0 || attachedFiles.length > 0;
  const canSend = hasContent && !disabled;

  const handleSelectFileInternal = (file: FileInfo) => {
    const cursorPos = textareaRef.current?.selectionStart || 0;
    const textBefore = value.substring(0, cursorPos);
    const lastAt = textBefore.lastIndexOf('@');

    let newValue = value;
    if (lastAt !== -1) {
      newValue = value.substring(0, lastAt) + `@${file.name}` + value.substring(cursorPos);
      onChange(newValue);
    }
    onFileSelected?.(file, newValue);
    onDismissAutocomplete?.();
  };

  return (
    <div className="px-4 pb-5 pt-3 bg-claude-bg">
      <div className="mx-auto max-w-3xl">
        {/* 附件列表 */}
        {attachedFiles.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {attachedFiles.map((file, index) => (
              <div key={index} className="relative">
                <button
                  type="button"
                  onClick={() => {
                    if (onPreviewAttachment) {
                      onPreviewAttachment(file);
                      return;
                    }
                    if (isImageFile(file) && file.data_url) {
                      setPreviewImage({ src: file.data_url, name: file.name });
                    }
                  }}
                  className="group relative w-24 h-20 rounded-xl overflow-hidden border border-claude-border bg-white hover:border-claude-border-strong transition-colors"
                  title={`预览 ${file.name}`}
                >
                  <div className={`absolute top-1.5 right-1.5 text-[9px] px-1.5 py-0.5 rounded-md uppercase tracking-wide z-10 ${getFileBadgeClass(file)}`}>
                    {getFileExtLabel(file)}
                  </div>
                  {isImageFile(file) && file.data_url ? (
                    <img
                      src={file.data_url}
                      alt={file.name}
                      className="w-full h-full object-cover transition-transform group-hover:scale-105"
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center bg-claude-surface">
                      {(() => {
                        const Icon = getFileIcon(file);
                        return <Icon className={`w-6 h-6 ${getFileIconClass(file)}`} />;
                      })()}
                    </div>
                  )}
                  <div className="absolute inset-x-0 bottom-0 bg-black/55 text-white text-[10px] px-1.5 py-1 truncate">
                    {file.name}
                  </div>
                </button>

                {onRemoveAttachment && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemoveAttachment(index);
                    }}
                    className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-white border border-claude-border text-claude-muted hover:text-claude-error flex items-center justify-center shadow-sm"
                    aria-label={`移除 ${file.name}`}
                  >
                    <X className="w-3 h-3" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {/* 输入框容器 — 膠囊形 */}
        <div className="relative">
          <div
            onDragOver={handleDragOverInput}
            onDragLeave={handleDragLeaveInput}
            onDrop={handleDropInput}
            className={`flex flex-col bg-white rounded-3xl border transition-all duration-200 ${
            hasContent
              ? 'border-claude-border-strong shadow-md ring-2 ring-claude-accent/10'
              : 'border-claude-border shadow-sm hover:border-claude-border-strong'
          } ${isInputDragging ? 'ring-2 ring-claude-accent/25 border-claude-accent/50 bg-claude-accent/5' : ''}`}
          >
            {/* textarea */}
            <textarea
              ref={textareaRef}
              value={value}
              onChange={handleChange}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder={placeholder}
              disabled={disabled}
              rows={1}
              className="w-full bg-transparent border-none focus:ring-0 text-[15px] py-3.5 pl-4 pr-14 resize-none max-h-[200px] placeholder:text-claude-muted disabled:opacity-50 disabled:cursor-not-allowed outline-none text-claude-text"
            />

            {/* 底部工具栏 */}
            <div className="flex items-center justify-between px-3 pb-2">
              <div className="flex items-center gap-1">
                {/* 文件上传 */}
                {onFileUpload && (
                  <>
                    <input
                      ref={fileInputRef}
                      type="file"
                      multiple
                      className="hidden"
                      onChange={(e) => onFileUpload(e.target.files)}
                    />
                    <button
                      onClick={() => fileInputRef.current?.click()}
                      disabled={uploading || disabled}
                      className="p-1.5 text-claude-muted hover:text-claude-secondary hover:bg-claude-hover rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                      title="上传文件"
                    >
                      {uploading ? (
                        <Loader2 className="w-4.5 h-4.5 animate-spin text-claude-secondary" />
                      ) : (
                        <Paperclip className="w-4.5 h-4.5" />
                      )}
                    </button>
                  </>
                )}
              </div>

              {/* 发送/停止按钮 — 圆形 */}
              {sendingLabel && onStop ? (
                <button
                  onClick={onStop}
                  className="w-8 h-8 rounded-full flex items-center justify-center transition-all bg-claude-error text-white hover:opacity-80 active:scale-95"
                  title="停止生成"
                >
                  <Square className="w-3.5 h-3.5 fill-current" />
                </button>
              ) : (
                <button
                  onClick={onSend}
                  disabled={!canSend}
                  className={`w-8 h-8 rounded-full flex items-center justify-center transition-all ${
                    canSend
                      ? 'bg-claude-text text-white hover:opacity-80 active:scale-95'
                      : 'bg-claude-border text-claude-muted cursor-not-allowed'
                  }`}
                >
                  {sendingLabel ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <ArrowUp className="w-4 h-4" />
                  )}
                </button>
              )}
            </div>
          </div>

          {/* 文件自动补全 */}
          {showFileAutocomplete && onFileSelected && (
            <div className="absolute bottom-full left-0 right-0 mb-2 bg-white border border-claude-border rounded-xl shadow-xl max-h-60 overflow-y-auto z-50 animate-zoom-in">
              <div className="px-3 py-2 text-xs font-medium text-claude-muted border-b border-claude-border">建议文件</div>
              {availableFiles
                .filter(f => {
                  const cursorPos = textareaRef.current?.selectionStart || 0;
                  const textBefore = value.substring(0, cursorPos);
                  const lastAt = textBefore.lastIndexOf('@');
                  const search = textBefore.substring(lastAt + 1).toLowerCase();
                  return f.name.toLowerCase().includes(search);
                })
                .map((file, index) => (
                  <button
                    key={index}
                    onClick={() => handleSelectFileInternal(file)}
                    className="w-full px-4 py-2.5 text-left hover:bg-claude-hover flex items-center gap-3 transition-colors border-b border-claude-border/30 last:border-0"
                  >
                    <FileText className="w-4 h-4 text-claude-muted" />
                    <span className="text-sm text-claude-text">{file.name}</span>
                  </button>
                ))}
            </div>
          )}
        </div>

        <p className="text-[10px] text-claude-muted mt-2 text-center">
          OpenCapyBox · 内容由 AI 生成，请仔细甄别
        </p>
      </div>

      {previewImage && (
        <div
          className="fixed inset-0 z-[110] flex items-center justify-center bg-black/60 backdrop-blur-sm p-6"
          onClick={() => setPreviewImage(null)}
        >
          <div
            className="relative max-w-[90vw] max-h-[90vh] bg-white rounded-2xl p-3"
            onClick={(e) => e.stopPropagation()}
          >
            <img
              src={previewImage.src}
              alt={previewImage.name}
              className="max-w-[88vw] max-h-[82vh] object-contain rounded-xl"
            />
            <button
              type="button"
              onClick={() => setPreviewImage(null)}
              className="absolute -top-2 -right-2 w-8 h-8 rounded-full bg-black text-white flex items-center justify-center"
              aria-label="关闭图片预览"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
