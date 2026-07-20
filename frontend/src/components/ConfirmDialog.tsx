import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';

interface ConfirmDialogProps {
  title: string;
  description: string;
  confirmLabel: string;
  busyLabel: string;
  busy: boolean;
  error?: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDialog({
  title,
  description,
  confirmLabel,
  busyLabel,
  busy,
  error,
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const wasBusyRef = useRef(false);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const appRoot = document.getElementById('root');
    if (!appRoot) return undefined;
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
    cancelButtonRef.current?.focus();
  }, []);

  useEffect(() => {
    if (busy) {
      // Avoid leaving focus on a button that has just become disabled.
      dialogRef.current?.focus();
    } else if (wasBusyRef.current && error) {
      confirmButtonRef.current?.focus();
    }
    wasBusyRef.current = busy;
  }, [busy, error]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      if (!busy) {
        onCancel();
      }
      return;
    }

    if (event.key !== 'Tab') return;

    const focusableElements = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    );

    if (focusableElements.length === 0) {
      event.preventDefault();
      dialogRef.current?.focus();
      return;
    }

    const firstElement = focusableElements[0];
    const lastElement = focusableElements[focusableElements.length - 1];
    if (event.shiftKey && document.activeElement === firstElement) {
      event.preventDefault();
      lastElement.focus();
    } else if (!event.shiftKey && document.activeElement === lastElement) {
      event.preventDefault();
      firstElement.focus();
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[rgba(35,30,23,0.30)] px-4"
      onClick={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={busy}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className="w-[min(420px,calc(100vw-32px))] rounded-2xl border border-[#e8e3d9] bg-[#fffdf9] p-5 text-[#1c1a16] shadow-[0_22px_70px_rgba(20,16,10,0.30)] outline-none"
      >
        <h2 id={titleId} className="text-[16px] font-bold text-[#1c1a16]">
          {title}
        </h2>
        <p id={descriptionId} className="mt-2 text-[13.5px] leading-6 text-[#6f6960]">
          {description}
        </p>
        {error && (
          <p role="alert" className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-[13px] text-claude-error">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            ref={cancelButtonRef}
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="h-9 rounded-[10px] border border-[#e8e3d9] bg-white px-4 text-[13px] font-semibold text-[#1c1a16] transition-colors hover:bg-[#f6f2ea] focus:outline-none focus:ring-2 focus:ring-[#b8814a]/25 disabled:cursor-not-allowed disabled:opacity-50"
          >
            取消
          </button>
          <button
            ref={confirmButtonRef}
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className="h-9 rounded-[10px] bg-claude-error px-4 text-[13px] font-semibold text-white transition-colors hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500/30 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
