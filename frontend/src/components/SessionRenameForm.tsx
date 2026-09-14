import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { apiService } from '../services/api';
import type { Session } from '../types';
import FeedbackMessage from './FeedbackMessage';

interface SessionRenameFormProps {
  session: Session;
  onSaved: (session: Session) => void;
  onCancel: () => void;
}

export function SessionRenameForm({ session, onSaved, onCancel }: SessionRenameFormProps) {
  const [title, setTitle] = useState(session.title ?? '');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const inputId = useId();
  const feedbackId = useId();
  const normalizedTitle = title.trim();
  const titleTooLong = Array.from(normalizedTitle).length > 255;

  useEffect(() => {
    const appRoot = document.getElementById('root');
    if (!appRoot) return;
    const previousInert = appRoot.inert;
    const previousAriaHidden = appRoot.getAttribute('aria-hidden');
    appRoot.inert = true;
    appRoot.setAttribute('aria-hidden', 'true');
    return () => {
      appRoot.inert = previousInert;
      if (previousAriaHidden === null) appRoot.removeAttribute('aria-hidden');
      else appRoot.setAttribute('aria-hidden', previousAriaHidden);
    };
  }, []);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useEffect(() => {
    if (pending) inputRef.current?.focus();
  }, [pending]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pending || !normalizedTitle || titleTooLong) return;
    setPending(true);
    setError('');
    try {
      const updatedSession = await apiService.renameSession(session.id, normalizedTitle);
      onSaved(updatedSession);
    } catch (cause) {
      console.error('Failed to rename session:', cause);
      setError('重命名失败，请重试。');
      setPending(false);
      inputRef.current?.focus();
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-[160] flex items-center justify-center bg-black/30 px-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) event.preventDefault();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget && !pending) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-md rounded-2xl border border-claude-border bg-claude-surface p-5 text-claude-text shadow-xl outline-none"
        onKeyDown={(event) => {
          event.stopPropagation();
          if (event.nativeEvent.isComposing || event.keyCode === 229) {
            if (event.key === 'Enter') event.preventDefault();
            return;
          }
          if (event.key === 'Escape') {
            event.preventDefault();
            if (!pending) onCancel();
          }
          if (event.key === 'Tab') {
            const focusable = Array.from(dialogRef.current!.querySelectorAll<HTMLElement>('input, button:not([disabled])'));
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (document.activeElement === dialogRef.current) {
              event.preventDefault();
              (event.shiftKey ? last : first).focus();
            } else if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first.focus();
            }
          }
        }}
      >
        <form aria-label="重命名会话" aria-busy={pending} onSubmit={save}>
          <h2 id={titleId} className="text-[17px] font-medium">重命名会话</h2>
          <label htmlFor={inputId} className="mb-2 mt-5 block text-[13px] text-claude-secondary">会话名称</label>
          <input
            ref={inputRef}
            id={inputId}
            aria-label="会话名称"
            aria-describedby={feedbackId}
            value={title}
            aria-invalid={titleTooLong || undefined}
            readOnly={pending}
            onChange={(event) => {
              setTitle(event.target.value);
              setError('');
            }}
            className="h-11 w-full rounded-xl border border-claude-border bg-white px-3 text-sm text-claude-text outline-none focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/25"
          />
          <div id={feedbackId} className="mt-2 min-h-5 text-[12px] leading-5">
            {error ? (
              <FeedbackMessage tone="error" onDismiss={() => setError('')}>{error}</FeedbackMessage>
            ) : (
              <p role="status" className={titleTooLong ? 'text-claude-error' : 'text-claude-muted'}>
                {pending ? '保存中…' : titleTooLong ? '会话名称最多 255 个字符。' : 'Enter 保存 · Esc 取消'}
              </p>
            )}
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <button
              type="button"
              aria-label="取消重命名"
              disabled={pending}
              onClick={onCancel}
              className="h-10 cursor-pointer rounded-xl border border-claude-border bg-white px-4 text-[13px] text-claude-secondary hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35 disabled:cursor-not-allowed disabled:opacity-40"
            >取消</button>
            <button
              type="submit"
              aria-label="保存名称"
              disabled={pending || !normalizedTitle || titleTooLong}
              className="h-10 cursor-pointer rounded-xl bg-claude-text px-4 text-[13px] text-white hover:bg-claude-text/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35 disabled:cursor-not-allowed disabled:opacity-40"
            >保存</button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}
