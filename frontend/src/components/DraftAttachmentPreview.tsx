import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, FileText, X } from 'lucide-react';
import type { ComposerAttachment } from '../types';
import { isImageFile } from '../utils/fileUtils';
import { PlainTextPreview } from './file-preview/PlainTextPreview';
import FeedbackMessage from './FeedbackMessage';

interface DraftAttachmentPreviewProps {
  file: ComposerAttachment | null;
  onClose: () => void;
}

const textExtensions = new Set(['txt', 'md', 'markdown', 'json', 'csv', 'log', 'xml', 'yaml', 'yml']);

const extensionOf = (name: string) => name.split('.').pop()?.toLowerCase() ?? '';

const formatSize = (size: number) => {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
};

export function DraftAttachmentPreview({ file, onClose }: DraftAttachmentPreviewProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const [localUrl, setLocalUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [textLoading, setTextLoading] = useState(false);
  const [textError, setTextError] = useState(false);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!file) return;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ) ?? []);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      returnFocusRef.current?.focus();
    };
  }, [file]);

  useEffect(() => {
    if (!file?.localFile) {
      setLocalUrl(null);
      return;
    }
    const url = URL.createObjectURL(file.localFile);
    setLocalUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  useEffect(() => {
    setText(null);
    setTextError(false);
    setTextLoading(false);
    if (!file) return;
    if (file.pastedText !== undefined) {
      setText(file.pastedText);
      return;
    }
    if (!file.localFile || !textExtensions.has(extensionOf(file.name))) return;
    let cancelled = false;
    setTextLoading(true);
    void file.localFile.text()
      .then((content) => {
        if (!cancelled) setText(content);
      })
      .catch(() => {
        if (!cancelled) setTextError(true);
      })
      .finally(() => {
        if (!cancelled) setTextLoading(false);
      });
    return () => { cancelled = true; };
  }, [file]);

  const close = useCallback(() => onClose(), [onClose]);
  if (!file) return null;

  const imageUrl = file.data_url ?? (isImageFile(file) ? localUrl : null);
  const pdfUrl = file.type === 'application/pdf' || extensionOf(file.name) === 'pdf' ? localUrl : null;
  const downloadUrl = localUrl ?? file.data_url;
  const isTextFile = file.pastedText !== undefined || textExtensions.has(extensionOf(file.name));
  const isPlainText = ['txt', 'log'].includes(extensionOf(file.name));

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" onMouseDown={close}>
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="draft-attachment-preview-title"
        className="relative flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-claude-border bg-white shadow-2xl"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="flex items-center gap-3 border-b border-claude-border px-4 py-3">
          <FileText className="h-5 w-5 shrink-0 text-claude-secondary" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <h2 id="draft-attachment-preview-title" className="truncate text-sm font-medium text-claude-text">{file.name}</h2>
            <p className="text-xs text-claude-muted">{formatSize(file.size)}</p>
          </div>
          {downloadUrl && (
            <a href={downloadUrl} download={file.name} className="flex h-10 w-10 items-center justify-center rounded-lg text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label={`下载 ${file.name}`}>
              <Download className="h-4 w-4" />
            </a>
          )}
          <button ref={closeButtonRef} type="button" onClick={close} className="flex h-10 w-10 items-center justify-center rounded-lg text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label="关闭附件预览">
            <X className="h-5 w-5" />
          </button>
        </header>
        <div className={`min-h-0 overflow-auto ${isPlainText ? 'p-6 sm:p-8' : 'p-4'}`}>
          {imageUrl && <img src={imageUrl} alt={file.name} className="mx-auto max-h-[72vh] max-w-full rounded-lg object-contain" />}
          {!imageUrl && text !== null && (isPlainText
            ? <PlainTextPreview text={text} />
            : <pre className="whitespace-pre-wrap break-words rounded-xl bg-claude-surface p-4 text-sm leading-6 text-claude-text">{text}</pre>)}
          {!imageUrl && textLoading && <p className="p-4 text-sm text-claude-muted">正在读取文本…</p>}
          {!imageUrl && text === null && pdfUrl && <iframe title={`${file.name} PDF 预览`} src={pdfUrl} className="h-[72vh] w-full rounded-lg border border-claude-border" />}
          {!imageUrl && !textLoading && text === null && !pdfUrl && !isTextFile && (
            <div className="flex min-h-48 flex-col items-center justify-center gap-2 text-center text-sm text-claude-muted">
              <FileText className="h-10 w-10" aria-hidden="true" />
              <p>此文件暂不支持本地预览。</p>
              <p className="text-xs">可下载后使用对应应用打开。</p>
            </div>
          )}
          {textError && <FeedbackMessage tone="error" messageKey={file} className="text-sm text-claude-error">无法读取此文本文件。</FeedbackMessage>}
        </div>
      </section>
    </div>
  );
}
